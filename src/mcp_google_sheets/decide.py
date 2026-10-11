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
    QUEUE_SHEET_ID   spreadsheet holding the shared "Task Queue" tab that any
                     agent can write proposals into (default: GTD CCORE Pipeline)
    DECIDE_AGENT_CAP max cards one agent may fill in a batch before others
                     get a turn (default 5)
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
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse

logger = logging.getLogger(__name__)

DEFAULT_SHEET_ID = "1TgmcojGtlXWOK2rKZFli4U9glUBwYkw9bje2x9aWf3s"
BATCH_SIZE = 15

DEFAULT_QUEUE_SHEET_ID = "18DX6GMnxn2hadduPZevCv36Ecu6y76OzzojwRmZjhCU"
AGENT_CAP = 5
QUEUE_TAB = "Task Queue"

# tab name -> (1-based header row, kind of action)
TABS = {
    "Organize Proposal": (1, "move"),
    "Delete Candidates": (2, "delete"),
    QUEUE_TAB: (1, "task"),
}
CLEANUP_AGENT = "Folder CleanUp"

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


def _queue_sheet_id() -> str:
    return os.environ.get("QUEUE_SHEET_ID") or DEFAULT_QUEUE_SHEET_ID


def _agent_cap() -> int:
    try:
        return max(1, int(os.environ.get("DECIDE_AGENT_CAP", AGENT_CAP)))
    except ValueError:
        return AGENT_CAP


def _sheet_for(tab: str) -> str:
    return _queue_sheet_id() if tab == QUEUE_TAB else _sheet_id()


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
        spreadsheetId=_sheet_for(tab), range=f"'{tab}'!A:Z").execute()
    return res.get("values", [])


