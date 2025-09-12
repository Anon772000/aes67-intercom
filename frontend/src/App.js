import React, { useState, useEffect } from 'react';

function App() {
  const [status, setStatus] = useState(null);
  const [peers, setPeers] = useState([]);

  useEffect(() => {
    const interval = setInterval(() => {
      fetch('/status').then(r=>r.json()).then(setStatus);
      fetch('/rx/peers').then(r=>r.json()).then(d=>setPeers(d.peers||[]));
    }, 1000);
    return () => clearInterval(interval);
  }, []);

  return (
    <div style={{padding:"2rem", fontFamily:"sans-serif"}}>
      <h1>AES67 Intercom</h1>
      {status && (
        <div>
          <p>TX Running: {status.tx_running ? "Yes" : "No"}</p>
          <p>RX Running: {status.rx_running ? "Yes" : "No"}</p>
        </div>
      )}
      <h2>Active Peers</h2>
      <ul>
        {peers.map((p,i)=>(<li key={i}>{p.name} - {p.packets} packets</li>))}
      </ul>
    </div>
  );
}
export default App;
