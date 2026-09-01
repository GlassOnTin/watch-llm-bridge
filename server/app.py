"""Watch→LLM Bridge server.

Phase 1: HTTPS transport, bearer auth, provisioning, and the Trello tools
fully wired against the live API.
Phase 2 (this file): `route()` sends the spoken sentence to a Groq LLM that
picks among the same tools the direct endpoints use; the server stays the
fail-loud authority on where cards land (resolve_target).

Run:
    set -a; source ../.env; set +a     # or export the vars directly
    uvicorn app:app --host 0.0.0.0 --port 8000
"""
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone

import pyotp
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

import store

load_dotenv()  # reads .env from the CWD if present; real env vars win

BRIDGE_TOKEN = os.environ["BRIDGE_TOKEN"]
TRELLO_KEY = os.environ["TRELLO_KEY"]
TRELLO_TOKEN = os.environ["TRELLO_TOKEN"]
PAGES_ORIGIN = os.environ.get("PAGES_ORIGIN", "https://glassontin.github.io")
COMMAND_URL = os.environ.get("COMMAND_URL", "https://bridge.upperpeas.com/command")
# Provision-page second factor. The same secret must be enrolled in Haven
# (create_totp_secret) so codes come from the phone. Empty disables /provision.
TOTP_SECRET = os.environ.get("TOTP_SECRET", "")
# Multi-user: INVITE_CODE gates /signup (empty = signup disabled). SESSION_SECRET
# signs dashboard cookies — an ephemeral random value means sessions die on restart.
INVITE_CODE = os.environ.get("INVITE_CODE", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "") or secrets.token_hex(32)
OWNER_USERNAME = os.environ.get("OWNER_USERNAME", "ian")
OWNER_PASSWORD = os.environ.get("OWNER_PASSWORD", "")

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
            f"(known: {', '.join(sorted(boards))})"
        )
    board_id = boards[board_hits[0]]
    pairs = lists_by_board.get(board_id, [])
    list_hits = [lid for n, lid in pairs if n.lower() == list_name.lower()]
    if len(list_hits) != 1:
        detail = f"{len(list_hits)} lists named '{list_name}'" if list_hits else "matches nothing"
        known = ", ".join(sorted({n for n, _ in pairs}))
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

# Per-user Trello clients. The owner's client is the `trello` singleton above,
# seeded into this cache at startup — tests and the existing endpoints patch the
# singleton's attributes, so its object identity must not change.
_clients: dict[int, Trello] = {}


def trello_for(user: dict) -> Trello | None:
    """The user's cached Trello client, or None while they haven't connected.
    First use after a restart refreshes boards/lists from the API."""
    token = user.get("trello_token")
    if not token:
        return None
    client = _clients.get(user["id"])
    if client is None:
        client = Trello(TRELLO_KEY, token)
        client.refresh()
        _clients[user["id"]] = client
    return client


def require_user(authorization: str) -> dict:
    """Resolve the Authorization header to a user row; 401 on anything else."""
    token = authorization.removeprefix("Bearer ").strip()
    user = store.get_user_by_token(token) if token else None
    if not user:
        raise HTTPException(status_code=401, detail="bad token")
    return user


# --- dashboard sessions: HMAC-signed cookie, stdlib only ---

SESSION_COOKIE = "bridge_session"
SESSION_TTL = 30 * 24 * 3600


def _sign(payload: str) -> str:
    return hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_session(user_id: int) -> str:
    payload = f"{user_id}.{int(time.time()) + SESSION_TTL}"
    return f"{payload}.{_sign(payload)}"


def session_user(request: Request) -> dict | None:
    raw = request.cookies.get(SESSION_COOKIE, "")
    payload, _, sig = raw.rpartition(".")
    if not payload or not hmac.compare_digest(sig, _sign(payload)):
        return None
    user_id, _, exp = payload.partition(".")
    try:
        if int(exp) < time.time():
            return None
        return store.get_user(int(user_id))
    except ValueError:
        return None