def _write_cells(updates: List[Dict[str, Any]]) -> None:
    """updates: [{"tab": "Tab", "range": "'Tab'!A5:B5", "values": [["Y","Coach"]]}]
    Grouped per spreadsheet because the cleanup tabs and the Task Queue live
    in different spreadsheets."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for u in updates:
        groups.setdefault(_sheet_for(u.get("tab", "")), []).append(
            {"range": u["range"], "values": u["values"]})
    for sheet_id, data in groups.items():
        _sheets_service().spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "RAW", "data": data}).execute()


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


def _safe_link(url: str) -> str:
    return url if url.startswith("https://") else ""


def parse_tab(tab: str, values: List[List[str]]) -> List[Dict[str, Any]]:
    header_row, kind = TABS[tab]
    if len(values) < header_row:
        return []
    cols = _columns(values[header_row - 1])
    ia, ih = cols.get(H_APPROVE), cols.get(H_HANDLER)
    if ia is None or ih is None:
        raise ValueError(f"'{tab}' is missing the Approve?/Handler columns")
    ip = cols.get("id" if kind == "task" else "current path")
    icat, isub = cols.get("category"), cols.get("subfolder")
    idest, isize = cols.get("proposed new path"), cols.get("size")
    imod, inote = cols.get("modified"), cols.get("notes")
    iagent, ititle = cols.get("agent"), cols.get("title")
    idetail, ipro = cols.get("detail"), cols.get("proposed handler")
    ineeds, ilink = cols.get("needs you"), cols.get("link")

    items = []
    for idx in range(header_row, len(values)):
        row = values[idx]
        path = _cell(row, ip)
        if not path:
            continue
        approve, handler = _cell(row, ia), _cell(row, ih)
        if approve and handler:
            continue  # fully decided
        if kind == "task":
            title = _cell(row, ititle) or path
            reason = "Needs your judgment" if _cell(row, ineeds).lower() in (
                "y", "yes", "true", "1") else ""
            hint = _cell(row, ipro)
            item = {
                "tab": tab, "row": idx + 1, "kind": kind, "path": path,
                "name": title, "agent": _cell(row, iagent) or "Agent",
                "category": "", "subfolder": "", "dest": "", "size": "",
                "modified": "", "notes": _cell(row, idetail),
                "link": _safe_link(_cell(row, ilink)),
                "approve": approve, "handler": handler, "manual_reason": reason,
            }
            item["proposal"] = (
                {"approve": "Y", "handler": hint}
                if hint in ("Me", "Coach") and not reason else None)
            items.append(item)
            continue
        category, sub, notes = _cell(row, icat), _cell(row, isub), _cell(row, inote)
        reason = ""
        blob = f"{category} {sub}".lower()
        if any(w in blob for w in SENSITIVE_WORDS):
            reason = "Sensitive category"
        elif notes.lower().startswith("possible duplicate"):
            reason = "Possible duplicate"
        item = {
            "tab": tab, "row": idx + 1, "kind": kind, "path": path,
            "name": path.rsplit("/", 1)[-1], "agent": CLEANUP_AGENT,
            "category": category, "subfolder": sub,
            "dest": _cell(row, idest), "size": _cell(row, isize),
            "modified": _cell(row, imod), "notes": notes, "link": "",
            "approve": approve, "handler": handler,
            "manual_reason": reason,
        }
        item["proposal"] = _proposal(item)
        items.append(item)
    return items


def _mix(items: List[Dict[str, Any]], n: int, cap: int) -> List[Dict[str, Any]]:
    """Pick n cards, at most `cap` per agent, round-robin across agents so no
    single agent crowds out the others. If that leaves the batch short (e.g.
    only one agent has work), fill the rest from whoever still has items."""
    by_agent: Dict[str, List[Dict[str, Any]]] = {}
    for it in items:
        by_agent.setdefault(it["agent"], []).append(it)
    picked: List[Dict[str, Any]] = []
    taken: Dict[str, int] = {a: 0 for a in by_agent}
    progress = True
    while len(picked) < n and progress:
        progress = False
        for agent, queue in by_agent.items():
            if len(picked) >= n:
                break
            if queue and taken[agent] < cap:
                picked.append(queue.pop(0))
                taken[agent] += 1
                progress = True
    for agent, queue in by_agent.items():  # second pass: ignore the cap
        while queue and len(picked) < n:
            picked.append(queue.pop(0))
    return picked


def _group(it: Dict[str, Any]) -> str:
    return f"{it['tab']}|{it['category']}|{it['subfolder']}"


def _focus_cleanup(items: List[Dict[str, Any]]) -> (List[Dict[str, Any]], str):
    """Small-batch rule: cleanup cards in one batch all come from a single
    category/subfolder, so a batch is one coherent question ("these 5
    School - BME files") instead of a random slice of 188. Picks the group
    with the most rows an Accept-all could clear, ties by sheet order."""
    cleanup = [i for i in items if i["kind"] != "task"]
    if not cleanup:
        return items, ""
    score: Dict[str, int] = {}
    first: Dict[str, int] = {}
    for pos, it in enumerate(cleanup):
        g = _group(it)
        first.setdefault(g, pos)
        score[g] = score.get(g, 0) + (1 if it["proposal"] else 0)
    focus = max(score, key=lambda g: (score[g], -first[g]))
    keep = [i for i in items if i["kind"] == "task" or _group(i) == focus]
    sample = next(i for i in cleanup if _group(i) == focus)
    label = sample["category"] or "Delete candidates"
    if sample["subfolder"]:
        label += " / " + sample["subfolder"]
    return keep, label


def get_batch(n: int = BATCH_SIZE) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    for tab in TABS:
        try:
            items.extend(parse_tab(tab, _read_tab(tab)))
        except Exception:
            if tab != QUEUE_TAB:
                raise
            logger.warning("decide: Task Queue tab unavailable; skipping", exc_info=True)
    counts: Dict[str, int] = {}
    for it in items:
        counts[it["agent"]] = counts.get(it["agent"], 0) + 1
    batch_items, focus = _focus_cleanup(items)
    return {"items": _mix(batch_items, n, _agent_cap()), "pending_total": len(items),
            "focus": focus,
            "agent_counts": counts, "write_enabled": _write_enabled(),
            "sheet_url": f"https://docs.google.com/spreadsheets/d/{_sheet_id()}/edit",
            "queue_url": f"https://docs.google.com/spreadsheets/d/{_queue_sheet_id()}/edit"}


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
            idcol = "id" if TABS[tab][1] == "task" else "current path"
            if _cell(cur, cols.get(idcol)) != ans.get("path"):
                results.append({**rid, "status": "stale"})
                continue
            ia, ih = cols[H_APPROVE], cols[H_HANDLER]
            if _cell(cur, ia) and _cell(cur, ih):
                results.append({**rid, "status": "already_decided"})
                continue
            if ih != ia + 1 or ih > 25:
                results.append({**rid, "status": "invalid"})
                continue
            col = lambda i: chr(ord("A") + i)  # columns A..Z
            updates.append({
                "tab": tab,
                "range": f"'{tab}'!{col(ia)}{row}:{col(ih)}{row}",
                "values": [[approve, handler]]})
            results.append({**rid, "status": "ok"})
        if updates:
            _write_cells(updates)
    return results


# ------------------------------------------------------------ move script
APPROVED_TABS = ("Organize Proposal", "Delete Candidates")


def approved_actions() -> List[Dict[str, str]]:
    """Rows recorded as Approve=Y and Handler=Coach, as move/delete actions."""
    out: List[Dict[str, str]] = []
    for tab in APPROVED_TABS:
        values = _read_tab(tab)
        header_row, kind = TABS[tab]
        if len(values) < header_row:
            continue
        cols = _columns(values[header_row - 1])
        ia, ih = cols.get(H_APPROVE), cols.get(H_HANDLER)
        ip, idest = cols.get("current path"), cols.get("proposed new path")
        if None in (ia, ih, ip):
            continue
        for row in values[header_row:]:
            if _cell(row, ia) != "Y" or _cell(row, ih) != "Coach":
                continue
            src = _cell(row, ip)
            dest = _cell(row, idest) if kind == "move" else ""
            if kind == "move" and not dest:
                continue
            out.append({"kind": kind, "src": src, "dest": dest})
    return out


def _ps(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def build_script(actions: List[Dict[str, str]]) -> str:
    """PowerShell that applies approved actions safely: dry run unless
    -Apply, never overwrites, deletes only go to a _to_delete folder, and
    every real move is logged with a ready-made undo script."""
    lines = []
    for a in actions:
        parts = [a["src"], a["dest"]]
        if any(p.startswith(("/", "\\")) or ".." in p.replace("\\", "/").split("/")
               or ":" in p for p in parts if p):
            continue  # refuse anything that could leave the Downloads tree
        if not a["src"].startswith("Downloads/"):
            continue
        if a["kind"] == "move" and not a["dest"].startswith("Downloads/"):
            continue
        lines.append(f"    @{{ Kind={_ps(a['kind'])}; Src={_ps(a['src'])}; Dest={_ps(a['dest'])} }}")
    body = ",\n".join(lines)
    return f"""# Approved cleanup moves (generated from your Y + Coach rows).
# Dry run by default: shows what WOULD happen and changes nothing.
#   .\\moves.ps1            -> preview
#   .\\moves.ps1 -Apply     -> do it (writes a log and undo script)
param([string]$Root = $env:USERPROFILE, [switch]$Apply)
$ErrorActionPreference = 'Stop'
$actions = @(
{body}
)
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$logDir = Join-Path $Root 'Downloads\_cleanup_logs'
$trash  = Join-Path $Root 'Downloads\_to_delete'
$log = @(); $undo = @(); $moved = 0; $skipped = 0
foreach ($a in $actions) {{
    $src = Join-Path $Root ($a.Src -replace '/', '\\')
    if ($a.Kind -eq 'delete') {{
        $dst = Join-Path $trash (Split-Path $src -Leaf)
    }} else {{
        $dst = Join-Path $Root ($a.Dest -replace '/', '\\')
    }}
    if (-not (Test-Path -LiteralPath $src)) {{ Write-Host "SKIP (missing)  $src"; $skipped++; continue }}
    if (Test-Path -LiteralPath $dst)        {{ Write-Host "SKIP (exists)   $dst"; $skipped++; continue }}
    Write-Host ("{{0,-7}} {{1}}  ->  {{2}}" -f $a.Kind.ToUpper(), $src, $dst)
    if ($Apply) {{
        New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
        Move-Item -LiteralPath $src -Destination $dst
        $log  += [pscustomobject]@{{ Kind=$a.Kind; From=$src; To=$dst }}
        $undo += "Move-Item -LiteralPath '$($dst -replace "'","''")' -Destination '$($src -replace "'","''")'"
        $moved++
    }}
}}
if ($Apply -and $moved -gt 0) {{
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $log  | Export-Csv -NoTypeInformation (Join-Path $logDir "moves-$stamp.csv")
    $undo | Set-Content (Join-Path $logDir "undo-$stamp.ps1")
    Write-Host "Moved $moved. Skipped $skipped. Undo: $logDir\\undo-$stamp.ps1"
}} elseif (-not $Apply) {{
    Write-Host "Preview only. $($actions.Count) approved, $skipped would be skipped. Re-run with -Apply to do it."
}}
"""


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


async def _script(request: Request):
    if not _authorized(request):
        return _deny()
    try:
        actions = await asyncio.to_thread(approved_actions)
    except Exception as e:
        logger.exception("decide: script failed")
        return JSONResponse({"error": _explain(e)}, status_code=502, headers=_NO_CACHE)
    headers = {**_NO_CACHE, "Content-Disposition": 'attachment; filename="moves.ps1"'}
    return PlainTextResponse(build_script(actions), headers=headers)


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
    mcp.custom_route("/d/{token}/moves.ps1", methods=["GET"])(_script)
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
.agent{font-size:12px;letter-spacing:.04em;text-transform:uppercase;color:var(--acc);margin-bottom:4px;font-weight:600}
a.meta{display:inline-block;margin-top:6px;color:var(--acc)}
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
  c.insertBefore(el('div', 'agent', it.agent || 'Agent'), c.firstChild);
  if (it.kind !== 'task') {
    const where = it.kind === 'delete' ? 'Delete' : '→ ' + (it.dest || it.category);
    c.appendChild(el('div', 'meta', where));
    c.appendChild(el('div', 'meta', [it.size, it.modified].filter(Boolean).join(' · ')));
  }
  c.appendChild(el('span', 'tag' + (it.kind === 'delete' ? ' del' : ''),
    it.kind === 'delete' ? 'Delete' : (it.kind === 'task' ? 'Task' : 'Move')));
  if (it.manual_reason) c.appendChild(el('span', 'tag warn', it.manual_reason + ' — your call'));
  if (it.notes) c.appendChild(el('div', 'meta', it.notes));
  if (it.link && /^https:\/\//.test(it.link)) {
    const a = el('a', 'meta', 'Open details');
    a.href = it.link; a.target = '_blank'; a.rel = 'noopener noreferrer';
    c.appendChild(a);
  }
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
  const b = el('button', 'all', 'Accept all ' + ok.length + ' proposals');
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
  function link(text, url) {
    const a = document.createElement('a');
    a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer';
    a.textContent = text;
    return a;
  }
  const okUrl = u => u && /^https:\/\/docs\.google\.com\//.test(u);
  function setSub(d) {
    const sub = $('sub');
    sub.replaceChildren();
    sub.append(d.pending_total + ' undecided in ');
    if (okUrl(d.sheet_url)) sub.append(link('the sheet', d.sheet_url)); else sub.append('the sheet');
    sub.append(' · showing ' + d.items.length);
    const counts = Object.entries(d.agent_counts || {});
    if (counts.length > 1 || (counts.length === 1 && counts[0][0] !== 'Folder CleanUp')) {
      const line = el('div', 'meta', counts.map(([k, v]) => k + ' ' + v).join(' · '));
      if (okUrl(d.queue_url) && counts.some(([k]) => k !== 'Folder CleanUp')) {
        line.append(' · '); line.append(link('task queue', d.queue_url));
      }
      sub.append(line);
    }
  }
  try {
    const r = await fetch(base + '/api/batch');
    const d = await r.json();
    if (!r.ok) { banner(d.error, true); $('sub').textContent = ''; return; }
    state = {items: d.items, write: d.write_enabled, sheetUrl: d.sheet_url};
    banner(d.write_enabled ? '' : 'Read-only preview — buttons are off.', false);
    setSub(d);
    $('list').replaceChildren(...d.items.map(card));
    renderAll();
    if (!d.items.length) afterChange();
  } catch (e) { banner('Could not load. Check your connection.', true); }
}
load();
</script></body></html>
"""
