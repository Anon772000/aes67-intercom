# AES67 Intercom

Backend (Flask + GStreamer) and Frontend (React) demo.

## Systemd Deployment (Gunicorn)

Prereqs:
- Repo path: `/opt/aes67-intercom`
- Backend path: `/opt/aes67-intercom/backend`
- Python venv: `/opt/aes67-intercom/backend/venv`
- Run as user: `harrison` (add to `audio` group if using ALSA)

Quick install (on the Pi):
- `cd /opt/aes67-intercom`
- `sudo ./deploy/install.sh`  # builds frontend if npm is available
- Custom unit name: `sudo ./deploy/install.sh --name intercom-prod`
  - Or via env: `sudo SERVICE_NAME=intercom-prod ./deploy/install.sh`

Frontend dev server (recommended while developing):
- Run CRA dev server under systemd alongside the backend:
  - `sudo ./deploy/install.sh --frontend-dev`
  - Access UI at `http://<pi-ip>:3000` (API proxied to backend on :8080)
  - To name the frontend unit: `--frontend-name web-intercom`
  - Stop backend static serving is not required; dev server takes precedence when you visit :3000.
  - The service auto-installs npm dependencies on boot if missing (ExecStartPre); first boot may take a while on a Pi.

Manual steps (equivalent to the script):
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
 - If you’re using the IQaudIO CODEC Zero, configure capture in `alsamixer -c 0` (F4) and enable Mic Bias if needed, then `sudo alsactl store`.
