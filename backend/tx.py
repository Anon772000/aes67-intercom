import subprocess, re
_proc = None

def stop_tx():
    global _proc
    if _proc and _proc.poll() is None:
        try: _proc.terminate()
        except Exception: pass
    _proc=None

def _normalize_alsa_device(dev: str) -> str:
    if not dev:
        return ""
    d = dev.strip()
    # Accept common good forms as-is
    if d.startswith(("hw:", "plughw:", "default", "sysdefault")):
        return d
    # hw0:0 or hw0,0 -> hw:0,0
    m = re.match(r"^hw(\d+)[,:](\d+)$", d)
    if m:
        return f"hw:{m.group(1)},{m.group(2)}"
    # 0 or 1 -> hw:<card>
    if re.fullmatch(r"\d+", d):
        return f"hw:{d}"
    # 0,0 -> hw:0,0
    m = re.match(r"^(\d+)[,:](\d+)$", d)
    if m:
        return f"hw:{m.group(1)},{m.group(2)}"
    return d


def start_tx(cfg: dict):
    """
    Sends L16/48k/mono to cfg['tx_multicast']:cfg['tx_port'] with 4 ms packet time.
    """
    global _proc
    stop_tx()
    raw_caps = ["audio/x-raw,format=S16LE,channels=1,rate=48000"]

    if (cfg.get("tx_source") or "sine") == "mic":
        dev = _normalize_alsa_device((cfg.get("tx_mic_device") or "").strip())
        src = ["alsasrc"] + ([f"device={dev}"] if dev else [])
        src += ["!", *raw_caps]
    else:
        freq = int(cfg.get("tx_sine_freq") or 1000)
        src = ["audiotestsrc","is-live=true","wave=sine",f"freq={freq}","!",*raw_caps]

    ssrc = int(cfg.get("tx_ssrc") or 12345678)

    args = [
        "gst-launch-1.0","-q",
        *src,"!","audioconvert","!","audioresample","!",
        "rtpL16pay","pt=96","min-ptime=4000000","max-ptime=4000000",f"ssrc={ssrc}","!",
        "udpsink",f"host={cfg['tx_multicast']}",f"port={int(cfg['tx_port'])}","auto-multicast=true","ttl=16"
    ]
    _proc = subprocess.Popen(args)

def is_running():
    return _proc is not None and _proc.poll() is None
