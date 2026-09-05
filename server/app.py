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
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

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
# Google Calendar OAuth (one connection per user). Unset = the calendar is
# simply absent: no dashboard card, no tools. The redirect URI must match the
# OAuth client in Google Cloud Console exactly.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.environ.get(
    "GOOGLE_REDIRECT_URI", "https://bridge.upperpeas.com/app/google/callback")
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GCAL_API = "https://www.googleapis.com/calendar/v3"
GCAL_SCOPE = ("https://www.googleapis.com/auth/calendar.events "
              "https://www.googleapis.com/auth/calendar.readonly")

TRELLO_API = "https://api.trello.com/1"


class Command(BaseModel):
    text: str


class CardIn(BaseModel):
    board: str
    list: str
    name: str
    desc: str = ""


class CardPatchIn(BaseModel):
    """Edit / move / due-date / archive a card picked by name."""
    board: str
    name: str
    list: str = ""
    new_name: str = ""
    desc: str = ""
    due_day: str = ""
    due_time: str = ""
    to_list: str = ""
    to_board: str = ""
    archive: bool = False


class CommentIn(BaseModel):
    board: str
    name: str
    text: str
    list: str = ""


class ListIn(BaseModel):
    board: str
    name: str


class ListPatchIn(BaseModel):
    board: str
    list: str
    new_name: str


class EventIn(BaseModel):
    """Create an event: timed when `time` is set (or all_day=False), all-day
    otherwise. `all_day=True` beats a time."""
    title: str
    day: str = ""
    time: str = ""
    duration_min: int = 60
    all_day: bool | None = None
    location: str = ""
    description: str = ""
    attendees: list[str] = []


class EventPatchIn(BaseModel):
    """Edit / move an event picked by spoken name from a day."""
    day: str = ""
    name: str
    new_title: str = ""
    new_day: str = ""
    new_time: str = ""
    duration_min: int | None = None
    location: str = ""
    description: str = ""


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

    def _put(self, path: str, **params) -> dict:
        r = requests.put(f"{TRELLO_API}{path}", params={**self.auth, **params}, timeout=15)
        r.raise_for_status()
        return r.json()

    def find_card(self, board: str, name: str, list_name: str = "") -> dict:
        """Look a card up by spoken name in a list, or the whole board when no
        list is given. Exact (case-insensitive) first, then substring; a miss
        or several hits raise KeyError with the candidates so the agent can
        offer them instead of guessing."""
        board_id = self._board_id(board)
        if list_name:
            _, list_id = resolve_target(self.boards, self.lists_by_board,
                                        board, list_name)
            cards = self._get(f"/lists/{list_id}/cards", fields="id,name")
        else:
            cards = self._get(f"/boards/{board_id}/cards", filter="open",
                              fields="id,name")
        low = name.lower()
        hits = [c for c in cards if c["name"].lower() == low] or \
               [c for c in cards if low in c["name"].lower()]
        if len(hits) != 1:
            known = ", ".join(sorted(c["name"] for c in cards))
            where = f" on list '{list_name}'" if list_name else f" on board '{board}'"
            raise KeyError(f"card '{name}' matches {len(hits) or 'nothing'}{where}"
                           f" (known: {known})")
        return hits[0]

    def card_details(self, board: str, name: str, list_name: str = "") -> dict:
        card = self.find_card(board, name, list_name)
        full = self._get(f"/cards/{card['id']}", fields="name,desc,due,url,labels",
                         actions="commentCard", action_fields="data")
        comments = [a["data"]["text"] for a in reversed(full.get("actions", []))
                    if "text" in a.get("data", {})][-5:]
        return {"id": full["id"], "name": full["name"], "desc": full["desc"],
                "due": full.get("due"), "url": full.get("url"),
                "labels": [l["name"] or l["color"] for l in full.get("labels", [])],
                "comments": comments}

    def update_card(self, card_id: str, **fields) -> dict:
        return self._put(f"/cards/{card_id}", **fields)

    def add_comment(self, card_id: str, text: str) -> dict:
        r = requests.post(f"{TRELLO_API}/cards/{card_id}/actions/comments",
                          params={**self.auth, "text": text}, timeout=15)
        r.raise_for_status()
        return r.json()

    def _board_id(self, board: str) -> str:
        hits = [b for b in self.boards if b.lower() == board.lower()]
        if len(hits) != 1:
            raise KeyError(f"board '{board}' matches {len(hits) or 'nothing'} "
                           f"(known: {', '.join(sorted(self.boards))})")
        return self.boards[hits[0]]

    def create_list(self, board: str, name: str) -> dict:
        board_id = self._board_id(board)
        created = requests.post(
            f"{TRELLO_API}/lists",
            params={**self.auth, "idBoard": board_id, "name": name},
            timeout=15,
        )
        created.raise_for_status()
        body = created.json()
        self.lists_by_board.setdefault(board_id, []).append((body["name"], body["id"]))
        return body

    def rename_list(self, board: str, list_name: str, new_name: str) -> dict:
        board_id, list_id = resolve_target(self.boards, self.lists_by_board,
                                           board, list_name)
        updated = self._put(f"/lists/{list_id}", name=new_name)
        pairs = self.lists_by_board.get(board_id, [])
        self.lists_by_board[board_id] = [(new_name, lid) if lid == list_id
                                         else (n, lid) for n, lid in pairs]
        return updated

    def board_overview(self, board: str) -> dict[str, list[str]]:
        board_id = self._board_id(board)
        lists = self._get(f"/boards/{board_id}/lists", cards="open",
                          card_fields="name")
        return {l["name"]: [c["name"] for c in l.get("cards", [])] for l in lists}


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


# --- Google Calendar: hand-rolled OAuth 2.0, one connection per user ---------
# Unlike Trello's paste-a-token flow, Google needs an authorization redirect:
# /app/google/start sends the user to consent, /app/google/callback trades the
# code for tokens, and the refresh token is refreshed-on-use from then on.

class GcalDisconnected(Exception):
    """The stored Google grant is gone (revoked, or the refresh token expired
    because the consent screen is still in Testing)."""