def seed_owner() -> None:
    """First boot: turn the single-tenant .env credentials into the owner's
    account so the existing BRIDGE_TOKEN watch shortcut keeps working."""
    if store.count_users():
        return
    password = OWNER_PASSWORD or secrets.token_urlsafe(12)
    owner = store.create_user(OWNER_USERNAME, password,
                              api_token=BRIDGE_TOKEN, trello_token=TRELLO_TOKEN)
    _clients[owner["id"]] = trello
    if not OWNER_PASSWORD:
        logging.getLogger("uvicorn.error").warning(
            "Seeded owner '%s' with a random dashboard password (shown once): %s",
            OWNER_USERNAME, password)

app = FastAPI(title="watch-llm-bridge")
# CORS is only needed for the endpoint tester on the Pages guide. Lock the
# origin; omit the middleware entirely if you never use the tester.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[PAGES_ORIGIN],
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.on_event("startup")
def cache_trello() -> None:
    store.connect()
    seed_owner()
    try:
        trello.refresh()
    except requests.RequestException as e:
        # A Trello outage at boot shouldn't take the whole bridge down.
        logging.getLogger("uvicorn.error").warning("Trello refresh at boot failed: %s", e)


@app.get("/health")
def health() -> dict:
    return {"ok": True, "boards_cached": len(trello.boards)}


@app.post("/command")
def command(cmd: Command, authorization: str = Header(default="")) -> dict:
    """The watch shortcut's single entry point."""
    user = require_user(authorization)
    try:
        t = trello_for(user)
    except requests.RequestException as e:
        logging.getLogger("uvicorn.error").warning("Trello refresh for %s failed: %s",
                                                   user["username"], e)
        return {"reply": "Trello is unreachable right now."}
    if t is None:
        return {"reply": "Your Trello isn't connected yet. Open the bridge dashboard to connect it."}
    return {"reply": route(cmd.text, t)}


def route(text: str) -> str:
    """Phase 1: echo, so the watch round-trip is verifiable end to end.
    Phase 2 replaces this body with an LLM selecting among:
      trello_create_card / trello_list_cards / gcal_list_events / gcal_create_event
    """
    return f"echo: {text}"


# --- LLM routing (Phase 2): any OpenAI-compatible chat endpoint with tools ---
# Configured in .env: LLM_BASE_URL, LLM_API_KEY, LLM_MODEL.

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "")

# Spoken aliases for board names dictation cannot produce verbatim.
# Extend or override with VOICE_ALIASES=<json object> in .env.
DEFAULT_ALIASES = {
    "move board": "Move To A Nicer Spot",
    "nicer spot": "Move To A Nicer Spot",
    "best of": "The Best of (Ian...Jenni)",
    "flower adventure": "The Flower Tattoo Adventure",
}


