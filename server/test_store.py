"""Unit tests for the user store: scrypt hashing, unique tokens, lookups."""
import pytest

import store
from store import (accounts_for, add_account, count_users, create_user,
                   delete_account, delete_google_account, get_account,
                   get_google_account, get_user, get_user_by_name,
                   get_user_by_token, hash_password, invalidate_google_grant,
                   list_users, migrate_accounts, new_api_token,
                   save_google_account, save_google_calendar, set_admin,
                   set_password, set_trello_token, verify_password)


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    """A throwaway sqlite file per test; store.connect caches globally."""
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "bridge.db"))
    yield
    store._conn = None


def test_password_hash_round_trip():
    stored = hash_password("correct horse")
    assert stored != hash_password("correct horse")  # unique salt
    assert verify_password("correct horse", stored)
    assert not verify_password("wrong horse", stored)
    assert not verify_password("correct horse", "not-a-hash")
    assert not verify_password("correct horse", "abcd$zz")


def test_create_user_makes_unique_token_and_is_lookupable():
    u = create_user("ian", "hunter2hunter")
    assert get_user(u["id"])["username"] == "ian"
    assert get_user_by_token(u["api_token"])["id"] == u["id"]
    # sqlite = is case-sensitive: login lowercases before lookup, the store doesn't
    assert get_user_by_name("ian")["id"] == u["id"]
    assert get_user_by_name("IAN") is None
    assert get_user_by_name("nobody") is None
    assert create_user("jenni", "hunter2hunter")["api_token"] != u["api_token"]


def test_duplicate_username_raises_integrity_error():
    create_user("ian", "hunter2hunter")
    with pytest.raises(Exception, match="UNIQUE"):
        create_user("ian", "other-password")


def test_new_tokens_are_long_and_distinct():
    assert new_api_token() != new_api_token()
    assert len(new_api_token()) == 64


def test_set_trello_token_and_password():
    u = create_user("ian", "hunter2hunter")
    assert u["trello_token"] is None
    set_trello_token(u["id"], "ATTAtoken")
    assert get_user(u["id"])["trello_token"] == "ATTAtoken"
    set_password(u["id"], "new-password-9")
    assert not verify_password("hunter2hunter", get_user(u["id"])["password_hash"])
    assert verify_password("new-password-9", get_user(u["id"])["password_hash"])
    assert count_users() == 1


def test_admin_flag_round_trip_and_listing():
    admin = create_user("ian", "hunter2hunter", is_admin=True)
    member = create_user("jenni", "hunter2hunter")
    assert get_user(admin["id"])["is_admin"] == 1
    assert get_user(member["id"])["is_admin"] == 0
    assert set_admin(member["id"], True)
    assert get_user(member["id"])["is_admin"] == 1
    assert set_admin(member["id"], False)
    assert get_user(member["id"])["is_admin"] == 0
    assert not set_admin(999, True)  # no such user
    users = list_users()
    assert [u["username"] for u in users] == ["ian", "jenni"]  # insert order
    assert users[0]["is_admin"] == 1 and users[1]["is_admin"] == 0
    assert "password_hash" not in users[0] and "api_token" not in users[0]


def test_accounts_crud_and_label_uniqueness():
    u = create_user("ian", "hunter2hunter")
    assert accounts_for(u["id"]) == []
    add_account(u["id"], "trello", "ATTAa")
    add_account(u["id"], "work", "ATTAb")
    labels = [a["label"] for a in accounts_for(u["id"])]
    assert labels == ["trello", "work"]  # add order preserved
    assert get_account(u["id"], "work")["token"] == "ATTAb"
    assert get_account(u["id"], "nope") is None
    with pytest.raises(Exception, match="UNIQUE"):
        add_account(u["id"], "work", "ATTAc")  # labels are unique per user
    assert delete_account(u["id"], "work") is True
    assert delete_account(u["id"], "work") is False
    assert [a["label"] for a in accounts_for(u["id"])] == ["trello"]


def test_migrate_accounts_copies_legacy_tokens_once():
    u = create_user("ian", "hunter2hunter", trello_token="ATTAlegacy")
    migrate_accounts()
    acc = get_account(u["id"], "trello")
    assert acc is not None and acc["token"] == "ATTAlegacy"
    migrate_accounts()  # idempotent
    assert len(accounts_for(u["id"])) == 1
    # a user who already has account rows is never re-seeded from the legacy column
    add_account(u["id"], "work", "ATTAw")
    set_trello_token(u["id"], "ATTAother")
    migrate_accounts()
    assert [a["label"] for a in accounts_for(u["id"])] == ["trello", "work"]