class Gcal:
    """Minimal Google Calendar client on top of a stored OAuth grant."""

    def __init__(self, user_id: int):
        self.user_id = user_id
        self.account = store.get_google_account(user_id) or {}
        self.timezone = self.account.get("timezone", "UTC")
        # The default calendar every tool acts on, chosen in the dashboard.
        self.calendar_id = self.account.get("calendar_id") or "primary"
        self.email = ""
        self.summary = ""

    @property
    def connected(self) -> bool:
        return bool(self.account and self.account.get("refresh_token"))

    def calendars(self) -> list[dict]:
        """The account's calendar list, trimmed to what the picker needs."""
        items = self._get("/users/me/calendarList", minAccessRole="reader") \
            .get("items", [])
        return [{"id": e["id"], "summary": e.get("summary", ""),
                 "primary": e.get("primary", False),
                 "accessRole": e.get("accessRole", "")} for e in items]

    def _grant_died(self) -> None:
        """The OAuth grant is gone. Invalidate the stored row rather than
        deleting it, so the chosen default calendar survives until the user
        reconnects, and drop the cached client."""
        store.invalidate_google_grant(self.user_id)
        _gcal.pop(self.user_id, None)

    def _token(self) -> str:
        """A live access token, refreshing and persisting it near expiry."""
        if not self.account or not self.account.get("refresh_token"):
            raise GcalDisconnected()
        if self.account["expires_at"] - 60 < time.time():
            r = requests.post(GOOGLE_TOKEN_URL, data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": self.account["refresh_token"],
            }, timeout=15)
            if r.status_code != 200:  # invalid_grant: revoked or expired
                self._grant_died()
                raise GcalDisconnected()
            payload = r.json()
            self.account["access_token"] = payload["access_token"]
            self.account["expires_at"] = time.time() + payload["expires_in"]
            store.save_google_account(
                self.user_id, payload["access_token"],
                self.account["refresh_token"], self.account["expires_at"],
                self.timezone)
        return self.account["access_token"]

    def _get(self, path: str, **params) -> dict:
        return self._send("GET", path, params=params)

    def _send(self, method: str, path: str, params=None, body=None):
        try:
            kw = {"headers": {"Authorization": f"Bearer {self._token()}"},
                  "timeout": 15}
            if method == "GET":
                r = requests.get(f"{GCAL_API}{path}", params=params, **kw)
            elif method == "POST":
                r = requests.post(f"{GCAL_API}{path}", json=body, **kw)
            elif method == "PATCH":
                r = requests.patch(f"{GCAL_API}{path}", json=body, **kw)
            else:
                r = requests.delete(f"{GCAL_API}{path}", **kw)
            r.raise_for_status()
        except requests.HTTPError as e:
            # 401/403 here means the grant died under a token we thought alive.
            if e.response is not None and e.response.status_code in (401, 403):
                logging.getLogger("uvicorn.error").warning(
                    "gcal %s -> %s %s", path, e.response.status_code, e.response.text[:200])
                self._grant_died()
                raise GcalDisconnected() from e
            raise
        if method == "DELETE":
            return None if r.status_code == 204 else r.json()
        return r.json()

    def _post(self, path: str, body: dict) -> dict:
        return self._send("POST", path, body=body)

    def _patch(self, path: str, body: dict) -> dict:
        return self._send("PATCH", path, body=body)

    def _delete(self, path: str) -> dict | None:
        return self._send("DELETE", path)

    def refresh(self) -> None:
        """Validate the grant and load the default calendar's name, timezone
        and id. The calendar's own timezone wins when it has one; the account
        default covers calendars without one."""
        try:
            cal = self._get(f"/calendars/{self.calendar_id}")
        except requests.HTTPError as e:
            # A stored default can outlive the account it belonged to (e.g.
            # the user reconnects as a different Google account). 404 means
            # the calendar is not on this account: fall back to primary
            # instead of failing every calendar call forever.
            if (e.response is not None and e.response.status_code == 404
                    and self.calendar_id != "primary"):
                logging.getLogger("uvicorn.error").warning(
                    "default calendar %s not on this account; back to primary",
                    self.calendar_id)
                store.save_google_calendar(self.user_id, "primary", self.timezone)
                self.calendar_id = "primary"
                cal = self._get("/calendars/primary")
            else:
                raise
        self.email = cal.get("id", "")
        self.summary = cal.get("summary", "")
        if cal.get("timeZone"):
            self.timezone = cal["timeZone"]
        else:
            self.timezone = self._get("/users/me/settings/timezone")["value"]

    def events(self, day: str) -> list[dict]:
        """The day's events, in order: [{start, title, location, description}]."""
        items = self._list_events(day)
        return [{"start": e["start"].get("dateTime") or e["start"].get("date", ""),
                 "title": e.get("summary", "(untitled)"),
                 "location": e.get("location", ""),
                 "description": e.get("description", "")}
                for e in items]

    def _list_events(self, day: str = "") -> list[dict]:
        """One events.list call: the named local day, or a rolling 14-day
        window when the user named no day. singleEvents expands recurring
        events, so a weekly event shows once per occurrence."""
        tz = ZoneInfo(self.timezone)
        start = parse_day(day, tz) if day else \
            datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1 if day else 14)
        data = self._get(f"/calendars/{self.calendar_id}/events",
                         timeMin=start.isoformat(),
                         timeMax=end.isoformat(), singleEvents="true",
                         orderBy="startTime", maxResults="50")
        return data.get("items", [])

    def find_events(self, name: str, day: str = "") -> list[dict]:
        """Events matching the spoken name: exact case-insensitive first, then
        substring. Nothing or several matches raises KeyError with the
        candidates, in the same shape as find_card."""
        items = self._list_events(day)
        low = name.lower()
        hits = [e for e in items if e.get("summary", "").lower() == low] or \
               [e for e in items if low in e.get("summary", "").lower()]
        if len(hits) != 1:
            known = ", ".join(sorted(e.get("summary", "(untitled)") for e in items))
            raise KeyError(f"event '{name}' matches {len(hits) or 'nothing'} "
                           f"in the calendar (known: {known})")
        return hits

    def event_details(self, name: str, day: str = "") -> dict:
        hit = self.find_events(name, day)[0]
        return {"id": hit["id"],
                "title": hit.get("summary", "(untitled)"),
                "when": hit["start"].get("dateTime") or hit["start"].get("date", ""),
                "end": hit["end"].get("dateTime") or hit["end"].get("date", ""),
                "location": hit.get("location", ""),
                "description": hit.get("description", ""),
                "attendees": [a.get("email", "") for a in hit.get("attendees", [])],
                "all_day": "date" in hit["start"]}

    def update_event(self, event_id: str, **body) -> dict:
        """PATCH the event: only the keys sent change (Google partial update)."""
        return self._patch(
            f"/calendars/{self.calendar_id}/events/{event_id}", body) or {}

    def delete_event(self, event_id: str) -> dict:
        self._delete(f"/calendars/{self.calendar_id}/events/{event_id}")
        return {"id": event_id}

    def create_event(self, title: str, day: str, when: str = "",
                     duration_min: int = 60, all_day: bool | None = None,
                     location: str = "", description: str = "",
                     attendees: list[str] | None = None) -> dict:
        """Create on the primary calendar. `all_day` wins when given: True
        forces all-day even with a time, False forces a timed event (so a
        missing `when` raises). No `all_day` infers from `when`."""
        tz = ZoneInfo(self.timezone)
        start = parse_day(day, tz)
        body: dict = {"summary": title}
        if location:
            body["location"] = location
        if description:
            body["description"] = description
        if attendees:
            body["attendees"] = [{"email": a} for a in attendees]
        timed = (when != "") if all_day is None else (not all_day)
        if timed:
            hour, minute = parse_time(when)
            start = start.replace(hour=hour, minute=minute)
            end = start + timedelta(minutes=duration_min)
            body["start"] = {"dateTime": start.isoformat(), "timeZone": self.timezone}
            body["end"] = {"dateTime": end.isoformat(), "timeZone": self.timezone}
        else:
            end = start + timedelta(days=1)
            body["start"] = {"date": start.date().isoformat()}
            body["end"] = {"date": end.date().isoformat()}
        return self._post(f"/calendars/{self.calendar_id}/events", body) or body


def parse_day(day: str, tz) -> datetime:
    """The day's midnight in the user's timezone: today/tomorrow/yesterday or
    YYYY-MM-DD. Anything else raises KeyError with what was actually said."""
    text = (day or "today").strip().lower()
    now = datetime.now(tz)
    if text in ("", "today"):
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    steps = {"tomorrow": 1, "yesterday": -1}
    if text in steps:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start + timedelta(days=steps[text])
    try:
        return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=tz)
    except ValueError:
        raise KeyError(f"calendar day '{day}' is not today, tomorrow, "
                       "yesterday or YYYY-MM-DD") from None


def parse_time(when: str) -> tuple[int, int]:
    """(hour, minute) of the 24-hour HH:MM time the LLM converts spoken words
    into. Anything else raises KeyError with what was actually said."""
    try:
        t = datetime.strptime((when or "").strip(), "%H:%M")
        return t.hour, t.minute
    except ValueError:
        raise KeyError(f"calendar time '{when}' is not HH:MM") from None


_gcal: dict[int, Gcal] = {}


