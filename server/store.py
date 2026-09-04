"""SQLite user store: registration, scrypt password hashes, per-user bearer
tokens, and each user's Trello token (authorized under the server's shared
TRELLO_KEY). One table, no ORM — this box runs one process.

The DB path is `bridge.db` in the CWD unless BRIDGE_DB is set. DEPLOY.sh
excludes *.db from its rsync so a deploy can never wipe it.
"""
import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get("BRIDGE_DB", os.path.join(os.getcwd(), "bridge.db"))

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
)
"""

_conn: sqlite3.Connection | None = None


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