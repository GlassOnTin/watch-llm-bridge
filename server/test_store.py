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
