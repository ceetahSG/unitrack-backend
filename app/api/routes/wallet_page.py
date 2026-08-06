"""A minimal student wallet, served by the API itself.

The full student PWA (offline wallet, live map, IndexedDB) belongs in
`unitrack-web` and is not built. This is the smallest thing that makes a ticket
usable: sign in, see your tickets, show a boarding code that rotates.

It exists for two reasons, and it earns its keep on both.

1. **Nothing else can render a QR.** The backend issues codes and the helper
   app scans them, but until now there was no way to put one in front of a
   camera — so the scanner could never be tested on real hardware, and the
   whole boarding path was unproven end to end.
2. **A student with no app still has to board.** A phone browser is the lowest
   common denominator, and it works today.

Deliberately server-rendered with no build step and no framework. Anything more
would duplicate work that belongs in the PWA, and this page should stay small
enough to delete without regret when that lands.
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["wallet"])

# The QR refreshes every 10s against a 30s slice with ±1 slice tolerance, so a
# displayed code is never close to expiring while someone queues to scan it.
_REFRESH_MS = 10_000

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>UniTrack — My tickets</title>
<style>
  :root { color-scheme: light dark; --bg:#f4f6fb; --card:#fff; --ink:#1f2937;
          --muted:#6b7280; --brand:#1a3c8f; --line:#e4e8f0; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#0f172a; --card:#1e293b; --ink:#e8ecf4; --muted:#94a3b8;
            --brand:#4f7fe0; --line:#334155; }
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 Inter,
         system-ui, -apple-system, "Segoe UI", sans-serif; }
  header { background:var(--brand); color:#fff; padding:14px 18px; font-weight:700; }
  main { padding:18px; max-width:520px; margin:0 auto; }
  .card { background:var(--card); border-radius:12px; padding:18px; margin-bottom:14px;
          box-shadow:0 1px 2px rgba(16,24,40,.06), 0 4px 12px rgba(16,24,40,.04); }
  .muted { color:var(--muted); font-size:13px; }
  label { display:block; font-size:13px; font-weight:600; color:var(--muted); margin:10px 0 4px; }
  input, button { font:inherit; width:100%; padding:11px 12px; border-radius:8px;
                  border:1px solid var(--line); background:var(--card); color:var(--ink); }
  button { background:var(--brand); color:#fff; border:0; font-weight:700; margin-top:14px; }
  button:disabled { opacity:.55; }
  /* The QR must survive being read off a dim phone in a moving bus, so it is
     always on white regardless of theme, and as large as the screen allows. */
  .qr { background:#fff; border-radius:12px; padding:14px; display:grid; place-items:center; }
  .qr img { width:100%; max-width:320px; height:auto; image-rendering:pixelated; }
  .row { display:flex; justify-content:space-between; gap:12px; align-items:baseline; }
  .rides { font-size:26px; font-weight:800; letter-spacing:-.02em; }
  .err { color:#b91c1c; font-size:14px; margin-top:10px; }
  .back { background:transparent; color:var(--muted); border:1px solid var(--line); }
</style>
</head>
<body>
<header>UniTrack</header>
<main id="app"></main>

<script>
const app = document.getElementById('app');
let token = sessionStorage.getItem('ut_token');
let timer = null;

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function stopRefresh() { if (timer) { clearInterval(timer); timer = null; } }

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { 'content-type': 'application/json', ...(opts.headers || {}),
               ...(token ? { authorization: 'Bearer ' + token } : {}) },
  });
  if (res.status === 401) {
    token = null;
    sessionStorage.removeItem('ut_token');
    login();
    return null;
  }
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.status);
  return res.json();
}

function login(message) {
  stopRefresh();
  app.innerHTML = `<div class="card">
    <h2 style="margin:0 0 4px">Sign in</h2>
    <p class="muted" style="margin:0">Use your student account.</p>
    <label for="e">Email</label><input id="e" type="email" autocomplete="username">
    <label for="p">Password</label><input id="p" type="password" autocomplete="current-password">
    <button id="go">Sign in</button>
    ${message ? `<p class="err">${esc(message)}</p>` : ''}
  </div>`;
  const go = document.getElementById('go');
  go.onclick = async () => {
    go.disabled = true;
    try {
      const r = await fetch('/auth/login', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ email: e.value, password: p.value }),
      });
      if (!r.ok) throw new Error('Incorrect email or password');
      // sessionStorage, not localStorage: the token dies with the tab. This
      // page is a stopgap, and a bearer token left on a shared phone is worse
      // than making someone sign in again.
      token = (await r.json()).access_token;
      sessionStorage.setItem('ut_token', token);
      wallet();
    } catch (err) { go.disabled = false; login(err.message); }
  };
}

async function wallet() {
  stopRefresh();
  app.innerHTML = '<p class="muted">Loading…</p>';
  const tickets = await api('/shop/tickets');
  if (!tickets) return;

  const active = tickets.filter(t => t.status === 'active');
  if (!active.length) {
    app.innerHTML = `<div class="card"><h2 style="margin:0 0 6px">No active tickets</h2>
      <p class="muted" style="margin:0">Buy one to start riding.</p></div>`;
    return;
  }

  app.innerHTML = active.map(t => `
    <div class="card">
      <div class="row">
        <div>
          <strong>${t.rides_total === null ? 'Unlimited pass' : 'Ride ticket'}</strong>
          <div class="muted">Valid to ${esc(t.valid_to.slice(0, 10))}</div>
        </div>
        <div class="rides">${t.rides_remaining === null ? '∞' : esc(t.rides_remaining)}</div>
      </div>
      <button data-id="${esc(t.id)}">Show boarding code</button>
    </div>`).join('');

  for (const b of app.querySelectorAll('button[data-id]')) {
    b.onclick = () => show(b.dataset.id);
  }
}

function show(id) {
  stopRefresh();
  app.innerHTML = `<div class="card">
      <p class="muted" style="margin:0 0 10px">Show this to the helper.
        It refreshes automatically.</p>
      <div class="qr"><img id="qr" alt="Boarding code"></div>
    </div>
    <button class="back" id="back">Back to tickets</button>`;
  document.getElementById('back').onclick = wallet;

  const img = document.getElementById('qr');
  // Cache-buster: the browser would otherwise reuse the previous image and the
  // student would present a code from an expired slice.
  const draw = () => { img.src = `/shop/tickets/${id}/qr.png?t=${Date.now()}`; };
  draw();
  timer = setInterval(draw, REFRESH_MS);
}

const REFRESH_MS = __REFRESH_MS__;
token ? wallet().catch(() => login()) : login();
</script>
</body>
</html>
"""


@router.get("/wallet", response_class=HTMLResponse, include_in_schema=False)
async def wallet_page() -> HTMLResponse:
    """The page itself. Public — it authenticates from inside, like any SPA."""
    return HTMLResponse(_PAGE.replace("__REFRESH_MS__", str(_REFRESH_MS)))
