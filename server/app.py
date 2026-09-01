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
        self.token = token
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

# Per-user Trello clients, one per connected account: user id -> label -> client.
# The owner's first account is the `trello` singleton above, seeded into this
# cache at startup — tests and the existing endpoints patch the singleton's
# attributes, so its object identity must not change.
_clients: dict[int, dict[str, Trello]] = {}


def clients_for(user: dict) -> list[tuple[str, Trello]]:
    """The user's connected accounts as (label, client) pairs, in the order
    they were added. First use after a restart refreshes boards/lists from
    the API; a stale cache entry (token replaced) is rebuilt."""
    accounts = store.accounts_for(user["id"])
    if not accounts and user.get("trello_token"):
        store.migrate_accounts()  # legacy single-token row, not yet migrated
        accounts = store.accounts_for(user["id"])
    if not accounts:
        return []
    cached = _clients.setdefault(user["id"], {})
    out = []
    for acc in accounts:
        client = cached.get(acc["label"])
        if client is None or client.token != acc["token"]:
            client = Trello(TRELLO_KEY, acc["token"])
            client.refresh()
            cached[acc["label"]] = client
        out.append((acc["label"], client))
    return out


def trello_for(user: dict) -> Trello | None:
    """The user's first Trello client, or None while they haven't connected."""
    clients = clients_for(user)
    return clients[0][1] if clients else None


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
    store.add_account(owner["id"], "trello", TRELLO_TOKEN)
    _clients[owner["id"]] = {"trello": trello}
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
    store.migrate_accounts()
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
    return {"reply": route(cmd.text, clients=clients_for(user))}


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
                    "account": {"type": "string",
                                "description": "Trello account label, when the user named one"},
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
                    "account": {"type": "string",
                                "description": "Trello account label, when the user named one"},
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


