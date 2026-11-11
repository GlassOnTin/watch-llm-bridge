"""SQLite user store: registration, scrypt password hashes, per-user bearer
tokens, and each user's Trello token (authorized under the server's shared
TRELLO_KEY). One table, no ORM — this box runs one process.

The DB path is `bridge.db` in the CWD unless BRIDGE_DB is set. DEPLOY.sh
excludes *.db from its rsync so a deploy can never wipe it.
"""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone

DB_PATH = os.environ.get("BRIDGE_DB", os.path.join(os.getcwd(), "bridge.db"))
DEFAULT_DEV_PROJECT = "watch-llm-bridge"
DEFAULT_GEMINI_LIVE_MODEL = "models/gemini-3.8-live"
PROJECT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  api_token TEXT UNIQUE NOT NULL,
  trello_token TEXT,
  is_admin INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trello_accounts (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  label TEXT NOT NULL,
  token TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(user_id, label)
);

CREATE TABLE IF NOT EXISTS google_accounts (
  user_id INTEGER PRIMARY KEY,
  access_token TEXT NOT NULL,
  refresh_token TEXT NOT NULL,
  expires_at REAL NOT NULL,
  timezone TEXT NOT NULL,
  calendar_id TEXT NOT NULL DEFAULT 'primary'
);

