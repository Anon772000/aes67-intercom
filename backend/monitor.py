import socket, struct, threading, time

class RxMonitor:
    def __init__(self):
        self.thread=None; self.stop_evt=threading.Event(); self.lock=threading.Lock()
        self.group=None; self.port=None
        self.stats={"packets_total":0,"bytes_total":0,"pps_recent":0.0,"bps_recent":0.0,"last_packet_ts":None}

    def _run(self, group, port):
        with self.lock:
            self.group, self.port = group, int(port)
            self.stats={"packets_total":0,"bytes_total":0,"pps_recent":0.0,"bps_recent":0.0,"last_packet_ts":None}
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try: sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except OSError: pass
        try: sock.bind(("", int(port)))
        except OSError:
            sock.close()
            while not self.stop_evt.wait(0.2): pass
            return
        mreq = struct.pack("=4sl", socket.inet_aton(group), socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.settimeout(0.2)
        window=[]; WINDOW_SEC=2.0
        try:
            while not self.stop_evt.is_set():
                try:
                    data,_ = sock.recvfrom(65535)
                    now=time.time(); n=len(data)
                    with self.lock:
                        s=self.stats
                        s["packets_total"]+=1; s["bytes_total"]+=n; s["last_packet_ts"]=now
                    window.append((now,n)); cutoff=now-WINDOW_SEC
                    while window and window[0][0]<cutoff: window.pop(0)
                    if window:
                        dt=max(1e-6, window[-1][0]-window[0][0])
                        pps=len(window)/dt; bps=sum(sz for _,sz in window)/dt
                    else: pps=bps=0.0
                    with self.lock:
                        s=self.stats; s["pps_recent"]=pps; s["bps_recent"]=bps
                except socket.timeout:
                    with self.lock:
                        s=self.stats; s["pps_recent"]*=0.9; s["bps_recent"]*=0.9
        finally:
            try: sock.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, mreq)
            except OSError: pass
            sock.close()

    def start(self, group, port):
        self.stop(); self.stop_evt.clear()
        self.thread=threading.Thread(target=self._run, args=(group,int(port)), daemon=True); self.thread.start()

    def stop(self):
        if self.thread and self.thread.is_alive(): self.stop_evt.set(); self.thread.join(timeout=1.0)
        self.thread=None; self.stop_evt.clear()

    def read_stats(self):
        with self.lock:
            s=dict(self.stats); s["group"]=self.group; s["port"]=self.port
            s["receiving"] = (s["last_packet_ts"] is not None) and ((time.time()-s["last_packet_ts"])<2.5)
            return s