def gcal_for(user: dict) -> Gcal:
    """The user's calendar client. First use after a restart re-validates the
    grant and reloads the timezone; unconnected users get an empty client."""
    g = _gcal.get(user["id"])
    if g is None:
        g = Gcal(user["id"])
        if g.connected:
            g.refresh()
            _gcal[user["id"]] = g
    return g


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
        # The named owner is the operator: an admin after any migration.
        owner = store.get_user_by_name(OWNER_USERNAME)
        if owner and not owner["is_admin"]:
            store.set_admin(owner["id"], True)
        return
    password = OWNER_PASSWORD or secrets.token_urlsafe(12)
    owner = store.create_user(OWNER_USERNAME, password,
                              api_token=BRIDGE_TOKEN, trello_token=TRELLO_TOKEN,
                              is_admin=True)
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
    g = None
    if google_configured():
        try:
            g = gcal_for(user)
            if not g.connected:
                g = None
        except (GcalDisconnected, requests.RequestException) as e:
            logging.getLogger("uvicorn.error").warning(
                "Calendar for %s failed: %s", user["username"], e)
            g = None
    return {"reply": route(cmd.text, clients=clients_for(user), gcal=g)}


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


def _card_params(required: list[str] | None = None, **extra) -> dict:
    """Parameter schema for the card-targeting tools: board/list optional,
    the card picked by name, plus any tool-specific extras."""
    props = {
        "board": {"type": "string"},
        "list": {"type": "string"},
        "name": {"type": "string"},
        "account": {"type": "string",
                    "description": "Trello account label, when the user named one"},
        **extra,
    }
    return {"type": "object", "properties": props, "required": required or ["name"]}


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
    {
        "type": "function",
        "function": {
            "name": "trello_card_details",
            "description": "Read one card's description, due date, labels and "
                           "comments. The card is picked by name.",
            "parameters": _card_params(),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "trello_move_card",
            "description": "Move a card to another list. The card is picked by "
                           "name; to_board is only for a cross-board move.",
            "parameters": _card_params(to_list={"type": "string"},
                                       to_board={"type": "string"}),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "trello_edit_card",
            "description": "Rename a card or change its description. At least "
                           "one of new_name / desc must be given.",
            "parameters": _card_params(new_name={"type": "string"},
                                       desc={"type": "string"}),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "trello_set_due",
            "description": "Set a card's due date. day is today, tomorrow, or "
                           "YYYY-MM-DD; omit time for a due date with no time of day.",
            "parameters": _card_params(day={"type": "string",
                                            "description": "today, tomorrow, or YYYY-MM-DD"},
                                       time={"type": "string",
                                             "description": "24-hour HH:MM; omit for no time"},
                                       required=["day"]),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "trello_archive_card",
            "description": "Close (archive) a card. The card is picked by name.",
            "parameters": _card_params(),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "trello_comment_card",
            "description": "Add a comment to a card.",
            "parameters": _card_params(text={"type": "string"},
                                       required=["text"]),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "trello_create_list",
            "description": "Create a new list on a board.",
            "parameters": {
                "type": "object",
                "properties": {
                    "board": {"type": "string"},
                    "name": {"type": "string"},
                    "account": {"type": "string",
                                "description": "Trello account label, when the user named one"},
                },
                "required": ["board", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "trello_rename_list",
            "description": "Rename a list on a board.",
            "parameters": {
                "type": "object",
                "properties": {
                    "board": {"type": "string"},
                    "list": {"type": "string"},
                    "new_name": {"type": "string"},
                    "account": {"type": "string",
                                "description": "Trello account label, when the user named one"},
                },
                "required": ["board", "list", "new_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "trello_board_overview",
            "description": "Read a board's lists together with each list's cards.",
            "parameters": {
                "type": "object",
                "properties": {
                    "board": {"type": "string"},
                    "account": {"type": "string",
                                "description": "Trello account label, when the user named one"},
                },
                "required": ["board"],
            },
        },
    },
]

GCAL_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "gcal_list_events",
            "description": "Read the day's events on the user's Google Calendar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "day": {"type": "string",
                            "description": "today, tomorrow, yesterday, or YYYY-MM-DD"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gcal_create_event",
            "description": "Create an event on the user's Google Calendar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "day": {"type": "string",
                            "description": "today, tomorrow, yesterday, or YYYY-MM-DD"},
                    "time": {"type": "string",
                             "description": "24-hour HH:MM start time; omit for all-day"},
                    "duration": {"type": "number",
                                 "description": "length in minutes; default 60"},
                    "all_day": {"type": "boolean",
                                "description": "true forces an all-day event even when a time was said"},
                    "location": {"type": "string"},
                    "description": {"type": "string"},
                    "attendees": {"type": "array", "items": {"type": "string"},
                                  "description": "email addresses to invite"},
                },
                "required": ["title", "day"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gcal_find_event",
            "description": "Read one event's details (time, location, description, invitees) by its spoken name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "day": {"type": "string",
                            "description": "the day the user mentioned, if any"},
                    "name": {"type": "string", "description": "the event's spoken name"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gcal_edit_event",
            "description": "Edit or move an event: rename it, change its day or time, or update location and description.",
            "parameters": {
                "type": "object",
                "properties": {
                    "day": {"type": "string",
                            "description": "the day the event is currently on"},
                    "name": {"type": "string", "description": "the event's spoken name"},
                    "new_title": {"type": "string"},
                    "new_day": {"type": "string",
                                "description": "today, tomorrow, yesterday, or YYYY-MM-DD to move it"},
                    "new_time": {"type": "string",
                                 "description": "24-hour HH:MM new start time"},
                    "duration": {"type": "number",
                                 "description": "length in minutes when moving the time"},
                    "location": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["day", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gcal_delete_event",
            "description": "Delete an event from the user's Google Calendar by its spoken name. Confirm with the user first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "day": {"type": "string",
                            "description": "the day the event is on"},
                    "name": {"type": "string", "description": "the event's spoken name"},
                },
                "required": ["day", "name"],
            },
        },
    },
]


def tools_for(gcal: Gcal | None = None) -> list[dict]:
    """The tools this user's agent gets: calendar tools only once connected."""
    return TOOLS + (GCAL_TOOLS if gcal is not None and gcal.connected else [])


