from flask import Flask, request, jsonify, send_from_directory, send_file
from flask_cors import CORS
import traceback
import os, time, threading, subprocess, shlex
from pathlib import Path
from mic_monitor import MicMonitor

from config_store import load_config, save_config
from monitor import RxMonitor
from tx import start_tx, stop_tx, is_running as tx_running
from rx_worker import RxPartylineWorker

app = Flask(__name__, static_folder="../frontend/build", static_url_path="")
# Enable CORS for development (allows calls from :3000 dev server or other hosts)
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=False)

rx_worker = None
rxmon = RxMonitor()
micmon = MicMonitor()

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
    # Start RX first to ensure IGMP join and jitterbuffer are ready, then TX
    start_rx_internal(cfg)
    try:
        time.sleep(0.25)
    except Exception:
        pass
    try:
        # Ensure mic monitor isn't holding the capture device
        micmon.stop()
    except Exception:
        pass
    start_tx(cfg)
    return jsonify({"ok": True, "rx": "started", "tx": "started"})

@app.post("/start/tx")
def start_tx_only():
    cfg = load_config()
    try:
        micmon.stop()
    except Exception:
        pass
    start_tx(cfg)
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

@app.post("/restart/backend")
def restart_backend():
    # Respond immediately, then exit process so an external manager can restart it
    def _do_exit():
        try:
            time.sleep(0.5)
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=_do_exit, daemon=True).start()
    return jsonify({"ok": True, "backend": "restarting"})

@app.get("/rx/metrics")
def rx_metrics():
    if rx_worker is not None:
        m = rx_worker.metrics_snapshot()
        m["mix_level_db"] = getattr(rx_worker, "mix_level_db", None)
        return jsonify(m)
    return jsonify(rxmon.read_stats())

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

@app.get("/download/mix")
def download_mix():
    # Prefer the live worker's path; otherwise use configured default
    p = None
    try:
        if rx_worker is not None and getattr(rx_worker, "sink_path", None):
            p = Path(rx_worker.sink_path)
        else:
            cfg = load_config()
            p = Path(__file__).with_name((cfg.get("rx_sink") or {}).get("path", "mix.wav"))
        if not p.is_file():
            return jsonify({"ok": False, "error": f"File not found: {p}"}), 404
        resp = send_file(p, mimetype="audio/wav", as_attachment=True, download_name=p.name)
        resp.headers["Cache-Control"] = "no-store"
        return resp
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/alsa/devices")
def alsa_devices():
    """List ALSA PCM device strings useful for capture (best-effort).
    Returns { devices: [ { id, desc } ] }
    """
    devices = []
    seen = set()
    try:
        # arecord -L: logical devices (default, sysdefault, plughw, dsnoop, ...)
        p = subprocess.run(["arecord","-L"], capture_output=True, text=True, timeout=3)
        lines = p.stdout.splitlines()
        cur_id = None
        for ln in lines:
            if not ln.strip():
                continue
            if not ln.startswith(" ") and not ln.startswith("\t"):
                cur_id = ln.strip()
                if cur_id == "null":
                    cur_id = None
                    continue
                if cur_id not in seen:
                    seen.add(cur_id)
                    devices.append({"id": cur_id, "desc": ""})
            elif devices and cur_id:
                # First indented line is description
                if not devices[-1]["desc"]:
                    devices[-1]["desc"] = ln.strip()
    except Exception:
        pass
    # Prioritize commonly useful capture devices
    def score(d):
        s = d["id"].lower()
        if s.startswith("sysdefault") or s == "default": return 0
        if s.startswith("plughw"): return 1
        if s.startswith("dsnoop"): return 2
        if s.startswith("hw"): return 3
        return 4
    devices.sort(key=score)
    # Recommend dsnoop for IQaudIOCODEC cards (CODEC Zero) to allow sharing and proper format
    rec = None
    for d in devices:
        sid = (d.get("id") or "").lower()
        if sid.startswith("dsnoop:") and "iqaudiocodec" in sid:
            rec = d["id"]
            break
    return jsonify({"devices": devices[:40], "recommended": rec})

# ---------- Mic monitor (listen locally + VU) ----------
@app.post("/monitor/mic/start")
def mic_monitor_start():
    try:
        cfg = load_config()
        dev = cfg.get("tx_mic_device") or ""
        ok = micmon.start(dev, with_audio=True)
        if ok:
            return jsonify({"ok": True, "monitoring": True})
        return jsonify({"ok": False, "error": "failed to start monitor"}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.post("/monitor/mic/stop")
def mic_monitor_stop():
    try:
        micmon.stop()
        return jsonify({"ok": True, "monitoring": False})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/monitor/mic/level")
def mic_monitor_level():
    try:
        return jsonify({"db": micmon.get_level()})
    except Exception as e:
        return jsonify({"db": None, "error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
