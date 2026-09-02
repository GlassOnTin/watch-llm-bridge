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
    # a cached client only serves a user who has connected a Trello account
    store.add_account(ua["id"], "trello", "ATTAa")
    store.add_account(ub["id"], "trello", "ATTAb")

    def client_with(boards, lists, token):
        t = app.Trello("k", token)  # token must match the stored account row
        t.boards = boards
        t.lists_by_board = lists
        return t

    app._clients[ua["id"]] = {"trello": client_with({"HomeA": "b1"}, {"b1": [("ListA", "l1")]}, "ATTAa")}
    app._clients[ub["id"]] = {"trello": client_with({"HomeB": "b2"}, {"b2": [("ListB", "l2")]}, "ATTAb")}
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
    acc = store.get_account(user["id"], "trello")
    assert acc is not None and acc["token"] == "ATTAxyz"
    assert user["id"] in app._clients  # boards were refreshed into the cache


def test_connect_trello_rejects_bad_token_without_storing(client, monkeypatch):
    signup(client)
    user = store.get_user_by_name("tester")

    def bad_get(url, params=None, timeout=None):
        raise app.requests.HTTPError("401")

    monkeypatch.setattr(app.requests, "get", bad_get)
    r = client.post("/app/trello", json={"token": "ATTAbad"})
    assert r.status_code == 401
    assert store.get_account(user["id"], "trello") is None
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
    app._clients[ua["id"]] = {"trello": t}
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


# --- admins -----------------------------------------------------------------

def owner_session(client):
    owner = store.get_user_by_name(app.OWNER_USERNAME)
    client.cookies.set(app.SESSION_COOKIE, app.make_session(owner["id"]))
    return owner


def test_owner_is_seeded_and_backfilled_as_admin(client):
    assert store.get_user_by_name(app.OWNER_USERNAME)["is_admin"] == 1
    # simulate a live DB created before admins existed
    store.set_admin(store.get_user_by_name(app.OWNER_USERNAME)["id"], False)
    app.seed_owner()
    assert store.get_user_by_name(app.OWNER_USERNAME)["is_admin"] == 1


def test_admin_promotes_a_user_and_state_lists_them(client):
    signup(client, "beta")
    owner = owner_session(client)  # re-set: signup replaced the session cookie
    r = client.post("/app/admin", json={"username": "beta", "admin": True})
    assert r.status_code == 200
    assert store.get_user_by_name("beta")["is_admin"] == 1
    state = r.json()
    assert [u["username"] for u in state["users"]] == [app.OWNER_USERNAME, "beta"]
    assert state["users"][1]["is_admin"] is True
    # a promoted user sees the invite code too, though they are not the owner
    client.cookies.set(app.SESSION_COOKIE, app.make_session(
        store.get_user_by_name("beta")["id"]))
    beta_state = client.get("/app/state").json()
    assert beta_state["invite_code"] == app.INVITE_CODE
    assert [u["username"] for u in beta_state["users"]] == [app.OWNER_USERNAME, "beta"]


def test_non_admin_cannot_promote_or_see_the_user_list(client):
    signup(client)
    r = client.post("/app/admin", json={"username": "ian", "admin": True})
    assert r.status_code == 403
    state = client.get("/app/state").json()
    assert "users" not in state
    assert state["invite_code"] == ""


def test_admin_cannot_change_own_status(client):
    owner = owner_session(client)
    r = client.post("/app/admin", json={"username": app.OWNER_USERNAME, "admin": False})
    assert r.status_code == 400
    assert store.get_user(owner["id"])["is_admin"] == 1
    r = client.post("/app/admin", json={"username": "ghost", "admin": True})
    assert r.status_code == 404


# --- multiple Trello accounts per user ---

def mkclient(boards, lists):
    t = app.Trello("k", "t")
    t.boards = boards
    t.lists_by_board = lists
    return t


def connect_as(client, monkeypatch, token, label=""):
    monkeypatch.setattr(app.requests, "get",
                        lambda url, params=None, timeout=None: FakeResp({"id": "me"}))
    return client.post("/app/trello", json={"token": token, "label": label})


def test_second_account_adds_and_routes_by_label(client, monkeypatch):
    signup(client)
    assert connect_as(client, monkeypatch, "ATTAone", "").status_code == 200
    r = connect_as(client, monkeypatch, "ATTAtwo", "Work")
    assert r.status_code == 200
    state = r.json()
    assert [a["label"] for a in state["accounts"]] == ["trello", "work"]
    # both accounts resolve through clients_for, in add order
    user = store.get_user_by_name("tester")
    clients = app.clients_for(user)
    assert [label for label, _ in clients] == ["trello", "work"]
    assert clients[0][1].token == "ATTAone" and clients[1][1].token == "ATTAtwo"