def spoken_aliases() -> dict[str, str]:
    raw = os.environ.get("VOICE_ALIASES", "")
    if raw:
        try:
            extra = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"VOICE_ALIASES is not valid JSON: {e}") from e
        return {**DEFAULT_ALIASES, **extra}
    return dict(DEFAULT_ALIASES)


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "trello_create_card",
            "description": "Create a card in a named list. Omit board when unsure: "
                           "the server finds the board if the list name is unique.",
            "parameters": {
                "type": "object",
                "properties": {
                    "board": {"type": "string"},
                    "list": {"type": "string"},
                    "name": {"type": "string"},
                    "desc": {"type": "string"},
                },
                "required": ["list", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "trello_list_cards",
            "description": "Read the cards in a list. Omit board when unsure: "
                           "the server finds the board if the list name is unique.",
            "parameters": {
                "type": "object",
                "properties": {
                    "board": {"type": "string"},
                    "list": {"type": "string"},
                },
                "required": ["list"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "trello_list_boards",
            "description": "Enumerate every board and its lists.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def build_system_prompt(t: Trello | None = None) -> str:
    t = t or trello
    inventory = "\n".join(
        f"- board '{board}': " + " | ".join(n for n, _ in t.lists_by_board.get(bid, []))
        for board, bid in t.boards.items()
    )
    aliases = ", ".join(f'"{k}" means board "{v}"' for k, v in spoken_aliases().items())
    today = datetime.now(timezone.utc).strftime("%A %d %B %Y")
    return f"""You are the brain of a voice assistant driven from an Apple Watch. Each
user message is one spoken sentence. Your reply is read aloud, so keep it to
one or two short sentences of plain words; never mention IDs, JSON, or URLs.

Today is {today} (UTC).

Tools:
- trello_create_card(board, list, name, desc) — add a card to a list
- trello_list_cards(board, list) — read the cards in a list
- trello_list_boards() — enumerate boards and their lists

Exact inventory of boards and lists:

{inventory}

Rules:
1. Match the spoken words to this inventory case-insensitively and fuzzily.
   Ignore emoji and punctuation in list names, and treat near-misses ("huge"
   for "Hugge") as matches. NEVER invent a board or list name that is not in
   the inventory, and never guess between two candidates: ask instead.
2. Pass the board only when the user named one. If you are unsure, omit it:
   the server finds the board when the list name is unique, and answers
   ambiguous_board when several boards have that list. On ambiguous_board,
   ask which board in one short question that models the retry, e.g. "Food
   is on Home and Plans — say: add it to Food on plans". Never invent a
   board name.
3. Spoken aliases map phrases to real board names: {aliases}. Apply them
   before matching.
4. Fill tool arguments only from what the user said. Do not add a note
   (desc) unless asked.
5. After a successful tool call, confirm in one short sentence what was
   done, or read the items out as a compact spoken list.
6. Google Calendar is not connected yet. Any calendar or events request gets
   a brief "calendar isn't set up yet" reply.
7. If the request matches no tool, answer helpfully in a sentence or two."""


def find_boards_with_list(list_name: str, t: Trello | None = None) -> list[str]:
    """Board names containing a list with this name (case-insensitive)."""
    t = t or trello
    want = list_name.lower()
    return [
        board
        for board, bid in t.boards.items()
        if any(n.lower() == want for n, _ in t.lists_by_board.get(bid, []))
    ]


def resolve_board(args: dict, list_name: str, t: Trello | None = None) -> dict:
    """Pick the board server-side. A board the LLM named is honoured; an
    omitted board resolves only if the list name is unique. Ambiguity comes
    back as an ask-the-user result, never a guess."""
    board = args.get("board") or ""
    if board:
        return {"ok": True, "board": board}
    hits = find_boards_with_list(list_name, t)
    if len(hits) == 1:
        return {"ok": True, "board": hits[0]}
    return {"ok": False, "error": "ambiguous_board", "list": list_name, "boards": hits}


def execute_tool(name: str, args: dict, t: Trello | None = None) -> dict:
    """Dispatch one tool call. KeyError from resolve_target = mismatch."""
    t = t or trello
    if name == "trello_create_card":
        picked = resolve_board(args, args["list"], t)
        if not picked["ok"]:
            return picked
        board = picked["board"]
        card = t.create_card(board, args["list"], args["name"], args.get("desc", ""))
        return {"ok": True, "created": card["name"], "list": args["list"], "board": board}
    if name == "trello_list_cards":
        picked = resolve_board(args, args["list"], t)
        if not picked["ok"]:
            return picked
        board = picked["board"]
        cards = t.cards(board, args["list"])
        return {"ok": True, "list": args["list"], "board": board, "cards": [c["name"] for c in cards]}
    if name == "trello_list_boards":
        return {
            board: [n for n, _ in t.lists_by_board.get(bid, [])]
            for board, bid in t.boards.items()
        }
    raise NotImplementedError(f"unknown tool '{name}'")


def spoken_error(message: str, max_candidates: int = 5) -> str:
    """Turn a resolve_target KeyError into a short spoken retry hint."""
    base, _, known = message.partition("(known:")
    candidates = [c.strip(" '\"") for c in known.rstrip(") ").split(",")]
    candidates = [c for c in candidates if c]
    hint = ""
    if candidates:
        shown = ", ".join(candidates[:max_candidates])
        more = " and more" if len(candidates) > max_candidates else ""
        hint = f" Options include: {shown}{more}."
    return f"I couldn't match that. {base.strip()}.{hint} Say it again and name the board."


def route(text: str, t: Trello | None = None) -> str:
    """Send the sentence to the LLM; the LLM picks tools, the server keeps
    resolve_target as the fail-loud authority on where cards go. Echoes only
    while no LLM backend is configured (Phase 1 fallback)."""
    t = t or trello
    if not (LLM_BASE_URL and LLM_API_KEY and LLM_MODEL):
        return f"echo: {text}"
    messages = [
        {"role": "system", "content": build_system_prompt(t)},
        {"role": "user", "content": text},
    ]
    for _ in range(3):
        try:
            r = requests.post(
                f"{LLM_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                json={
                    "model": LLM_MODEL,
                    "messages": messages,
                    "tools": TOOLS,
                    "tool_choice": "auto",
                    "temperature": 0.1,
                    "max_tokens": 300,
                },
                timeout=25,
            )
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
        except requests.RequestException as e:
            logging.getLogger("uvicorn.error").warning("LLM call failed: %s", e)
            return "The assistant brain is unreachable right now."
        calls = msg.get("tool_calls")
        if not calls:
            return msg.get("content") or "Done."
        messages.append(msg)
        for call in calls:
            args = json.loads(call["function"]["arguments"] or "{}")
            try:
                result = execute_tool(call["function"]["name"], args, t)
            except KeyError as e:
                return spoken_error(e.args[0])
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": json.dumps(result),
            })
    return "Done."


# --- direct tool endpoints: the same functions the LLM will call in Phase 2 ---

def _client_or_409(authorization: str) -> tuple[dict, Trello]:
    user = require_user(authorization)
    try:
        t = trello_for(user)
    except requests.RequestException as e:
        logging.getLogger("uvicorn.error").warning("Trello refresh for %s failed: %s",
                                                   user["username"], e)
        raise HTTPException(status_code=502, detail="trello unreachable")
    if t is None:
        raise HTTPException(status_code=409, detail="trello not connected for this user")
    return user, t


@app.get("/boards")
def boards(authorization: str = Header(default="")) -> dict:
    _, t = _client_or_409(authorization)
    return {
        name: {
            "id": bid,
            "lists": [{"name": n, "id": lid} for n, lid in t.lists_by_board.get(bid, [])],
        }
        for name, bid in t.boards.items()
    }


@app.get("/cards")
def cards(board: str, list: str, authorization: str = Header(default="")) -> list[dict]:
    _, t = _client_or_409(authorization)
    try:
        return t.cards(board, list)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/card")
def card(card_in: CardIn, authorization: str = Header(default="")) -> dict:
    _, t = _client_or_409(authorization)
    try:
        created = t.create_card(card_in.board, card_in.list, card_in.name, card_in.desc)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"id": created["id"], "name": created["name"], "url": created["url"]}


