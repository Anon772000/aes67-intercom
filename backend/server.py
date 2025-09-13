from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import traceback
import os, time
from pathlib import Path

from config_store import load_config, save_config
from monitor import RxMonitor
from tx import start_tx, stop_tx, is_running as tx_running
from rx_worker import RxPartylineWorker

app = Flask(__name__, static_folder="../frontend/build", static_url_path="")
# Enable CORS for development (allows calls from :3000 dev server or other hosts)
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=False)

rx_worker = None
rxmon = RxMonitor()

# ---------- API ----------
@app.get("/status")
def status():
    cfg = load_config()
    return jsonify({
        "config": cfg,
        "tx_running": tx_running(),
        "rx_running": (rx_worker is not None)
    })

@app.post("/config")
def update_config():
    cfg = load_config()
    incoming = request.get_json(force=True) or {}
    if "tx_sine_freq" in incoming:
        try: incoming["tx_sine_freq"] = max(20, min(20000, int(incoming["tx_sine_freq"])))
        except Exception: incoming["tx_sine_freq"] = 1000
    if "tx_ssrc" in incoming:
        try: incoming["tx_ssrc"] = int(incoming["tx_ssrc"]) & 0xFFFFFFFF
        except Exception: incoming["tx_ssrc"] = 12345678
    cfg.update(incoming)
    try:
        cfg.setdefault("ssrc_names", {})[str(int(cfg["tx_ssrc"]))] = cfg.get("tx_name") or f"SSRC {cfg['tx_ssrc']}"
    except Exception: pass
    save_config(cfg)
    return jsonify({"ok": True, "config": cfg})

@app.post("/restart")
def restart_both():
    cfg = load_config()
    start_tx(cfg); start_rx_internal(cfg)
    return jsonify({"ok": True, "tx": "started", "rx": "started"})

@app.post("/start/tx")
def start_tx_only():
    cfg = load_config(); start_tx(cfg)
    return jsonify({"ok": True, "tx": "started"})

@app.post("/start/rx")
def start_rx_only():
    cfg = load_config()
    try:
        start_rx_internal(cfg)
        return jsonify({"ok": True, "rx": "started"})
    except ModuleNotFoundError as e:
        err = str(e)
        if "gi" in err.lower():
            hint = (
                "GStreamer Python bindings not found. Install: "
                "sudo apt update && sudo apt install -y python3-gi gir1.2-gstreamer-1.0 "
                "gstreamer1.0-tools gstreamer1.0-alsa gstreamer1.0-plugins-base "
                "gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly"
            )
            print("/start/rx error (gi missing):\n" + traceback.format_exc())
            return jsonify({"ok": False, "error": err, "hint": hint}), 500
        print("/start/rx error:\n" + traceback.format_exc())
        return jsonify({"ok": False, "error": err}), 500
    except Exception as e:
        print("/start/rx error (generic):\n" + traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)}), 500

@app.post("/stop/tx")
def stop_tx_only():
    stop_tx(); return jsonify({"ok": True, "tx": "stopped"})

@app.post("/stop/rx")
def stop_rx_only():
    stop_rx_internal(); return jsonify({"ok": True, "rx": "stopped"})

@app.get("/rx/metrics")
def rx_metrics():
    m = rxmon.read_stats()
    if rx_worker is not None:
        m["mix_level_db"] = rx_worker.mix_level_db
    return jsonify(m)

@app.get("/rx/peers")
def rx_peers():
    if rx_worker is None:
        return jsonify({"peers": [], "mix_level_db": None})
    return jsonify({
        "peers": rx_worker.peers_snapshot(),
        "mix_level_db": getattr(rx_worker, "mix_level_db", None)
    })

# ---------- helpers ----------
def start_rx_internal(cfg):
    global rx_worker
    stop_rx_internal()
    rxmon.start(cfg["rx_multicast"], cfg["rx_port"])
    sink_mode = (cfg.get("rx_sink") or {}).get("mode","file")
    outpath = Path(__file__).with_name((cfg.get("rx_sink") or {}).get("path","mix.wav"))
    ssrc_names = cfg.get("ssrc_names") or {}
    iface = cfg.get("rx_iface")  # Optional: e.g. "eth0"; None/empty means default
    rx_worker = RxPartylineWorker(cfg["rx_multicast"], cfg["rx_port"], sink_mode, outpath, ssrc_names, iface)
    rx_worker.start()

def stop_rx_internal():
    global rx_worker
    if rx_worker:
        try: rx_worker.stop()
        except Exception: pass
    rx_worker=None
    rxmon.stop()

# ---------- Static (React) ----------
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