def test_duplicate_account_label_conflicts(client, monkeypatch):
    signup(client)
    assert connect_as(client, monkeypatch, "ATTAone", "work").status_code == 200
    r = connect_as(client, monkeypatch, "ATTAtwo", "WORK")
    assert r.status_code == 409
    assert "work" in r.json()["detail"]


def test_second_account_requires_a_name(client, monkeypatch):
    signup(client)
    assert connect_as(client, monkeypatch, "ATTAone", "").status_code == 200
    r = connect_as(client, monkeypatch, "ATTAtwo", "")
    assert r.status_code == 409  # empty label resolves to the existing "trello"


def test_invalid_account_label_rejected(client, monkeypatch):
    signup(client)
    r = connect_as(client, monkeypatch, "ATTAone", "Bad Name!")
    assert r.status_code == 400


def test_delete_account(client, monkeypatch):
    signup(client)
    connect_as(client, monkeypatch, "ATTAone", "")
    connect_as(client, monkeypatch, "ATTAtwo", "work")
    r = client.request("DELETE", "/app/trello", params={"label": "work"})
    assert r.status_code == 200
    assert [a["label"] for a in r.json()["accounts"]] == ["trello"]
    r = client.request("DELETE", "/app/trello", params={"label": "ghost"})
    assert r.status_code == 404


def test_multi_account_prompt_and_resolution(client):
    signup(client, "alpha")
    ua = store.get_user_by_name("alpha")
    home = mkclient({"Home": "b1"}, {"b1": [("Shopping", "l1")]})
    work = mkclient({"Home": "b2"}, {"b2": [("Shopping", "l3"), ("Done", "l4")]})
    clients = [("trello", home), ("work", work)]
    app._clients[ua["id"]] = {"trello": home, "work": work}

    prompt = app.build_system_prompt(clients=clients)
    assert "- account 'trello', board 'Home': Shopping" in prompt
    assert "- account 'work', board 'Home': Shopping | Done" in prompt
    assert "several Trello accounts" in prompt

    # unique across both accounts: the pair is chosen
    picked = app.resolve_board({}, "done", clients=clients)
    assert picked == {"ok": True, "board": "Home", "account": "work"}
    # same list name on both accounts: ambiguity carries account/board pairs
    picked = app.resolve_board({}, "shopping", clients=clients)
    assert picked["ok"] is False
    assert picked["pairs"] == ["trello / Home", "work / Home"]
    # a named account narrows the search
    picked = app.resolve_board({"account": "Work"}, "shopping", clients=clients)
    assert picked == {"ok": True, "board": "Home", "account": "work"}
    # an account nobody has is refused, never guessed
    assert app.resolve_board({"account": "ghost"}, "shopping",
                             clients=clients)["error"] == "unknown_account"

    out = app.execute_tool("trello_list_boards", {}, clients=clients)
    assert out == {"trello": {"Home": ["Shopping"]}, "work": {"Home": ["Shopping", "Done"]}}


def test_multi_account_create_tags_the_account(client):
    signup(client, "alpha")
    ua = store.get_user_by_name("alpha")
    home = mkclient({"Home": "b1"}, {"b1": [("Shopping", "l1")]})
    work = mkclient({"Work": "b2"}, {"b2": [("Chores", "l9")]})
    clients = [("trello", home), ("work", work)]
    seen = {}

    def fake_create(board, list_name, name, desc=""):
        seen.update(board=board, list_name=list_name, name=name)
        return {"name": name}

    home.create_card = fake_create
    work.create_card = fake_create
    app._clients[ua["id"]] = {"trello": home, "work": work}
    out = app.execute_tool("trello_create_card",
                           {"list": "chores", "name": "Bin day", "account": "work"},
                           clients=clients)
    assert out == {"ok": True, "created": "Bin day", "list": "chores",
                   "board": "Work", "account": "work"}
    assert seen == {"board": "Work", "list_name": "chores", "name": "Bin day"}


def test_single_account_tools_keep_the_old_shape(client):
    signup(client, "alpha")
    ua = store.get_user_by_name("alpha")
    home = mkclient({"Home": "b1", "Plans": "b2"},
                    {"b1": [("Done", "l2")], "b2": [("Done", "l4")]})
    app._clients[ua["id"]] = {"trello": home}
    picked = app.resolve_board({}, "done", t=home)
    assert picked == {"ok": False, "error": "ambiguous_board", "list": "done",
                      "boards": ["Home", "Plans"]}
    assert "pairs" not in picked  # single account: the pre-multi-account shape


def test_legacy_trello_token_migrates_on_first_use(client, monkeypatch):
    signup(client, "alpha")
    ua = store.get_user_by_name("alpha")
    store.set_trello_token(ua["id"], "ATTAlegacy")
    app._clients.pop(ua["id"], None)
    t = app.clients_for(store.get_user(ua["id"]))  # fresh row: has trello_token
    assert [label for label, _ in t] == ["trello"]
    assert store.get_account(ua["id"], "trello")["token"] == "ATTAlegacy"