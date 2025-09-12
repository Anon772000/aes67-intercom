// frontend/src/App.js
import React, { useEffect, useState } from "react";

const API_BASE = process.env.REACT_APP_API_BASE || "";
const api = (path, opts) => fetch(API_BASE + path, opts).then(r => r.json());

function DbMeter({ db, width=160 }) {
  // Map -60..0 dBFS to 0..100%
  const v = (db == null || db <= -60) ? 0 : Math.max(0, Math.min(100, 100*(db+60)/60));
  return (
    <div style={{ width, height: 10, background: "#eee", borderRadius: 6, overflow: "hidden" }}>
      <div style={{ width: `${v}%`, height: "100%", background: v>85?"#ef4444":v>60?"#f59e0b":"#10b981" }} />
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
    ssrc_names: { "12345678": "Unit A" }
  });
  const [status, setStatus] = useState({ tx_running: false, rx_running: false });
  const [metrics, setMetrics] = useState({ receiving: false, pps_recent: 0, bps_recent: 0, mix_level_db: null });
  const [peers, setPeers] = useState([]);
  const [mixDb, setMixDb] = useState(null);

  const refreshStatus = () =>
    api("/status").then(s => {
      setConfig(s.config);
      setStatus({ tx_running: s.tx_running, rx_running: s.rx_running });
    }).catch(() => {});

  useEffect(() => {
    refreshStatus();
    const t1 = setInterval(() => api("/rx/metrics").then(m => { setMetrics(m); if (m.mix_level_db !== undefined) setMixDb(m.mix_level_db); }).catch(()=>{}), 500);
    const t2 = setInterval(() => api("/rx/peers").then(r => { setPeers(r.peers || []); if (r.mix_level_db !== undefined) setMixDb(r.mix_level_db); }).catch(()=>{}), 500);
    return () => { clearInterval(t1); clearInterval(t2); };
  }, []);

  const saveConfig = (e) => {
    e.preventDefault();
    api("/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(config)
    }).then(refreshStatus);
  };

  const restartBoth = () => api("/restart", { method: "POST" }).then(refreshStatus);
  const startTx = () => api("/start/tx", { method: "POST" }).then(refreshStatus);
  const startRx = () => api("/start/rx", { method: "POST" }).then(refreshStatus);
  const stopTx = () => api("/stop/tx", { method: "POST" }).then(refreshStatus);
  const stopRx = () => api("/stop/rx", { method: "POST" }).then(refreshStatus);

  const badge = (ok, label) => (
    <span style={{
      padding: "2px 8px", borderRadius: 12, marginLeft: 8,
      background: ok ? "#d1fae5" : "#fee2e2",
      color: ok ? "#065f46" : "#991b1b", fontWeight: 600, fontSize: 12
    }}>{label}</span>
  );

  return (
    <div style={{ fontFamily: "system-ui, sans-serif", maxWidth: 980, margin: "2rem auto", lineHeight: 1.4 }}>
      <h1>AES67 Intercom (Party-line)</h1>

      <p>
        Status: TX {badge(status.tx_running, status.tx_running ? "running" : "stopped")}
        {" "}· RX {badge(status.rx_running, status.rx_running ? "running" : "stopped")}
        {" "}· Stream {badge(metrics.receiving, metrics.receiving ? "receiving" : "no packets")}
      </p>

      <div style={{fontSize: 14, color: "#555", marginBottom: 12}}>
        {metrics.group && (<div>RX group <code>{metrics.group}:{metrics.port}</code></div>)}
        <div>PPS: {metrics.pps_recent?.toFixed?.(1) || 0} · BPS: {Math.round(metrics.bps_recent || 0)}</div>
      </div>

      <div style={{ margin: "8px 0 18px" }}>
        <div style={{display:"flex", alignItems:"center", gap:12}}>
          <strong>Mix Level</strong>
          <DbMeter db={mixDb} width={280} />
          <span style={{minWidth: 60, textAlign: "right"}}>{mixDb!=null ? `${mixDb.toFixed(1)} dBFS` : "--"}</span>
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
                onChange={e => setConfig({ ...config, tx_source: e.target.value })}
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
                  type="number" min={20} max={20000}
                  value={Number(config.tx_sine_freq ?? 1000)}
                  onChange={e => setConfig({ ...config, tx_sine_freq: Number(e.target.value || 1000) })}
                  style={{ marginLeft: 8, width: 120 }}
                />
              </label>
            )}

            {(config.tx_source || "sine") === "mic" && (
              <label>
                ALSA device:
                <input
                  placeholder="e.g. hw:0 (leave blank to auto)"
                  value={config.tx_mic_device || ""}
                  onChange={e => setConfig({ ...config, tx_mic_device: e.target.value })}
                  style={{ marginLeft: 8, width: 200 }}
                />
              </label>
            )}

            <label>
              TX Name:
              <input
                value={config.tx_name || ""}
                onChange={e => setConfig({ ...config, tx_name: e.target.value })}
                style={{ marginLeft: 8, width: 180 }}
              />
            </label>

            <label>
              TX SSRC:
              <input
                type="number"
                value={Number(config.tx_ssrc ?? 12345678)}
                onChange={e => setConfig({ ...config, tx_ssrc: Number(e.target.value || 12345678) })}
                style={{ marginLeft: 8, width: 160 }}
              />
            </label>
          </div>

          <div style={{ marginTop: 8 }}>
            <label>
              Multicast:
              <input
                value={config.tx_multicast}
                onChange={e => setConfig({ ...config, tx_multicast: e.target.value })}
                style={{ marginLeft: 8, width: 220 }}
              />
            </label>
            <label style={{ marginLeft: 12 }}>
              Port:
              <input
                type="number"
                value={config.tx_port}
                onChange={e => setConfig({ ...config, tx_port: Number(e.target.value) })}
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
                onChange={e => setConfig({ ...config, rx_multicast: e.target.value })}
                style={{ marginLeft: 8, width: 220 }}
              />
            </label>
            <label style={{ marginLeft: 12 }}>
              Port:
              <input
                type="number"
                value={config.rx_port}
                onChange={e => setConfig({ ...config, rx_port: Number(e.target.value) })}
                style={{ marginLeft: 8, width: 120 }}
              />
            </label>
          </div>
          <div style={{ marginTop: 8 }}>
            <label>
              Sink:
              <select
                value={config.rx_sink?.mode || "file"}
                onChange={e => setConfig({ ...config, rx_sink: { ...(config.rx_sink||{}), mode: e.target.value } })}
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
                  onChange={e => setConfig({ ...config, rx_sink: { ...(config.rx_sink||{}), path: e.target.value } })}
                  style={{ marginLeft: 8, width: 260 }}
                />
              </label>
            )}
          </div>
        </fieldset>

        <div style={{ display: "flex", gap: "0.5rem", marginTop: 8, flexWrap: "wrap" }}>
          <button type="submit">Save config</button>
          <button type="button" onClick={restartBoth}>Restart TX+RX</button>
          <button type="button" onClick={startTx}>Start TX</button>
          <button type="button" onClick={stopTx}>Stop TX</button>
          <button type="button" onClick={startRx}>Start RX</button>
          <button type="button" onClick={stopRx}>Stop RX</button>
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
                <div style={{display:"flex", alignItems:"center", gap:8}}>
                  <DbMeter db={p.level_db} />
                  <span style={{minWidth: 60, textAlign: "right"}}>{p.level_db!=null ? `${p.level_db.toFixed(1)} dBFS` : "--"}</span>
                </div>
              </td>
              <td>{p.last_seen_sec}</td>
            </tr>
          ))}
          {(!peers || peers.length === 0) && (
            <tr><td colSpan="5" style={{ padding: "8px 0", color: "#666" }}>No talkers detected yet.</td></tr>
          )}
        </tbody>
      </table>

      <p style={{ marginTop: 16, color: "#666" }}>
        Tip: Give each box a unique <b>TX SSRC</b> and <b>TX Name</b>. The receiver maps SSRC → name, and meters show per-talker levels plus the overall mix.
      </p>
    </div>
  );
}
