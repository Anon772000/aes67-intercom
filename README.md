# AES67 Intercom

Backend (Flask + GStreamer) and Frontend (React) demo.

## Systemd Deployment (Gunicorn)

Prereqs:
- Repo path: `/opt/aes67-intercom`
- Backend path: `/opt/aes67-intercom/backend`
- Python venv: `/opt/aes67-intercom/backend/venv`
- Run as user: `harrison` (add to `audio` group if using ALSA)

Setup venv + packages (on the Pi):
- `cd /opt/aes67-intercom/backend`
- `python3 -m venv --system-site-packages venv`
- `source venv/bin/activate`
- `pip install -U pip wheel`
- `pip install -r requirements.txt`
- `sudo usermod -aG audio harrison`  # log out/in for group to apply

Install systemd service:
- `sudo cp /opt/aes67-intercom/deploy/aes67-intercom.service /etc/systemd/system/aes67-intercom.service`
- If your user/path differs, edit the file accordingly (`User=`, `WorkingDirectory=`, `ExecStart=`)
- `sudo systemctl daemon-reload`
- `sudo systemctl enable --now aes67-intercom.service`

Monitor:
- `systemctl status aes67-intercom.service`
- `journalctl -u aes67-intercom.service -f`

Notes:
- The service runs Gunicorn on `0.0.0.0:8080` with 1 worker and 2 threads.
- `PYTHONPATH=/usr/lib/python3/dist-packages` is set so apt-installed `python3-gi` (GStreamer) is importable in the venv.
- The UI “Restart Backend” button exits the process; with `Restart=always`, systemd brings it back automatically.
