# backend/rx_worker.py
import time
import threading

class RxPartylineWorker:
    """
    Multicast RX -> jitterbuffer -> rtpssrcdemux -> [capsfilter(RTP) -> rtpL16depay -> audioconvert
    -> audioresample -> capsfilter(PCM) -> level -> queue] x N -> audiomixer -> level_mix -> sink
    Exposes per-talker levels and overall mix level. Uses a BUS POLLER thread (no GLib loop required).
    """

    def __init__(self, group, port, sink_mode, sink_path, ssrc_names: dict, iface: str | None = None):
        import gi
        gi.require_version('Gst', '1.0')
        gi.require_version('GObject', '2.0')
        from gi.repository import Gst, GObject

        self.Gst = Gst
        self.GObject = GObject

        self.group = group
        self.port = int(port)
        self.iface = iface              # optional NIC name for multicast join
        self.sink_mode = sink_mode      # "auto" or "file"
        self.sink_path = sink_path
        self.ssrc_names = {int(k): v for k, v in (ssrc_names or {}).items()}

        # State exposed to server
        self.active_peers = {}     # ssrc -> {name,last_ts,packets,level_db}
        self.level_map = {}        # level element NAME -> ssrc
        self.mix_level_db = None

        # Control
        self._stop_evt = threading.Event()
        self._bus_thread = None

        Gst.init(None)
        self.pipeline = Gst.Pipeline.new("rx-mix")
        self._build()

    def _build(self):
        Gst = self.Gst

        # Source (multicast)
        self.udpsrc = Gst.ElementFactory.make("udpsrc", "src")
        self.udpsrc.set_property("address", self.group)
        self.udpsrc.set_property("port", self.port)
        self.udpsrc.set_property("auto-multicast", True)
        # Pin interface if requested (important in VMs / multi-NIC hosts)
        if self.iface:
            try:
                self.udpsrc.set_property("multicast-iface", self.iface)
            except Exception:
                print(f"WARN: failed to set multicast-iface='{self.iface}' on udpsrc")
        # Base caps (some demux pads may not forward full RTP caps)
        self.udpsrc.set_property(
            "caps",
            Gst.Caps.from_string(
                "application/x-rtp,media=audio,encoding-name=L16,payload=96,clock-rate=48000,channels=1"
            ),
        )

        # Jitter buffer + demux by SSRC
        self.jbuf = Gst.ElementFactory.make("rtpjitterbuffer", "jbuf")
        self.jbuf.set_property("mode", 2)        # slave
        self.jbuf.set_property("latency", 80)    # generous; tune lower on wired
        self.jbuf.set_property("do-lost", True)

        self.demux = Gst.ElementFactory.make("rtpssrcdemux", "demux")

        # Mixer and a mix-level meter
        self.mixer = Gst.ElementFactory.make("audiomixer", "mixer")
        self.level_mix = Gst.ElementFactory.make("level", "level_mix")
        self.level_mix.set_property("interval", 100000000)   # 100ms
        self.level_mix.set_property("post-messages", True)

        # Tail to sink
        self.aconv = Gst.ElementFactory.make("audioconvert", "aconv")
        self.ares  = Gst.ElementFactory.make("audioresample", "ares")

        for e in [self.udpsrc, self.jbuf, self.demux, self.mixer, self.level_mix, self.aconv, self.ares]:
            self.pipeline.add(e)

        # Link head
        self.udpsrc.link(self.jbuf)
        self.jbuf.link(self.demux)

        # Tail: either play or write WAV
        if self.sink_mode == "auto":
            self.sink = Gst.ElementFactory.make("autoaudiosink", "sink")
            self.sink.set_property("sync", True)
            self.pipeline.add(self.sink)
            self.mixer.link(self.level_mix)
            self.level_mix.link(self.aconv)
            self.aconv.link(self.ares)
            self.ares.link(self.sink)
        else:
            self.wavenc = Gst.ElementFactory.make("wavenc", "wavenc")
            self.sink   = Gst.ElementFactory.make("filesink", "fsink")
            self.sink.set_property("location", str(self.sink_path))
            self.pipeline.add(self.wavenc)
            self.pipeline.add(self.sink)
            self.mixer.link(self.level_mix)
            self.level_mix.link(self.aconv)
            self.aconv.link(self.ares)
            self.ares.link(self.wavenc)
            self.wavenc.link(self.sink)

        # Per-SSRC branches will be created here
        self.demux.connect("pad-added", self._on_pad_added)

        # Bus (we'll poll it in our own thread)
        self.bus = self.pipeline.get_bus()

    def _on_pad_added(self, demux, pad):
        Gst = self.Gst
        pad_name = pad.get_name() or ""

        # Ignore RTCP pads early
        if pad_name.startswith("rtcp_"):
            try:
                caps = pad.get_current_caps() or pad.query_caps(None)
                print("RX demux pad-added (ignored RTCP):", pad_name, caps.to_string() if caps else "(no caps)")
            except Exception:
                pass
            return

        # Try to extract SSRC from pad name (src_<ssrc>)
        import re
        m = re.search(r"(\d+)", pad_name)
        ssrc = int(m.group(1)) if m else None

        # Force full RTP caps before depay in case demux pad exposes only application/x-rtp
        rtp_capsf = Gst.ElementFactory.make("capsfilter", None)
        rtp_caps = Gst.Caps.from_string(
            "application/x-rtp,media=audio,encoding-name=L16,payload=96,clock-rate=48000,channels=1"
        )
        rtp_capsf.set_property("caps", rtp_caps)

        # Build branch: caps(RTP)->depay->aconv->ares->caps(PCM)->level->queue->mixer
        depay = Gst.ElementFactory.make("rtpL16depay", None)
        aconv = Gst.ElementFactory.make("audioconvert", None)
        ares  = Gst.ElementFactory.make("audioresample", None)
        capsf = Gst.ElementFactory.make("capsfilter", None)
        level = Gst.ElementFactory.make("level", f"level_{ssrc if ssrc is not None else 'unk'}")
        queue = Gst.ElementFactory.make("queue", None)

        capsf.set_property("caps", Gst.Caps.from_string("audio/x-raw,format=S16LE,rate=48000,channels=1"))
        level.set_property("interval", 100000000)  # 100ms
        level.set_property("post-messages", True)

        for e in [rtp_capsf, depay, aconv, ares, capsf, level, queue]:
            self.pipeline.add(e)
            e.sync_state_with_parent()

        # Link demux RTP pad -> forced RTP caps
        if pad.link(rtp_capsf.get_static_pad("sink")) != Gst.PadLinkReturn.OK:
            caps = pad.get_current_caps() or pad.query_caps(None)
            print(f"WARN: demux pad -> rtp_capsf link failed for {pad_name} (caps={caps.to_string() if caps else '(no caps)'})")
            # Cleanup branch if link fails
            try:
                for e in [queue, level, capsf, ares, aconv, depay, rtp_capsf]:
                    self.pipeline.remove(e)
            except Exception:
                pass
            return

        # Chain to mixer
        if not rtp_capsf.link(depay):  print("WARN: rtp_capsf->depay link failed")
        if not depay.link(aconv):      print("WARN: depay->aconv link failed")
        if not aconv.link(ares):       print("WARN: aconv->ares link failed")
        if not ares.link(capsf):       print("WARN: ares->capsf link failed")
        if not capsf.link(level):      print("WARN: capsf->level link failed")
        if not level.link(queue):      print("WARN: level->queue link failed")
        if not queue.link(self.mixer): print("WARN: queue->mixer link failed")

        # Track peer + map level element to SSRC
        label = self.ssrc_names.get(ssrc, f"SSRC {ssrc}" if ssrc is not None else "unknown")
        now = time.time()
        self.active_peers[ssrc] = {"name": label, "last_ts": now, "packets": 0, "level_db": None}
        self.level_map[level.get_name()] = ssrc

        # Count incoming RTP packets for stats/last-seen
        sinkpad = depay.get_static_pad("sink")
        def _probe_cb(_pad, _info):
            if ssrc in self.active_peers:
                self.active_peers[ssrc]["packets"] += 1
                self.active_peers[ssrc]["last_ts"] = time.time()
            return Gst.PadProbeReturn.OK
        sinkpad.add_probe(Gst.PadProbeType.BUFFER, _probe_cb)

        caps_dbg = None
        try:
            c = pad.get_current_caps() or pad.query_caps(None)
            caps_dbg = c.to_string() if c else "(no caps)"
        except Exception:
            caps_dbg = "(caps err)"
        print(f"RX branch up for {label} (ssrc={ssrc}) from pad '{pad_name}' caps='{caps_dbg}'")

    # ---------- BUS POLLING ----------
    def _bus_loop(self):
        Gst = self.Gst
        bus = self.bus
        while not self._stop_evt.is_set():
            msg = bus.timed_pop_filtered(
                100 * 1000 * 1000,  # 100ms
                Gst.MessageType.ERROR | Gst.MessageType.EOS | Gst.MessageType.ELEMENT
            )
            if msg is None:
                continue
            try:
                if msg.type == Gst.MessageType.ERROR:
                    err, dbg = msg.parse_error()
                    print("RX ERROR:", err, dbg)
                elif msg.type == Gst.MessageType.EOS:
                    print("RX EOS")
                elif msg.type == Gst.MessageType.ELEMENT:
                    s = msg.get_structure()
                    if not s or s.get_name() != "level":
                        continue
                    rms = s.get_value("rms")
                    db = None
                    if rms and len(rms) > 0 and rms[0] != -2147483648:
                        db = rms[0] / 100.0
                    src_name = msg.src.get_name() if msg.src else ""
                    if src_name == "level_mix":
                        self.mix_level_db = db
                    else:
                        ssrc = self.level_map.get(src_name)
                        if ssrc in self.active_peers:
                            self.active_peers[ssrc]["level_db"] = db
            except Exception as e:
                print("RX bus loop exception:", e)

    def start(self):
        self._stop_evt.clear()
        self.pipeline.set_state(self.Gst.State.PLAYING)
        # Start bus polling in a thread
        self._bus_thread = threading.Thread(target=self._bus_loop, daemon=True)
        self._bus_thread.start()

    def stop(self):
        self._stop_evt.set()
        if self._bus_thread and self._bus_thread.is_alive():
            self._bus_thread.join(timeout=1.0)
        self._bus_thread = None
        self.pipeline.set_state(self.Gst.State.NULL)

    def peers_snapshot(self):
        """Return dict with per-peer stats + current mix level."""
        now = time.time()
        out = []
        for ssrc, rec in list(self.active_peers.items()):
            idle = now - rec["last_ts"] if rec["last_ts"] else 999
            out.append({
                "ssrc": ssrc,
                "name": rec["name"],
                "last_seen_sec": round(idle, 2),
                "packets": rec["packets"],
                "level_db": rec.get("level_db"),
            })
        return {
            "peers": sorted(out, key=lambda x: (x["name"] or "", x["ssrc"] or 0)),
            "mix_level_db": self.mix_level_db,
        }