def test_google_account_save_get_delete():
    u = create_user("ian", "hunter2hunter")
    v = create_user("jenni", "hunter2hunter")
    assert get_google_account(u["id"]) is None  # absent before connect
    save_google_account(u["id"], "access1", "refresh1", 1234.5, "Europe/London")
    acc = get_google_account(u["id"])
    assert acc["access_token"] == "access1" and acc["refresh_token"] == "refresh1"
    assert acc["expires_at"] == 1234.5 and acc["timezone"] == "Europe/London"
    # re-save replaces the single row (reconnecting the calendar)
    save_google_account(u["id"], "access2", "refresh2", 5678.0, "America/New_York")
    acc = get_google_account(u["id"])
    assert acc["access_token"] == "access2" and acc["expires_at"] == 5678.0
    # rows are strictly per user
    assert get_google_account(v["id"]) is None
    assert delete_google_account(u["id"]) is True
    assert delete_google_account(u["id"]) is False  # already gone
    assert get_google_account(u["id"]) is None


def test_chosen_calendar_defaults_to_primary_and_survives_reconnect():
    u = create_user("ian", "hunter2hunter")
    save_google_account(u["id"], "access1", "refresh1", 1234.5, "UTC")
    acc = get_google_account(u["id"])
    assert acc["calendar_id"] == "primary"
    save_google_calendar(u["id"], "work@example.com", "Europe/Paris")
    acc = get_google_account(u["id"])
    assert acc["calendar_id"] == "work@example.com"
    assert acc["timezone"] == "Europe/Paris"
    # reconnecting mints fresh tokens but keeps the chosen calendar
    save_google_account(u["id"], "access2", "refresh2", 5678.0, "UTC")
    acc = get_google_account(u["id"])
    assert acc["calendar_id"] == "work@example.com"
    assert acc["timezone"] == "UTC"


def test_invalidate_google_grant_blanks_tokens_but_keeps_the_choice():
    u = create_user("ian", "hunter2hunter")
    save_google_account(u["id"], "access1", "refresh1", 1234.5, "Europe/London")
    save_google_calendar(u["id"], "work@example.com", "Europe/Paris")
    invalidate_google_grant(u["id"])
    acc = get_google_account(u["id"])
    assert acc["access_token"] == "" and acc["refresh_token"] == ""
    assert acc["expires_at"] == 0
    assert acc["calendar_id"] == "work@example.com"
    assert acc["timezone"] == "Europe/Paris"


def test_legacy_google_row_migrates_to_a_primary_calendar_id(tmp_path):
    """A DB written before the calendar_id column existed still reads, and
    the migration backfills 'primary' without touching stored tokens."""
    import sqlite3
    dbfile = tmp_path / "bridge.db"
    legacy = sqlite3.connect(dbfile)
    legacy.executescript(
        "CREATE TABLE google_accounts (user_id INTEGER PRIMARY KEY,"
        " access_token TEXT NOT NULL, refresh_token TEXT NOT NULL,"
        " expires_at REAL NOT NULL, timezone TEXT NOT NULL);"
        "INSERT INTO google_accounts VALUES (1, 'a', 'r', 1.0, 'UTC');")
    legacy.commit()
    legacy.close()
    store._conn = None
    store.connect(str(dbfile))
    acc = store.get_google_account(1)
    assert acc["calendar_id"] == "primary"
    assert acc["refresh_token"] == "r"


def test_dev_turn_lifecycle_and_conversation_continuity():
    u = create_user("ian", "hunter2hunter", is_admin=True)
    assert store.get_dev_state(u["id"]) == {
        "active_project": "watch-llm-bridge",
        "conversation_id": "",
        "worker_seen_at": 0.0,
        "worker_host": "",
    }
    t1 = store.enqueue_dev_turn(u["id"], "inspect health endpoint")
    assert t1["status"] == "queued"
    assert t1["conversation_id"] == ""
    claimed = store.claim_next_dev_turn(u["id"], host="workstation", now=1000.0)
    assert claimed["id"] == t1["id"]
    assert claimed["status"] == "running"
    assert claimed["conversation_id"] == ""
    assert store.claim_next_dev_turn(u["id"], host="workstation", now=1001.0) is None
    state = store.get_dev_state(u["id"])
    assert state["worker_seen_at"] == 1001.0
    assert state["worker_host"] == "workstation"

    done = store.complete_dev_turn(t1["id"], "Health returns ok.", conversation_id="conv-111")
    assert done["status"] == "done"
    assert done["reply"] == "Health returns ok."
    assert done["conversation_id"] == "conv-111"
    assert store.get_dev_state(u["id"])["conversation_id"] == "conv-111"

    t2 = store.enqueue_dev_turn(u["id"], "now add uptime")
    claimed2 = store.claim_next_dev_turn(u["id"], host="workstation", now=1010.0)
    assert claimed2["id"] == t2["id"]
    assert claimed2["conversation_id"] == "conv-111"
    store.complete_dev_turn(t2["id"], "Added uptime.", conversation_id="conv-111")

    t3 = store.enqueue_dev_turn(u["id"], "start fresh", new_thread=True)
    claimed3 = store.claim_next_dev_turn(u["id"], host="workstation", now=1020.0)
    assert claimed3["id"] == t3["id"]
    assert claimed3["conversation_id"] == ""


