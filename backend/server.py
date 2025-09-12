# backend/server.py
from flask import Flask, request, jsonify, send_from_directory
import subprocess, json, os, socket, threading, time
from pathlib import Path

from rx_worker import RxPartylineWorker

app = Flask(__name__, static_folder="../frontend/build", static_url_path="")

CONFIG_PATH = Path(__file__).with_name("config.json")
gst_tx = None        # subprocess for TX
rx_worker = None     # in-process RX
rxmon_lock = threading.Lock()

DEFAULT_CFG = {
    # TX settings
    "tx_source": "sine",          # "sine" | "mic"
    "tx_sine_freq": 1000,         # Hz
    "tx_mic_device": "hw:0",      # ALSA device; blank = auto
    "tx_name": "Unit A",
    "tx_ssrc": 12345678,
    "tx_multicast": "239.69.69.69",
    "tx_port": 5004,

    # Optional network pinning for TX (helps in VMs / multi-NIC hosts)
    "tx_iface": "",               # e.g. "eth0" (maps to udpsink multicast-iface)
    "tx_bind_ip": "",             # e.g. "192.168.1.50" (maps to udpsink bind-address)

    # RX party-line (same group:port)
    "rx_multicast": "239.69.69.69",
    "rx_port": 5004,
    "rx_sink": {"mode": "file", "path": "mix.wav"},  # "file" or "auto"

    # Optional network pinning for RX
    "rx_iface": "",               # e.g. "eth0" (maps to udpsrc multicast-iface)

    # Known SSRC->Name mapping (string keys for JSON)
    "ssrc_names": { "12345678": "Unit A", "23456789": "Unit B" }
}

def load_config():
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open() as f:
            return {**DEFAULT_CFG, **json.load(f)}
    return DEFAULT_CFG.copy()

def save_config(cfg: dict):
    tmp = CONFIG_PATH.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(cfg, f, indent=2)
    tmp.replace(CONFIG_PATH)

# ---------------- TX control ----------------
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
    Build: [sine|alsasrc] -> audioconvert -> audioresample -> rtpL16pay (pt=96, ssrc=..) -> udpsink
    """
    global gst_tx
    stop_tx()

    raw_caps = ["audio/x-raw,format=S16LE,channels=1,rate=48000"]

    # choose source
    if (cfg.get("tx_source") or "sine") == "mic":
        dev = (cfg.get("tx_mic_device") or "").strip()
        src = ["alsasrc"] + ([f"device={dev}"] if dev else [])
        src += ["!", *raw_caps]
    else:
        freq = int(cfg.get("tx_sine_freq") or 1000)
        src = ["audiotestsrc","is-live=true","wave=sine",f"freq={freq}","!",*raw_caps]

    ssrc = int(cfg.get("tx_ssrc") or 12345678)

    # udpsink with optional interface pinning
    udpsink = [
        "udpsink",
        f"host={cfg['tx_multicast']}",
        f"port={int(cfg['tx_port'])}",
        "auto-multicast=true",
        "ttl=16",
    ]
    tx_iface = (cfg.get("tx_iface") or "").strip()
    if tx_iface:
        udpsink.append(f"multicast-iface={tx_iface}")
    bind_ip = (cfg.get("tx_bind_ip") or "").strip()
    if bind_ip:
        udpsink.append(f"bind-address={bind_ip}")

    args = [
        "gst-launch-1.0","-q",
        *src,"!","audioconvert","!","audioresample","!",
        "rtpL16pay","pt=96",f"ssrc={ssrc}","!"," ".join(udpsink)
    ]
    # Use shell=False; pass list; but we joined udpsink props above, so split them back:
    flat_args = []
    for a in args:
        if isinstance(a, str) and " " in a and not a.startswith("rtpL16pay"):
            flat_args.extend(a.split(" "))
        else:
            flat_args.append(a)
    gst_tx = subprocess.Popen(flat_args)

# ---------------- RX control ----------------
def stop_rx():
    global rx_worker
    if rx_worker:
        try: rx_worker.stop()
        except Exception: pass
    rx_worker = None

def start_rx(cfg):
    """
    Start Option B RX: same group:port -> demux by SSRC -> level meters -> audiomixer -> sink
    Allows optional iface pinning for the multicast join.
    """
    global rx_worker
    stop_rx()

    sink_mode = (cfg.get("rx_sink") or {}).get("mode","file")
    outpath = Path(__file__).with_name((cfg.get("rx_sink") or {}).get("path","mix.wav"))
    ssrc_names = cfg.get("ssrc_names") or {}
    rx_iface = (cfg.get("rx_iface") or "").strip() or None

    rx_worker = RxPartylineWorker(
        cfg["rx_multicast"], cfg["rx_port"], sink_mode, outpath, ssrc_names,
        iface=rx_iface
    )
    rx_worker.start()

# ---------------- API ----------------
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
    # sanitize
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
    # keep mapping updated with our own name
    try:
        cfg.setdefault("ssrc_names", {})[str(int(cfg["tx_ssrc"]))] = cfg.get("tx_name") or f"SSRC {cfg['tx_ssrc']}"
    except Exception:
        pass
    save_config(cfg)
    return jsonify({"ok": True, "config": cfg})

@app.post("/restart")
def restart_both():
    # IMPORTANT: start RX first, wait for IGMP join, then TX
    cfg = load_config()
    start_rx(cfg)
    time.sleep(0.5)   # small cushion so the switch/AP learns the multicast membership
    start_tx(cfg)
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
    if rx_worker is None:
        return jsonify({"group": None, "port": None, "receiving": False, "pps_recent": 0.0, "bps_recent": 0.0})
    # We no longer expose socket-sniffer metrics here to simplify; use peers_snapshot for activity
    snap = rx_worker.peers_snapshot()
    # derive "receiving" if any peer seen in last 2.5s
    now = time.time()
    receiving = any(p.get("last_seen_sec", 999) < 2.5 for p in snap.get("peers", []))
    return jsonify({
        "group": rx_worker.group,
        "port": rx_worker.port,
        "receiving": receiving,
        "mix_level_db": snap.get("mix_level_db")
    })

@app.get("/rx/peers")
def rx_peers():
    if rx_worker is None:
        return jsonify({"peers": [], "mix_level_db": None})
    return jsonify(rx_worker.peers_snapshot())

# ---------------- Static (React) ----------------
@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_frontend(path):
    build_dir = app.static_folder
    index = os.path.join(build_dir, "index.html")
    if path and os.path.exists(os.path.join(build_dir, path)):
        return send_from_directory(build_dir, path)
    if os.path.exists(index):
        return send_from_directory(build_dir, "index.html")
    return jsonify({"ok": True, "api": "running", "hint": "Use CRA dev server with proxy or build the frontend."})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
