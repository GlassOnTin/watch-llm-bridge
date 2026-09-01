"""Unit tests for the user store: scrypt hashing, unique tokens, lookups."""
import pytest

import store
from store import (count_users, create_user, get_user, get_user_by_name,
                   get_user_by_token, hash_password, new_api_token, set_password,
                   set_trello_token, verify_password)


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