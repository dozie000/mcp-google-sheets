"""Mobile decision page for the GTD CCORE coach.

Adds a small token-protected web page (and JSON API) to the MCP server so
pending Approve? / Handler decisions in the cleanup approval Sheet can be
answered with one tap each from a phone.

Routes (all require the secret token in the path):
    GET  /d/{token}             the mobile page
    GET  /d/{token}/api/batch   next batch of undecided rows
    POST /d/{token}/api/answer  record decisions (needs DECIDE_WRITE=1)

Environment:
    DECIDE_TOKEN     secret path token. Unset => routes are not registered.
    DECIDE_SHEET_ID  approval spreadsheet id (default: Downloads_Cleanup_Proposal)
    DECIDE_WRITE     "1" to allow writes. Anything else => read-only preview.
"""

import asyncio
import base64
import hmac
import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

logger = logging.getLogger(__name__)

DEFAULT_SHEET_ID = "1TgmcojGtlXWOK2rKZFli4U9glUBwYkw9bje2x9aWf3s"
BATCH_SIZE = 15

# tab name -> (1-based header row, kind of action)
TABS = {
    "Organize Proposal": (1, "move"),
    "Delete Candidates": (2, "delete"),
}

H_APPROVE = "approve? (y/n)"
H_HANDLER = "handler (me/coach)"

# Rows in these categories are never offered for bulk accept.
SENSITIVE_WORDS = ("legal", "insurance", "medical", "job search", "tax",
                   "financ", "immigration", "bank", "identity")

VALID_APPROVE = {"Y", "N"}
VALID_HANDLER = {"Me", "Coach", "n/a"}

_write_lock = threading.Lock()
_service_cache: Dict[str, Any] = {}


# ---------------------------------------------------------------- settings
def _sheet_id() -> str:
    return os.environ.get("DECIDE_SHEET_ID") or DEFAULT_SHEET_ID


def _write_enabled() -> bool:
    return os.environ.get("DECIDE_WRITE", "") == "1"


# ------------------------------------------------------------- Google access
def _sheets_service():
    if "svc" in _service_cache:
        return _service_cache["svc"]
    from google.oauth2 import service_account
    import google.auth
    from googleapiclient.discovery import build

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = None
    cfg = os.environ.get("CREDENTIALS_CONFIG")
    path = os.environ.get("SERVICE_ACCOUNT_PATH", "service_account.json")
    if cfg:
        creds = service_account.Credentials.from_service_account_info(
            json.loads(base64.b64decode(cfg)), scopes=scopes)
    elif path and os.path.exists(path):
        creds = service_account.Credentials.from_service_account_file(path, scopes=scopes)
    else:
        creds, _ = google.auth.default(scopes=scopes)
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
    _service_cache["svc"] = svc
    return svc


def _read_tab(tab: str) -> List[List[str]]:
    """All values of a tab as a list of rows (row 1 is index 0)."""
    res = _sheets_service().spreadsheets().values().get(
        spreadsheetId=_sheet_id(), range=f"'{tab}'!A:I").execute()
    return res.get("values", [])


def _write_cells(updates: List[Dict[str, Any]]) -> None:
    """updates: [{"range": "'Tab'!A5:B5", "values": [["Y","Coach"]]}]"""
    _sheets_service().spreadsheets().values().batchUpdate(
        spreadsheetId=_sheet_id(),
        body={"valueInputOption": "RAW", "data": updates}).execute()


# ------------------------------------------------------------------- logic
def _cell(row: List[str], i: Optional[int]) -> str:
    if i is None or i >= len(row):
        return ""
    return str(row[i]).strip()


def _columns(header: List[str]) -> Dict[str, int]:
    return {str(h).strip().lower(): i for i, h in enumerate(header)}


