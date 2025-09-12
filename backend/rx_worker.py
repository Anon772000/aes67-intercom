import time, threading
class RxPartylineWorker:
    def __init__(self, group, port, sink_mode, sink_path, ssrc_names):
        import gi
        gi.require_version('Gst', '1.0')
        from gi.repository import Gst
        self.Gst = Gst
        self.group = group; self.port = int(port)
        self.sink_mode = sink_mode; self.sink_path = sink_path
        self.ssrc_names = {int(k): v for k,v in (ssrc_names or {}).items()}
        self.active_peers = {}
        self.pipeline = Gst.Pipeline.new("rx")
        Gst.init(None)
        self.udpsrc = Gst.ElementFactory.make("udpsrc", "src")
        self.udpsrc.set_property("address", group)
        self.udpsrc.set_property("port", self.port)
        caps = Gst.Caps.from_string("application/x-rtp,media=audio,encoding-name=L16,payload=96,clock-rate=48000,channels=1")
        self.udpsrc.set_property("caps", caps)
        self.jbuf = Gst.ElementFactory.make("rtpjitterbuffer", "jbuf")
        self.demux = Gst.ElementFactory.make("rtpssrcdemux", "demux")
        self.mixer = Gst.ElementFactory.make("audiomixer", "mixer")
        self.sink = Gst.ElementFactory.make("autoaudiosink", "sink")
        for e in [self.udpsrc,self.jbuf,self.demux,self.mixer,self.sink]:
            self.pipeline.add(e)
        self.udpsrc.link(self.jbuf); self.jbuf.link(self.demux)
        self.mixer.link(self.sink)
        self.demux.connect("pad-added", self._on_pad_added)
    def _on_pad_added(self, demux, pad):
        Gst=self.Gst
        depay=Gst.ElementFactory.make("rtpL16depay",None)
        aconv=Gst.ElementFactory.make("audioconvert",None)
        ares=Gst.ElementFactory.make("audioresample",None)
        queue=Gst.ElementFactory.make("queue",None)
        for e in [depay,aconv,ares,queue]:
            self.pipeline.add(e); e.sync_state_with_parent()
        pad.link(depay.get_static_pad("sink"))
        depay.link(aconv); aconv.link(ares); ares.link(queue); queue.link(self.mixer)
    def start(self): self.pipeline.set_state(self.Gst.State.PLAYING)
    def stop(self): self.pipeline.set_state(self.Gst.State.NULL)
    def peers_snapshot(self): return {"peers":[{"name":"demo","packets":100}]}