def build_system_prompt(t: Trello | None = None,
                        clients: list[tuple[str, Trello]] | None = None,
                        gcal: Gcal | None = None) -> str:
    clients = clients if clients is not None else [(None, t or trello)]
    multi = len(clients) > 1
    lines = []
    for label, c in clients:
        for board, bid in c.boards.items():
            names = " | ".join(n for n, _ in c.lists_by_board.get(bid, []))
            lines.append(f"- account '{label}', board '{board}': {names}"
                         if multi else f"- board '{board}': {names}")
    calendar = gcal if gcal is not None and gcal.connected else None
    if calendar:
        name = calendar.summary or calendar.email or "primary"
        lines.append(f"- calendar '{name}' (the default one): "
                     f"timezone {calendar.timezone}")
    inventory = "\n".join(lines)
    aliases = ", ".join(f'"{k}" means board "{v}"' for k, v in spoken_aliases().items())
    today = datetime.now(timezone.utc).strftime("%A %d %B %Y")
    account_rules = ""
    if multi:
        account_rules = """9. The user has several Trello accounts. When they name one ("on my work
   trello"), pass its label as the account argument; when the tool result has
   pairs, ask which account and board, e.g. "that Food list is on work / Home
   and personal / Plans — which one?". Never guess an account."""
    tool_lines = [
        "- trello_create_card(board, list, name, desc) — add a card to a list",
        "- trello_list_cards(board, list) — read the cards in a list",
        "- trello_list_boards() — enumerate boards and their lists",
        "- trello_card_details(board, list, name) — read a card's description, due date, labels and comments",
        "- trello_move_card(board, list, name, to_list, to_board) — move a card to another list",
        "- trello_edit_card(board, list, name, new_name, desc) — rename a card or change its description",
        "- trello_set_due(board, list, name, day, time) — set a card's due date",
        "- trello_archive_card(board, list, name) — close (archive) a card",
        "- trello_comment_card(board, list, name, text) — add a comment to a card",
        "- trello_create_list(board, name) — create a list on a board",
        "- trello_rename_list(board, list, new_name) — rename a list",
        "- trello_board_overview(board) — read a board's lists and their cards",
    ]
    if calendar:
        tool_lines += [
            "- gcal_list_events(day) — read a day's events from the calendar",
            "- gcal_find_event(day, name) — read one event's time, location, description and invitees",
            "- gcal_create_event(title, day, time, duration, all_day, location, description, attendees) — create an event",
            "- gcal_edit_event(day, name, new_title, new_day, new_time, duration, location, description) — edit or move an event",
            "- gcal_delete_event(day, name) — delete an event",
        ]
    calendar_rule = (
        """7. Calendar: the user's Google Calendar is connected. "day" is today,
   tomorrow, yesterday, or YYYY-MM-DD in the user's timezone. Convert spoken
   times to 24-hour HH:MM ("3pm" is 15:00); when the user gives no time,
   omit it so the event is all-day. Events are picked by the name the user
   said, from the day they mentioned. On a no_event or ambiguous_event
   result, offer the closest candidates and ask; never invent an event name.
   Confirm before deleting: "shall I delete Dentist tomorrow?". Never guess
   a day, a time, or which event. On a calendar_disconnected result, say
   their calendar needs reconnecting from the bridge dashboard."""
        if calendar else
        """7. Google Calendar is not connected yet. Any calendar or events request gets
   a brief "calendar isn't set up yet" reply.""")
    return f"""You are the brain of a voice assistant driven from an Apple Watch. Each
user message is one spoken sentence. Your reply is read aloud, so keep it to
one or two short sentences of plain words; never mention IDs, JSON, or URLs.

Today is {today} (UTC).

Tools:
{chr(10).join(tool_lines)}

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
6. Card actions (move, edit, due date, archive, comment) pick the card by
   name from the list the user mentioned — or the whole board when they
   named no list. On a no_card or ambiguous_card result, offer the closest
   candidates and ask; never invent a card name. For a due date, "day" is
   today, tomorrow, or YYYY-MM-DD, and spoken times become 24-hour HH:MM;
   when the user gives no time, omit it.
{calendar_rule}
8. If the request matches no tool, answer helpfully in a sentence or two.
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


def card_error(e: KeyError) -> dict:
    """A find_card KeyError as an ask-the-user tool result."""
    return _match_error(e, "no_card", "ambiguous_card")


def event_error(e: KeyError) -> dict:
    """A find_events KeyError as an ask-the-user tool result."""
    return _match_error(e, "no_event", "ambiguous_event")


def _match_error(e: KeyError, none_name: str, ambiguous_name: str) -> dict:
    msg = e.args[0]
    _, _, known = msg.partition("(known:")
    candidates = [c.strip(" '\"") for c in known.rstrip(") ").split(",")]
    candidates = [c for c in candidates if c]
    error = none_name if ("nothing" in msg or "matches 0" in msg) else ambiguous_name
    return {"ok": False, "error": error, "candidates": candidates}


def resolve_card_board(args: dict,
                       clients: list[tuple[str, Trello]] | None = None) -> dict:
    """Find the board a spoken card name lives on. A board or list the user
    named is honoured through resolve_board; when neither was named, the one
    board holding a card with that name wins, anything else asks."""
    clients = clients if clients is not None else [(None, trello)]
    multi = len(clients) > 1
    if args.get("board") or args.get("list"):
        return resolve_board(args, args.get("list") or "", clients=clients)
    hits = []
    for label, c in clients:
        for board in c.boards:
            try:
                c.find_card(board, args["name"])
            except KeyError as e:
                if "nothing" not in e.args[0] and "matches 0" not in e.args[0]:
                    return card_error(e)  # several cards of that name on one board
                continue
            hits.append((label, board))
    if len(hits) == 1:
        picked = {"ok": True, "board": hits[0][1]}
        if multi:
            picked["account"] = hits[0][0]
        return picked
    return {"ok": False, "error": "ambiguous_board", "boards": [b for _, b in hits]}


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
                 clients: list[tuple[str, Trello]] | None = None,
                 gcal: Gcal | None = None) -> dict:
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
    def due_tz():
        """The timezone due dates are interpreted in: the connected calendar's
        zone when there is one, else the server's clock."""
        if gcal is not None and gcal.connected and gcal.timezone:
            return ZoneInfo(gcal.timezone)
        return timezone.utc

    if name == "trello_card_details":
        picked = resolve_card_board(args, clients)
        if not picked["ok"]:
            return picked
        try:
            d = client_for(picked).card_details(picked["board"], args["name"],
                                                args.get("list") or "")
        except KeyError as e:
            return tagged(picked, card_error(e))
        return tagged(picked, {"ok": True, "name": d["name"], "desc": d["desc"],
                               "due": d["due"], "labels": d["labels"],
                               "comments": d["comments"]})
    if name == "trello_move_card":
        picked = resolve_card_board(args, clients)
        if not picked["ok"]:
            return picked
        t = client_for(picked)
        try:
            card = t.find_card(picked["board"], args["name"], args.get("list") or "")
        except KeyError as e:
            return card_error(e)
        try:
            _, to_list_id = resolve_target(t.boards, t.lists_by_board,
                                           args.get("to_board") or picked["board"],
                                           args["to_list"])
        except KeyError as e:
            return {"ok": False, "error": "bad_target", "detail": e.args[0]}
        fields = {"idList": to_list_id}
        if args.get("to_board"):
            fields["idBoard"] = t._board_id(args["to_board"])
        t.update_card(card["id"], **fields)
        return tagged(picked, {"ok": True, "moved": card["name"],
                               "to_list": args["to_list"],
                               "to_board": args.get("to_board") or picked["board"]})
    if name == "trello_edit_card":
        if not (args.get("new_name") or args.get("desc")):
            return {"ok": False, "error": "nothing_to_edit"}
        picked = resolve_card_board(args, clients)
        if not picked["ok"]:
            return picked
        t = client_for(picked)
        try:
            card = t.find_card(picked["board"], args["name"], args.get("list") or "")
        except KeyError as e:
            return card_error(e)
        fields = {}
        if args.get("new_name"):
            fields["name"] = args["new_name"]
        if args.get("desc"):
            fields["desc"] = args["desc"]
        t.update_card(card["id"], **fields)
        out = {"ok": True, "edited": args["name"]}
        if args.get("new_name"):
            out["renamed_to"] = args["new_name"]
        return tagged(picked, out)
    if name == "trello_set_due":
        picked = resolve_card_board(args, clients)
        if not picked["ok"]:
            return picked
        t = client_for(picked)
        try:
            card = t.find_card(picked["board"], args["name"], args.get("list") or "")
        except KeyError as e:
            return card_error(e)
        try:
            day = parse_day(args["day"], due_tz()).date()
        except KeyError as e:
            return {"ok": False, "error": "bad_day", "detail": e.args[0]}
        due = day.isoformat()
        if args.get("time"):
            hh, mm = parse_time(args["time"])
            due += f"T{hh:02d}:{mm:02d}:00"
        else:
            due += "T12:00:00"  # noon: a midnight due renders as the wrong day
        t.update_card(card["id"], due=due)
        return tagged(picked, {"ok": True, "due": due, "on": card["name"]})
    if name == "trello_archive_card":
        picked = resolve_card_board(args, clients)
        if not picked["ok"]:
            return picked
        t = client_for(picked)
        try:
            card = t.find_card(picked["board"], args["name"], args.get("list") or "")
        except KeyError as e:
            return card_error(e)
        t.update_card(card["id"], closed="true")
        return tagged(picked, {"ok": True, "archived": card["name"]})
    if name == "trello_comment_card":
        picked = resolve_card_board(args, clients)
        if not picked["ok"]:
            return picked
        t = client_for(picked)
        try:
            card = t.find_card(picked["board"], args["name"], args.get("list") or "")
        except KeyError as e:
            return card_error(e)
        t.add_comment(card["id"], args["text"])
        return tagged(picked, {"ok": True, "commented": args["text"]})
    if name == "trello_create_list":
        t = client_for(args)
        try:
            t.create_list(args["board"], args["name"])
        except KeyError as e:
            return card_error(e)
        return {"ok": True, "created_list": args["name"], "board": args["board"]}
    if name == "trello_rename_list":
        t = client_for(args)
        try:
            t.rename_list(args["board"], args["list"], args["new_name"])
        except KeyError as e:
            return card_error(e)
        return {"ok": True, "renamed": args["list"], "to": args["new_name"],
                "board": args["board"]}
    if name == "trello_board_overview":
        t = client_for(args)
        try:
            overview = t.board_overview(args["board"])
        except KeyError as e:
            return card_error(e)
        out = {"ok": True, "board": args["board"], "lists": overview}
        return out
    if name == "gcal_list_events":
        if gcal is None or not gcal.connected:
            return {"ok": False, "error": "calendar_disconnected"}
        day = args.get("day") or "today"
        try:
            events = gcal.events(day)
        except KeyError as e:
            return {"ok": False, "error": "bad_day", "detail": e.args[0]}
        return {"ok": True, "day": day, "events": [
            {"when": spoken_when(e["start"]), "title": e["title"],
             **({"location": e["location"]} if e.get("location") else {}),
             **({"description": e["description"]} if e.get("description") else {})}
            for e in events]}
    if name == "gcal_create_event":
        if gcal is None or not gcal.connected:
            return {"ok": False, "error": "calendar_disconnected"}
        try:
            created = gcal.create_event(
                args["title"], args.get("day") or "today",
                args.get("time", ""), int(args.get("duration") or 60),
                all_day=args.get("all_day"),
                location=args.get("location", ""),
                description=args.get("description", ""),
                attendees=args.get("attendees") or None)
        except KeyError as e:
            return {"ok": False, "error": "bad_day", "detail": e.args[0]}
        return {"ok": True, "created": args["title"], "id": created.get("id", ""),
                "day": args.get("day") or "today",
                "time": args.get("time") or "all day"}
    if name in ("gcal_find_event", "gcal_edit_event", "gcal_delete_event"):
        if gcal is None or not gcal.connected:
            return {"ok": False, "error": "calendar_disconnected"}
        day = args.get("day") or ""
        try:
            hit = gcal.find_events(args["name"], day)[0]
        except KeyError as e:
            return event_error(e)
        if name == "gcal_find_event":
            return {"ok": True,
                    "title": hit.get("summary", "(untitled)"),
                    "when": spoken_when(hit["start"].get("dateTime")
                                        or hit["start"].get("date", "")),
                    "location": hit.get("location", ""),
                    "description": hit.get("description", ""),
                    "attendees": [a.get("email", "") for a in hit.get("attendees", [])]}
        if name == "gcal_edit_event":
            body = _edit_event_body(args, hit, gcal.timezone)
            if body is None:
                return {"ok": False, "error": "nothing_to_edit"}
            gcal.update_event(hit["id"], **body)
            title = body.get("summary", hit.get("summary", "(untitled)"))
            out = {"ok": True, "edited": title}
            if "start" in body:
                out["moved_to"] = spoken_when(body["start"].get("dateTime")
                                              or body["start"].get("date", ""))
            return out
        gcal.delete_event(hit["id"])
        return {"ok": True, "deleted": hit.get("summary", "(untitled)")}
    raise NotImplementedError(f"unknown tool '{name}'")


