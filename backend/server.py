# backend/server.py
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import subprocess, json, os, socket, struct, threading, time
from pathlib import Path

from rx_worker import RxPartylineWorker

app = Flask(__name__, static_folder="../frontend/build", static_url_path="")
CORS(app)

CONFIG_PATH = Path(__file__).with_name("config.json")
gst_tx = None
rx_worker = None

class RxMonitor:
    def __init__(self):
        self.thread = None
        self.stop_evt = threading.Event()
        self.lock = threading.Lock()
        self.group = None
        self.port = None
        self.iface = None
        self.stats = {
            "packets_total": 0, "bytes_total": 0,
            "pps_recent": 0.0, "bps_recent": 0.0,
            "last_packet_ts": None,
        }

    def _run(self, group, port, iface):
        with self.lock:
            self.group, self.port, self.iface = group, int(port), iface
            self.stats = {"packets_total":0,"bytes_total":0,"pps_recent":0.0,"bps_recent":0.0,"last_packet_ts":None}

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except OSError:
            pass
        try:
            sock.bind(("", int(port)))
        except OSError:
            sock.close()
            while not self.stop_evt.wait(0.2):
                pass
            return

        # Bind membership to a specific interface if set
        if iface:
            try:
                ifindex = socket.if_nametoindex(iface)
                # IP_MULTICAST_IF can be set with interface addr; easier approach:
                # set IP_ADD_MEMBERSHIP with INADDR_ANY and the kernel uses route.
            except OSError:
                ifindex = None

        mreq = struct.pack("=4sl", socket.inet_aton(group), socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.settimeout(0.2)

        window = []
        WINDOW_SEC = 2.0
        try:
            while not self.stop_evt.is_set():
                try:
                    data, _ = sock.recvfrom(65535)
                    now = time.time()
                    n = len(data)
                    with self.lock:
                        self.stats["packets_total"] += 1
                        self.stats["bytes_total"] += n
                        self.stats["last_packet_ts"] = now
                    window.append((now, n))
                    cutoff = now - WINDOW_SEC
                    while window and window[0][0] < cutoff:
                        window.pop(0)
                    if window:
                        dt = max(1e-6, window[-1][0] - window[0][0])
                        pps = len(window)/dt
                        bps = sum(sz for _, sz in window)/dt
                    else:
                        pps = bps = 0.0
                    with self.lock:
                        self.stats["pps_recent"] = pps
                        self.stats["bps_recent"] = bps
                except socket.timeout:
                    with self.lock:
                        self.stats["pps_recent"] *= 0.9
                        self.stats["bps_recent"] *= 0.9
        finally:
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, mreq)
            except OSError:
                pass
            sock.close()

    def start(self, group, port, iface):
        self.stop()
        self.stop_evt.clear()
        self.thread = threading.Thread(target=self._run, args=(group, int(port), iface), daemon=True)
        self.thread.start()

    def stop(self):
        if self.thread and self.thread.is_alive():
            self.stop_evt.set()
            self.thread.join(timeout=1.0)
        self.thread = None
        self.stop_evt.clear()

    def read_stats(self):
        with self.lock:
            s = dict(self.stats)
            s["group"] = self.group
            s["port"] = self.port
            s["iface"] = self.iface
            s["receiving"] = (s["last_packet_ts"] is not None) and ((time.time() - s["last_packet_ts"]) < 2.5)
            return s

rxmon = RxMonitor()

DEFAULT_CFG = {
    # Networking
    "net_iface": "eth0",          # IMPORTANT on Raspberry Pi: use wired NIC for multicast

    # TX settings
    "tx_source": "sine",          # "sine" | "mic"
    "tx_sine_freq": 1000,         # Hz
    "tx_mic_device": "",          # e.g. "alsasrc device=plughw:1,0" if you later switch to mic via custom source
    "tx_name": "Unit A",
    "tx_ssrc": 12345678,
    "tx_multicast": "239.69.69.69",
    "tx_port": 5004,

    # RX settings
    "rx_multicast": "239.69.69.69",
    "rx_port": 5004,
    "rx_sink": {"mode": "file", "path": "mix.wav"},  # "file" or "auto"

    # Known names
    "ssrc_names": {"12345678": "Unit A", "23456789": "Unit B"}
}

def load_config():
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open() as f:
            return {**DEFAULT_CFG, **json.load(f)}
    return DEFAULT_CFG.copy()

def save_config(cfg: dict):
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(cfg, f, indent=2)
    tmp.replace(CONFIG_PATH)

def stop_tx():
    global gst_tx
    if gst_tx and gst_tx.poll() is None:
        try:
            gst_tx.terminate()
        except Exception:
            pass
    gst_tx = None

