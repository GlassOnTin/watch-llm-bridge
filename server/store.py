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
  created_at TEXT NOT NULL
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
                trello_token: str | None = None) -> dict:
    """Insert a user; raises sqlite3.IntegrityError on duplicate username."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect() as db:
        cur = db.execute(
            "INSERT INTO users (username, password_hash, api_token, trello_token, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (username, hash_password(password), api_token or new_api_token(),
             trello_token, now),
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


def set_trello_token(user_id: int, trello_token: str) -> None:
    with connect() as db:
        db.execute("UPDATE users SET trello_token = ? WHERE id = ?", (trello_token, user_id))


def set_password(user_id: int, password: str) -> None:
    with connect() as db:
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                   (hash_password(password), user_id))