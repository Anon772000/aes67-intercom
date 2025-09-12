// frontend/src/api.js
const guessBase = () => {
  // Highest priority: explicit env var
  if (process.env.REACT_APP_API_BASE) return process.env.REACT_APP_API_BASE;

  // If running the CRA dev server on :3000, default to Flask on :8080
  if (window.location.port === '3000') return 'http://localhost:8080';

  // Otherwise (built UI served by Flask), same-origin works best
  return '';
};

export const API_BASE = guessBase();

async function request(path, opts) {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!res.ok) throw new Error(`${opts?.method || 'GET'} ${path} -> ${res.status}`);
  // Some endpoints return no JSON; guard it:
  const ct = res.headers.get('content-type') || '';
  return ct.includes('application/json') ? res.json() : res.text();
}

// GET helpers
export const apiGet = (path) => request(path, { method: 'GET' });

// POST helpers
export const apiPost = (path, body = {}) =>
  request(path, { method: 'POST', body: JSON.stringify(body) });