def _proposal(item: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """What the coach would do. None means: needs a human look."""
    if item["manual_reason"]:
        return None
    return {"approve": "Y", "handler": "Coach"}


def parse_tab(tab: str, values: List[List[str]]) -> List[Dict[str, Any]]:
    header_row, kind = TABS[tab]
    if len(values) < header_row:
        return []
    cols = _columns(values[header_row - 1])
    ia, ih = cols.get(H_APPROVE), cols.get(H_HANDLER)
    if ia is None or ih is None:
        raise ValueError(f"'{tab}' is missing the Approve?/Handler columns")
    ip = cols.get("current path")
    icat, isub = cols.get("category"), cols.get("subfolder")
    idest, isize = cols.get("proposed new path"), cols.get("size")
    imod, inote = cols.get("modified"), cols.get("notes")

    items = []
    for idx in range(header_row, len(values)):
        row = values[idx]
        path = _cell(row, ip)
        if not path:
            continue
        approve, handler = _cell(row, ia), _cell(row, ih)
        if approve and handler:
            continue  # fully decided
        category, sub, notes = _cell(row, icat), _cell(row, isub), _cell(row, inote)
        reason = ""
        blob = f"{category} {sub}".lower()
        if any(w in blob for w in SENSITIVE_WORDS):
            reason = "Sensitive category"
        elif notes.lower().startswith("possible duplicate"):
            reason = "Possible duplicate"
        item = {
            "tab": tab, "row": idx + 1, "kind": kind, "path": path,
            "name": path.rsplit("/", 1)[-1],
            "category": category, "subfolder": sub,
            "dest": _cell(row, idest), "size": _cell(row, isize),
            "modified": _cell(row, imod), "notes": notes,
            "approve": approve, "handler": handler,
            "manual_reason": reason,
        }
        item["proposal"] = _proposal(item)
        items.append(item)
    return items


def get_batch(n: int = BATCH_SIZE) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    for tab in TABS:
        items.extend(parse_tab(tab, _read_tab(tab)))
    return {"items": items[:n], "pending_total": len(items),
            "write_enabled": _write_enabled(),
            "sheet_url": f"https://docs.google.com/spreadsheets/d/{_sheet_id()}/edit"}


def apply_answers(answers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Write decisions. Each answer re-checks the row's path first so a
    stale page can never overwrite the wrong row, and never overwrites a
    decision that is already recorded."""
    results: List[Dict[str, Any]] = []
    by_tab: Dict[str, List[List[str]]] = {}
    updates: List[Dict[str, Any]] = []
    with _write_lock:
        for ans in answers:
            tab = ans.get("tab")
            row = ans.get("row")
            approve = ans.get("approve")
            handler = ans.get("handler")
            rid = {"tab": tab, "row": row}
            if (tab not in TABS or not isinstance(row, int) or row <= TABS[tab][0]
                    or approve not in VALID_APPROVE or handler not in VALID_HANDLER):
                results.append({**rid, "status": "invalid"})
                continue
            if tab not in by_tab:
                by_tab[tab] = _read_tab(tab)
            values = by_tab[tab]
            cols = _columns(values[TABS[tab][0] - 1])
            cur = values[row - 1] if row - 1 < len(values) else []
            if _cell(cur, cols.get("current path")) != ans.get("path"):
                results.append({**rid, "status": "stale"})
                continue
            ia, ih = cols[H_APPROVE], cols[H_HANDLER]
            if _cell(cur, ia) and _cell(cur, ih):
                results.append({**rid, "status": "already_decided"})
                continue
            if ih != ia + 1:
                results.append({**rid, "status": "invalid"})
                continue
            col = lambda i: chr(ord("A") + i)  # columns A..I only
            updates.append({
                "range": f"'{tab}'!{col(ia)}{row}:{col(ih)}{row}",
                "values": [[approve, handler]]})
            results.append({**rid, "status": "ok"})
        if updates:
            _write_cells(updates)
    return results


# --------------------------------------------------------------------- web
_NO_CACHE = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
}


def _authorized(request: Request) -> bool:
    expected = os.environ.get("DECIDE_TOKEN", "")
    given = request.path_params.get("token", "")
    return bool(expected) and hmac.compare_digest(expected, given)


def _deny() -> JSONResponse:
    return JSONResponse({"error": "not found"}, status_code=404, headers=_NO_CACHE)


async def _page(request: Request):
    if not _authorized(request):
        return _deny()
    return HTMLResponse(PAGE_HTML, headers=_NO_CACHE)


async def _batch(request: Request):
    if not _authorized(request):
        return _deny()
    try:
        n = int(request.query_params.get("n", BATCH_SIZE))
    except ValueError:
        n = BATCH_SIZE
    n = max(1, min(n, 50))
    try:
        data = await asyncio.to_thread(get_batch, n)
    except Exception as e:  # surface a readable cause on the page
        logger.exception("decide: batch failed")
        return JSONResponse({"error": _explain(e)}, status_code=502, headers=_NO_CACHE)
    return JSONResponse(data, headers=_NO_CACHE)


async def _answer(request: Request):
    if not _authorized(request):
        return _deny()
    if not _write_enabled():
        return JSONResponse({"error": "read-only preview"}, status_code=403, headers=_NO_CACHE)
    try:
        body = await request.json()
        answers = body["answers"]
        if not isinstance(answers, list) or len(answers) > 100:
            raise ValueError
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400, headers=_NO_CACHE)
    try:
        results = await asyncio.to_thread(apply_answers, answers)
    except Exception as e:
        logger.exception("decide: write failed")
        return JSONResponse({"error": _explain(e)}, status_code=502, headers=_NO_CACHE)
    return JSONResponse({"results": results}, headers=_NO_CACHE)


def _explain(e: Exception) -> str:
    text = str(e)
    if "403" in text or "permission" in text.lower():
        return ("The service account can't open the approval sheet. Share it "
                "with the service account as Editor.")
    return "Could not reach the sheet."


def register(mcp) -> None:
    """Attach the routes to a FastMCP instance (no-op without DECIDE_TOKEN)."""
    if not os.environ.get("DECIDE_TOKEN"):
        logger.info("decide: DECIDE_TOKEN not set; decision page disabled")
        return
    mcp.custom_route("/d/{token}", methods=["GET"])(_page)
    mcp.custom_route("/d/{token}/api/batch", methods=["GET"])(_batch)
    mcp.custom_route("/d/{token}/api/answer", methods=["POST"])(_answer)
    logger.info("decide: page enabled (write=%s)", _write_enabled())


PAGE_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex">
<title>Decisions</title>
<style>
:root{--bg:#f6f6f4;--card:#fff;--ink:#1c1c1a;--mute:#6b6b66;--line:#e2e2dd;
--ok:#1f7a4d;--okbg:#e5f3ec;--no:#b3261e;--nobg:#fbeae8;--warn:#8a5a00;--warnbg:#fdf1d6;--acc:#2f4fd1}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1e1c;--ink:#ecece8;--mute:#9b9b94;--line:#33332f;
--ok:#6fd0a0;--okbg:#17302438;--no:#f2a49d;--nobg:#3a1b19;--warn:#f0c46a;--warnbg:#3a2f12;--acc:#8ea2ff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.4 -apple-system,system-ui,sans-serif;
padding:16px max(16px,env(safe-area-inset-right)) 96px max(16px,env(safe-area-inset-left))}
h1{font-size:20px;margin:4px 0 2px}
.sub{color:var(--mute);font-size:14px;margin-bottom:14px}
.sub a{color:inherit;font-weight:600;text-decoration:underline}
.banner{background:var(--warnbg);color:var(--warn);border-radius:10px;padding:10px 12px;margin-bottom:12px;font-size:14px}
.err{background:var(--nobg);color:var(--no)}
button{font:inherit;border:0;border-radius:12px;min-height:48px;padding:0 14px;cursor:pointer;
background:var(--line);color:var(--ink);touch-action:manipulation}
button:disabled{opacity:.45}
.all{width:100%;background:var(--acc);color:#fff;font-weight:600;min-height:56px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin-bottom:12px}
.name{font-weight:600;word-break:break-word}
.meta{color:var(--mute);font-size:13px;margin-top:2px;word-break:break-word}
.tag{display:inline-block;font-size:12px;border-radius:6px;padding:2px 7px;margin:8px 6px 0 0;background:var(--line)}
.tag.warn{background:var(--warnbg);color:var(--warn)}
.tag.del{background:var(--nobg);color:var(--no)}
.row{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:12px}
.row button.full{grid-column:1/-1}
.yes{background:var(--okbg);color:var(--ok);font-weight:600}
.rej{background:var(--nobg);color:var(--no);font-weight:600}
.done{text-align:center;color:var(--mute);padding:28px 0}
.toast{position:fixed;left:16px;right:16px;bottom:calc(16px + env(safe-area-inset-bottom));background:var(--ink);
color:var(--bg);border-radius:12px;padding:12px 14px;display:none;font-size:14px}
</style></head><body>
<h1 id="title">Decisions</h1>
<div class="sub" id="sub">Loading…</div>
<div id="banner"></div>
<div id="all"></div>
<div id="list"></div>
<div class="toast" id="toast"></div>
<script>
const base = location.pathname.replace(/\/$/, '');
const $ = id => document.getElementById(id);
let state = {items: [], write: false};

function el(tag, cls, text){
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
function toast(msg){
  const t = $('toast'); t.textContent = msg; t.style.display = 'block';
  clearTimeout(toast.h); toast.h = setTimeout(() => t.style.display = 'none', 2200);
}
function banner(msg, err){
  $('banner').replaceChildren();
  if (!msg) return;
  $('banner').appendChild(el('div', 'banner' + (err ? ' err' : ''), msg));
}
async function post(list){
  const r = await fetch(base + '/api/answer', {method: 'POST',
    headers: {'content-type': 'application/json'}, body: JSON.stringify({answers: list})});
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || 'Failed');
  return d.results;
}
function payload(it, approve, handler){
  return {tab: it.tab, row: it.row, path: it.path, approve, handler};
}
async function decide(it, card, approve, handler){
  card.querySelectorAll('button').forEach(b => b.disabled = true);
  try {
    const res = await post([payload(it, approve, handler)]);
    const s = res[0].status;
    if (s === 'ok' || s === 'already_decided') {
      card.remove(); state.items = state.items.filter(x => x !== it);
      toast(s === 'ok' ? 'Saved' : 'Already decided'); afterChange();
    } else {
      toast('Row changed in the sheet — reloading'); setTimeout(load, 800);
    }
  } catch (e) {
    card.querySelectorAll('button').forEach(b => b.disabled = false);
    toast(e.message);
  }
}
function skip(it, card){ card.remove(); state.items = state.items.filter(x => x !== it); afterChange(); }

function card(it){
  const c = el('div', 'card');
  c.appendChild(el('div', 'name', it.name));
  const where = it.kind === 'delete' ? 'Delete' : '→ ' + (it.dest || it.category);
  c.appendChild(el('div', 'meta', where));
  c.appendChild(el('div', 'meta', [it.size, it.modified].filter(Boolean).join(' · ')));
  c.appendChild(el('span', 'tag' + (it.kind === 'delete' ? ' del' : ''), it.kind === 'delete' ? 'Delete' : 'Move'));
  if (it.manual_reason) c.appendChild(el('span', 'tag warn', it.manual_reason + ' — your call'));
  if (it.notes) c.appendChild(el('div', 'meta', it.notes));
  const row = el('div', 'row');
  const mk = (label, cls, a, h, full) => {
    const b = el('button', cls + (full ? ' full' : ''), label);
    b.disabled = !state.write;
    b.addEventListener('click', () => decide(it, c, a, h));
    row.appendChild(b);
  };
  const verb = it.kind === 'delete' ? 'Delete' : 'Approve';
  mk(verb + ' · Coach', 'yes', 'Y', 'Coach');
  mk(verb + ' · Me', 'yes', 'Y', 'Me');
  mk('Reject', 'rej', 'N', 'n/a');
  const s = el('button', '', 'Skip'); s.addEventListener('click', () => skip(it, c)); row.appendChild(s);
  c.appendChild(row);
  return c;
}
function afterChange(){
  const n = state.items.length;
  renderAll();
  if (n === 0) {
    $('list').replaceChildren();
    const d = el('div', 'done', 'Batch done.');
    const b = el('button', '', 'Load next batch'); b.style.marginTop = '12px'; b.onclick = load;
    d.appendChild(el('div')); d.appendChild(b); $('list').appendChild(d);
  }
}
function renderAll(){
  $('all').replaceChildren();
  const ok = state.items.filter(i => i.proposal);
  if (!ok.length) return;
  const b = el('button', 'all', 'Accept all ' + ok.length + ' proposals (Coach handles)');
  b.disabled = !state.write;
  b.addEventListener('click', async () => {
    b.disabled = true;
    try {
      const res = await post(ok.map(i => payload(i, i.proposal.approve, i.proposal.handler)));
      const good = res.filter(r => r.status === 'ok' || r.status === 'already_decided').length;
      toast(good + ' saved' + (good < res.length ? ', ' + (res.length - good) + ' changed' : ''));
      load();
    } catch (e) { b.disabled = false; toast(e.message); }
  });
  $('all').appendChild(b);
}
async function load(){
  $('sub').textContent = 'Loading…';
  function setSub(total, shown, url) {
    const sub = $('sub');
    sub.replaceChildren();
    sub.append(total + ' undecided in ');
    if (url && /^https:\/\/docs\.google\.com\//.test(url)) {
      const a = document.createElement('a');
      a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer';
      a.textContent = 'the sheet';
      sub.append(a);
    } else { sub.append('the sheet'); }
    sub.append(' · showing ' + shown);
  }
  try {
    const r = await fetch(base + '/api/batch');
    const d = await r.json();
    if (!r.ok) { banner(d.error, true); $('sub').textContent = ''; return; }
    state = {items: d.items, write: d.write_enabled, sheetUrl: d.sheet_url};
    banner(d.write_enabled ? '' : 'Read-only preview — buttons are off.', false);
    setSub(d.pending_total, d.items.length, d.sheet_url);
    $('list').replaceChildren(...d.items.map(card));
    renderAll();
    if (!d.items.length) afterChange();
  } catch (e) { banner('Could not load. Check your connection.', true); }
}
load();
</script></body></html>
"""