# --- multi-user web front end: signup, login, dashboard, Trello onboarding ---

import sqlite3

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")


class SignupIn(BaseModel):
    username: str
    password: str
    invite: str = ""


class LoginIn(BaseModel):
    username: str
    password: str


class TrelloTokenIn(BaseModel):
    token: str


class PasswordIn(BaseModel):
    current: str
    new: str


def require_session(request: Request) -> dict:
    user = session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="not signed in")
    return user


def app_state(user: dict) -> dict:
    """Everything the dashboard renders, in one fetch."""
    try:
        t = trello_for(user)
    except requests.RequestException:
        t = None
    return {
        "username": user["username"],
        "connected": t is not None and bool(t.boards),
        "boards": {} if t is None else {
            name: [n for n, _ in t.lists_by_board.get(bid, [])]
            for name, bid in t.boards.items()
        },
        "command_url": COMMAND_URL,
        "token": user["api_token"],
        "authorize_url": (
            "https://trello.com/1/authorize?expiration=30days&name=Watch+Bridge"
            f"&scope=read,write&response_type=token&key={TRELLO_KEY}"
        ),
    }


@app.get("/")
def landing(request: Request):
    if session_user(request):
        return RedirectResponse("/app", status_code=303)
    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page() -> str:
    return LOGIN_HTML


@app.get("/signup", response_class=HTMLResponse)
def signup_page() -> str:
    return SIGNUP_HTML


