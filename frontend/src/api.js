// frontend/src/api.js
const guessBase = () => {
  // Highest priority: explicit env var
  if (process.env.REACT_APP_API_BASE) return process.env.REACT_APP_API_BASE;
  // If running the CRA dev server on :3000, use proxy (same origin)
  if (typeof window !== 'undefined' && window.location.port === '3000') return '';
  // Otherwise (built UI served by Flask), same-origin works best
  return '';
};

export const API_BASE = guessBase();

async function request(path, opts) {
  const init = { cache: 'no-store', ...(opts || {}) };
  // Only set JSON header when we actually send a body
  if (init.body) {
    init.headers = { 'Content-Type': 'application/json', ...(init.headers || {}) };
  }
  const res = await fetch(`${API_BASE}${path}`, init);
  if (!res.ok) {
    let t = '';
    try { t = await res.text(); } catch {}
    throw new Error(`${init.method || 'GET'} ${path} -> ${res.status}${t ? ' ' + t : ''}`);
  }
  // Some endpoints return no JSON; guard it:
  const ct = res.headers.get('content-type') || '';
  return ct.includes('application/json') ? res.json() : res.text();
}

// GET helpers
export const apiGet = (path) => request(path, { method: 'GET' });

// POST helpers
export const apiPost = (path, body = {}) =>
  request(path, { method: 'POST', body: JSON.stringify(body) });