def spoken_when(start: str) -> str:
    """An event start as spoken words: 'all day' or a 24-hour HH:MM."""
    if "T" not in start:
        return "all day"  # a bare YYYY-MM-DD date from the all-day shape
    try:
        return datetime.fromisoformat(start).strftime("%H:%M")
    except ValueError:
        return start


def _edit_event_body(args: dict, hit: dict, tz_name: str) -> dict | None:
    """The PATCH body for an edit: only what was asked. A day/time change
    sends the full new start/end pair (timed keeps its duration; all-day
    moves to date + next-day date) so Google never silently clears the end."""
    body: dict = {}
    if args.get("new_title"):
        body["summary"] = args["new_title"]
    if args.get("location"):
        body["location"] = args["location"]
    if args.get("description"):
        body["description"] = args["description"]
    if not (args.get("new_day") or args.get("new_time")):
        return body or None
    all_day = "date" in hit["start"]
    duration = int(args.get("duration") or 60)
    if all_day and not args.get("new_time"):
        day = parse_day(args.get("new_day") or "today", ZoneInfo(tz_name))
        body["start"] = {"date": day.date().isoformat()}
        body["end"] = {"date": (day + timedelta(days=1)).date().isoformat()}
        return body
    # timed: keep the existing start time unless a new one was said
    base = hit["start"].get("dateTime") or hit["start"].get("date", "")
    start = datetime.fromisoformat(base)
    if args.get("new_day"):
        day = parse_day(args["new_day"], ZoneInfo(tz_name))
        start = day.replace(hour=start.hour, minute=start.minute,
                            second=0, microsecond=0)
    if args.get("new_time"):
        hour, minute = parse_time(args["new_time"])
        start = start.replace(hour=hour, minute=minute, second=0, microsecond=0)
    end = start + timedelta(minutes=duration)
    body["start"] = {"dateTime": start.isoformat(), "timeZone": tz_name}
    body["end"] = {"dateTime": end.isoformat(), "timeZone": tz_name}
    return body


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
          clients: list[tuple[str, Trello]] | None = None,
          gcal: Gcal | None = None) -> str:
    """Send the sentence to the LLM; the LLM picks tools, the server keeps
    resolve_target as the fail-loud authority on where cards go. Echoes only
    while no LLM backend is configured (Phase 1 fallback)."""
    clients = clients if clients is not None else [(None, t or trello)]
    if not (LLM_BASE_URL and LLM_API_KEY and LLM_MODEL):
        return f"echo: {text}"
    messages = [
        {"role": "system",
         "content": build_system_prompt(clients=clients, gcal=gcal)},
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
                    "tools": tools_for(gcal),
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
                result = execute_tool(call["function"]["name"], args,
                                      clients=clients, gcal=gcal)
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


def _find_or_404(t: Trello, board: str, name: str, list_name: str = "") -> dict:
    try:
        return t.find_card(board, name, list_name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/card")
def one_card(board: str, name: str, list: str = "",
             authorization: str = Header(default="")) -> dict:
    _, t = _client_or_409(authorization)
    try:
        return t.card_details(board, name, list)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.patch("/card")
def patch_card(patch: CardPatchIn, authorization: str = Header(default="")) -> dict:
    _, t = _client_or_409(authorization)
    card = _find_or_404(t, patch.board, patch.name, patch.list)
    fields: dict = {}
    if patch.new_name:
        fields["name"] = patch.new_name
    if patch.desc:
        fields["desc"] = patch.desc
    if patch.archive:
        fields["closed"] = "true"
    if patch.to_list:
        _, list_id = resolve_target(t.boards, t.lists_by_board,
                                    patch.to_board or patch.board, patch.to_list)
        fields["idList"] = list_id
        if patch.to_board:
            fields["idBoard"] = t._board_id(patch.to_board)
    if patch.due_day:
        day = parse_day(patch.due_day, timezone.utc).date()
        due = day.isoformat()
        if patch.due_time:
            hh, mm = parse_time(patch.due_time)
            due += f"T{hh:02d}:{mm:02d}:00"
        else:
            due += "T12:00:00"  # noon: a midnight due renders as the wrong day
        fields["due"] = due
    if not fields:
        raise HTTPException(status_code=400, detail="nothing to change")
    updated = t.update_card(card["id"], **fields)
    return {"id": updated.get("id", card["id"]), "name": updated.get("name", card["name"])}


@app.post("/card/comment")
def comment_card(comment: CommentIn, authorization: str = Header(default="")) -> dict:
    _, t = _client_or_409(authorization)
    card = _find_or_404(t, comment.board, comment.name, comment.list)
    t.add_comment(card["id"], comment.text)
    return {"id": card["id"], "commented": comment.text}


@app.post("/list")
def new_list(list_in: ListIn, authorization: str = Header(default="")) -> dict:
    _, t = _client_or_409(authorization)
    try:
        created = t.create_list(list_in.board, list_in.name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"id": created["id"], "name": created["name"]}


@app.patch("/list")
def patch_list(patch: ListPatchIn, authorization: str = Header(default="")) -> dict:
    _, t = _client_or_409(authorization)
    try:
        t.rename_list(patch.board, patch.list, patch.new_name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"board": patch.board, "list": patch.new_name}


@app.get("/board/{board}/overview")
def board_overview(board: str, authorization: str = Header(default="")) -> dict:
    _, t = _client_or_409(authorization)
    try:
        return t.board_overview(board)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


# --- calendar REST: same find-by-name rules as the event voice tools ---

class CalendarPickIn(BaseModel):
    id: str


def _gcal_or_409(authorization: str) -> tuple[dict, Gcal]:
    user = require_user(authorization)
    try:
        g = gcal_for(user)
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="google calendar unreachable")
    if g is None or not g.connected:
        raise HTTPException(status_code=409,
                            detail="google calendar not connected for this user")
    return user, g


def _choose_calendar(user: dict, g: Gcal, wanted: str) -> str:
    """Validate `wanted` against the live calendarList, persist it (adopting
    its timezone) and drop the cached client so the next use re-reads it.
    Shared by the bearer and dashboard picker routes. Returns the timezone."""
    wanted = wanted.strip()
    if not wanted:
        raise HTTPException(status_code=422, detail="calendar id is required")
    try:
        entries = g.calendars()
    except (GcalDisconnected, requests.RequestException):
        raise HTTPException(
            status_code=409, detail="couldn't check your calendars — try again")
    entry = next((e for e in entries if e["id"] == wanted), None)
    if entry is None:
        raise HTTPException(
            status_code=404, detail="that calendar is not on this Google account")
    if entry["accessRole"] == "freeBusyReader":
        raise HTTPException(
            status_code=400, detail="that calendar is free/busy only — pick one with events")
    try:
        tz = g._get(f"/calendars/{wanted}").get("timeZone") or g.timezone
    except (GcalDisconnected, requests.RequestException):
        tz = g.timezone  # rare: keep the account timezone rather than failing
    store.save_google_calendar(user["id"], wanted, tz)
    _gcal.pop(user["id"], None)
    return tz


@app.get("/events")
def list_events(day: str = "", authorization: str = Header(default="")) -> dict:
    _, g = _gcal_or_409(authorization)
    try:
        return {"day": day or "today",
                "events": [{"when": spoken_when(e["start"]), "title": e["title"],
                            **({"location": e["location"]} if e.get("location") else {}),
                            **({"description": e["description"]} if e.get("description") else {})}
                           for e in g.events(day or "today")]}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/event")
def one_event(day: str = "", name: str = "",
              authorization: str = Header(default="")) -> dict:
    _, g = _gcal_or_409(authorization)
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    try:
        return g.event_details(name, day)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/event")
def new_event(event_in: EventIn, authorization: str = Header(default="")) -> dict:
    _, g = _gcal_or_409(authorization)
    try:
        created = g.create_event(event_in.title, event_in.day or "today",
                                 event_in.time, event_in.duration_min,
                                 all_day=event_in.all_day,
                                 location=event_in.location,
                                 description=event_in.description,
                                 attendees=event_in.attendees or None)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"id": created.get("id", ""), "title": created.get("summary", event_in.title)}