@app.post("/auth/signup")
def auth_signup(request: Request, body: SignupIn, response: Response) -> dict:
    if not INVITE_CODE:
        raise HTTPException(status_code=403, detail="signup is disabled on this server")
    if rate_limited(request.client.host):
        raise HTTPException(status_code=429, detail="too many attempts")
    if not hmac.compare_digest(body.invite.strip(), INVITE_CODE):
        raise HTTPException(status_code=403, detail="bad invite code")
    username = body.username.strip().lower()
    if not USERNAME_RE.match(username):
        raise HTTPException(status_code=400,
                            detail="username is 2-32 characters: a-z, 0-9, '-' or '_'")
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="password must be at least 8 characters")
    try:
        user = store.create_user(username, body.password)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="that username is taken")
    response.set_cookie(SESSION_COOKIE, make_session(user["id"]),
                        httponly=True, samesite="lax", max_age=SESSION_TTL)
    return {"ok": True}


@app.post("/auth/login")
def auth_login(request: Request, body: LoginIn, response: Response) -> dict:
    if rate_limited(request.client.host):
        raise HTTPException(status_code=429, detail="too many attempts")
    user = store.get_user_by_name(body.username.strip().lower())
    if not user or not store.verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="wrong username or password")
    response.set_cookie(SESSION_COOKIE, make_session(user["id"]),
                        httponly=True, samesite="lax", max_age=SESSION_TTL)
    return {"ok": True}


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/app", response_class=HTMLResponse)
def dashboard_page(request: Request):
    if not session_user(request):
        return RedirectResponse("/login", status_code=303)
    return DASHBOARD_HTML


@app.get("/app/state")
def dashboard_state(request: Request) -> dict:
    return app_state(require_session(request))


@app.post("/app/trello")
def connect_trello(request: Request, body: TrelloTokenIn) -> dict:
    """Verify the pasted token against Trello, store it, refresh boards."""
    user = require_session(request)
    token = body.token.strip()
    if not token:
        raise HTTPException(status_code=400, detail="paste the token from Trello first")
    try:
        probe = requests.get(f"{TRELLO_API}/members/me",
                             params={"key": TRELLO_KEY, "token": token}, timeout=15)
        probe.raise_for_status()
    except requests.RequestException:
        raise HTTPException(status_code=401,
                            detail="Trello rejected that token — copy it again from the authorize page")
    store.set_trello_token(user["id"], token)
    _clients.pop(user["id"], None)
    return app_state(store.get_user(user["id"]))


@app.post("/app/password")
def change_password(request: Request, body: PasswordIn) -> dict:
    user = require_session(request)
    if not store.verify_password(body.current, user["password_hash"]):
        raise HTTPException(status_code=401, detail="wrong current password")
    if len(body.new) < 8:
        raise HTTPException(status_code=400, detail="new password must be at least 8 characters")
    store.set_password(user["id"], body.new)
    return {"ok": True}


