# backend/rx_worker.py
import time
from pathlib import Path
from collections import deque
import threading

class RxPartylineWorker:
    """
    Party-line RX:
      - Join one (multicast) group:port
      - Demux by SSRC
      - Per-SSRC branch: depay -> convert -> resample -> level -> queue -> mixer
      - Optional sink: filesink (wav) or autoaudiosink
      - Exposes peers (name/ssrc/packets/level/last-seen)
    """
    def __init__(self, group, port, sink_mode, sink_path: Path, ssrc_names: dict, iface: str | None):
        # Ensure GI bindings are importable even inside a venv without system-site-packages
        try:
            import gi  # type: ignore
        except ModuleNotFoundError:
            import sys
            # Common system path for python3-gi on Debian/RPi OS
            sys.path.append("/usr/lib/python3/dist-packages")
            import gi  # type: ignore
        gi.require_version('Gst', '1.0')
        gi.require_version('GObject', '2.0')
        from gi.repository import Gst, GObject

        self.Gst = Gst
        self.GObject = GObject
        self.group = group
        self.port = int(port)
        self.iface = iface or ""  # e.g. "eth0" to force wired
        self.sink_mode = sink_mode
        self.sink_path = sink_path
        self.ssrc_names = {int(k): v for k, v in (ssrc_names or {}).items()}
        self.active_peers = {}  # ssrc -> {"name","last_ts","packets","level_db"}
        self.mix_level_db = None
        self._stats_lock = threading.Lock()
        self._window = deque()  # (ts, bytes)
        self._WINDOW_SEC = 2.0
        self.stats = {"packets_total":0,"bytes_total":0,"pps_recent":0.0,"bps_recent":0.0,"last_packet_ts":None}

        self.Gst.init(None)
        self.pipeline = self.Gst.Pipeline.new("rx-mix")
        self._build()

    def _build(self):
        Gst = self.Gst

        # udpsrc: use multicast-group (modern) and optionally multicast-iface
        self.udpsrc = Gst.ElementFactory.make("udpsrc", "src")
        if not self.udpsrc:
            raise RuntimeError("Missing GStreamer element: udpsrc (install gstreamer1.0-plugins-base)")
        self.udpsrc.set_property("multicast-group", self.group)
        self.udpsrc.set_property("port", self.port)
        self.udpsrc.set_property("auto-multicast", True)
        # Allow sharing the port with our monitor socket
        try:
            self.udpsrc.set_property("reuse", True)
        except Exception:
            pass
        if self.iface:
            # requires gstreamer >=1.14
            self.udpsrc.set_property("multicast-iface", self.iface)

        caps = Gst.Caps.from_string(
            "application/x-rtp,media=audio,encoding-name=L16,payload=96,clock-rate=48000,channels=1"
        )
        self.udpsrc.set_property("caps", caps)

        self.jbuf = Gst.ElementFactory.make("rtpjitterbuffer", "jbuf")
        if not self.jbuf:
            raise RuntimeError("Missing GStreamer element: rtpjitterbuffer (install gstreamer1.0-plugins-base)")
        self.jbuf.set_property("mode", 2)       # 2=slave to RTP timestamps
        self.jbuf.set_property("latency", 50)   # ms jitter buffer
        self.jbuf.set_property("do-lost", True)

        self.demux = Gst.ElementFactory.make("rtpssrcdemux", "demux")
        if not self.demux:
            raise RuntimeError("Missing GStreamer element: rtpssrcdemux (install gstreamer1.0-plugins-good)")
        self.mixer = Gst.ElementFactory.make("audiomixer", "mixer")
        if not self.mixer:
            raise RuntimeError("Missing GStreamer element: audiomixer (install gstreamer1.0-plugins-good)")
        self.aconv = Gst.ElementFactory.make("audioconvert", "aconv")
        if not self.aconv:
            raise RuntimeError("Missing GStreamer element: audioconvert (install gstreamer1.0-plugins-base)")
        self.ares = Gst.ElementFactory.make("audioresample", "ares")
        if not self.ares:
            raise RuntimeError("Missing GStreamer element: audioresample (install gstreamer1.0-plugins-base)")
        # Level meter for the mixed output
        self.level_mix = Gst.ElementFactory.make("level", "level_mix")
        if not self.level_mix:
            raise RuntimeError("Missing GStreamer element: level (install gstreamer1.0-plugins-good)")
        self.level_mix.set_property("interval", 100_000_000)
        self.level_mix.set_property("post-messages", True)
        self.level_mix.set_property("peak-ttl", 500_000_000)

        for e in [self.udpsrc, self.jbuf, self.demux, self.mixer, self.aconv, self.ares, self.level_mix]:
            self.pipeline.add(e)
        self.udpsrc.link(self.jbuf)
        self.jbuf.link(self.demux)

        # Tail sink (mix -> convert -> resample -> sink)
        if self.sink_mode == "auto":
            self.sink = Gst.ElementFactory.make("autoaudiosink", "sink")
            if not self.sink:
                raise RuntimeError("Missing GStreamer element: autoaudiosink (install gstreamer1.0-alsa or proper audio sink)")
            self.sink.set_property("sync", True)
            self.pipeline.add(self.sink)
            self.mixer.link(self.aconv)
            self.aconv.link(self.ares)
            self.ares.link(self.level_mix)
            self.level_mix.link(self.sink)
        else:
            self.wavenc = Gst.ElementFactory.make("wavenc", "wavenc")
            if not self.wavenc:
                raise RuntimeError("Missing GStreamer element: wavenc (install gstreamer1.0-plugins-good)")
            self.sink = Gst.ElementFactory.make("filesink", "fsink")
            if not self.sink:
                raise RuntimeError("Missing GStreamer element: filesink (install gstreamer1.0-plugins-base)")
            self.sink.set_property("location", str(self.sink_path))
            self.pipeline.add(self.wavenc)
            self.pipeline.add(self.sink)
            self.mixer.link(self.aconv)
            self.aconv.link(self.ares)
            self.ares.link(self.level_mix)
            self.level_mix.link(self.wavenc)
            self.wavenc.link(self.sink)

        # Dynamic pads per SSRC
        self.demux.connect("pad-added", self._on_pad_added)

        # Bus to catch messages (including level element messages)
        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.bus.connect("message", self._on_bus)

    def _on_pad_added(self, demux, pad):
        Gst = self.Gst
        name = pad.get_name()
        # Expect "src_<ssrc>" or "rtcp_src_<ssrc>"
        if not name.startswith("src_"):  # ignore RTCP pads etc
            print(f"RX demux pad-added (ignored): {name} {pad.get_current_caps().to_string() if pad.get_current_caps() else ''}")
            return

        try:
            ssrc = int(name.split("_")[1])
        except Exception:
            print(f"WARN: could not parse SSRC from pad name: {name}")
            ssrc = None
        else:
            print(f"RX demux SSRC detected: {ssrc}")

        depay = Gst.ElementFactory.make("rtpL16depay", None)
        if not depay:
            raise RuntimeError("Missing GStreamer element: rtpL16depay (install gstreamer1.0-plugins-good)")
        aconv = Gst.ElementFactory.make("audioconvert", None)
        if not aconv:
            raise RuntimeError("Missing GStreamer element: audioconvert (install gstreamer1.0-plugins-base)")
        ares = Gst.ElementFactory.make("audioresample", None)
        if not ares:
            raise RuntimeError("Missing GStreamer element: audioresample (install gstreamer1.0-plugins-base)")
        lvl = Gst.ElementFactory.make("level", None)  # per-talker level meter
        if not lvl:
            raise RuntimeError("Missing GStreamer element: level (install gstreamer1.0-plugins-good)")
        # Post messages ~10 times/sec, RMS over window
        lvl.set_property("interval", 100_000_000)  # 100ms in ns
        lvl.set_property("post-messages", True)
        lvl.set_property("peak-ttl", 500_000_000)
        q = Gst.ElementFactory.make("queue", None)

        for e in [depay, aconv, ares, lvl, q]:
            self.pipeline.add(e)
            e.sync_state_with_parent()

        # Link demux:pad -> depay
        if not pad.link(depay.get_static_pad("sink")) == Gst.PadLinkReturn.OK:
            print(f"WARN: could not link demux pad to depay for SSRC {ssrc}")
            return

        # Force common format for mixer
        mix_caps = Gst.Caps.from_string("audio/x-raw,format=S16LE,rate=48000,channels=1")
        if not depay.link_filtered(aconv, mix_caps):
            print("WARN: link_filtered failed; falling back to plain link")
            depay.link(aconv)

        aconv.link(ares)
        ares.link(lvl)
        lvl.link(q)
        q.link(self.mixer)

        # Track peer
        label = self.ssrc_names.get(ssrc, f"SSRC {ssrc}" if ssrc is not None else "unknown")
        self.active_peers[ssrc] = {"name": label, "last_ts": time.time(), "packets": 0, "level_db": None}

        # Count packets
        sinkpad = depay.get_static_pad("sink")

        def _probe_cb(_pad, info):
            now = time.time()
            if ssrc in self.active_peers:
                self.active_peers[ssrc]["packets"] += 1
                self.active_peers[ssrc]["last_ts"] = now
            # Update global metrics window
            try:
                buf = info.get_buffer()
                n = int(buf.get_size()) if buf is not None else 0
            except Exception:
                n = 0
            with self._stats_lock:
                self.stats["packets_total"] += 1
                self.stats["bytes_total"] += n
                self.stats["last_packet_ts"] = now
                self._window.append((now, n))
                cutoff = now - self._WINDOW_SEC
                while self._window and self._window[0][0] < cutoff:
                    self._window.popleft()
                if self._window:
                    dt = max(1e-6, self._window[-1][0] - self._window[0][0])
                    pps = len(self._window) / dt
                    bps = sum(sz for _, sz in self._window) / dt
                else:
                    pps = bps = 0.0
                self.stats["pps_recent"] = pps
                self.stats["bps_recent"] = bps
            return Gst.PadProbeReturn.OK

        sinkpad.add_probe(Gst.PadProbeType.BUFFER, _probe_cb)

        # Listen for level messages tagged with this branch
        # We'll set a custom name on 'lvl' and look for it in bus messages
        lvl_name = f"level_{ssrc}"
        lvl.set_property("name", lvl_name)

    def _on_bus(self, _bus, msg):
        t = msg.type
        if t == self.Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print("RX ERROR:", err, dbg)
        elif t == self.Gst.MessageType.EOS:
            print("RX EOS")
        elif t == self.Gst.MessageType.ELEMENT:
            s = msg.get_structure()
            if s and s.get_name() == "level":
                # Find which level element emitted this; parse RMS in dB
                src = msg.src
                name = src.get_name() if src else None
                # structure fields: "rms", "peak", "decay" arrays per channel
                try:
                    rms = s.get_value("rms")
                    if isinstance(rms, (list, tuple)) and rms:
                        db = float(rms[0])
                    else:
                        db = None
                except Exception:
                    db = None
                if name == "level_mix":
                    self.mix_level_db = db
                elif name and name.startswith("level_"):
                    try:
                        ssrc = int(name.split("_", 1)[1])
                        if ssrc in self.active_peers:
                            self.active_peers[ssrc]["level_db"] = db
                            self.active_peers[ssrc]["last_ts"] = time.time()
                    except Exception:
                        pass

    def start(self):
        self.pipeline.set_state(self.Gst.State.PLAYING)

    def stop(self):
        # Try to gracefully finalize WAV (if used)
        try:
            self.pipeline.send_event(self.Gst.Event.new_eos())
            bus = self.pipeline.get_bus()
            bus.timed_pop_filtered(2 * self.Gst.SECOND, self.Gst.MessageType.EOS)
        except Exception:
            pass
        self.pipeline.set_state(self.Gst.State.NULL)

    def peers_snapshot(self):
        now = time.time()
        out = []
        for ssrc, rec in list(self.active_peers.items()):
            idle = now - rec["last_ts"] if rec["last_ts"] else 999
            out.append({
                "ssrc": ssrc,
                "name": rec["name"],
                "packets": rec["packets"],
                "level_db": None if rec["level_db"] is None else round(rec["level_db"], 1),
                "last_seen_sec": round(idle, 2)
            })
        # sort by name, then ssrc for stability
        return sorted(out, key=lambda x: (x["name"] or "", x["ssrc"] or 0))

    def metrics_snapshot(self):
        with self._stats_lock:
            s = dict(self.stats)
        s["group"] = self.group
        s["port"] = self.port
        s["receiving"] = (s["last_packet_ts"] is not None) and ((time.time() - s["last_packet_ts"]) < 2.5)
        return s