@app.patch("/event")
def patch_event(patch: EventPatchIn, authorization: str = Header(default="")) -> dict:
    _, g = _gcal_or_409(authorization)
    try:
        hit = g.find_events(patch.name, patch.day)[0]
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    body = _edit_event_body(patch.model_dump(), hit, g.timezone)
    if body is None:
        raise HTTPException(status_code=400, detail="nothing to change")
    updated = g.update_event(hit["id"], **body)
    return {"id": updated.get("id", hit["id"]),
            "title": updated.get("summary", hit.get("summary", ""))}


@app.delete("/event")
def remove_event(day: str = "", name: str = "",
                 authorization: str = Header(default="")) -> dict:
    _, g = _gcal_or_409(authorization)
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    try:
        hit = g.find_events(name, day)[0]
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    g.delete_event(hit["id"])
    return {"id": hit["id"], "deleted": hit.get("summary", "")}


@app.get("/calendars")
def api_calendars(authorization: str = Header(default="")) -> dict:
    """The account's calendar list plus the current default."""
    _, g = _gcal_or_409(authorization)
    try:
        items = g.calendars()
    except (GcalDisconnected, requests.RequestException):
        raise HTTPException(
            status_code=409, detail="couldn't list your calendars — try again")
    return {"current": g.calendar_id, "calendars": items}


@app.post("/calendar")
def api_choose_calendar(pick: CalendarPickIn,
                        authorization: str = Header(default="")) -> dict:
    """Set the default calendar the event tools act on."""
    user, g = _gcal_or_409(authorization)
    tz = _choose_calendar(user, g, pick.id)
    acc = store.get_google_account(user["id"])
    return {"current": acc["calendar_id"], "timezone": acc["timezone"] or tz}


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


class AdminIn(BaseModel):
    username: str
    admin: bool


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
    is_admin = bool(user["is_admin"])
    state = {
        "username": user["username"],
        "connected": bool(accounts) and bool(accounts[0]["boards"]),
        "accounts": accounts,
        # first-account shape, kept for anything still reading "boards"
        "boards": accounts[0]["boards"] if accounts else {},
        "command_url": COMMAND_URL,
        "token": user["api_token"],
        "is_admin": is_admin,
        "invite_code": INVITE_CODE if is_admin else "",
        "google_configured": google_configured(),
        "authorize_url": (
            "https://trello.com/1/authorize?expiration=30days&name=Watch+Bridge"
            f"&scope=read,write&response_type=token&key={TRELLO_KEY}"
        ),
    }
    if google_configured():
        try:
            g = gcal_for(user)
            state["calendar"] = {
                "connected": g.connected, "email": g.email,
                "summary": g.summary, "timezone": g.timezone,
                "calendar_id": g.calendar_id,
            }
        except (GcalDisconnected, requests.RequestException):
            state["calendar"] = {"connected": False, "email": "", "summary": "",
                                 "timezone": "", "calendar_id": "primary"}
    if is_admin:
        state["users"] = [
            {"username": u["username"], "is_admin": bool(u["is_admin"]),
             "created": u["created_at"]}
            for u in store.list_users()
        ]
    return state


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


FOOTER = """<p class="foot">
 <a href="/privacy">privacy</a> ·
 <a href="https://github.com/GlassOnTin/watch-llm-bridge">source on GitHub</a> ·
 <a href="/health">status</a>
</p>"""

