"""Watch→LLM Bridge server.

Phase 1 (this file): HTTPS transport, bearer auth, echo routing, and the
Trello tools fully wired against the live API.
Phase 2: `route()` is replaced by an LLM that selects among the tools.

Run:
    set -a; source ../.env; set +a     # or export the vars directly
    uvicorn app:app --host 0.0.0.0 --port 8000
"""
import os

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()  # reads .env from the CWD if present; real env vars win

BRIDGE_TOKEN = os.environ["BRIDGE_TOKEN"]
TRELLO_KEY = os.environ["TRELLO_KEY"]
TRELLO_TOKEN = os.environ["TRELLO_TOKEN"]
PAGES_ORIGIN = os.environ.get("PAGES_ORIGIN", "https://glassontin.github.io")

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