def test_dev_turn_timeout_flag_is_atomic():
    u = create_user("ian", "hunter2hunter", is_admin=True)
    t1 = store.enqueue_dev_turn(u["id"], "slow refactor")
    store.claim_next_dev_turn(u["id"], host="ws", now=500.0)
    flagged = store.mark_dev_turn_timed_out(t1["id"])
    assert flagged["timed_out"] == 1
    done = store.complete_dev_turn(t1["id"], "Refactor done.", conversation_id="conv-2")
    assert done["timed_out"] == 1

    # Once completed, a late timeout check must not flip timed_out to 1.
    t2 = store.enqueue_dev_turn(u["id"], "fast query")
    store.claim_next_dev_turn(u["id"], host="ws", now=510.0)
    store.complete_dev_turn(t2["id"], "Fast answer.", conversation_id="conv-2")
    after = store.mark_dev_turn_timed_out(t2["id"])
    assert after["status"] == "done"
    assert after["timed_out"] == 0


def test_dev_projects_track_watch_started_and_preserve_per_project_threads():
    u = create_user("ian", "hunter2hunter", is_admin=True)
    # Default watch-llm-bridge is NOT marked started_via_watch.
    assert store.list_dev_projects(u["id"], watch_only=True) == []
    all_initial = store.list_dev_projects(u["id"], watch_only=False)
    assert [p["slug"] for p in all_initial] == ["watch-llm-bridge"]

    # Record a conversation on watch-llm-bridge.
    t_bridge = store.enqueue_dev_turn(u["id"], "inspect bridge")
    store.claim_next_dev_turn(u["id"], host="ws", now=100.0)
    store.complete_dev_turn(t_bridge["id"], "Bridge ok.", conversation_id="conv-bridge")

    # Start two new projects over the watch bridge.
    p1 = store.create_dev_project(u["id"], "Solar Logger", summary="Victron MQTT logger",
                                  started_via_watch=True)
    assert p1["slug"] == "solar-logger"
    assert p1["started_via_watch"] == 1
    store.set_active_dev_project(u["id"], "solar-logger")
    assert store.get_dev_state(u["id"])["active_project"] == "solar-logger"
    assert store.get_dev_state(u["id"])["conversation_id"] == ""

    t_solar = store.enqueue_dev_turn(u["id"], "scaffold cli")
    claimed_solar = store.claim_next_dev_turn(u["id"], host="ws", now=110.0)
    assert claimed_solar["project_slug"] == "solar-logger"
    assert claimed_solar["conversation_id"] == ""
    store.complete_dev_turn(t_solar["id"], "Scaffolded.", conversation_id="conv-solar")

    p2 = store.create_dev_project(u["id"], "Pebble Sync", summary="Health sync",
                                  started_via_watch=True)
    assert p2["slug"] == "pebble-sync"

    # Attach an existing repo (started_via_watch=False) -> excluded from watch_only=True.
    store.create_dev_project(u["id"], "fieldsmith", started_via_watch=False)

    watch_started = store.list_dev_projects(u["id"], watch_only=True)
    assert [p["slug"] for p in watch_started] == ["solar-logger", "pebble-sync"]

    # Switching back to watch-llm-bridge restores conv-bridge; switching to solar-logger restores conv-solar!
    store.set_active_dev_project(u["id"], "watch-llm-bridge")
    assert store.get_dev_state(u["id"])["conversation_id"] == "conv-bridge"
    store.set_active_dev_project(u["id"], "solar-logger")
    assert store.get_dev_state(u["id"])["conversation_id"] == "conv-solar"