def start_tx(cfg):
    """
    TX sends RTP L16 mono 48k to multicast. Force multicast iface to eth0 if provided.
    """
    global gst_tx
    stop_tx()

    raw_caps = "audio/x-raw,format=S16LE,channels=1,rate=48000"
    src = ["audiotestsrc", "is-live=true", "wave=sine", f"freq={int(cfg.get('tx_sine_freq', 1000))}", "!", raw_caps]
    if (cfg.get("tx_source") or "sine") == "mic":
        # If you later wire up real mic, replace above with alsasrc settings
        src = ["alsasrc", "!", raw_caps]

    ssrc = int(cfg.get("tx_ssrc") or 12345678)
    group = cfg["tx_multicast"]
    port = int(cfg["tx_port"])
    iface = (cfg.get("net_iface") or "").strip()

    args = [
        "gst-launch-1.0", "-q",
        *src, "!", "audioconvert", "!", "audioresample", "!",
        "rtpL16pay", "pt=96", f"ssrc={ssrc}", "!", "identity",  # identity just keeps tags simple
        "udpsink", f"host={group}", f"port={port}", "auto-multicast=true", "ttl=16"
    ]
    if iface:
        # These properties exist on udpsink in modern GStreamer:
        args += [f"multicast-iface={iface}", f"bind-interface={iface}"]

    gst_tx = subprocess.Popen(args)

def stop_rx():
    global rx_worker
    if rx_worker:
        try:
            rx_worker.stop()
        except Exception:
            pass
    rx_worker = None
    rxmon.stop()

def start_rx(cfg):
    global rx_worker
    stop_rx()
    sink_mode = (cfg.get("rx_sink") or {}).get("mode", "file")
    outpath = Path(__file__).with_name((cfg.get("rx_sink") or {}).get("path", "mix.wav"))
    ssrc_names = cfg.get("ssrc_names") or {}
    iface = cfg.get("net_iface") or ""
    rxmon.start(cfg["rx_multicast"], cfg["rx_port"], iface)
    rx_worker = RxPartylineWorker(cfg["rx_multicast"], cfg["rx_port"], sink_mode, outpath, ssrc_names, iface)
    rx_worker.start()

@app.get("/status")
def status():
    cfg = load_config()
    return jsonify({
        "config": cfg,
        "tx_running": (gst_tx is not None and gst_tx.poll() is None),
        "rx_running": (rx_worker is not None)
    })

@app.post("/config")
def update_config():
    cfg = load_config()
    incoming = request.get_json(force=True) or {}
    if "tx_sine_freq" in incoming:
        try:
            incoming["tx_sine_freq"] = max(20, min(20000, int(incoming["tx_sine_freq"])))
        except Exception:
            incoming["tx_sine_freq"] = 1000
    if "tx_ssrc" in incoming:
        try:
            incoming["tx_ssrc"] = int(incoming["tx_ssrc"]) & 0xFFFFFFFF
        except Exception:
            incoming["tx_ssrc"] = 12345678
    cfg.update(incoming)
    try:
        cfg.setdefault("ssrc_names", {})[str(int(cfg["tx_ssrc"]))] = cfg.get("tx_name") or f"SSRC {cfg['tx_ssrc']}"
    except Exception:
        pass
    save_config(cfg)
    return jsonify({"ok": True, "config": cfg})

@app.post("/restart")
def restart_both():
    cfg = load_config(); start_tx(cfg); start_rx(cfg)
    return jsonify({"ok": True, "tx": "started", "rx": "started"})

@app.post("/start/tx")
def start_tx_only():
    cfg = load_config(); start_tx(cfg)
    return jsonify({"ok": True, "tx": "started"})

@app.post("/start/rx")
def start_rx_only():
    cfg = load_config(); start_rx(cfg)
    return jsonify({"ok": True, "rx": "started"})

@app.post("/stop/tx")
def stop_tx_only():
    stop_tx(); return jsonify({"ok": True, "tx": "stopped"})

@app.post("/stop/rx")
def stop_rx_only():
    stop_rx(); return jsonify({"ok": True, "rx": "stopped"})

@app.get("/rx/metrics")
def rx_metrics():
    return jsonify(rxmon.read_stats())

@app.get("/rx/peers")
def rx_peers():
    if rx_worker is None:
        return jsonify({"peers": []})
    return jsonify({"peers": rx_worker.peers_snapshot()})

# Optional: serve built React if you later push it to frontend/build
@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_frontend(path):
    build_dir = app.static_folder
    index = os.path.join(build_dir, "index.html")
    if path and os.path.exists(os.path.join(build_dir, path)):
        return send_from_directory(build_dir, path)
    if os.path.exists(index):
        return send_from_directory(build_dir, "index.html")
    return jsonify({"ok": True, "api": "running"})
    
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
