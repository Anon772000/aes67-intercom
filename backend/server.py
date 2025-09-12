from flask import Flask, request, jsonify, send_from_directory
import subprocess, json, os, time
from pathlib import Path
from rx_worker import RxPartylineWorker

app = Flask(__name__, static_folder="../frontend/build", static_url_path="")

CONFIG_PATH = Path(__file__).with_name("config.json")
gst_tx = None
rx_worker = None

DEFAULT_CFG = {
    "tx_source": "sine",
    "tx_sine_freq": 1000,
    "tx_mic_device": "hw:0",
    "tx_name": "Unit A",
    "tx_ssrc": 12345678,
    "tx_multicast": "239.69.69.69",
    "tx_port": 5004,
    "rx_multicast": "239.69.69.69",
    "rx_port": 5004,
    "rx_sink": {"mode": "auto"},
    "ssrc_names": {"12345678": "Unit A"}
}

def load_config():
    if CONFIG_PATH.exists():
        return {**DEFAULT_CFG, **json.load(open(CONFIG_PATH))}
    return DEFAULT_CFG.copy()

def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)

def stop_tx():
    global gst_tx
    if gst_tx and gst_tx.poll() is None:
        gst_tx.terminate()
    gst_tx = None

def start_tx(cfg):
    global gst_tx
    stop_tx()
    freq = int(cfg.get("tx_sine_freq") or 1000)
    ssrc = int(cfg.get("tx_ssrc") or 12345678)
    args = [
        "gst-launch-1.0", "-q",
        "audiotestsrc", "is-live=true", "wave=sine", f"freq={freq}", "!",
        "audioconvert", "!", "audioresample", "!",
        "rtpL16pay", "pt=96", f"ssrc={ssrc}", "!",
        "udpsink", f"host={cfg['tx_multicast']}", f"port={int(cfg['tx_port'])}",
        "auto-multicast=true", "ttl=16"
    ]
    gst_tx = subprocess.Popen(args)

def stop_rx():
    global rx_worker
    if rx_worker: rx_worker.stop()
    rx_worker = None

def start_rx(cfg):
    global rx_worker
    stop_rx()
    rx_worker = RxPartylineWorker(cfg["rx_multicast"], cfg["rx_port"], cfg["rx_sink"]["mode"], "mix.wav", cfg["ssrc_names"])
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
    cfg.update(incoming)
    save_config(cfg)
    return jsonify({"ok": True, "config": cfg})

@app.post("/restart")
def restart_both():
    cfg = load_config()
    start_rx(cfg)
    time.sleep(0.5)
    start_tx(cfg)
    return jsonify({"ok": True})

@app.get("/rx/peers")
def rx_peers():
    if rx_worker is None:
        return jsonify({"peers": []})
    return jsonify(rx_worker.peers_snapshot())

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