def test_legacy_dev_tables_migrate_project_columns(tmp_path):
    import sqlite3
    dbfile = tmp_path / "legacy_dev.db"
    legacy = sqlite3.connect(dbfile)
    legacy.executescript(
        "CREATE TABLE dev_state (user_id INTEGER PRIMARY KEY,"
        " conversation_id TEXT NOT NULL DEFAULT '',"
        " worker_seen_at REAL NOT NULL DEFAULT 0,"
        " worker_host TEXT NOT NULL DEFAULT '');"
        "INSERT INTO dev_state VALUES (1, 'conv-existing', 999.0, 'msi-z790');"
        "CREATE TABLE dev_turns (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
        " prompt TEXT NOT NULL, new_thread INTEGER NOT NULL DEFAULT 0,"
        " conversation_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,"
        " reply TEXT NOT NULL DEFAULT '', timed_out INTEGER NOT NULL DEFAULT 0,"
        " created_at TEXT NOT NULL, completed_at TEXT NOT NULL DEFAULT '');"
    )
    legacy.commit()
    legacy.close()
    store._conn = None
    store.connect(str(dbfile))
    st = store.get_dev_state(1)
    assert st["active_project"] == "watch-llm-bridge"
    assert st["conversation_id"] == "conv-existing"


def test_dev_pending_proposal_round_trip_expiry_and_auto_clear():
    u = create_user("ian", "hunter2hunter", is_admin=True)
    assert store.get_dev_proposal(u["id"], now=1000.0) is None

    prop = store.save_dev_proposal(
        u["id"],
        name="OctoPrint Logger",
        summary="Polls OctoPrint temps",
        initial_task="Build a CLI that polls OctoPrint temperatures into SQLite",
        now=1000.0,
    )
    assert prop["slug"] == "octoprint-logger"
    assert prop["name"] == "OctoPrint Logger"
    live = store.get_dev_proposal(u["id"], now=1500.0, ttl=900.0)
    assert live is not None
    assert live["initial_task"].startswith("Build a CLI")

    # Expired after TTL
    assert store.get_dev_proposal(u["id"], now=2000.0, ttl=900.0) is None

    # Creating the project clears any active proposal
    store.save_dev_proposal(u["id"], "OctoPrint Logger", "Polls", "Build CLI", now=2100.0)
    store.create_dev_project(u["id"], "octoprint-logger", summary="Polls", started_via_watch=True)
    assert store.get_dev_proposal(u["id"], now=2105.0, ttl=900.0) is None


def test_complete_dev_turn_stores_spoken_reply_and_raw_reply():
    u = create_user("ian", "hunter2hunter", is_admin=True)
    t1 = store.enqueue_dev_turn(u["id"], "build converter")
    store.claim_next_dev_turn(u["id"], host="ws", now=100.0)
    done = store.complete_dev_turn(
        t1["id"],
        "Built the miles-to-km converter and all tests passed.",
        conversation_id="conv-raw",
        raw_reply="## Summary\n- Built `miles_to_km.py` with factor `1.609344`.\n- 13 pytest cases passed.",
    )
    assert done["reply"] == "Built the miles-to-km converter and all tests passed."
    assert "1.609344" in done["raw_reply"]
    # When raw_reply is omitted, it defaults to reply.
    t2 = store.enqueue_dev_turn(u["id"], "ping")
    store.claim_next_dev_turn(u["id"], host="ws", now=110.0)
    done2 = store.complete_dev_turn(t2["id"], "Pong.", conversation_id="conv-raw")
    assert done2["raw_reply"] == "Pong."


def test_gemini_key_save_get_overwrite_and_delete():
    u = create_user("ian", "hunter2hunter", is_admin=True)
    assert store.get_gemini_key(u["id"]) is None
    store.save_gemini_key(u["id"], "AIza-first", model="models/gemini-3.8-live", voice="Puck")
    row = store.get_gemini_key(u["id"])
    assert row["api_key"] == "AIza-first"
    assert row["model"] == "models/gemini-3.8-live"
    assert row["voice"] == "Puck"

    store.save_gemini_key(u["id"], "AIza-second", model="models/gemini-3.8-live", voice="Aoede")
    row2 = store.get_gemini_key(u["id"])
    assert row2["api_key"] == "AIza-second"
    assert row2["voice"] == "Aoede"

    assert store.delete_gemini_key(u["id"]) is True
    assert store.get_gemini_key(u["id"]) is None
    assert store.delete_gemini_key(u["id"]) is False