def build_system_prompt(t: Trello | None = None,
                        clients: list[tuple[str, Trello]] | None = None) -> str:
    clients = clients if clients is not None else [(None, t or trello)]
    multi = len(clients) > 1
    lines = []
    for label, c in clients:
        for board, bid in c.boards.items():
            names = " | ".join(n for n, _ in c.lists_by_board.get(bid, []))
            lines.append(f"- account '{label}', board '{board}': {names}"
                         if multi else f"- board '{board}': {names}")
    inventory = "\n".join(lines)
    aliases = ", ".join(f'"{k}" means board "{v}"' for k, v in spoken_aliases().items())
    today = datetime.now(timezone.utc).strftime("%A %d %B %Y")
    account_rules = ""
    if multi:
        account_rules = """8. The user has several Trello accounts. When they name one ("on my work
   trello"), pass its label as the account argument; when the tool result has
   pairs, ask which account and board, e.g. "that Food list is on work / Home
   and personal / Plans — which one?". Never guess an account."""
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
7. If the request matches no tool, answer helpfully in a sentence or two.
{account_rules}"""


def find_boards_with_list(list_name: str, t: Trello | None = None) -> list[str]:
    """Board names containing a list with this name (case-insensitive)."""
    t = t or trello
    want = list_name.lower()
    return [
        board
        for board, bid in t.boards.items()
        if any(n.lower() == want for n, _ in t.lists_by_board.get(bid, []))
    ]


def resolve_board(args: dict, list_name: str, t: Trello | None = None,
                  clients: list[tuple[str, Trello]] | None = None) -> dict:
    """Pick the board (and, with several accounts, the account) server-side.
    A board or account the LLM named is honoured; an omitted board resolves
    only if the list name is unique. Ambiguity comes back as an ask-the-user
    result, never a guess."""
    clients = clients if clients is not None else [(None, t or trello)]
    multi = len(clients) > 1
    board = args.get("board") or ""
    label = (args.get("account") or "").strip().lower()
    if label:
        clients = [(l, c) for l, c in clients if l and l.lower() == label]
        if not clients:
            return {"ok": False, "error": "unknown_account", "account": args.get("account")}
    if board:
        picked = {"ok": True, "board": board}
        if multi:
            picked["account"] = clients[0][0]
        return picked
    hits = [(l, b) for l, c in clients for b in find_boards_with_list(list_name, c)]
    if len(hits) == 1:
        picked = {"ok": True, "board": hits[0][1]}
        if multi:
            picked["account"] = hits[0][0]
        return picked
    out = {"ok": False, "error": "ambiguous_board", "list": list_name,
           "boards": [b for _, b in hits]}
    if multi:
        out["pairs"] = [f"{l} / {b}" for l, b in hits]
    return out


def execute_tool(name: str, args: dict, t: Trello | None = None,
                 clients: list[tuple[str, Trello]] | None = None) -> dict:
    """Dispatch one tool call. KeyError from resolve_target = mismatch."""
    clients = clients if clients is not None else [(None, t or trello)]
    multi = len(clients) > 1

    def client_for(picked: dict) -> Trello:
        if not multi:
            return clients[0][1]
        label = (picked.get("account") or "").lower()
        return next(c for l, c in clients if l.lower() == label)

    def tagged(picked: dict, out: dict) -> dict:
        if multi:
            out["account"] = picked.get("account")
        return out

    if name == "trello_create_card":
        picked = resolve_board(args, args["list"], clients=clients)
        if not picked["ok"]:
            return picked
        board = picked["board"]
        card = client_for(picked).create_card(
            board, args["list"], args["name"], args.get("desc", ""))
        return tagged(picked, {"ok": True, "created": card["name"],
                               "list": args["list"], "board": board})
    if name == "trello_list_cards":
        picked = resolve_board(args, args["list"], clients=clients)
        if not picked["ok"]:
            return picked
        board = picked["board"]
        cards = client_for(picked).cards(board, args["list"])
        return tagged(picked, {"ok": True, "list": args["list"], "board": board,
                               "cards": [c["name"] for c in cards]})
    if name == "trello_list_boards":
        if not multi:
            c = clients[0][1]
            return {
                board: [n for n, _ in c.lists_by_board.get(bid, [])]
                for board, bid in c.boards.items()
            }
        return {
            label: {board: [n for n, _ in c.lists_by_board.get(bid, [])]
                    for board, bid in c.boards.items()}
            for label, c in clients
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


def route(text: str, t: Trello | None = None,
          clients: list[tuple[str, Trello]] | None = None) -> str:
    """Send the sentence to the LLM; the LLM picks tools, the server keeps
    resolve_target as the fail-loud authority on where cards go. Echoes only
    while no LLM backend is configured (Phase 1 fallback)."""
    clients = clients if clients is not None else [(None, t or trello)]
    if not (LLM_BASE_URL and LLM_API_KEY and LLM_MODEL):
        return f"echo: {text}"
    messages = [
        {"role": "system", "content": build_system_prompt(clients=clients)},
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
                result = execute_tool(call["function"]["name"], args, clients=clients)
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
    label: str = ""  # account name; empty means the user's first account


ACCOUNT_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


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
        clients = clients_for(user)
    except requests.RequestException:
        clients = []
    accounts = [
        {
            "label": label,
            "boards": {
                name: [n for n, _ in c.lists_by_board.get(bid, [])]
                for name, bid in c.boards.items()
            },
        }
        for label, c in clients
    ]
    return {
        "username": user["username"],
        "connected": bool(accounts) and bool(accounts[0]["boards"]),
        "accounts": accounts,
        # first-account shape, kept for anything still reading "boards"
        "boards": accounts[0]["boards"] if accounts else {},
        "command_url": COMMAND_URL,
        "token": user["api_token"],
        "invite_code": INVITE_CODE if user["username"] == OWNER_USERNAME else "",
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
    """Verify the pasted token against Trello, store it as an account,
    refresh boards. An empty label names the user's first account."""
    user = require_session(request)
    token = body.token.strip()
    if not token:
        raise HTTPException(status_code=400, detail="paste the token from Trello first")
    label = body.label.strip().lower()
    if not label:
        existing = store.accounts_for(user["id"])
        label = existing[0]["label"] if len(existing) == 1 else "trello"
    if not ACCOUNT_LABEL_RE.match(label):
        raise HTTPException(status_code=400,
                            detail="account name is 1-32 characters: a-z, 0-9, '-' or '_'")
    try:
        probe = requests.get(f"{TRELLO_API}/members/me",
                             params={"key": TRELLO_KEY, "token": token}, timeout=15)
        probe.raise_for_status()
    except requests.RequestException:
        raise HTTPException(status_code=401,
                            detail="Trello rejected that token — copy it again from the authorize page")
    if store.get_account(user["id"], label):
        raise HTTPException(status_code=409,
                            detail=f"you already have an account named '{label}' — pick another name")
    store.add_account(user["id"], label, token)
    _clients.pop(user["id"], None)
    return app_state(store.get_user(user["id"]))


