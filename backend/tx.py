import subprocess, re, time, sys
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

    def _build_src(dev_str: str):
        dev = _normalize_alsa_device(dev_str or "")
        if dev.startswith("hw:"):
            dev = "plughw:" + dev.split(":",1)[1]
        return ["alsasrc"] + ([f"device={dev}"] if dev else [])

    if (cfg.get("tx_source") or "sine") == "mic":
        user_dev = (cfg.get("tx_mic_device") or "").strip()
        src = _build_src(user_dev)
        # Do conversion downstream to avoid device open failures on strict hw devices
    else:
        freq = int(cfg.get("tx_sine_freq") or 1000)
        src = ["audiotestsrc","is-live=true","wave=sine",f"freq={freq}","!",*raw_caps]

    ssrc = int(cfg.get("tx_ssrc") or 12345678)

    def _launch(src_chain):
        args = [
            "gst-launch-1.0","-q",
            *src_chain,
            "!","audioconvert","!","audioresample",
            "!",*raw_caps,                # ensure mono/48k/S16LE
            "!","audioconvert",          # convert endianness for RTP L16
            "!","audio/x-raw,format=S16BE",
            "!","rtpL16pay","pt=96","min-ptime=4000000","max-ptime=4000000",f"ssrc={ssrc}","!",
            "udpsink",f"host={cfg['tx_multicast']}",f"port={int(cfg['tx_port'])}","auto-multicast=true","ttl=16"
        ]
        return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # First attempt
    _proc = _launch(src)
    time.sleep(0.7)
    if _proc.poll() is not None and _proc.returncode != 0 and (cfg.get("tx_source") or "sine") == "mic":
        # Retry with dsnoop variant if busy
        dev = _normalize_alsa_device((cfg.get("tx_mic_device") or "").strip())
        dsnoop_dev = None
        m = re.match(r"^(?:hw|plughw):(?:(CARD=([^,]+),DEV=(\d+))|(\d+),(\d+))$", dev)
        if m:
            if m.group(1):
                dsnoop_dev = f"dsnoop:CARD={m.group(2)},DEV={m.group(3)}"
            else:
                dsnoop_dev = f"dsnoop:{m.group(4)},{m.group(5)}"
        elif dev.startswith("sysdefault:"):
            # Try dsnoop for the same card if specified
            tail = dev.split(":",1)[1]
            dsnoop_dev = f"dsnoop:{tail}"
        if dsnoop_dev:
            try:
                src2 = _build_src(dsnoop_dev)
                _proc = _launch(src2)
            except Exception:
                pass

def is_running():
    return _proc is not None and _proc.poll() is None
