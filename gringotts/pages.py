"""Server-rendered HTML shells and fragments (no template engine, htmx-enhanced)."""

import html
from collections.abc import Sequence

from .models import CreditTransaction

_BASE_CSS = """
  * { box-sizing: border-box; }
  body { font-family: system-ui, sans-serif; max-width: 64rem;
         margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }
  h1 { font-size: 1.4rem; } h2 { font-size: 1.05rem; margin-top: 1.5rem; }
  .muted { color: #667; font-size: 0.85rem; }
  .keybar { display: flex; gap: 8px; margin: 1rem 0; }
  .keybar input { flex: 1; padding: 0.45rem; font-family: monospace; }
  button { padding: 0.45rem 0.9rem; cursor: pointer; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
           gap: 12px; margin: 1rem 0; }
  .tile { border: 1px solid #ddd; border-radius: 8px; padding: 12px 14px; }
  .tile .num { font-size: 1.6rem; font-weight: 600; }
  .tile .lbl { color: #667; font-size: 0.78rem; margin-top: 2px; }
  table { width: 100%; border-collapse: collapse; margin: 0.5rem 0; }
  th, td { padding: 6px 8px; border-bottom: 1px solid #eee;
           text-align: left; font-size: 0.88rem; }
  th { color: #667; font-weight: 500; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
  @media (max-width: 50rem) { .grid2 { grid-template-columns: 1fr; } }
  form.inline { display: inline-flex; gap: 6px; align-items: center; }
  form.inline input { width: 5.5rem; padding: 0.25rem; }
  .keyreveal { background: #f6f6f6; border: 1px solid #ddd; border-radius: 6px;
               padding: 10px; font-family: monospace; word-break: break-all; }
  details { margin: 0.75rem 0; }
"""

_KEY_SCRIPT = """
  const STORE = "gringotts_api_key";
  function setKey() {
    const v = document.getElementById("key-input").value.trim();
    if (v) { sessionStorage.setItem(STORE, v); location.reload(); }
  }
  function clearKey() { sessionStorage.removeItem(STORE); location.reload(); }
  document.addEventListener("htmx:configRequest", (e) => {
    const k = sessionStorage.getItem(STORE);
    if (k) e.detail.headers["X-API-Key"] = k;
  });
  document.addEventListener("htmx:responseError", (e) => {
    const s = e.detail.xhr.status;
    if (s === 401) e.detail.target.innerHTML =
      "<p class='muted'>Enter a valid API key above.</p>";
    else if (s === 403) e.detail.target.innerHTML =
      "<p class='muted'>This key does not have admin access.</p>";
  });
"""


def shell(title: str, body: str, mount: str) -> str:
    """Full page: shared CSS, vendored htmx, the key bar, and `body`."""
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<script src="{mount}/static/htmx.min.js"></script>
<style>{_BASE_CSS}</style>
</head>
<body>
<h1>{html.escape(title)}</h1>
<div class="keybar">
  <input id="key-input" type="password" placeholder="Paste your API key (gk_...)">
  <button onclick="setKey()">Use key</button>
  <button onclick="clearKey()">Forget key</button>
</div>
{body}
<script>{_KEY_SCRIPT}</script>
</body>
</html>"""


def tile(value: str, label: str) -> str:
    return (
        f'<div class="tile"><div class="num">{html.escape(value)}</div>'
        f'<div class="lbl">{html.escape(label)}</div></div>'
    )


def usage_table(transactions: Sequence[CreditTransaction]) -> str:
    if not transactions:
        return '<p class="muted">No activity yet.</p>'
    rows = "".join(
        f"<tr><td>{t.created_at:%Y-%m-%d %H:%M}</td>"
        f"<td>{html.escape(t.kind)}</td>"
        f"<td>{html.escape(t.endpoint or '—')}</td>"
        f'<td class="num">{t.amount:+d}</td></tr>'
        for t in transactions
    )
    return (
        "<table><thead><tr><th>When (UTC)</th><th>Kind</th><th>Endpoint</th>"
        '<th class="num">Credits</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )
