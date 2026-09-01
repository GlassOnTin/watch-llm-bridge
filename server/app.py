"""Watch→LLM Bridge server.

Phase 1 (this file): HTTPS transport, bearer auth, echo routing, and the
Trello tools fully wired against the live API.
Phase 2: `route()` is replaced by an LLM that selects among the tools.

Run:
    set -a; source ../.env; set +a     # or export the vars directly
    uvicorn app:app --host 0.0.0.0 --port 8000
"""
import os
import time

import pyotp
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

load_dotenv()  # reads .env from the CWD if present; real env vars win

BRIDGE_TOKEN = os.environ["BRIDGE_TOKEN"]
TRELLO_KEY = os.environ["TRELLO_KEY"]
TRELLO_TOKEN = os.environ["TRELLO_TOKEN"]
PAGES_ORIGIN = os.environ.get("PAGES_ORIGIN", "https://glassontin.github.io")
COMMAND_URL = os.environ.get("COMMAND_URL", "https://bridge.upperpeas.com/command")
# Provision-page second factor. The same secret must be enrolled in Haven
# (create_totp_secret) so codes come from the phone. Empty disables /provision.
TOTP_SECRET = os.environ.get("TOTP_SECRET", "")

TRELLO_API = "https://api.trello.com/1"


class Command(BaseModel):
    text: str


class CardIn(BaseModel):
    board: str
    list: str
    name: str
    desc: str = ""


def resolve_target(boards: dict, lists_by_board: dict, board: str, list_name: str) -> tuple[str, str]:
    """Map (board name, list name) to (board_id, list_id), case-insensitive.

    `lists_by_board[board_id]` is a list of (name, id) pairs, not a dict —
    boards can and do have duplicate list names, and a dict would silently
    collapse them. Fails loudly on ambiguity or mismatch — the caller must
    never guess, because a silently-defaulted list is a card in the wrong place.
    """
    board_hits = [b for b in boards if b.lower() == board.lower()]
    if len(board_hits) != 1:
        raise KeyError(
            f"board '{board}' matches {board_hits or 'nothing'} "
            f"(known: {sorted(boards)})"
        )
    board_id = boards[board_hits[0]]
    pairs = lists_by_board.get(board_id, [])
    list_hits = [lid for n, lid in pairs if n.lower() == list_name.lower()]
    if len(list_hits) != 1:
        detail = f"{len(list_hits)} lists named '{list_name}'" if list_hits else "matches nothing"
        known = sorted({n for n, _ in pairs})
        raise KeyError(f"list '{list_name}' {detail} on '{board_hits[0]}' (known: {known})")
    return board_id, list_hits[0]


class Trello:
    """Minimal Trello client: board/list discovery, read and create cards."""

    def __init__(self, key: str, token: str):
        self.auth = {"key": key, "token": token}
        self.boards: dict[str, str] = {}                     # name -> board id
        self.lists_by_board: dict[str, list[tuple[str, str]]] = {}  # board id -> [(name, id)]

    def _get(self, path: str, **params) -> dict | list:
        r = requests.get(f"{TRELLO_API}{path}", params={**self.auth, **params}, timeout=15)
        r.raise_for_status()
        return r.json()

    def refresh(self) -> None:
        """(Re)load open boards and their lists. Cached for routing and prompts."""
        boards = self._get("/members/me/boards", filter="open", fields="name,id")
        self.boards = {b["name"]: b["id"] for b in boards}
        self.lists_by_board = {}
        for board_id in self.boards.values():
            lists = self._get(f"/boards/{board_id}/lists", fields="name")
            # name/ID pairs, not a dict: duplicate list names must stay visible
            self.lists_by_board[board_id] = [(l["name"], l["id"]) for l in lists]

    def create_card(self, board: str, list_name: str, name: str, desc: str = "") -> dict:
        board_id, list_id = resolve_target(self.boards, self.lists_by_board, board, list_name)
        r = requests.post(
            f"{TRELLO_API}/cards",
            params={**self.auth, "idList": list_id, "name": name, "desc": desc},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

    def cards(self, board: str, list_name: str) -> list[dict]:
        board_id, list_id = resolve_target(self.boards, self.lists_by_board, board, list_name)
        cards = self._get(f"/lists/{list_id}/cards", fields="name,desc,due,url")
        return cards


trello = Trello(TRELLO_KEY, TRELLO_TOKEN)

app = FastAPI(title="watch-llm-bridge")
# CORS is only needed for the endpoint tester on the Pages guide. Lock the
# origin; omit the middleware entirely if you never use the tester.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[PAGES_ORIGIN],
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)


def require_token(authorization: str) -> None:
    if authorization != f"Bearer {BRIDGE_TOKEN}":
        raise HTTPException(status_code=401, detail="bad token")


@app.on_event("startup")
def cache_trello() -> None:
    trello.refresh()


@app.get("/health")
def health() -> dict:
    return {"ok": True, "boards_cached": len(trello.boards)}


@app.post("/command")
def command(cmd: Command, authorization: str = Header(default="")) -> dict:
    """The watch shortcut's single entry point."""
    require_token(authorization)
    return {"reply": route(cmd.text)}


def route(text: str) -> str:
    """Phase 1: echo, so the watch round-trip is verifiable end to end.
    Phase 2 replaces this body with an LLM selecting among:
      trello_create_card / trello_list_cards / gcal_list_events / gcal_create_event
    """
    return f"echo: {text}"


# --- direct tool endpoints: the same functions the LLM will call in Phase 2 ---

@app.get("/boards")
def boards(authorization: str = Header(default="")) -> dict:
    require_token(authorization)
    return {
        name: {
            "id": bid,
            "lists": [{"name": n, "id": lid} for n, lid in trello.lists_by_board.get(bid, [])],
        }
        for name, bid in trello.boards.items()
    }


@app.get("/cards")
def cards(board: str, list: str, authorization: str = Header(default="")) -> list[dict]:
    require_token(authorization)
    try:
        return trello.cards(board, list)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/card")
def card(card_in: CardIn, authorization: str = Header(default="")) -> dict:
    require_token(authorization)
    try:
        created = trello.create_card(card_in.board, card_in.list, card_in.name, card_in.desc)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"id": created["id"], "name": created["name"], "url": created["url"]}


# --- provisioning page: TOTP-gated reveal of the shortcut recipe + token ---

class TotpIn(BaseModel):
    code: str


_attempts: dict[str, list[float]] = {}  # ip -> timestamps; in-memory, single process


def rate_limited(ip: str, max_per_min: int = 6) -> bool:
    now = time.time()
    hits = [t for t in _attempts.get(ip, []) if now - t < 60]
    hits.append(now)
    _attempts[ip] = hits
    return len(hits) > max_per_min


@app.get("/provision", response_class=HTMLResponse)
def provision_page() -> str:
    if not TOTP_SECRET:
        raise HTTPException(status_code=404, detail="provisioning disabled")
    return PROVISION_HTML


@app.post("/provision/verify")
def provision_verify(req: Request, body: TotpIn) -> dict:
    if not TOTP_SECRET:
        raise HTTPException(status_code=404, detail="provisioning disabled")
    if rate_limited(req.client.host):
        raise HTTPException(status_code=429, detail="too many attempts")
    ok = pyotp.TOTP(TOTP_SECRET).verify(body.code.strip().replace(" ", ""), valid_window=1)
    if not ok:
        raise HTTPException(status_code=401, detail="bad code")
    return {"command_url": COMMAND_URL, "token": BRIDGE_TOKEN}

PROVISION_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Watch Bridge — provisioning</title>
<style>
 :root{--bg:#0d1117;--panel:#161b22;--border:#2d333b;--text:#e6edf3;--muted:#8b949e;--accent:#4fb3ff;--green:#3fb950;--code:#0a0d12;--mono:ui-monospace,Menlo,monospace}
 *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,"Segoe UI",Roboto,sans-serif;line-height:1.6}
 main{max-width:640px;margin:0 auto;padding:48px 20px 80px}
 h1{font-size:1.4rem;margin:0 0 4px}.sub{color:var(--muted)}
 .card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:18px;margin:16px 0}
 input{width:100%;background:var(--code);border:1px solid var(--border);color:var(--text);border-radius:6px;padding:10px 12px;font-family:var(--mono);font-size:1.1rem;text-align:center;letter-spacing:0.3em}
 button{background:var(--accent);color:#04121f;border:none;border-radius:6px;padding:10px 22px;font-weight:600;cursor:pointer;margin-top:10px}
 button.sec{background:transparent;border:1px solid var(--border);color:var(--muted);font-family:var(--mono);font-size:.75rem;padding:3px 10px;font-weight:400}
 pre{background:var(--code);border:1px solid var(--border);border-radius:6px;padding:10px 12px;font-family:var(--mono);font-size:.82rem;overflow-x:auto}
 .row{display:flex;justify-content:space-between;align-items:center;gap:8px}
 .ok{color:var(--green)}#err{color:#f85149;font-family:var(--mono);font-size:.85rem}
 .token{font-family:var(--mono);font-size:.8rem;word-break:break-all;background:var(--code);border:1px solid var(--border);border-radius:6px;padding:8px 10px}
 ol{padding-left:20px}li{margin-bottom:8px}
 .head{display:flex;justify-content:space-between;align-items:center;margin:20px 0 6px}
 .head b{font-size:.95rem}
</style></head><body><main>
<h1>Watch Bridge provisioning</h1>
<p class="sub">Enter the 6-digit code from Haven on your phone. The page then shows the shortcut recipe and the bearer token.</p>
<div class="card">
 <input id="code" inputmode="numeric" pattern="[0-9 ]*" maxlength="7" placeholder="000000" autocomplete="one-time-code">
 <button onclick="go()">Unlock</button>
 <span id="err"></span>
</div>
<div id="out"></div>
<script>
function copy(btn, text){navigator.clipboard.writeText(text).then(()=>{const o=btn.textContent;btn.textContent='Copied';setTimeout(()=>btn.textContent=o,1400)})}
async function go(){
 const code=document.getElementById('code').value;
 const err=document.getElementById('err'); err.textContent='';
 const r=await fetch('/provision/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code})});
 if(!r.ok){err.textContent=(r.status===429?'slow down — too many attempts':'bad code');return}
 const d=await r.json();
 const headers='Content-Type: application/json\\nAuthorization: Bearer '+d.token;
 const bodyJson='{"text": "<Dictated Text>"}';
 document.getElementById('out').innerHTML=`
 <div class="head"><b>1 · The shortcut</b><button class="sec" onclick="copy(this,'Dictate Text → Get Contents of URL → Get Dictionary Value (reply) → Speak Text')">Copy step list</button></div>
 <ol>
  <li><b>Dictate Text</b></li>
  <li><b>Get Contents of URL</b> — URL below, Method <b>POST</b>, Headers as below, Request Body → JSON with a field <code>text</code> = the <i>Dictated Text</i> magic variable</li>
  <li><b>Get Dictionary Value</b> — key <code>reply</code></li>
  <li><b>Speak Text</b></li>
 </ol>
 <div class="head"><b>2 · Endpoint URL</b><button class="sec" onclick="copy(this,'${d.command_url}')">Copy</button></div>
 <pre>${d.command_url}</pre>
 <div class="head"><b>3 · Headers</b><button class="sec" onclick="copy(this,headers)">Copy</button></div>
 <pre>${headers.replace(/Bearer (.+)/,'Bearer <span class="ok">$1</span>')}</pre>
 <div class="head"><b>4 · Request body (JSON)</b><button class="sec" onclick="copy(this,bodyJson)">Copy</button></div>
 <pre>${bodyJson}</pre>
 <p class="sub">Then enable <b>Show on Apple Watch</b>; on an Ultra you can assign it to the Action button. Keep this token private — it can create and read cards on your boards.</p>`;
}
document.getElementById('code').addEventListener('keydown',e=>{if(e.key==='Enter')go()});
</script></main></body></html>"""