@app.delete("/app/trello")
def disconnect_account(request: Request, label: str) -> dict:
    user = require_session(request)
    if not store.delete_account(user["id"], label.strip().lower()):
        raise HTTPException(status_code=404, detail="no account with that name")
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
<title>☾ Watch Bridge</title>
<style>
 :root{--bg:#150b26;--panel:#1f1335;--border:#4a2e6b;--border2:#6b3fa0;--text:#e8dff5;--muted:#a891c9;--accent:#c9a0ff;--accent2:#ffd9a0;--green:#9fe6b8;--code:#120a20;--mono:ui-monospace,Menlo,monospace;--serif:Georgia,'Times New Roman',serif}
 *{box-sizing:border-box}
 body{margin:0;color:var(--text);font-family:var(--serif);line-height:1.6;
  background:radial-gradient(ellipse at 20% -10%,#33205c 0%,transparent 55%),
   radial-gradient(ellipse at 85% 15%,#241542 0%,transparent 50%),
   radial-gradient(1.5px 1.5px at 12% 22%,#fff 50%,transparent 51%),
   radial-gradient(1px 1px at 32% 8%,#e8dff5 50%,transparent 51%),
   radial-gradient(1.5px 1.5px at 55% 30%,#fff 50%,transparent 51%),
   radial-gradient(1px 1px at 71% 12%,#ffd9a0 50%,transparent 51%),
   radial-gradient(2px 2px at 88% 34%,#fff 50%,transparent 51%),
   radial-gradient(1px 1px at 8% 55%,#e8dff5 50%,transparent 51%),
   radial-gradient(1.5px 1.5px at 42% 62%,#fff 50%,transparent 51%),
   radial-gradient(1px 1px at 64% 76%,#ffd9a0 50%,transparent 51%),
   radial-gradient(1.5px 1.5px at 93% 68%,#fff 50%,transparent 51%),
   radial-gradient(1px 1px at 22% 88%,#e8dff5 50%,transparent 51%),
   radial-gradient(1.5px 1.5px at 78% 92%,#fff 50%,transparent 51%),
   linear-gradient(180deg,#150b26 0%,#1a0f30 60%,#241542 100%);
  background-attachment:fixed}
 main{max-width:640px;margin:0 auto;padding:48px 20px 80px}
 h1{font-size:1.5rem;margin:0 0 4px;letter-spacing:.06em;color:var(--accent2);text-shadow:0 0 18px rgba(255,217,160,.35)}
 h1::before{content:'☾ ';color:var(--accent)}
 .sub{color:var(--muted);font-size:.92rem}
 hr{border:none;border-top:1px solid var(--border);margin:14px 0}
 .card{background:rgba(31,19,53,.85);border:1px solid var(--border);border-radius:14px;padding:18px;margin:16px 0;
  box-shadow:0 0 0 1px rgba(201,160,255,.06),0 6px 24px rgba(0,0,0,.35)}
 .card b{color:var(--accent2)}
 input{width:100%;background:var(--code);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:10px 12px;font-family:var(--mono);font-size:.95rem;margin:6px 0}
 input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 8px rgba(201,160,255,.35)}
 button{background:linear-gradient(180deg,#c9a0ff,#a874e8);color:#1a0f30;border:1px solid var(--accent);border-radius:8px;padding:10px 22px;font-family:var(--serif);font-weight:700;cursor:pointer;margin-top:10px;letter-spacing:.03em}
 button:hover{box-shadow:0 0 14px rgba(201,160,255,.5)}
 button.sec{background:transparent;border:1px solid var(--border);color:var(--muted);font-family:var(--mono);font-size:.75rem;padding:3px 10px;font-weight:400;margin:0}
 a.btn{display:inline-block;background:linear-gradient(180deg,#c9a0ff,#a874e8);color:#1a0f30;border:1px solid var(--accent);border-radius:8px;padding:10px 22px;font-weight:700;text-decoration:none;margin-top:10px}
 pre{background:var(--code);border:1px solid var(--border);border-radius:8px;padding:10px 12px;font-family:var(--mono);font-size:.82rem;overflow-x:auto;word-break:break-all}
 .row{display:flex;justify-content:space-between;align-items:center;gap:8px;margin:14px 0 6px}
 .row b{font-size:.98rem}
 .ok{color:var(--green)}#err{color:#ff8fa3;font-size:.85rem;min-height:1.2em}
 a{color:var(--accent)}
 ol{padding-left:20px}li{margin-bottom:8px}
 .token{font-family:var(--mono);font-size:.8rem;word-break:break-all;background:var(--code);border:1px solid var(--border);border-radius:8px;padding:8px 10px}
 .top{display:flex;justify-content:space-between;align-items:baseline}
 .top a{color:var(--muted);font-size:.85rem}
 details summary{cursor:pointer;color:var(--muted);font-size:.85rem}
 ul{margin:6px 0;padding-left:20px}
 .kv{display:flex;align-items:center;gap:8px;background:var(--code);border:1px solid var(--border);border-radius:8px;padding:8px 10px;margin:8px 0}
 .kv .k{flex:0 0 auto;min-width:118px;color:var(--accent2);font-size:.8rem;letter-spacing:.04em}
 .kv .v{flex:1 1 auto;font-family:var(--mono);font-size:.8rem;word-break:break-all}
 .kv button{margin:0;white-space:nowrap}
 details.dcard{background:rgba(31,19,53,.85);border:1px solid var(--border);border-radius:14px;margin:16px 0;
  box-shadow:0 6px 24px rgba(0,0,0,.35)}
 details.dcard>summary{cursor:pointer;padding:14px 18px;color:var(--accent2);font-weight:700;letter-spacing:.03em;
  list-style:none;display:flex;align-items:center;gap:8px}
 details.dcard>summary::-webkit-details-marker{display:none}
 details.dcard>summary::after{content:'✦';margin-left:auto;color:var(--muted);transition:transform .2s}
 details.dcard[open]>summary::after{transform:rotate(90deg)}
 details.dcard[open]>summary{border-bottom:1px solid var(--border)}
 details.dcard .inner{padding:2px 18px 18px}
 details.dcard details{margin-top:10px}
 .gear{background:transparent;border:none;color:var(--muted);font-size:1.05rem;cursor:pointer;padding:0 2px;margin:0;vertical-align:baseline}
 .gear:hover{color:var(--accent);text-shadow:0 0 10px rgba(201,160,255,.6)}
 code{color:var(--accent2)}
 .orn{color:var(--accent);letter-spacing:.4em;text-align:center;margin:20px 0 0;font-size:.8rem}
</style></head><body><main>
"""

LANDING_HTML = STYLE + """<h1>Watch Bridge</h1>
<p class="sub">Speak a command on your Apple Watch; it lands on your Trello boards.</p>
<div class="card">
 <p><a class="btn" href="/login">Log in</a> <a class="btn" href="/signup">Sign up</a></p>
 <p class="sub">Signing up needs an invite code from the operator. You then connect
 your own Trello account and get a personal token for your watch shortcut.</p>
</div>
<p class="orn">✦ ✦ ✦</p>
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
<p class="sub">Already have one? <a href="/login">Log in</a></p>
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
<p class="sub">No account? <a href="/signup">Sign up</a> (invite code needed).</p>
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

DASHBOARD_HTML = STYLE + """<div class="top"><h1>Watch Bridge</h1><span><span id="who" class="sub"></span> · <button class="gear" id="gearbtn" title="Settings">⚙</button> · <a href="/logout">log out</a></span></div>
<details id="trello" class="dcard"><summary id="trellos"></summary><div class="inner" id="trellobody"></div></details>
<details id="watchd" class="dcard"><summary>⌚ Your watch shortcut</summary><div class="inner" id="watch"></div></details>
<details id="talkd" class="dcard"><summary>☾ How to talk to it</summary><div class="inner">
 <p class="sub">Speak one sentence. It goes to the LLM agent together with the exact inventory
 of your boards and lists — the same names shown in the Trello section above. The agent picks
 an action, the server carries it out on Trello, and the reply is read aloud in a sentence or two.</p>
 <p><b>Things you can say</b></p>
 <ul>
  <li>“add bin day to chores” — creates a card in the list named Chores</li>
  <li>“what's on my shopping list” — reads a list out loud</li>
  <li>“what boards do I have”</li>
  <li>With more than one account connected: “add milk to shopping on my work trello”</li>
 </ul>
 <p><b>One exchange, up close</b></p>
 <pre>you:    add bin day to chores on my work trello
agent:  trello_create_card(list "Chores", name "Bin day", account "work")
trello: POST /1/cards  → card created
spoken: “Bin day is on the Chores list of Work Stuff.”</pre>
 <p class="sub">Two rules keep it honest. It never invents board or list names: if your words
 don't match the inventory it offers the closest options and asks you to try again. And if a
 list name exists on more than one board or account it asks which one instead of guessing —
 “Food is on work / Home and personal / Plans — which one?”</p>
 <p><b>What Trello supports today</b></p>
 <ul>
  <li>Create a card in a list — <code>POST /1/cards</code></li>
  <li>Read the cards in a list — <code>GET /1/lists/{id}/cards</code></li>
  <li>Enumerate boards and lists — answered from the server's cache</li>
 </ul>
 <p class="sub">Not wired yet: moving or deleting cards, due dates, and Calendar. The agent is
 only given the tools above, so it says it can't do the rest rather than improvising.</p>
</div></details>
<details id="invited" class="dcard"><summary>✉ Invite someone</summary><div class="inner" id="invite"></div></details>
<details id="settings" class="dcard"><summary>⚙ Settings</summary><div class="inner">
 <div class="card">
  <div class="row"><b>Change password</b></div>
  <input id="pwcur" type="password" placeholder="Current password" autocomplete="current-password">
  <input id="pwnew" type="password" placeholder="New password (8+ characters)" autocomplete="new-password">
  <button onclick="pw()">Update</button> <span id="pwerr"></span>
 </div>
 <p class="sub">More integrations (Calendar and friends) will appear here as their own sections.</p>
</div></details>
<p class="orn">✦ ☾ ✦</p>
<script>
function copy(btn, text){navigator.clipboard.writeText(text).then(()=>{const o=btn.textContent;btn.textContent='Copied';setTimeout(()=>btn.textContent=o,1400)})}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
function escA(s){return esc(s).replace(/"/g,'&quot;')}
async function pw(){
 const err=document.getElementById('pwerr'); err.textContent='';
 const r=await fetch('/app/password',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({current:pwcur.value,new:pwnew.value})});
 if(r.ok){err.className='ok';err.textContent='updated';pwcur.value=pwnew.value='';return}
 const d=await r.json().catch(()=>({detail:'error '+r.status})); err.className=''; err.textContent=d.detail||'error '+r.status;
}
function recipe(d){
 const pairs=[
  {k:'URL', v:d.command_url},
  {k:'Method', v:'POST'},
  {k:'Header key 1', v:'Content-Type'},
  {k:'Header value 1', v:'application/json'},
  {k:'Header key 2', v:'Authorization'},
  {k:'Header value 2', v:'Bearer '+d.token},
  {k:'Body type', v:'JSON'},
  {k:'Body key', v:'text'},
  {k:'Body value', v:'<Dictated Text>'},
 ];
 const boxes=pairs.map(p=>`
  <div class="kv"><span class="k">${esc(p.k)}</span><span class="v">${esc(p.v)}</span>
  <button class="sec" data-v="${escA(p.v)}">Copy</button></div>`).join('');
 return `
 <ol>
  <li><b>Dictate Text</b></li>
  <li><b>Get Contents of URL</b> — fill each field below, one box per field:</li>
  <li><b>Get Dictionary Value</b> — key <code>reply</code></li>
  <li><b>Speak Text</b></li>
 </ol>
 ${boxes}
 <p class="sub">This token is yours alone — it can create and read cards on your boards. Enable <b>Show on Apple Watch</b>; on an Ultra assign it to the Action button.</p>`;
}
function accountBlock(a,idx,d){
 const boards=Object.entries(a.boards).map(([b,lists])=>`<li><b>${esc(b)}</b>: ${lists.map(esc).join(' · ')||'<i>no lists</i>'}</li>`).join('');
 const label=(idx===0&&d.accounts.length===1)?'':'<span class="sub">account “'+esc(a.label)+'”</span> ';
 return `${label}<p class="sub"><span class="ok">connected</span></p>
  <ul>${boards}</ul>
  <button class="sec" data-acc="${escA(a.label)}">Remove</button>`;
}
function trelloCard(d){
 if(!d.accounts.length){
  return `<ol><li>Open the <a href="${d.authorize_url}" target="_blank" rel="noopener" style="color:var(--accent)">Trello authorize page</a> and click <b>Allow</b>.</li>
  <li>Trello shows a long token — copy it (give it a few seconds to appear).</li>
  <li>Paste it below.</li></ol>
  <input id="tok" placeholder="Paste your Trello token" autocomplete="off">
  <button onclick="connect()">Connect</button> <span id="terr"></span>`;
 }
 const blocks=d.accounts.map((a,i)=>accountBlock(a,i,d)).join('<hr>');
 const addForm=`<details><summary>Add another Trello account</summary>
  <p class="sub">Open the authorize link, copy the token, paste it below. Name it so you can say “on my work trello”.</p>
  <p><a class="btn" href="${d.authorize_url}" target="_blank" rel="noopener">Authorize on Trello</a></p>
  <input id="lbl" placeholder="Account name, e.g. work" autocomplete="off">
  <input id="tok" placeholder="Paste your Trello token" autocomplete="off">
  <button onclick="connect()">Connect</button> <span id="terr"></span>
 </details>`;
 return blocks+addForm;
}
async function connect(){
 const err=document.getElementById('terr'); err.textContent='';
 const lbl=document.getElementById('lbl');
 const r=await fetch('/app/trello',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({token:tok.value,label:lbl?lbl.value:''})});
 if(!r.ok){const d=await r.json().catch(()=>({detail:'error '+r.status}));err.textContent=d.detail||'error '+r.status;return}
 render(await r.json());
}
async function removeAccount(label){
 if(!confirm('Remove the Trello account "'+label+'"?'))return;
 const r=await fetch('/app/trello?label='+encodeURIComponent(label),{method:'DELETE'});
 if(r.ok)render(await r.json());
}
function inviteCard(d){
 if(!d.invite_code)return '';
 return `<p class="sub">Send them this code with the <a href="/signup" target="_blank" rel="noopener">signup page</a>. They connect their own Trello and get their own token.</p>
 <div class="row"><span class="token" style="flex:1">${esc(d.invite_code)}</span><button class="sec" id="invbtn">Copy</button></div>`;
}
function render(d){
 document.getElementById('who').textContent=d.username;
 document.getElementById('trellos').textContent=!d.accounts.length
  ?'🔮 Trello — connect your account'
  :d.accounts.length===1?`🔮 Trello — ${Object.keys(d.boards).length} boards`
  :`🔮 Trello — ${d.accounts.length} accounts`;
 document.getElementById('trellobody').innerHTML=trelloCard(d);
 document.getElementById('trello').open=!d.connected;
 document.getElementById('watch').innerHTML=recipe(d);
 document.getElementById('invite').innerHTML=inviteCard(d);
 document.getElementById('invited').style.display=d.invite_code?'':'none';
 const ib=document.getElementById('invbtn');
 if(ib)ib.onclick=e=>copy(e.target,d.invite_code);
}
(async()=>{render(await (await fetch('/app/state')).json())})();
document.getElementById('watch').addEventListener('click',e=>{
 const b=e.target.closest('button.sec'); if(b&&b.dataset.v!==undefined)copy(b,b.dataset.v)});
document.getElementById('trellobody').addEventListener('click',e=>{
 const b=e.target.closest('button.sec'); if(b&&b.dataset.acc!==undefined)removeAccount(b.dataset.acc)});
document.getElementById('gearbtn').onclick=()=>{
 const s=document.getElementById('settings'); s.open=true; s.scrollIntoView({behavior:'smooth'})};
document.addEventListener('keydown',e=>{if(e.key==='Enter'&&e.target.id==='pwcur')pw()});
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
