from pathlib import Path
import json

CONFIG_PATH = Path(__file__).with_name("config.json")

DEFAULT_CFG = {
    "tx_source": "sine",          # "sine" | "mic"
    "tx_sine_freq": 1000,         # Hz
    "tx_mic_device": "",          # ALSA device; blank = auto (UI may suggest dsnoop on IQaudIO CODEC)
    "tx_name": "Unit A",
    "tx_ssrc": 12345678,
    "tx_multicast": "239.69.69.69",
    "tx_port": 5004,
    "tx_iface": None,

    "rx_multicast": "239.69.69.69",
    "rx_port": 5004,
    "rx_sink": {"mode": "file", "path": "mix.wav"},
    "rx_iface": None,

    "ssrc_names": { "12345678": "Unit A", "23456789": "Unit B" }
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