CREATE TABLE IF NOT EXISTS ha_instances (
  user_id INTEGER PRIMARY KEY,
  url TEXT NOT NULL,
  token TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gemini_keys (
  user_id INTEGER PRIMARY KEY,
  api_key TEXT NOT NULL,
  model TEXT NOT NULL DEFAULT 'models/gemini-3.8-live',
  voice TEXT NOT NULL DEFAULT 'Puck',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS converse_turns (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS dev_state (
  user_id INTEGER PRIMARY KEY,
  active_project TEXT NOT NULL DEFAULT 'watch-llm-bridge',
  conversation_id TEXT NOT NULL DEFAULT '',
  worker_seen_at REAL NOT NULL DEFAULT 0,
  worker_host TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS dev_projects (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  slug TEXT NOT NULL,
  name TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  conversation_id TEXT NOT NULL DEFAULT '',
  started_via_watch INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(user_id, slug)
);

CREATE TABLE IF NOT EXISTS dev_proposals (
  user_id INTEGER PRIMARY KEY,
  slug TEXT NOT NULL,
  name TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  initial_task TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS dev_turns (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  project_slug TEXT NOT NULL DEFAULT 'watch-llm-bridge',
  init_project INTEGER NOT NULL DEFAULT 0,
  delete_project INTEGER NOT NULL DEFAULT 0,
  prompt TEXT NOT NULL,
  new_thread INTEGER NOT NULL DEFAULT 0,
  conversation_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  reply TEXT NOT NULL DEFAULT '',
  raw_reply TEXT NOT NULL DEFAULT '',
  timed_out INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  completed_at TEXT NOT NULL DEFAULT ''
)
"""

_conn: sqlite3.Connection | None = None
_dev_lock = threading.Lock()


def connect(path: str | None = None) -> sqlite3.Connection:
    """One process-wide connection; WAL so a stray sqlite3 CLI read is safe."""
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(path or DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
        # SQLite can't add a column IF NOT EXISTS; patch live DBs created
        # before admins existed, and before a default calendar was choosable.
        cols = {row[1] for row in _conn.execute("PRAGMA table_info(users)")}
        if "is_admin" not in cols:
            _conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
        gcols = {row[1] for row in _conn.execute("PRAGMA table_info(google_accounts)")}
        if gcols and "calendar_id" not in gcols:
            _conn.execute("ALTER TABLE google_accounts ADD COLUMN "
                          "calendar_id TEXT NOT NULL DEFAULT 'primary'")
        dcols = {row[1] for row in _conn.execute("PRAGMA table_info(dev_state)")}
        if dcols and "active_project" not in dcols:
            _conn.execute("ALTER TABLE dev_state ADD COLUMN "
                          "active_project TEXT NOT NULL DEFAULT 'watch-llm-bridge'")
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for srow in _conn.execute("SELECT user_id, conversation_id FROM dev_state").fetchall():
                _conn.execute(
                    "INSERT INTO dev_projects (user_id, slug, name, summary, conversation_id, "
                    "started_via_watch, created_at, updated_at) VALUES (?, ?, ?, '', ?, 0, ?, ?) "
                    "ON CONFLICT(user_id, slug) DO UPDATE SET conversation_id=excluded.conversation_id",
                    (srow["user_id"], DEFAULT_DEV_PROJECT, "Watch LLM Bridge",
                     srow["conversation_id"], now, now),
                )
        tcols = {row[1] for row in _conn.execute("PRAGMA table_info(dev_turns)")}
        if tcols and "project_slug" not in tcols:
            _conn.execute("ALTER TABLE dev_turns ADD COLUMN "
                          "project_slug TEXT NOT NULL DEFAULT 'watch-llm-bridge'")
        if tcols and "init_project" not in tcols:
            _conn.execute("ALTER TABLE dev_turns ADD COLUMN "
                          "init_project INTEGER NOT NULL DEFAULT 0")
        if tcols and "delete_project" not in tcols:
            _conn.execute("ALTER TABLE dev_turns ADD COLUMN "
                          "delete_project INTEGER NOT NULL DEFAULT 0")
        if tcols and "raw_reply" not in tcols:
            _conn.execute("ALTER TABLE dev_turns ADD COLUMN "
                          "raw_reply TEXT NOT NULL DEFAULT ''")
        _conn.commit()
    return _conn


def _row_to_user(row: sqlite3.Row) -> dict:
    return dict(row)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    salt_hex, _, digest_hex = stored.partition("$")
    if not salt_hex or not digest_hex:
        return False
    try:
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
    except ValueError:
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def new_api_token() -> str:
    return secrets.token_hex(32)


def create_user(username: str, password: str, api_token: str | None = None,
                trello_token: str | None = None, is_admin: bool = False) -> dict:
    """Insert a user; raises sqlite3.IntegrityError on duplicate username."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect() as db:
        cur = db.execute(
            "INSERT INTO users (username, password_hash, api_token, trello_token, "
            "is_admin, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (username, hash_password(password), api_token or new_api_token(),
             trello_token, 1 if is_admin else 0, now),
        )
    return get_user(cur.lastrowid)


def get_user(user_id: int) -> dict | None:
    row = connect().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _row_to_user(row) if row else None


def get_user_by_name(username: str) -> dict | None:
    row = connect().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return _row_to_user(row) if row else None


def get_user_by_token(api_token: str) -> dict | None:
    row = connect().execute("SELECT * FROM users WHERE api_token = ?", (api_token,)).fetchone()
    return _row_to_user(row) if row else None


def count_users() -> int:
    return connect().execute("SELECT COUNT(*) FROM users").fetchone()[0]


def list_users() -> list[dict]:
    """Dashboard-facing summary: no password hashes, no bearer tokens."""
    rows = connect().execute(
        "SELECT id, username, is_admin, created_at FROM users ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def set_admin(user_id: int, is_admin: bool) -> bool:
    with connect() as db:
        cur = db.execute("UPDATE users SET is_admin = ? WHERE id = ?",
                         (1 if is_admin else 0, user_id))
    return cur.rowcount > 0


def set_trello_token(user_id: int, trello_token: str) -> None:
    with connect() as db:
        db.execute("UPDATE users SET trello_token = ? WHERE id = ?", (trello_token, user_id))


# --- multiple Trello accounts per user -------------------------------------
# Each account is a (label, token) pair; `label` is what the user says to pick
# between them ("on my work trello"). The legacy single users.trello_token
# column is migrated into one account labelled "trello".

def add_account(user_id: int, label: str, token: str) -> dict:
    """Insert an account; raises sqlite3.IntegrityError on a duplicate label."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect() as db:
        db.execute(
            "INSERT INTO trello_accounts (user_id, label, token, created_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, label, token, now),
        )
    return get_account(user_id, label)


def accounts_for(user_id: int) -> list[dict]:
    rows = connect().execute(
        "SELECT * FROM trello_accounts WHERE user_id = ? ORDER BY id", (user_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_account(user_id: int, label: str) -> dict | None:
    row = connect().execute(
        "SELECT * FROM trello_accounts WHERE user_id = ? AND label = ?",
        (user_id, label),
    ).fetchone()
    return dict(row) if row else None


def delete_account(user_id: int, label: str) -> bool:
    with connect() as db:
        cur = db.execute(
            "DELETE FROM trello_accounts WHERE user_id = ? AND label = ?",
            (user_id, label),
        )
    return cur.rowcount > 0


def set_account_token(user_id: int, label: str, token: str) -> bool:
    """Rotate an existing account's token (a re-paste onto a name that
    already exists). created_at is kept: the account, and its role in the
    system prompt, survive; only the credential changes."""
    with connect() as db:
        cur = db.execute(
            "UPDATE trello_accounts SET token = ? WHERE user_id = ? AND label = ?",
            (token, user_id, label),
        )
    return cur.rowcount > 0


def migrate_accounts() -> None:
    """Copy each legacy users.trello_token into an account row labelled
    'trello'. Idempotent: users that already have account rows are skipped."""
    db = connect()
    legacy = db.execute(
        "SELECT id, trello_token FROM users "
        "WHERE trello_token IS NOT NULL AND trello_token != '' "
        "AND id NOT IN (SELECT DISTINCT user_id FROM trello_accounts)"
    ).fetchall()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db:
        for row in legacy:
            db.execute(
                "INSERT INTO trello_accounts (user_id, label, token, created_at) "
                "VALUES (?, 'trello', ?, ?)",
                (row["id"], row["trello_token"], now),
            )


def set_password(user_id: int, password: str) -> None:
    with connect() as db:
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                   (hash_password(password), user_id))


# --- Google Calendar OAuth, one connection per user --------------------------
# One row per user: the Google account is singular (unlike Trello's labelled
# accounts). expires_at is epoch seconds for the access token. calendar_id is
# the default calendar voice commands act on; it survives reconnects, since
# re-consenting the Google account should not reset the user's choice.

def save_google_account(user_id: int, access_token: str, refresh_token: str,
                        expires_at: float, tz: str) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO google_accounts (user_id, access_token, refresh_token, "
            "expires_at, timezone, calendar_id) VALUES (?, ?, ?, ?, ?, 'primary') "
            "ON CONFLICT(user_id) DO UPDATE SET access_token=excluded.access_token, "
            "refresh_token=excluded.refresh_token, expires_at=excluded.expires_at, "
            "timezone=excluded.timezone",
            (user_id, access_token, refresh_token, expires_at, tz),
        )


def save_google_calendar(user_id: int, calendar_id: str, tz: str) -> None:
    """Set the default calendar and adopt its timezone (timed events are
    written in that zone)."""
    with connect() as db:
        db.execute(
            "UPDATE google_accounts SET calendar_id = ?, timezone = ? "
            "WHERE user_id = ?",
            (calendar_id, tz, user_id),
        )


def invalidate_google_grant(user_id: int) -> None:
    """The OAuth grant died (revoked or expired server-side). Blank the
    tokens so the account reads as disconnected, but keep the row: the
    chosen default calendar survives until the user reconnects."""
    with connect() as db:
        db.execute("UPDATE google_accounts SET access_token = '', "
                   "refresh_token = '', expires_at = 0 WHERE user_id = ?",
                   (user_id,))


def get_google_account(user_id: int) -> dict | None:
    row = connect().execute(
        "SELECT * FROM google_accounts WHERE user_id = ?", (user_id,)
    ).fetchone()
    return dict(row) if row else None


def delete_google_account(user_id: int) -> bool:
    with connect() as db:
        cur = db.execute("DELETE FROM google_accounts WHERE user_id = ?", (user_id,))
    return cur.rowcount > 0


# --- Home Assistant, one connection per user ---------------------------------
# Like Google's single row: the home is singular. The token is a long-lived
# access token pasted from the user's HA profile; it never refreshes, so a 401
# means it was revoked and the user must paste a new one (re-saving replaces).

def save_ha_instance(user_id: int, url: str, token: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect() as db:
        db.execute(
            "INSERT INTO ha_instances (user_id, url, token, created_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET url=excluded.url, "
            "token=excluded.token, created_at=excluded.created_at",
            (user_id, url, token, now),
        )


def get_ha_instance(user_id: int) -> dict | None:
    row = connect().execute(
        "SELECT * FROM ha_instances WHERE user_id = ?", (user_id,)
    ).fetchone()
    return dict(row) if row else None


def delete_ha_instance(user_id: int) -> bool:
    with connect() as db:
        cur = db.execute("DELETE FROM ha_instances WHERE user_id = ?", (user_id,))
    return cur.rowcount > 0


# --- Gemini Live API key, one per user ---------------------------------------

def save_gemini_key(user_id: int, api_key: str,
                    model: str = DEFAULT_GEMINI_LIVE_MODEL,
                    voice: str = "Puck") -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect() as db:
        db.execute(
            "INSERT INTO gemini_keys (user_id, api_key, model, voice, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET api_key=excluded.api_key, "
            "model=excluded.model, voice=excluded.voice, created_at=excluded.created_at",
            (user_id, api_key.strip(), model.strip() or DEFAULT_GEMINI_LIVE_MODEL,
             voice.strip() or "Puck", now),
        )


def get_gemini_key(user_id: int) -> dict | None:
    row = connect().execute(
        "SELECT * FROM gemini_keys WHERE user_id = ?", (user_id,)
    ).fetchone()
    return dict(row) if row else None


def delete_gemini_key(user_id: int) -> bool:
    with connect() as db:
        cur = db.execute("DELETE FROM gemini_keys WHERE user_id = ?", (user_id,))
    return cur.rowcount > 0


# --- Multi-turn headless voice conversation history (uncapped turns) ---------

def append_converse_turn(user_id: int, user_text: str, assistant_text: str,
                         now: float | None = None) -> None:
    ts = time.time() if now is None else now
    with _dev_lock, connect() as db:
        db.execute(
            "INSERT INTO converse_turns (user_id, role, content, created_at) VALUES (?, 'user', ?, ?)",
            (user_id, user_text, ts),
        )
        db.execute(
            "INSERT INTO converse_turns (user_id, role, content, created_at) VALUES (?, 'assistant', ?, ?)",
            (user_id, assistant_text, ts),
        )
        # Keep SQLite lean over multi-hour sessions while never capping turns.
        db.execute(
            "DELETE FROM converse_turns WHERE user_id = ? AND id NOT IN ("
            "  SELECT id FROM converse_turns WHERE user_id = ? ORDER BY id DESC LIMIT 200"
            ")",
            (user_id, user_id),
        )


def get_converse_history(user_id: int, limit: int = 24,
                         now: float | None = None,
                         idle_ttl: float = 900.0) -> list[dict]:
    ts = time.time() if now is None else now
    with _dev_lock, connect() as db:
        last = db.execute(
            "SELECT created_at FROM converse_turns WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if not last:
            return []
        if ts - last["created_at"] > idle_ttl:
            db.execute("DELETE FROM converse_turns WHERE user_id = ?", (user_id,))
            return []
        rows = db.execute(
            "SELECT role, content FROM converse_turns WHERE user_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def clear_converse_history(user_id: int) -> None:
    with _dev_lock, connect() as db:
        db.execute("DELETE FROM converse_turns WHERE user_id = ?", (user_id,))


# --- Workstation dev worker queue (AGY / Carbon multi-turn conversation) -----

def slugify_project(name: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-_")
    if not PROJECT_SLUG_RE.match(slug):
        raise ValueError(f"invalid project name '{name}'")
    return slug


def _ensure_default_project(db: sqlite3.Connection, user_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    default_summary = (
        "Apple Watch voice assistant bridge (Trello, Google Calendar, "
        "Home Assistant, dashboard, voice tools)"
    )
    db.execute(
        "INSERT INTO dev_projects (user_id, slug, name, summary, conversation_id, "
        "started_via_watch, created_at, updated_at) "
        "VALUES (?, ?, 'Watch LLM Bridge', ?, '', 0, ?, ?) "
        "ON CONFLICT(user_id, slug) DO UPDATE SET "
        "summary = CASE WHEN dev_projects.summary = '' THEN excluded.summary "
        "ELSE dev_projects.summary END",
        (user_id, DEFAULT_DEV_PROJECT, default_summary, now, now),
    )


def list_dev_projects(user_id: int, watch_only: bool = False) -> list[dict]:
    with _dev_lock, connect() as db:
        _ensure_default_project(db, user_id)
        if watch_only:
            rows = db.execute(
                "SELECT * FROM dev_projects WHERE user_id = ? AND started_via_watch = 1 "
                "ORDER BY id ASC",
                (user_id,),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM dev_projects WHERE user_id = ? ORDER BY id ASC",
                (user_id,),
            ).fetchall()
    return [dict(r) for r in rows]


def list_dev_projects_detailed(user_id: int, query: str = "",
                               watch_only: bool = False) -> list[dict]:
    q = query.strip().lower()
    want_slug = re.sub(r"[^a-z0-9_-]+", "-", q).strip("-_") if q else ""
    with _dev_lock, connect() as db:
        _ensure_default_project(db, user_id)
        srow = db.execute(
            "SELECT active_project FROM dev_state WHERE user_id = ?", (user_id,)
        ).fetchone()
        active = (srow["active_project"] if srow else "") or DEFAULT_DEV_PROJECT

        sql = (
            "SELECT * FROM dev_projects WHERE user_id = ?"
            + (" AND started_via_watch = 1" if watch_only else "")
            + " ORDER BY CASE WHEN slug = ? THEN 0 ELSE 1 END, updated_at DESC, id ASC"
        )
        prows = db.execute(sql, (user_id, active)).fetchall()

        turn_counts = {
            r["project_slug"]: r["cnt"]
            for r in db.execute(
                "SELECT project_slug, COUNT(*) AS cnt FROM dev_turns "
                "WHERE user_id = ? AND delete_project = 0 GROUP BY project_slug",
                (user_id,),
            ).fetchall()
        }
        latest_by_slug: dict[str, sqlite3.Row] = {}
        for trow in db.execute(
            "SELECT project_slug, status, prompt, reply, completed_at, created_at "
            "FROM dev_turns WHERE user_id = ? AND delete_project = 0 ORDER BY id DESC",
            (user_id,),
        ).fetchall():
            ps = trow["project_slug"]
            if ps not in latest_by_slug:
                latest_by_slug[ps] = trow

    out: list[dict] = []
    for r in prows:
        p = dict(r)
        slug = p["slug"]
        if q:
            hay_name = p["name"].lower()
            hay_summary = (p.get("summary") or "").lower()
            matched = (
                q in slug
                or q in hay_name
                or q in hay_summary
                or (want_slug and want_slug in slug)
            )
            if not matched:
                continue
        lt = latest_by_slug.get(slug)
        started_watch = bool(p["started_via_watch"])
        is_default = slug == DEFAULT_DEV_PROJECT
        p.update({
            "started_via_watch": started_watch,
            "active": slug == active,
            "has_thread": bool(p["conversation_id"]),
            "can_delete": not is_default,
            "can_delete_files": (not is_default) and started_watch,
            "workspace_path": f"~/Code/{slug}",
            "turn_count": int(turn_counts.get(slug, 0)),
            "latest_status": lt["status"] if lt else "",
            "latest_prompt": lt["prompt"] if lt else "",
            "latest_reply": lt["reply"] if lt else "",
            "latest_at": (lt["completed_at"] or lt["created_at"]) if lt else p["updated_at"],
        })
        out.append(p)
    return out


def delete_dev_project(user_id: int, slug: str,
                       delete_files: bool = False) -> dict | None:
    clean_slug = slug.strip().lower()
    if clean_slug == DEFAULT_DEV_PROJECT:
        raise ValueError(f"cannot delete default project {DEFAULT_DEV_PROJECT}")
    if not PROJECT_SLUG_RE.match(clean_slug):
        return None
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _dev_lock, connect() as db:
        _ensure_default_project(db, user_id)
        prow = db.execute(
            "SELECT * FROM dev_projects WHERE user_id = ? AND slug = ?",
            (user_id, clean_slug),
        ).fetchone()
        if not prow:
            return None
        if delete_files and not prow["started_via_watch"]:
            raise ValueError(
                "refusing to delete workstation files for a project not started via Watch Bridge"
            )
        running = db.execute(
            "SELECT id FROM dev_turns WHERE user_id = ? AND project_slug = ? "
            "AND status = 'running' LIMIT 1",
            (user_id, clean_slug),
        ).fetchone()
        if running:
            raise RuntimeError(
                f"cannot delete '{clean_slug}' while a Carbon turn is running"
            )

        db.execute(
            "DELETE FROM dev_turns WHERE user_id = ? AND project_slug = ?",
            (user_id, clean_slug),
        )
        db.execute(
            "DELETE FROM dev_proposals WHERE user_id = ? AND slug = ?",
            (user_id, clean_slug),
        )
        db.execute(
            "DELETE FROM dev_projects WHERE user_id = ? AND slug = ?",
            (user_id, clean_slug),
        )

        srow = db.execute(
            "SELECT active_project FROM dev_state WHERE user_id = ?", (user_id,)
        ).fetchone()
        if srow and srow["active_project"] == clean_slug:
            drow = db.execute(
                "SELECT conversation_id FROM dev_projects WHERE user_id = ? AND slug = ?",
                (user_id, DEFAULT_DEV_PROJECT),
            ).fetchone()
            default_conv = drow["conversation_id"] if drow else ""
            db.execute(
                "UPDATE dev_state SET active_project = ?, conversation_id = ? WHERE user_id = ?",
                (DEFAULT_DEV_PROJECT, default_conv, user_id),
            )

        if delete_files:
            db.execute(
                "INSERT INTO dev_turns (user_id, project_slug, init_project, delete_project, "
                "prompt, new_thread, status, created_at) "
                "VALUES (?, ?, 0, 1, ?, 0, 'queued', ?)",
                (user_id, clean_slug, f"[delete project folder {clean_slug}]", now),
            )

    result = dict(prow)
    result["files_deletion_queued"] = bool(delete_files)
    return result


def create_dev_project(user_id: int, name: str, summary: str = "",
                       started_via_watch: bool = True) -> dict:
    slug = slugify_project(name)
    display = name.strip() or slug
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _dev_lock, connect() as db:
        _ensure_default_project(db, user_id)
        db.execute("DELETE FROM dev_proposals WHERE user_id = ?", (user_id,))
        db.execute(
            "INSERT INTO dev_projects (user_id, slug, name, summary, conversation_id, "
            "started_via_watch, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, '', ?, ?, ?) "
            "ON CONFLICT(user_id, slug) DO UPDATE SET "
            "summary = CASE WHEN excluded.summary != '' THEN excluded.summary ELSE dev_projects.summary END, "
            "updated_at = excluded.updated_at",
            (user_id, slug, display, summary.strip(), 1 if started_via_watch else 0, now, now),
        )
        row = db.execute(
            "SELECT * FROM dev_projects WHERE user_id = ? AND slug = ?",
            (user_id, slug),
        ).fetchone()
    return dict(row)


def save_dev_proposal(user_id: int, name: str, summary: str = "",
                      initial_task: str = "", now: float | None = None) -> dict:
    slug = slugify_project(name)
    display = name.strip() or slug
    ts = time.time() if now is None else now
    with _dev_lock, connect() as db:
        db.execute(
            "INSERT INTO dev_proposals (user_id, slug, name, summary, initial_task, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET slug=excluded.slug, name=excluded.name, "
            "summary=excluded.summary, initial_task=excluded.initial_task, "
            "created_at=excluded.created_at",
            (user_id, slug, display, summary.strip(), initial_task.strip(), ts),
        )
        row = db.execute(
            "SELECT * FROM dev_proposals WHERE user_id = ?", (user_id,)
        ).fetchone()
    return dict(row)


def get_dev_proposal(user_id: int, now: float | None = None,
                     ttl: float = 900.0) -> dict | None:
    ts = time.time() if now is None else now
    with _dev_lock, connect() as db:
        row = db.execute(
            "SELECT * FROM dev_proposals WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row:
            return None
        if ts - row["created_at"] > ttl:
            db.execute("DELETE FROM dev_proposals WHERE user_id = ?", (user_id,))
            return None
    return dict(row)


def clear_dev_proposal(user_id: int) -> None:
    with _dev_lock, connect() as db:
        db.execute("DELETE FROM dev_proposals WHERE user_id = ?", (user_id,))


def find_dev_project(user_id: int, query: str) -> dict:
    q = query.strip().lower()
    want_slug = re.sub(r"[^a-z0-9_-]+", "-", q).strip("-_")
    projects = list_dev_projects(user_id, watch_only=False)
    exact = [
        p for p in projects
        if p["slug"] == want_slug or p["name"].lower() == q
    ]
    hits = exact or [
        p for p in projects
        if (want_slug and want_slug in p["slug"]) or (q and q in p["name"].lower())
    ]
    if len(hits) != 1:
        known = ", ".join(p["name"] for p in projects)
        raise KeyError(f"project '{query}' matches {len(hits) or 'nothing'} (known: {known})")
    return hits[0]


def set_active_dev_project(user_id: int, slug: str) -> dict:
    with _dev_lock, connect() as db:
        _ensure_default_project(db, user_id)
        prow = db.execute(
            "SELECT * FROM dev_projects WHERE user_id = ? AND slug = ?",
            (user_id, slug),
        ).fetchone()
        if not prow:
            raise KeyError(f"project '{slug}' matches nothing")
        db.execute(
            "INSERT INTO dev_state (user_id, active_project, conversation_id, worker_seen_at, worker_host) "
            "VALUES (?, ?, ?, 0, '') "
            "ON CONFLICT(user_id) DO UPDATE SET active_project=excluded.active_project, "
            "conversation_id=excluded.conversation_id",
            (user_id, slug, prow["conversation_id"]),
        )
    return dict(prow)


def get_dev_state(user_id: int) -> dict:
    with _dev_lock, connect() as db:
        _ensure_default_project(db, user_id)
        row = db.execute(
            "SELECT active_project, conversation_id, worker_seen_at, worker_host "
            "FROM dev_state WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row:
            return {
                "active_project": DEFAULT_DEV_PROJECT,
                "conversation_id": "",
                "worker_seen_at": 0.0,
                "worker_host": "",
            }
        active = row["active_project"] or DEFAULT_DEV_PROJECT
        prow = db.execute(
            "SELECT conversation_id FROM dev_projects WHERE user_id = ? AND slug = ?",
            (user_id, active),
        ).fetchone()
        conv = prow["conversation_id"] if prow else row["conversation_id"]
    return {
        "active_project": active,
        "conversation_id": conv,
        "worker_seen_at": row["worker_seen_at"],
        "worker_host": row["worker_host"],
    }


def touch_dev_worker(user_id: int, host: str = "", now: float | None = None) -> None:
    seen = time.time() if now is None else now
    with _dev_lock, connect() as db:
        _ensure_default_project(db, user_id)
        db.execute(
            "INSERT INTO dev_state (user_id, active_project, conversation_id, worker_seen_at, worker_host) "
            "VALUES (?, ?, '', ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET worker_seen_at=excluded.worker_seen_at, "
            "worker_host=excluded.worker_host",
            (user_id, DEFAULT_DEV_PROJECT, seen, host),
        )


def reset_dev_thread(user_id: int, project_slug: str = "") -> None:
    with _dev_lock, connect() as db:
        _ensure_default_project(db, user_id)
        srow = db.execute(
            "SELECT active_project FROM dev_state WHERE user_id = ?", (user_id,)
        ).fetchone()
        slug = project_slug or (srow["active_project"] if srow else DEFAULT_DEV_PROJECT)
        db.execute(
            "UPDATE dev_projects SET conversation_id = '' WHERE user_id = ? AND slug = ?",
            (user_id, slug),
        )
        db.execute(
            "INSERT INTO dev_state (user_id, active_project, conversation_id, worker_seen_at, worker_host) "
            "VALUES (?, ?, '', 0, '') "
            "ON CONFLICT(user_id) DO UPDATE SET conversation_id=''",
            (user_id, slug),
        )


def get_dev_turn(turn_id: int) -> dict | None:
    with _dev_lock:
        row = connect().execute(
            "SELECT * FROM dev_turns WHERE id = ?", (turn_id,)
        ).fetchone()
    return dict(row) if row else None


def latest_dev_turn(user_id: int) -> dict | None:
    with _dev_lock:
        row = connect().execute(
            "SELECT * FROM dev_turns WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def enqueue_dev_turn(user_id: int, prompt: str, new_thread: bool = False,
                     project_slug: str = "", init_project: bool = False) -> dict:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _dev_lock, connect() as db:
        _ensure_default_project(db, user_id)
        db.execute("DELETE FROM dev_proposals WHERE user_id = ?", (user_id,))
        srow = db.execute(
            "SELECT active_project FROM dev_state WHERE user_id = ?", (user_id,)
        ).fetchone()
        slug = project_slug or (srow["active_project"] if srow else DEFAULT_DEV_PROJECT)
        if new_thread:
            db.execute(
                "UPDATE dev_projects SET conversation_id = '' WHERE user_id = ? AND slug = ?",
                (user_id, slug),
            )
            db.execute(
                "INSERT INTO dev_state (user_id, active_project, conversation_id, worker_seen_at, worker_host) "
                "VALUES (?, ?, '', 0, '') "
                "ON CONFLICT(user_id) DO UPDATE SET conversation_id=''",
                (user_id, slug),
            )
        cur = db.execute(
            "INSERT INTO dev_turns (user_id, project_slug, init_project, prompt, new_thread, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'queued', ?)",
            (user_id, slug, 1 if init_project else 0, prompt, 1 if new_thread else 0, now),
        )
        row = db.execute("SELECT * FROM dev_turns WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


def claim_next_dev_turn(user_id: int, host: str = "", now: float | None = None) -> dict | None:
    seen = time.time() if now is None else now
    with _dev_lock, connect() as db:
        _ensure_default_project(db, user_id)
        db.execute(
            "INSERT INTO dev_state (user_id, active_project, conversation_id, worker_seen_at, worker_host) "
            "VALUES (?, ?, '', ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET worker_seen_at=excluded.worker_seen_at, "
            "worker_host=excluded.worker_host",
            (user_id, DEFAULT_DEV_PROJECT, seen, host),
        )
        row = db.execute(
            "SELECT * FROM dev_turns WHERE user_id = ? AND status = 'queued' "
            "ORDER BY id ASC LIMIT 1",
            (user_id,),
        ).fetchone()
        if not row:
            return None
        slug = row["project_slug"] or DEFAULT_DEV_PROJECT
        conv_id = ""
        if not row["new_thread"]:
            prow = db.execute(
                "SELECT conversation_id FROM dev_projects WHERE user_id = ? AND slug = ?",
                (user_id, slug),
            ).fetchone()
            conv_id = prow["conversation_id"] if prow else ""
        db.execute(
            "UPDATE dev_turns SET status = 'running', conversation_id = ? WHERE id = ?",
            (conv_id, row["id"]),
        )
        claimed = db.execute("SELECT * FROM dev_turns WHERE id = ?", (row["id"],)).fetchone()
    return dict(claimed)


def mark_dev_turn_timed_out(turn_id: int) -> dict | None:
    with _dev_lock, connect() as db:
        db.execute(
            "UPDATE dev_turns SET timed_out = 1 "
            "WHERE id = ? AND status IN ('queued', 'running')",
            (turn_id,),
        )
        row = db.execute("SELECT * FROM dev_turns WHERE id = ?", (turn_id,)).fetchone()
    return dict(row) if row else None


def complete_dev_turn(turn_id: int, reply: str, conversation_id: str = "",
                      error: bool = False, raw_reply: str = "") -> dict | None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    status = "error" if error else "done"
    full_raw = raw_reply if raw_reply else reply
    with _dev_lock, connect() as db:
        existing = db.execute("SELECT * FROM dev_turns WHERE id = ?", (turn_id,)).fetchone()
        if not existing:
            return None
        uid = existing["user_id"]
        slug = existing["project_slug"] or DEFAULT_DEV_PROJECT
        _ensure_default_project(db, uid)
        conv = conversation_id or existing["conversation_id"]
        db.execute(
            "UPDATE dev_turns SET status = ?, reply = ?, raw_reply = ?, "
            "conversation_id = ?, completed_at = ? WHERE id = ?",
            (status, reply, full_raw, conv, now, turn_id),
        )
        if not error and conv and not existing["delete_project"]:
            db.execute(
                "UPDATE dev_projects SET conversation_id = ?, updated_at = ? "
                "WHERE user_id = ? AND slug = ?",
                (conv, now, uid, slug),
            )
            srow = db.execute(
                "SELECT active_project FROM dev_state WHERE user_id = ?", (uid,)
            ).fetchone()
            active = srow["active_project"] if srow else DEFAULT_DEV_PROJECT
            if active == slug:
                db.execute(
                    "INSERT INTO dev_state (user_id, active_project, conversation_id, worker_seen_at, worker_host) "
                    "VALUES (?, ?, ?, 0, '') "
                    "ON CONFLICT(user_id) DO UPDATE SET conversation_id=excluded.conversation_id",
                    (uid, slug, conv),
                )
        row = db.execute("SELECT * FROM dev_turns WHERE id = ?", (turn_id,)).fetchone()
    return dict(row)