PRIVACY_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Privacy — Watch Bridge</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{font-family:Georgia,serif;max-width:40rem;margin:3rem auto;
padding:0 1rem;line-height:1.6}h1{font-size:1.4rem}
.foot{margin-top:2.5rem;font-size:.8rem}
.foot a{color:#666}</style></head><body>
<h1>Privacy</h1>
<p>This is a private, personal voice-assistant bridge. It is not a commercial
product and has no analytics or advertising.</p>
<p>What it stores: your username and a salted hash of your password, the
tokens you connect (Trello, Google Calendar), your calendar's timezone, and
a log of the commands you send. Commands are relayed to the LLM backend
configured by the operator to decide what to do, and actions are carried out
on the services you connected.</p>
<p>What it never does: load remote content in your browser, track you, or
share your data with anyone else. Google is told only that this app may read
and add events on your calendars; you choose which one is the default in the
dashboard. Disconnecting a service from the dashboard deletes its stored
token (and revokes the Google grant).</p>
<p>Operator contact: the person who gave you your invite code.</p>
""" + FOOTER + """
</body></html>"""


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page() -> str:
    return PRIVACY_HTML


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


# --- Google Calendar OAuth: consent redirect, callback, disconnect ----------

# user id -> (state, created); the callback consumes it once. In-memory like
# _attempts — a restart just means the user clicks Connect again.
_google_state: dict[int, tuple[str, float]] = {}


def google_configured() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def google_start_url(state: str) -> str:
    return GOOGLE_AUTH_URL + "?" + urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": GCAL_SCOPE,
        "access_type": "offline",   # a refresh token must come back
        "prompt": "consent",        # re-issue it even for repeat connections
        "state": state,
    })


@app.get("/app/google/start")
def google_start(request: Request):
    user = require_session(request)
    if not google_configured():
        raise HTTPException(status_code=404, detail="calendar not configured")
    state = secrets.token_urlsafe(16)
    _google_state[user["id"]] = (state, time.time())
    return RedirectResponse(google_start_url(state), status_code=303)


@app.get("/app/google/callback")
def google_callback(request: Request, code: str = "", state: str = "",
                    error: str = ""):
    user = require_session(request)

    def back(reason: str):
        return RedirectResponse(f"/app?google_err={quote(reason)}", status_code=303)

    held = _google_state.pop(user["id"], (None, 0.0))  # single use
    if error:
        return back("Google denied the connection"
                    if error == "access_denied" else f"Google error: {error}")
    if state != held[0] or time.time() - held[1] > 600:
        return back("that connection attempt expired — try again")
    if not code:
        return back("no authorization code came back from Google")
    try:
        r = requests.post(GOOGLE_TOKEN_URL, data={
            "code": code, "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": GOOGLE_REDIRECT_URI,
            "grant_type": "authorization_code"}, timeout=15)
        r.raise_for_status()
        payload = r.json()
        access, refresh = payload["access_token"], payload["refresh_token"]
    except (requests.RequestException, KeyError):
        return back("Google refused the token exchange — try connecting again")
    tz = "UTC"
    try:
        r = requests.get(f"{GCAL_API}/users/me/settings/timezone", headers={
            "Authorization": f"Bearer {access}"}, timeout=15)
        r.raise_for_status()
        tz = r.json()["value"]
    except (requests.RequestException, KeyError):
        pass  # rare: keep UTC rather than failing the whole connect
    store.save_google_account(user["id"], access, refresh,
                              time.time() + payload.get("expires_in", 3600), tz)
    _gcal.pop(user["id"], None)
    return RedirectResponse("/app?google=connected", status_code=303)


@app.post("/app/google/disconnect")
def google_disconnect(request: Request) -> dict:
    user = require_session(request)
    account = store.get_google_account(user["id"])
    if account:
        try:  # best effort: drop the grant server-side too
            requests.post("https://oauth2.googleapis.com/revoke",
                          data={"token": account["refresh_token"]}, timeout=15)
        except requests.RequestException:
            pass
    store.delete_google_account(user["id"])
    _gcal.pop(user["id"], None)
    return app_state(store.get_user(user["id"]))


# --- default-calendar picker --------------------------------------------------
# Voice commands act on one calendar per user. Until they pick one it is
# "primary"; the choice is stored with the grant and survives reconnects.

def _live_gcal(request: Request) -> tuple[dict, Gcal]:
    """A signed-in user with a working grant; 409 with a plain reason else."""
    user = require_session(request)
    try:
        g = gcal_for(user)
    except (GcalDisconnected, requests.RequestException):
        g = Gcal(user["id"])  # no grant / grant died: treat as unconnected
    if not g.connected:
        raise HTTPException(status_code=409,
                            detail="connect your Google Calendar first")
    return user, g


@app.get("/app/google/calendars")
def google_calendars(request: Request) -> dict:
    user, g = _live_gcal(request)
    try:
        items = g.calendars()
    except (GcalDisconnected, requests.RequestException):
        raise HTTPException(
            status_code=409, detail="couldn't list your calendars — try again")
    return {"current": g.calendar_id, "calendars": items}


@app.post("/app/google/calendar")
def choose_google_calendar(request: Request, body: CalendarPickIn) -> dict:
    """Set the default calendar. The id must be one of the account's own
    calendars; free/busy-only ones are refused because no event could ever
    be read from or written to them."""
    user, g = _live_gcal(request)
    _choose_calendar(user, g, body.id)
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


@app.post("/app/admin")
def set_user_admin(request: Request, body: AdminIn) -> dict:
    """Promote or demote a user. Admins may not change their own flag, so the
    last admin can never lock themselves out."""
    actor = require_session(request)
    if not actor["is_admin"]:
        raise HTTPException(status_code=403, detail="admin only")
    target = store.get_user_by_name(body.username.strip().lower())
    if not target:
        raise HTTPException(status_code=404, detail="no such user")
    if target["id"] == actor["id"]:
        raise HTTPException(status_code=400, detail="you cannot change your own admin status")
    store.set_admin(target["id"], body.admin)
    # the actor's own state: the user list must refresh under the admin's view
    return app_state(store.get_user(actor["id"]))


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
 input,select{width:100%;background:var(--code);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:10px 12px;font-family:var(--mono);font-size:.95rem;margin:6px 0}
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
 .foot{margin-top:2.5rem;font-size:.8rem;text-align:center}
 .foot a{color:var(--muted)}
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
""" + FOOTER + """</main></body></html>"""

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
</script>""" + FOOTER + """</main></body></html>"""

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
</script>""" + FOOTER + """</main></body></html>"""

DASHBOARD_HTML = STYLE + """<div class="top"><h1>Watch Bridge</h1><span><span id="who" class="sub"></span> · <button class="gear" id="gearbtn" title="Settings">⚙</button> · <a href="/logout">log out</a></span></div>
<details id="trello" class="dcard"><summary id="trellos"></summary><div class="inner" id="trellobody"></div></details>
<details id="cald" class="dcard"><summary id="cals"></summary><div class="inner" id="calbody"></div></details>
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
  <li>“move the milk card to done” · “move it to done on plans” for a cross-board move</li>
  <li>“what's on the bin day card” — description, due date, labels, last comments</li>
  <li>“put a due date on bin day tomorrow” — or “...tomorrow at 4”</li>
  <li>“rename the chores list to jobs” · “what's on my home board”</li>
  <li>With more than one account connected: “add milk to shopping on my work trello”</li>
  <li>Once your calendar is connected: “what's on my calendar today” and “put
      dentists appointment in the calendar tomorrow at 3”</li>
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
  <li>Read one card's details and comments — <code>GET /1/cards/{id}</code></li>
  <li>Move, rename, describe, date, archive and comment on a card — <code>PUT /1/cards/{id}</code></li>
  <li>Create and rename lists — <code>POST /1/lists</code>, <code>PUT /1/lists/{id}</code></li>
 </ul>
 <p><b>What Calendar supports today</b></p>
 <ul>
  <li>Read a day's events — “today”, “tomorrow”, or a date</li>
  <li>Read one event's details — who's invited, where, the description</li>
  <li>Find an event by name — “the dentist thing next week”</li>
  <li>Edit or move an event — rename, new day, new time</li>
  <li>Delete an event — after the agent confirms with you</li>
  <li>Create an event, timed or all-day — your calendar's own timezone is used</li>
  <li>All of the above happen on the default calendar you pick in the Calendar
      section above (primary until you change it)</li>
 </ul>
 <p class="sub">Closing a card is an archive, so it can be brought back; a calendar event is
 deleted for real, which is why the agent asks before it deletes. The agent is only given
 the tools above, so it says it can't do the rest rather than improvising.</p>
</div></details>
<details id="invited" class="dcard"><summary>✉ Invite someone</summary><div class="inner" id="invite"></div></details>
<details id="admind" class="dcard"><summary>✧ Admin</summary><div class="inner" id="admin"></div></details>
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
function calendarCard(d){
 if(!d.google_configured)return '';
 if(!d.calendar||!d.calendar.connected)
  return `<p class="sub">Connect your Google Calendar to ask “what's on today” and add events
  by voice. Google shows a consent page; only your calendar events are touched.</p>
  <a class="btn" href="/app/google/start">Connect Google Calendar</a><span id="calerr"></span>`;
 const name=d.calendar.summary||d.calendar.email||'primary calendar';
 return `<p class="sub"><span class="ok">connected</span> — ${esc(name)}
  · ${esc(d.calendar.timezone)}</p>
  <div class="kv"><span class="k">Default</span><span class="v" id="calcur">${esc(name)}</span></div>
  <details ontoggle="loadCalendars()"><summary>Change the default calendar</summary>
   <p class="sub">Everything the agent does on your calendar — reads, creates, edits,
   deletes — happens on this calendar. “Primary” is the one named after your account.</p>
   <div id="callist" class="sub">loading…</div>
  </details>
  <button class="sec" id="caldisc">Disconnect</button><span id="calerr"></span>`;
}
async function loadCalendars(){
 const box=document.getElementById('callist');
 if(!box||box.dataset.loaded)return;
 const r=await fetch('/app/google/calendars');
 if(!r.ok){box.textContent='could not load your calendars — try again';return}
 const d=await r.json();
 if(!d.calendars.length){box.textContent='no calendars found on this account';return}
 box.innerHTML=`<select id="calpick">${d.calendars.map(c=>
  `<option value="${escA(c.id)}"${c.id===d.current?' selected':''}>${esc(c.summary||c.id)}${c.accessRole==='reader'?' (read-only)':''}</option>`).join('')}
 </select><span class="sub" id="calpickerr"></span>`;
 box.dataset.loaded='1';
 const sel=document.getElementById('calpick'),err=document.getElementById('calpickerr');
 sel.onchange=async()=>{
  err.textContent='';
  const rr=await fetch('/app/google/calendar',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({id:sel.value})});
  if(rr.ok)render(await rr.json());
  else{const dd=await rr.json().catch(()=>({detail:'error '+rr.status}));err.textContent=dd.detail||'error '+rr.status;}
 };
}
async function disconnectGoogle(){
 if(!confirm('Disconnect your Google Calendar?'))return;
 const r=await fetch('/app/google/disconnect',{method:'POST'});
 if(r.ok)render(await r.json());
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
function adminCard(d){
 const rows=d.users.map(u=>`
  <div class="kv"><span class="k">${esc(u.username)}${u.is_admin?' ✦':''}</span>
   <span class="v" style="color:var(--muted)">${esc(u.created.slice(0,10))}${u.is_admin?' · admin':''}</span>
   ${u.username===d.username?'':`<button class="sec" data-user="${escA(u.username)}" data-to="${u.is_admin?0:1}">${u.is_admin?'Revoke admin':'Make admin'}</button>`}
  </div>`).join('');
 return `<p class="sub">Admins see the invite code and can make other users admin. You cannot change your own status.</p>${rows}`;
}
async function setAdmin(username,to){
 if(!confirm((to?'Make ':'Revoke admin for ')+'"'+username+'"?'))return;
 const r=await fetch('/app/admin',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({username,admin:to})});
 if(!r.ok)return;
 render(await r.json());
}
function render(d){
 document.getElementById('who').textContent=d.username;
 document.getElementById('trellos').textContent=!d.accounts.length
  ?'🔮 Trello — connect your account'
  :d.accounts.length===1?`🔮 Trello — ${Object.keys(d.boards).length} boards`
  :`🔮 Trello — ${d.accounts.length} accounts`;
 document.getElementById('trellobody').innerHTML=trelloCard(d);
 document.getElementById('trello').open=!d.connected;
 document.getElementById('cals').textContent=!d.google_configured?'':(!d.calendar||!d.calendar.connected)
  ?'📅 Calendar — connect Google':'📅 Calendar — connected';
 document.getElementById('calbody').innerHTML=calendarCard(d);
 document.getElementById('cald').style.display=d.google_configured?'':'none';
 document.getElementById('cald').open=d.google_configured&&(!d.calendar||!d.calendar.connected);
 const cd=document.getElementById('caldisc');
 if(cd)cd.onclick=disconnectGoogle;
 const q=new URLSearchParams(location.search), ce=document.getElementById('calerr');
 if(ce&&q.get('google_err'))ce.textContent=q.get('google_err');
 if(ce&&q.get('google')==='connected'){ce.className='ok';ce.textContent='calendar connected'}
 if(q.get('google')||q.get('google_err'))history.replaceState(null,'','/app');
 document.getElementById('watch').innerHTML=recipe(d);
 document.getElementById('invite').innerHTML=inviteCard(d);
 document.getElementById('invited').style.display=d.invite_code?'':'none';
 const ib=document.getElementById('invbtn');
 if(ib)ib.onclick=e=>copy(e.target,d.invite_code);
 document.getElementById('admin').innerHTML=adminCard(d);
 document.getElementById('admind').style.display=d.is_admin?'':'none';
}
(async()=>{render(await (await fetch('/app/state')).json())})();
document.getElementById('watch').addEventListener('click',e=>{
 const b=e.target.closest('button.sec'); if(b&&b.dataset.v!==undefined)copy(b,b.dataset.v)});
document.getElementById('trellobody').addEventListener('click',e=>{
 const b=e.target.closest('button.sec'); if(b&&b.dataset.acc!==undefined)removeAccount(b.dataset.acc)});
document.getElementById('admin').addEventListener('click',e=>{
 const b=e.target.closest('button.sec'); if(b&&b.dataset.user!==undefined)setAdmin(b.dataset.user,b.dataset.to==='1')});
document.getElementById('gearbtn').onclick=()=>{
 const s=document.getElementById('settings'); s.open=true; s.scrollIntoView({behavior:'smooth'})};
document.addEventListener('keydown',e=>{if(e.key==='Enter'&&e.target.id==='pwcur')pw()});
</script>""" + FOOTER + """</main></body></html>"""


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
def provision_page(request: Request):
    if not TOTP_SECRET:
        raise HTTPException(status_code=404, detail="provisioning disabled")
    if not session_user(request):
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(PROVISION_HTML)


@app.post("/provision/verify")
def provision_verify(req: Request, body: TotpIn) -> dict:
    """TOTP-gated reveal of the signed-in user's own watch recipe. The code
    proves physical presence; the session decides whose token is revealed."""
    if not TOTP_SECRET:
        raise HTTPException(status_code=404, detail="provisioning disabled")
    user = require_session(req)
    if rate_limited(req.client.host):
        raise HTTPException(status_code=429, detail="too many attempts")
    ok = pyotp.TOTP(TOTP_SECRET).verify(body.code.strip().replace(" ", ""), valid_window=1)
    if not ok:
        raise HTTPException(status_code=401, detail="bad code")
    return {"command_url": COMMAND_URL, "token": user["api_token"]}

PROVISION_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Watch Bridge — provisioning</title>
<style>
 :root{--bg:#0d1117;--panel:#161b22;--border:#2d333b;--text:#e6edf3;--muted:#8b949e;--accent:#4fb3ff;--green:#3fb950;--code:#0a0d12;--mono:ui-monospace,Menlo,monospace}
 *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,"Segoe UI",Roboto,sans-serif;line-height:1.6}
 main{max-width:640px;margin:0 auto;padding:48px 20px 80px}
 h1{font-size:1.4rem;margin:0 0 4px}.sub{color:var(--muted)}
 .foot{margin-top:2.5rem;font-size:.8rem;text-align:center}
 .foot a{color:var(--muted)}
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
<p class="sub">Enter the 6-digit code from Haven on your phone. The page then shows your
shortcut recipe and your own bearer token — pair each person's watch from their
own sign-in, so commands land on their calendar and boards.</p>
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
</script>""" + FOOTER + """</main></body></html>"""