STYLE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Watch Bridge</title>
<style>
 :root{--bg:#0d1117;--panel:#161b22;--border:#2d333b;--text:#e6edf3;--muted:#8b949e;--accent:#4fb3ff;--green:#3fb950;--code:#0a0d12;--mono:ui-monospace,Menlo,monospace}
 *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,"Segoe UI",Roboto,sans-serif;line-height:1.6}
 main{max-width:640px;margin:0 auto;padding:48px 20px 80px}
 h1{font-size:1.4rem;margin:0 0 4px}.sub{color:var(--muted);font-size:.9rem}
 .card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:18px;margin:16px 0}
 input{width:100%;background:var(--code);border:1px solid var(--border);color:var(--text);border-radius:6px;padding:10px 12px;font-family:var(--mono);font-size:.95rem;margin:6px 0}
 button{background:var(--accent);color:#04121f;border:none;border-radius:6px;padding:10px 22px;font-weight:600;cursor:pointer;margin-top:10px}
 button.sec{background:transparent;border:1px solid var(--border);color:var(--muted);font-family:var(--mono);font-size:.75rem;padding:3px 10px;font-weight:400;margin:0}
 a.btn{display:inline-block;background:var(--accent);color:#04121f;border-radius:6px;padding:10px 22px;font-weight:600;text-decoration:none;margin-top:10px}
 pre{background:var(--code);border:1px solid var(--border);border-radius:6px;padding:10px 12px;font-family:var(--mono);font-size:.82rem;overflow-x:auto;word-break:break-all}
 .row{display:flex;justify-content:space-between;align-items:center;gap:8px;margin:14px 0 6px}
 .row b{font-size:.95rem}
 .ok{color:var(--green)}#err{color:#f85149;font-size:.85rem;min-height:1.2em}
 ol{padding-left:20px}li{margin-bottom:8px}
 .token{font-family:var(--mono);font-size:.8rem;word-break:break-all;background:var(--code);border:1px solid var(--border);border-radius:6px;padding:8px 10px}
 .top{display:flex;justify-content:space-between;align-items:baseline}
 .top a{color:var(--muted);font-size:.85rem}
 details summary{cursor:pointer;color:var(--muted);font-size:.85rem}
 ul{margin:6px 0;padding-left:20px}
</style></head><body><main>
"""

LANDING_HTML = STYLE + """<h1>Watch Bridge</h1>
<p class="sub">Speak a command on your Apple Watch; it lands on your Trello boards.</p>
<div class="card">
 <p><a class="btn" href="/login">Log in</a> <a class="btn" href="/signup">Sign up</a></p>
 <p class="sub">Signing up needs an invite code from the operator. You then connect
 your own Trello account and get a personal token for your watch shortcut.</p>
</div>
</main></body></html>"""

SIGNUP_HTML = STYLE + """<h1>Sign up</h1>
<p class="sub">Accounts are invite-only. Ask the operator for the code.</p>
<div class="card">
 <input id="invite" placeholder="Invite code" autocomplete="off">
 <input id="username" placeholder="Username (a-z, 0-9, -)" autocomplete="username">
 <input id="password" type="password" placeholder="Password (8+ characters)" autocomplete="new-password">
 <button onclick="go()">Create account</button>
 <span id="err"></span>
</div>
<p class="sub">Already have one? <a href="/login" style="color:var(--accent)">Log in</a></p>
<script>
async function go(){
 const err=document.getElementById('err'); err.textContent='';
 const r=await fetch('/auth/signup',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({invite:invite.value,username:username.value,password:password.value})});
 if(r.ok){location.href='/app';return}
 const d=await r.json().catch(()=>({detail:'error '+r.status}));
 err.textContent=d.detail||('error '+r.status);
}
document.querySelectorAll('input').forEach(i=>i.addEventListener('keydown',e=>{if(e.key==='Enter')go()}));
</script></main></body></html>"""

LOGIN_HTML = STYLE + """<h1>Watch Bridge</h1>
<p class="sub">Log in to manage your watch shortcut and Trello connection.</p>
<div class="card">
 <input id="username" placeholder="Username" autocomplete="username">
 <input id="password" type="password" placeholder="Password" autocomplete="current-password">
 <button onclick="go()">Log in</button>
 <span id="err"></span>
</div>
<p class="sub">No account? <a href="/signup" style="color:var(--accent)">Sign up</a> (invite code needed).</p>
<script>
async function go(){
 const err=document.getElementById('err'); err.textContent='';
 const r=await fetch('/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({username:username.value,password:password.value})});
 if(r.ok){location.href='/app';return}
 const d=await r.json().catch(()=>({detail:'error '+r.status}));
 err.textContent=r.status===429?'slow down — too many attempts':(d.detail||('error '+r.status));
}
document.querySelectorAll('input').forEach(i=>i.addEventListener('keydown',e=>{if(e.key==='Enter')go()}));
</script></main></body></html>"""

DASHBOARD_HTML = STYLE + """<div class="top"><h1>Watch Bridge</h1><span><span id="who" class="sub"></span> · <a href="/logout">log out</a></span></div>
<div id="trello"></div>
<div id="watch" class="card"></div>
<div class="card">
 <div class="row"><b>Change password</b></div>
 <input id="cur" type="password" placeholder="Current password" autocomplete="current-password">
 <input id="new" type="password" placeholder="New password (8+ characters)" autocomplete="new-password">
 <button onclick="pw()">Update</button> <span id="pwerr"></span>
</div>
<script>
function copy(btn, text){navigator.clipboard.writeText(text).then(()=>{const o=btn.textContent;btn.textContent='Copied';setTimeout(()=>btn.textContent=o,1400)})}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
async function pw(){
 const err=document.getElementById('pwerr'); err.textContent='';
 const r=await fetch('/app/password',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({current:cur.value,new:new.value})});
 if(r.ok){err.className='ok';err.textContent='updated';cur.value=new.value='';return}
 const d=await r.json().catch(()=>({detail:'error '+r.status})); err.className=''; err.textContent=d.detail||'error '+r.status;
}
function recipe(d){
 const headers='Content-Type: application/json\\nAuthorization: Bearer '+d.token;
 const bodyJson='{"text": "<Dictated Text>"}';
 return `
 <div class="row"><b>Your watch shortcut</b></div>
 <ol>
  <li><b>Dictate Text</b></li>
  <li><b>Get Contents of URL</b> — URL below, Method <b>POST</b>, Headers as below, Request Body → JSON with a field <code>text</code> = the <i>Dictated Text</i> magic variable</li>
  <li><b>Get Dictionary Value</b> — key <code>reply</code></li>
  <li><b>Speak Text</b></li>
 </ol>
 <div class="row"><b>Endpoint URL</b><button class="sec" onclick="copy(this,'${d.command_url}')">Copy</button></div>
 <pre>${esc(d.command_url)}</pre>
 <div class="row"><b>Headers</b><button class="sec" onclick="copy(this,headers)">Copy</button></div>
 <pre>${esc(headers)}</pre>
 <div class="row"><b>Request body (JSON)</b><button class="sec" onclick="copy(this,bodyJson)">Copy</button></div>
 <pre>${esc(bodyJson)}</pre>
 <p class="sub">This token is yours alone — it can create and read cards on your boards. Enable <b>Show on Apple Watch</b>; on an Ultra assign it to the Action button.</p>`;
}
function trelloCard(d){
 if(d.connected){
  const boards=Object.entries(d.boards).map(([b,lists])=>`<li><b>${esc(b)}</b>: ${lists.map(esc).join(' · ')||'<i>no lists</i>'}</li>`).join('');
  return `<div class="card">
   <div class="row"><b>Trello</b><span class="ok">connected — ${Object.keys(d.boards).length} boards</span></div>
   <ul>${boards}</ul>
   <details><summary>Connect a different Trello account</summary>
    <p class="sub">Open the authorize link, copy the token, paste it below.</p>
    <p><a class="btn" href="${d.authorize_url}" target="_blank" rel="noopener">Authorize on Trello</a></p>
    <input id="tok" placeholder="Paste your Trello token">
    <button onclick="connect()">Connect</button> <span id="terr"></span>
   </details></div>`;
 }
 return `<div class="card">
  <div class="row"><b>Step 1 · Connect your Trello</b></div>
  <ol><li>Open the <a href="${d.authorize_url}" target="_blank" rel="noopener" style="color:var(--accent)">Trello authorize page</a> and click <b>Allow</b>.</li>
  <li>Trello shows a long token — copy it (give it a few seconds to appear).</li>
  <li>Paste it below.</li></ol>
  <input id="tok" placeholder="Paste your Trello token" autocomplete="off">
  <button onclick="connect()">Connect</button> <span id="terr"></span></div>`;
}
async function connect(){
 const err=document.getElementById('terr'); err.textContent='';
 const r=await fetch('/app/trello',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({token:tok.value})});
 if(!r.ok){const d=await r.json().catch(()=>({detail:'error '+r.status}));err.textContent=d.detail||'error '+r.status;return}
 render(await r.json());
}
function render(d){
 document.getElementById('who').textContent=d.username;
 document.getElementById('trello').innerHTML=trelloCard(d);
 document.getElementById('watch').innerHTML=recipe(d);
}
(async()=>{render(await (await fetch('/app/state')).json())})();
document.addEventListener('keydown',e=>{if(e.key==='Enter'&&e.target.id==='cur')pw()});
</script></main></body></html>"""


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
