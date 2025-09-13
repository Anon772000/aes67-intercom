import time
import threading


class MicMonitor:
    def __init__(self):
        self.pipeline = None
        self.Gst = None
        self.level_db = None
        self._stop_evt = threading.Event()
        self._bus_thread = None
        self.device = ""

    def _norm_dev(self, dev: str) -> str:
        d = (dev or "").strip()
        if not d:
            return d
        if d.startswith(("hw:", "plughw:", "default", "sysdefault", "dsnoop:")):
            return d
        import re
        m = re.match(r"^hw(\d+)[,:](\d+)$", d)
        if m:
            return f"hw:{m.group(1)},{m.group(2)}"
        if re.fullmatch(r"\d+", d):
            return f"hw:{d}"
        m = re.match(r"^(\d+)[,:](\d+)$", d)
        if m:
            return f"hw:{m.group(1)},{m.group(2)}"
        return d

    def _bus_loop(self):
        Gst = self.Gst
        bus = self.pipeline.get_bus()
        mask = Gst.MessageType.ERROR | Gst.MessageType.EOS | Gst.MessageType.ELEMENT
        while not self._stop_evt.is_set():
            msg = bus.timed_pop_filtered(100 * Gst.MSECOND, mask)
            if not msg:
                continue
            t = msg.type
            if t == Gst.MessageType.ERROR:
                err, dbg = msg.parse_error()
                print("MIC MON ERROR:", err, dbg)
            elif t == Gst.MessageType.ELEMENT:
                s = msg.get_structure()
                if s and s.get_name() == "level":
                    try:
                        rms = s.get_value("rms")
                        if isinstance(rms, (list, tuple)) and rms:
                            self.level_db = float(rms[0])
                        else:
                            self.level_db = None
                    except Exception:
                        self.level_db = None

    def _build(self, dev: str, with_audio: bool):
        try:
            import gi  # type: ignore
        except ModuleNotFoundError:
            import sys
            sys.path.append("/usr/lib/python3/dist-packages")
            import gi  # type: ignore
        gi.require_version('Gst', '1.0')
        from gi.repository import Gst
        self.Gst = Gst
        Gst.init(None)

        pipe = Gst.Pipeline.new("mic-monitor")
        src = Gst.ElementFactory.make("alsasrc", "src")
        # conservative buffering
        src.set_property("do-timestamp", True)
        src.set_property("buffer-time", 200000)
        src.set_property("latency-time", 20000)
        if dev:
            src.set_property("device", dev)

        aconv = Gst.ElementFactory.make("audioconvert", None)
        ares = Gst.ElementFactory.make("audioresample", None)
        q1 = Gst.ElementFactory.make("queue", None)
        lvl = Gst.ElementFactory.make("level", "mic_level")
        lvl.set_property("interval", 100_000_000)  # 100ms
        lvl.set_property("post-messages", True)
        lvl.set_property("peak-ttl", 500_000_000)
        q2 = Gst.ElementFactory.make("queue", None)

        elems = [src, aconv, ares, q1, lvl, q2]
        for e in elems:
            pipe.add(e)
        src.link(aconv)
        aconv.link(ares)
        ares.link(q1)
        q1.link(lvl)
        lvl.link(q2)

        if with_audio:
            sink = Gst.ElementFactory.make("autoaudiosink", None)
            sink.set_property("sync", False)
            pipe.add(sink)
            q2.link(sink)
        else:
            fsink = Gst.ElementFactory.make("fakesink", None)
            fsink.set_property("sync", False)
            pipe.add(fsink)
            q2.link(fsink)

        self.pipeline = pipe

    def _start_try(self, dev: str, with_audio: bool) -> bool:
        self._build(dev, with_audio)
        self.level_db = None
        self._stop_evt.clear()
        self.pipeline.set_state(self.Gst.State.PAUSED)
        st = self.pipeline.get_state(timeout=2 * self.Gst.SECOND)
        self.pipeline.set_state(self.Gst.State.PLAYING)
        st = self.pipeline.get_state(timeout=2 * self.Gst.SECOND)
        ok = st and st[0] != self.Gst.StateChangeReturn.FAILURE
        if ok and (not self._bus_thread or not self._bus_thread.is_alive()):
            self._bus_thread = threading.Thread(target=self._bus_loop, daemon=True)
            self._bus_thread.start()
        return ok

    def start(self, device: str, with_audio: bool = True) -> bool:
        # Prefer plughw over hw; if fails, try dsnoop variant.
        self.stop()
        base = self._norm_dev(device)
        dev = base
        if dev.startswith("hw:"):
            dev = "plughw:" + dev.split(":", 1)[1]
        if self._start_try(dev, with_audio):
            self.device = dev
            return True
        # Try dsnoop variant
        ds = None
        if dev.startswith("plughw:"):
            tail = dev.split(":", 1)[1]
            ds = f"dsnoop:{tail}"
        elif dev.startswith("sysdefault:"):
            tail = dev.split(":", 1)[1]
            ds = f"dsnoop:{tail}"
        if ds and self._start_try(ds, with_audio):
            self.device = ds
            return True
        self.stop()
        return False

    def stop(self):
        try:
            self._stop_evt.set()
            if self.pipeline is not None:
                try:
                    self.pipeline.send_event(self.Gst.Event.new_eos())
                    bus = self.pipeline.get_bus()
                    bus.timed_pop_filtered(500 * self.Gst.MSECOND, self.Gst.MessageType.EOS)
                except Exception:
                    pass
                self.pipeline.set_state(self.Gst.State.NULL)
        finally:
            self.pipeline = None
            self.level_db = None

    def get_level(self):
        return self.level_db

