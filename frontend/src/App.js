// frontend/src/App.js
import React, { useEffect, useState, useCallback } from "react";
import { API_BASE, apiGet, apiPost } from "./api";

function DbMeter({ db, width = 160 }) {
  // Map -60..0 dBFS to 0..100%
  const v = db == null || db <= -60 ? 0 : Math.max(0, Math.min(100, 100 * (db + 60) / 60));
  return (
    <div style={{ width, height: 10, background: "#eee", borderRadius: 6, overflow: "hidden" }}>
      <div
        style={{
          width: `${v}%`,
          height: "100%",
          background: v > 85 ? "#ef4444" : v > 60 ? "#f59e0b" : "#10b981",
          transition: "width 120ms linear",
        }}
      />
    </div>
  );
}

export default function App() {
  const [config, setConfig] = useState({
    tx_source: "sine",
    tx_sine_freq: 1000,
    tx_mic_device: "hw:0",
    tx_name: "Unit A",
    tx_ssrc: 12345678,
    tx_multicast: "239.69.69.69",
    tx_port: 5004,
    rx_multicast: "239.69.69.69",
    rx_port: 5004,
    rx_sink: { mode: "file", path: "mix.wav" },
    ssrc_names: { "12345678": "Unit A" },
  });
  const [status, setStatus] = useState({ tx_running: false, rx_running: false });
  const [metrics, setMetrics] = useState({
    receiving: false,
    pps_recent: 0,
    bps_recent: 0,
    mix_level_db: null,
  });
  const [peers, setPeers] = useState([]);
  const [mixDb, setMixDb] = useState(null);
  const [micDb, setMicDb] = useState(null);
  const [err, setErr] = useState("");
  const [alsaDevices, setAlsaDevices] = useState([]);
  const [alsaRec, setAlsaRec] = useState("");

  const refreshStatus = useCallback(() => {
    return apiGet("/status")
      .then((s) => {
        setConfig(s.config);
        setStatus({ tx_running: s.tx_running, rx_running: s.rx_running });
        setErr("");
      })
      .catch((e) => setErr(e.message || String(e)));
  }, []);

  useEffect(() => {
    refreshStatus();
    const t1 = setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return; // pause when hidden
      apiGet("/rx/metrics")
          .then((m) => {
            setMetrics(m);
            if (typeof m.mix_level_db === "number") setMixDb(m.mix_level_db);
          })
        .catch((e) => setErr(e.message || String(e)));
    }, 500);

    const t2 = setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return; // pause when hidden
      apiGet("/rx/peers")
          .then((r) => {
            setPeers(r.peers || []);
            if (typeof r.mix_level_db === "number") setMixDb(r.mix_level_db);
          })
        .catch((e) => setErr(e.message || String(e)));
    }, 500);

      return () => {
        clearInterval(t1);
        clearInterval(t2);
      };
    }, [refreshStatus]);

  // Poll mic VU level
  useEffect(() => {
    const t = setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      apiGet("/monitor/mic/level")
        .then((v) => setMicDb(typeof v.db === "number" ? v.db : null))
        .catch(() => {});
    }, 300);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    if ((config.tx_source || "sine") === "mic") {
      apiGet("/alsa/devices")
        .then((d) => {
          setAlsaDevices(d.devices || []);
          setAlsaRec(d.recommended || "");
        })
        .catch(() => {});
    }
  }, [config.tx_source]);

  // If mic selected and no device set, prefill with recommended
  useEffect(() => {
    if ((config.tx_source || "sine") === "mic" && !config.tx_mic_device && alsaRec) {
      setConfig({ ...config, tx_mic_device: alsaRec });
    }
  }, [alsaRec, config.tx_source]);

  const saveConfig = (e) => {
    e.preventDefault();
    apiPost("/config", config)
      .then(refreshStatus)
      .catch((e) => setErr(e.message || String(e)));
  };

  const restartBoth = () =>
    apiPost("/restart")
      .then(refreshStatus)
      .catch((e) => setErr(e.message || String(e)));
  const restartBackend = () =>
    apiPost("/restart/backend")
      .then(() => setErr("Backend restarting... if it doesn't come back, restart the service on the Pi."))
      .catch((e) => setErr(e.message || String(e)));
  const startMicMonitor = () =>
    apiPost("/monitor/mic/start")
      .then(() => setErr(""))
      .catch((e) => setErr(e.message || String(e)));
  const stopMicMonitor = () =>
    apiPost("/monitor/mic/stop")
      .then(() => setErr(""))
      .catch((e) => setErr(e.message || String(e)));
  const startTx = () =>
    apiPost("/start/tx")
      .then(refreshStatus)
      .catch((e) => setErr(e.message || String(e)));
  const startRx = () =>
    apiPost("/start/rx")
      .then(refreshStatus)
      .catch((e) => setErr(e.message || String(e)));
  const stopTx = () =>
    apiPost("/stop/tx")
      .then(refreshStatus)
      .catch((e) => setErr(e.message || String(e)));
  const stopRx = () =>
    apiPost("/stop/rx")
      .then(refreshStatus)
      .catch((e) => setErr(e.message || String(e)));

  const downloadMix = async () => {
    try {
      const res = await fetch(`${API_BASE}/download/mix`, { cache: "no-store" });
      if (!res.ok) {
        const t = await res.text().catch(() => "");
        throw new Error(`download -> ${res.status}${t ? " " + t : ""}`);
      }
      const blob = await res.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "mix.wav";
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.URL.revokeObjectURL(url);
    } catch (e) {
      setErr(e.message || String(e));
    }
  };

  const badge = (ok, label) => (
    <span
      style={{
        padding: "2px 8px",
        borderRadius: 12,
        marginLeft: 8,
        background: ok ? "#d1fae5" : "#fee2e2",
        color: ok ? "#065f46" : "#991b1b",
        fontWeight: 600,
        fontSize: 12,
      }}
    >
      {label}
    </span>
  );

  return (
    <div style={{ fontFamily: "system-ui, sans-serif", maxWidth: 980, margin: "2rem auto", lineHeight: 1.4 }}>
      <h1>AES67 Intercom (Party-line)</h1>

      <div style={{ fontSize: 12, color: "#666", margin: "4px 0 12px" }}>
        API base: <code>{API_BASE || "(same-origin)"}</code>
      </div>

      {err && (
        <div style={{ background: "#fee2e2", color: "#7f1d1d", padding: 8, borderRadius: 8, marginBottom: 12 }}>
          {err}
        </div>
      )}

      <p>
        Status: TX {badge(status.tx_running, status.tx_running ? "running" : "stopped")} · RX{" "}
        {badge(status.rx_running, status.rx_running ? "running" : "stopped")} · Stream{" "}
        {badge(metrics.receiving, metrics.receiving ? "receiving" : "no packets")}
      </p>

      <div style={{ fontSize: 14, color: "#555", marginBottom: 12 }}>
        {metrics.group && (
          <div>
            RX group <code>{metrics.group}:{metrics.port}</code>
          </div>
        )}
        <div>
          PPS: {metrics.pps_recent?.toFixed?.(1) || 0} · BPS: {Math.round(metrics.bps_recent || 0)}
        </div>
      </div>

      <div style={{ margin: "8px 0 18px" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <strong>Mix Level</strong>
          <DbMeter db={mixDb} width={280} />
          <span style={{ minWidth: 60, textAlign: "right" }}>{mixDb != null ? `${mixDb.toFixed(1)} dBFS` : "--"}</span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 6 }}>
          <strong>Mic VU</strong>
          <DbMeter db={micDb} width={280} />
          <span style={{ minWidth: 60, textAlign: "right" }}>{micDb != null ? `${micDb.toFixed(1)} dBFS` : "--"}</span>
        </div>
      </div>

      <form onSubmit={saveConfig} style={{ display: "grid", gap: "0.75rem" }}>
        <fieldset style={{ padding: 12 }}>
          <legend>TX (Sender)</legend>

        <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
            <label>
              Source:
              <select
                value={config.tx_source || "sine"}
                onChange={(e) => setConfig({ ...config, tx_source: e.target.value })}
                style={{ marginLeft: 8 }}
              >
                <option value="sine">Sine tone</option>
                <option value="mic">Mic (ALSA)</option>
              </select>
            </label>

            {(config.tx_source || "sine") === "sine" && (
              <label>
                Freq (Hz):
                <input
                  type="number"
                  min={20}
                  max={20000}
                  value={Number(config.tx_sine_freq ?? 1000)}
                  onChange={(e) => setConfig({ ...config, tx_sine_freq: Number(e.target.value || 1000) })}
                  style={{ marginLeft: 8, width: 120 }}
                />
              </label>
            )}

            {(config.tx_source || "sine") === "mic" && (
              <label>
                ALSA device:
                <input
                  list="alsa-devices"
                  placeholder="leave blank for default"
                  value={config.tx_mic_device || ""}
                  onChange={(e) => setConfig({ ...config, tx_mic_device: e.target.value })}
                  style={{ marginLeft: 8, width: 260 }}
                />
                <datalist id="alsa-devices">
                  {alsaDevices.map((d, i) => (
                    <option key={i} value={d.id}>{d.desc || d.id}</option>
                  ))}
                </datalist>
                {alsaRec && (
                  <div style={{ fontSize: 12, color: "#666", marginTop: 4 }}>
                    Recommended for IQaudIO CODEC: <code>{alsaRec}</code> (device outputs S32_LE stereo; app downmixes to mono)
                  </div>
                )}
              </label>
            )}

            <label>
              TX Name:
              <input
                value={config.tx_name || ""}
                onChange={(e) => setConfig({ ...config, tx_name: e.target.value })}
                style={{ marginLeft: 8, width: 180 }}
              />
            </label>

            <label>
              TX SSRC:
              <input
                type="number"
                value={Number(config.tx_ssrc ?? 12345678)}
                onChange={(e) => setConfig({ ...config, tx_ssrc: Number(e.target.value || 12345678) })}
                style={{ marginLeft: 8, width: 160 }}
              />
            </label>
            <label>
              Interface (optional):
              <input
                placeholder="e.g. eth0"
                value={config.tx_iface || ""}
                onChange={(e) => setConfig({ ...config, tx_iface: e.target.value })}
                style={{ marginLeft: 8, width: 140 }}
              />
            </label>
          </div>

          <div style={{ marginTop: 8 }}>
            <label>
              Multicast:
              <input
                value={config.tx_multicast}
                onChange={(e) => setConfig({ ...config, tx_multicast: e.target.value })}
                style={{ marginLeft: 8, width: 220 }}
              />
            </label>
            <label style={{ marginLeft: 12 }}>
              Port:
              <input
                type="number"
                value={config.tx_port}
                onChange={(e) => setConfig({ ...config, tx_port: Number(e.target.value) })}
                style={{ marginLeft: 8, width: 120 }}
              />
            </label>
          </div>
        </fieldset>

        <fieldset style={{ padding: 12 }}>
          <legend>RX (Party-line: same group, mix all talkers)</legend>
      <div>
        <label>
          Multicast:
          <input
            value={config.rx_multicast}
            onChange={(e) => setConfig({ ...config, rx_multicast: e.target.value })}
            style={{ marginLeft: 8, width: 220 }}
          />
        </label>
        <label style={{ marginLeft: 12 }}>
          Port:
          <input
            type="number"
            value={config.rx_port}
            onChange={(e) => setConfig({ ...config, rx_port: Number(e.target.value) })}
            style={{ marginLeft: 8, width: 120 }}
          />
        </label>
        <label style={{ marginLeft: 12 }}>
          Interface (optional):
          <input
            placeholder="e.g. eth0"
            value={config.rx_iface || ""}
            onChange={(e) => setConfig({ ...config, rx_iface: e.target.value })}
            style={{ marginLeft: 8, width: 140 }}
          />
        </label>
      </div>
          <div style={{ marginTop: 8 }}>
            <label>
              Sink:
              <select
                value={config.rx_sink?.mode || "file"}
                onChange={(e) => setConfig({ ...config, rx_sink: { ...(config.rx_sink || {}), mode: e.target.value } })}
                style={{ marginLeft: 8 }}
              >
                <option value="file">Write mixed WAV</option>
                <option value="auto">Play on device (autoaudiosink)</option>
              </select>
            </label>
            {(config.rx_sink?.mode || "file") === "file" && (
              <label style={{ marginLeft: 12 }}>
                File path:
                <input
                  value={config.rx_sink?.path || "mix.wav"}
                  onChange={(e) =>
                    setConfig({ ...config, rx_sink: { ...(config.rx_sink || {}), path: e.target.value } })
                  }
                  style={{ marginLeft: 8, width: 260 }}
                />
              </label>
            )}
          </div>
        </fieldset>

        <div style={{ display: "flex", gap: "0.5rem", marginTop: 8, flexWrap: "wrap" }}>
          <button type="submit">Save config</button>
          <button type="button" onClick={restartBoth}>Restart TX+RX</button>
          <button type="button" onClick={restartBackend}>Restart Backend</button>
          <button type="button" onClick={startTx}>Start TX</button>
          <button type="button" onClick={stopTx}>Stop TX</button>
          <button type="button" onClick={startRx}>Start RX</button>
          <button type="button" onClick={stopRx}>Stop RX</button>
          <button type="button" onClick={downloadMix}>Download mix</button>
          <button type="button" onClick={startMicMonitor}>Monitor Mic</button>
          <button type="button" onClick={stopMicMonitor}>Stop Monitor</button>
        </div>
      </form>

      <h3 style={{ marginTop: 24 }}>Active Talkers</h3>
      <table style={{ width: "100%", borderCollapse: "collapse" }}>
        <thead>
          <tr style={{ textAlign: "left", borderBottom: "1px solid #ddd" }}>
            <th style={{ padding: "6px 0" }}>Name</th>
            <th>SSRC</th>
            <th>Packets</th>
            <th>Level</th>
            <th>Last seen (s)</th>
          </tr>
        </thead>
        <tbody>
          {(peers || []).map((p, i) => (
            <tr key={i} style={{ borderBottom: "1px solid #f1f1f1" }}>
              <td style={{ padding: "6px 0" }}>{p.name || ""}</td>
              <td><code>{p.ssrc}</code></td>
              <td>{p.packets}</td>
              <td>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <DbMeter db={p.level_db} />
                  <span style={{ minWidth: 60, textAlign: "right" }}>
                    {p.level_db != null ? `${p.level_db.toFixed(1)} dBFS` : "--"}
                  </span>
                </div>
              </td>
              <td>{p.last_seen_sec}</td>
            </tr>
          ))}
          {(!peers || peers.length === 0) && (
            <tr>
              <td colSpan="5" style={{ padding: "8px 0", color: "#666" }}>
                No talkers detected yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>

      <p style={{ marginTop: 16, color: "#666" }}>
        Tip: Give each box a unique <b>TX SSRC</b> and <b>TX Name</b>. The receiver maps SSRC → name, and meters show
        per-talker levels plus the overall mix.
      </p>
    </div>
  );
}
