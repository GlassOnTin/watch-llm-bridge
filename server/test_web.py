"""Web front end: signup/login/session, per-user bearer isolation, and the
Trello paste-connect flow. All network is faked; the DB is a tmp file."""


import pytest
from fastapi.testclient import TestClient

import app
import store

INVITE = "plum"


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient against a throwaway DB. Trello.refresh is a no-op so the
    per-user cache is controlled by the test, and the rate limiter is reset."""
    monkeypatch.setattr(app, "INVITE_CODE", INVITE)
    monkeypatch.setattr(app, "SESSION_SECRET", "test-secret")
    monkeypatch.setattr(app, "_attempts", {})
    monkeypatch.setattr(app.Trello, "refresh", lambda self: None)
    monkeypatch.setattr(app, "_clients", {})  # don't leak across tests' tmp DBs
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "bridge.db"))
    with TestClient(app.app) as c:  # startup seeds the owner from .env creds
        yield c
    store._conn = None


def signup(client, username="tester", password="password8", invite=INVITE):
    return client.post("/auth/signup", json={
        "username": username, "password": password, "invite": invite})


def test_signup_rejects_bad_invite_and_accepts_good(client):
    assert client.post("/auth/signup", json={
        "username": "tester", "password": "password8", "invite": "wrong"}).status_code == 403
    assert client.post("/auth/signup", json={
        "username": "tester", "password": "password8"}).status_code == 403
    r = signup(client)
    assert r.status_code == 200
    assert store.get_user_by_name("tester") is not None


def test_signup_disabled_without_invite_code(client, monkeypatch):
    monkeypatch.setattr(app, "INVITE_CODE", "")
    r = client.post("/auth/signup", json={
        "username": "tester", "password": "password8", "invite": ""})
    assert r.status_code == 403
    assert "disabled" in r.json()["detail"]


def test_signup_validates_username_and_password(client):
    assert client.post("/auth/signup", json={
        "username": "Bad Name!", "password": "password8", "invite": INVITE}).status_code == 400
    assert client.post("/auth/signup", json={
        "username": "tester", "password": "short", "invite": INVITE}).status_code == 400


def test_signup_duplicate_username_conflicts(client):
    assert signup(client).status_code == 200
    assert signup(client).status_code == 409


def test_login_sets_session_and_wrong_password_is_rejected(client):
    signup(client)
    assert client.post("/auth/login", json={
        "username": "tester", "password": "wrong-password"}).status_code == 401
    assert client.post("/auth/login", json={
        "username": "tester", "password": "password8"}).status_code == 200
    assert client.get("/app").status_code == 200
    state = client.get("/app/state").json()
    assert state["username"] == "tester"
    assert state["connected"] is False


def test_dashboard_requires_session(client):
    assert client.get("/app/state").status_code == 401
    assert client.get("/app", follow_redirects=False).status_code == 303  # to /login
    r = client.get("/app", follow_redirects=False, cookies={"bridge_session": "1.9999999999.deadbeef"})
    assert r.status_code == 303


def test_owner_bearer_works_and_gets_echo_command(client, monkeypatch):
    # The owner is seeded from .env: BRIDGE_TOKEN must keep working.
    monkeypatch.setattr(app, "LLM_API_KEY", "")  # echo mode
    r = client.get("/boards", headers={"Authorization": f"Bearer {app.BRIDGE_TOKEN}"})
    assert r.status_code == 200
    assert client.get("/boards", headers={"Authorization": "Bearer nope"}).status_code == 401
    r = client.post("/command", json={"text": "hello"},
                    headers={"Authorization": f"Bearer {app.BRIDGE_TOKEN}"})
    assert r.json() == {"reply": "echo: hello"}


def test_command_without_trello_asks_to_connect(client):
    signup(client)
    user = store.get_user_by_name("tester")
    r = client.post("/command", json={"text": "add milk"},
                    headers={"Authorization": f"Bearer {user['api_token']}"})
    assert "dashboard" in r.json()["reply"]


def test_boards_isolated_per_user(client):
    signup(client, "alpha")
    signup(client, "beta")
    ua = store.get_user_by_name("alpha")
    ub = store.get_user_by_name("beta")
    # a cached client only serves a user who has connected a Trello token
    store.set_trello_token(ua["id"], "ATTAa")
    store.set_trello_token(ub["id"], "ATTAb")

    def client_with(boards, lists):
        t = app.Trello("k", "t")
        t.boards = boards
        t.lists_by_board = lists
        return t

    app._clients[ua["id"]] = client_with({"HomeA": "b1"}, {"b1": [("ListA", "l1")]})
    app._clients[ub["id"]] = client_with({"HomeB": "b2"}, {"b2": [("ListB", "l2")]})
    ha = client.get("/boards", headers={"Authorization": f"Bearer {ua['api_token']}"}).json()
    hb = client.get("/boards", headers={"Authorization": f"Bearer {ub['api_token']}"}).json()
    assert list(ha) == ["HomeA"] and list(hb) == ["HomeB"]


def test_connect_trello_verifies_and_stores(client, monkeypatch):
    signup(client)
    user = store.get_user_by_name("tester")
    seen = {}

    def fake_get(url, params=None, timeout=None):
        seen.update(url=url, params=params)
        return FakeResp({"id": "me"})

    monkeypatch.setattr(app.requests, "get", fake_get)
    r = client.post("/app/trello", json={"token": "  ATTAxyz  "})
    assert r.status_code == 200
    assert seen["params"]["token"] == "ATTAxyz"
    assert seen["params"]["key"] == app.TRELLO_KEY
    assert store.get_user(user["id"])["trello_token"] == "ATTAxyz"
    assert user["id"] in app._clients  # boards were refreshed into the cache


def test_connect_trello_rejects_bad_token_without_storing(client, monkeypatch):
    signup(client)
    user = store.get_user_by_name("tester")

    def bad_get(url, params=None, timeout=None):
        raise app.requests.HTTPError("401")

    monkeypatch.setattr(app.requests, "get", bad_get)
    r = client.post("/app/trello", json={"token": "ATTAbad"})
    assert r.status_code == 401
    assert store.get_user(user["id"])["trello_token"] is None
    assert user["id"] not in app._clients


def test_password_change(client):
    signup(client)
    assert client.post("/app/password", json={
        "current": "wrong-password", "new": "new-password9"}).status_code == 401
    assert client.post("/app/password", json={
        "current": "password8", "new": "short"}).status_code == 400
    assert client.post("/app/password", json={
        "current": "password8", "new": "new-password9"}).status_code == 200
    assert client.post("/auth/login", json={
        "username": "tester", "password": "new-password9"}).status_code == 200


def test_system_prompt_is_per_user(client, monkeypatch):
    signup(client, "alpha")
    ua = store.get_user_by_name("alpha")
    t = app.Trello("k", "t")
    t.boards = {"Mine": "b9"}
    t.lists_by_board = {"b9": [("Chores", "l9")]}
    app._clients[ua["id"]] = t
    prompt = app.build_system_prompt(t)
    assert "board 'Mine': Chores" in prompt
    assert "Shopping List" not in prompt  # the owner's inventory doesn't leak in


def test_invite_code_shown_to_owner_only(client):
    owner = store.get_user_by_name(app.OWNER_USERNAME)
    client.cookies.set(app.SESSION_COOKIE, app.make_session(owner["id"]))
    state = client.get("/app/state").json()
    assert state["username"] == app.OWNER_USERNAME
    assert state["invite_code"] == app.INVITE_CODE
    signup(client, "beta")
    s = client.get("/app/state")  # signup replaced the session cookie with beta's
    assert s.json()["invite_code"] == ""
    assert s.json()["username"] == "beta"