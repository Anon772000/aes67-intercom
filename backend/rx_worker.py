# backend/rx_worker.py
import time
from pathlib import Path

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
        import gi
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

        self.Gst.init(None)
        self.pipeline = self.Gst.Pipeline.new("rx-mix")
        self._build()

    def _build(self):
        Gst = self.Gst

        # udpsrc: use multicast-group (modern) and optionally multicast-iface
        self.udpsrc = Gst.ElementFactory.make("udpsrc", "src")
        self.udpsrc.set_property("multicast-group", self.group)
        self.udpsrc.set_property("port", self.port)
        self.udpsrc.set_property("auto-multicast", True)
        if self.iface:
            # requires gstreamer >=1.14
            self.udpsrc.set_property("multicast-iface", self.iface)

        caps = Gst.Caps.from_string(
            "application/x-rtp,media=audio,encoding-name=L16,payload=96,clock-rate=48000,channels=1"
        )
        self.udpsrc.set_property("caps", caps)

        self.jbuf = Gst.ElementFactory.make("rtpjitterbuffer", "jbuf")
        self.jbuf.set_property("mode", 2)       # 2=slave to RTP timestamps
        self.jbuf.set_property("latency", 50)   # ms jitter buffer
        self.jbuf.set_property("do-lost", True)

        self.demux = Gst.ElementFactory.make("rtpssrcdemux", "demux")
        self.mixer = Gst.ElementFactory.make("audiomixer", "mixer")
        self.aconv = Gst.ElementFactory.make("audioconvert", "aconv")
        self.ares = Gst.ElementFactory.make("audioresample", "ares")

        for e in [self.udpsrc, self.jbuf, self.demux, self.mixer, self.aconv, self.ares]:
            self.pipeline.add(e)
        self.udpsrc.link(self.jbuf)
        self.jbuf.link(self.demux)

        # Tail sink (mix -> convert -> resample -> sink)
        if self.sink_mode == "auto":
            self.sink = Gst.ElementFactory.make("autoaudiosink", "sink")
            self.sink.set_property("sync", True)
            self.pipeline.add(self.sink)
            self.mixer.link(self.aconv)
            self.aconv.link(self.ares)
            self.ares.link(self.sink)
        else:
            self.wavenc = Gst.ElementFactory.make("wavenc", "wavenc")
            self.sink = Gst.ElementFactory.make("filesink", "fsink")
            self.sink.set_property("location", str(self.sink_path))
            self.pipeline.add(self.wavenc)
            self.pipeline.add(self.sink)
            self.mixer.link(self.aconv)
            self.aconv.link(self.ares)
            self.ares.link(self.wavenc)
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

        depay = Gst.ElementFactory.make("rtpL16depay", None)
        aconv = Gst.ElementFactory.make("audioconvert", None)
        ares = Gst.ElementFactory.make("audioresample", None)
        lvl = Gst.ElementFactory.make("level", None)  # per-talker level meter
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
            if ssrc in self.active_peers:
                self.active_peers[ssrc]["packets"] += 1
                self.active_peers[ssrc]["last_ts"] = time.time()
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
                if name and name.startswith("level_"):
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
