"""Web front end: signup/login/session, per-user bearer isolation, and the
Trello paste-connect flow. All network is faked; the DB is a tmp file."""


import time
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

# --- Google Calendar OAuth ---------------------------------------------------

GOOGLE_STATE_URL = "https://accounts.google.com/o/oauth2/v2/auth"


class GResp:
    """Fake response with a status code, for token/calendar endpoints."""

    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            e = app.requests.HTTPError(f"HTTP {self.status_code}")
            e.response = self
            raise e

    def json(self):
        return self._payload


@pytest.fixture
def gcal(client, monkeypatch):
    """Feature gate on, CSRF state and client caches empty."""
    monkeypatch.setattr(app, "GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setattr(app, "GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setattr(app, "_google_state", {})
    monkeypatch.setattr(app, "_gcal", {})
    return client


def google_state_of(client):
    from urllib.parse import parse_qs, urlparse
    r = client.get("/app/google/start", follow_redirects=False)
    return r, parse_qs(urlparse(r.headers["location"]).query)


def test_google_start_hidden_when_unconfigured(client):
    signup(client)
    assert client.get("/app/google/start", follow_redirects=False).status_code == 404


def test_google_start_sends_consent_url_with_state(gcal):
    signup(gcal)
    r, q = google_state_of(gcal)
    assert r.status_code == 303
    assert r.headers["location"].startswith(GOOGLE_STATE_URL)
    assert q["client_id"] == ["cid"]
    assert q["redirect_uri"] == [app.GOOGLE_REDIRECT_URI]
    assert q["scope"] == [app.GCAL_SCOPE]
    assert q["response_type"] == ["code"]
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    state = q["state"][0]
    assert app._google_state[store.get_user_by_name("tester")["id"]][0] == state


def test_google_callback_exchanges_code_and_stores_account(gcal, monkeypatch):
    signup(gcal)
    _, q = google_state_of(gcal)
    posts, gets = [], []

    def fake_post(url, data=None, **kw):
        posts.append((url, data))
        return GResp({"access_token": "ACC", "refresh_token": "REF",
                      "expires_in": 3600})

    def fake_get(url, params=None, headers=None, timeout=None):
        gets.append(url)
        return GResp({"value": "Europe/London"})

    monkeypatch.setattr(app.requests, "post", fake_post)
    monkeypatch.setattr(app.requests, "get", fake_get)
    r = gcal.get("/app/google/callback", params={"code": "xyz", "state": q["state"][0]},
                 follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app?google=connected"
    url, data = posts[0]
    assert url == app.GOOGLE_TOKEN_URL and data["code"] == "xyz"
    assert data["redirect_uri"] == app.GOOGLE_REDIRECT_URI
    assert gets == [f"{app.GCAL_API}/users/me/settings/timezone"]
    acc = store.get_google_account(store.get_user_by_name("tester")["id"])
    assert acc["access_token"] == "ACC" and acc["refresh_token"] == "REF"
    assert acc["timezone"] == "Europe/London"
    assert acc["expires_at"] > time.time()


def test_google_callback_rejects_wrong_and_reused_state(gcal, monkeypatch):
    signup(gcal)
    stored = {}

    def fail_post(url, data=None, **kw):  # the token endpoint must never be hit
        stored["hit"] = True
        return GResp({})

    monkeypatch.setattr(app.requests, "post", fail_post)
    r, q = google_state_of(gcal)
    bad = gcal.get("/app/google/callback",
                   params={"code": "xyz", "state": "forged"}, follow_redirects=False)
    assert bad.status_code == 303
    assert "expired" in bad.headers["location"]
    # single use: the real state was consumed by the failed attempt's pop
    reused = gcal.get("/app/google/callback", params={"code": "xyz", "state": q["state"][0]},
                      follow_redirects=False)
    assert "expired" in reused.headers["location"]
    assert "hit" not in stored
    assert store.get_google_account(store.get_user_by_name("tester")["id"]) is None


def test_google_callback_handles_denial_cleanly(gcal, monkeypatch):
    signup(gcal)
    _, q = google_state_of(gcal)
    monkeypatch.setattr(app.requests, "post",
                        lambda *a, **kw: pytest.fail("no exchange on denial"))
    r = gcal.get("/app/google/callback", params={"error": "access_denied"},
                 follow_redirects=False)
    assert r.status_code == 303 and "denied" in r.headers["location"]
    assert store.get_google_account(store.get_user_by_name("tester")["id"]) is None
    assert gcal.get("/app/google/callback", params={"code": "xyz", "state": q["state"][0]},
                    follow_redirects=False).status_code == 303  # state still valid


def test_google_callback_survives_a_failed_token_exchange(gcal, monkeypatch):
    signup(gcal)
    _, q = google_state_of(gcal)
    monkeypatch.setattr(app.requests, "post",
                        lambda *a, **kw: GResp({}, status=400))
    r = gcal.get("/app/google/callback", params={"code": "xyz", "state": q["state"][0]},
                 follow_redirects=False)
    assert "refused" in r.headers["location"]
    assert store.get_google_account(store.get_user_by_name("tester")["id"]) is None


def test_gcal_refreshes_an_expired_token_in_place(gcal, monkeypatch):
    signup(gcal)
    uid = store.get_user_by_name("tester")["id"]
    store.save_google_account(uid, "old", "REF", time.time() - 10, "UTC")
    posts = []

    def fake_post(url, data=None, **kw):
        posts.append(data)
        return GResp({"access_token": "new", "expires_in": 3500})

    monkeypatch.setattr(app.requests, "post", fake_post)
    monkeypatch.setattr(app.requests, "get", lambda *a, **kw: GResp({"items": []}))
    g = app.Gcal(uid)
    assert g._token() == "new"
    assert posts[0]["grant_type"] == "refresh_token"
    assert posts[0]["refresh_token"] == "REF"
    row = store.get_google_account(uid)
    assert row["access_token"] == "new" and row["refresh_token"] == "REF"
    assert row["expires_at"] > time.time()


def test_gcal_untouched_token_skips_the_refresh(gcal, monkeypatch):
    signup(gcal)
    uid = store.get_user_by_name("tester")["id"]
    store.save_google_account(uid, "live", "REF", time.time() + 3600, "UTC")

    def fail_post(*a, **kw):
        pytest.fail("no refresh while the token is fresh")

    monkeypatch.setattr(app.requests, "post", fail_post)
    monkeypatch.setattr(app.requests, "get", lambda *a, **kw: GResp({"items": []}))
    assert app.Gcal(uid)._token() == "live"


def test_gcal_invalid_grant_deletes_the_row_and_raises(gcal, monkeypatch):
    signup(gcal)
    uid = store.get_user_by_name("tester")["id"]
    store.save_google_account(uid, "old", "REF", time.time() - 10, "UTC")
    monkeypatch.setattr(app.requests, "post",
                        lambda *a, **kw: GResp({"error": "invalid_grant"}, status=400))
    with pytest.raises(app.GcalDisconnected):
        app.Gcal(uid)._token()
    assert store.get_google_account(uid) is None


def test_gcal_events_day_window_uses_the_stored_timezone(gcal, monkeypatch):
    signup(gcal)
    uid = store.get_user_by_name("tester")["id"]
    store.save_google_account(uid, "live", "REF", time.time() + 3600, "Asia/Tokyo")
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.update(url=url, params=params, auth=headers["Authorization"])
        return GResp({"items": [
            {"start": {"dateTime": "2030-01-15T10:00:00+09:00"}, "summary": "Standup"},
            {"start": {"date": "2030-01-15"}, "summary": "Off skiing"},
        ]})

    monkeypatch.setattr(app.requests, "get", fake_get)
    events = app.Gcal(uid).events("2030-01-15")
    assert seen["url"] == f"{app.GCAL_API}/calendars/primary/events"
    assert seen["params"]["timeMin"] == "2030-01-15T00:00:00+09:00"
    assert seen["params"]["timeMax"] == "2030-01-16T00:00:00+09:00"
    assert seen["params"]["singleEvents"] == "true"
    assert seen["auth"] == "Bearer live"
    assert events == [{"start": "2030-01-15T10:00:00+09:00", "title": "Standup"},
                      {"start": "2030-01-15", "title": "Off skiing"}]


def test_gcal_create_event_all_day_and_timed(gcal, monkeypatch):
    signup(gcal)
    uid = store.get_user_by_name("tester")["id"]
    store.save_google_account(uid, "live", "REF", time.time() + 3600, "Europe/London")
    bodies = []

    def fake_post(url, json=None, headers=None, timeout=None):
        bodies.append(json)
        return GResp({"id": "e1"})

    monkeypatch.setattr(app.requests, "post", fake_post)
    g = app.Gcal(uid)
    assert g.create_event("Bin day", "2030-06-05")["start"] == {"date": "2030-06-05"}
    assert bodies[-1]["end"] == {"date": "2030-06-06"}
    assert g.create_event("Dentist", "2030-06-05", "14:30",
                          duration_min=45)["start"] == {"dateTime": "2030-06-05T14:30:00+01:00"}
    assert bodies[-1]["end"] == {"dateTime": "2030-06-05T15:15:00+01:00"}


def test_gcal_api_401_deletes_the_row_and_raises(gcal, monkeypatch):
    signup(gcal)
    uid = store.get_user_by_name("tester")["id"]
    store.save_google_account(uid, "live", "REF", time.time() + 3600, "UTC")
    monkeypatch.setattr(app.requests, "get",
                        lambda *a, **kw: GResp({}, status=401))
    with pytest.raises(app.GcalDisconnected):
        app.Gcal(uid).events("today")
    assert store.get_google_account(uid) is None


class FakeGcal:
    """Connected stub for tool/prompt tests."""

    connected = True
    email = "ian@example.com"
    timezone = "Europe/London"

    def events(self, day):
        return [{"start": "2030-01-15T10:00:00+09:00", "title": "Standup"}]

    def create_event(self, title, day, when="", duration_min=60):
        return {"summary": title}


def test_execute_tool_speaks_calendar_results(gcal):
    out = app.execute_tool("gcal_list_events", {"day": "today"}, gcal=FakeGcal())
    assert out == {"ok": True, "day": "today", "events": [
        {"when": "10:00", "title": "Standup"}]}
    out = app.execute_tool("gcal_create_event",
                           {"title": "Dentist", "day": "tomorrow", "time": "14:30"},
                           gcal=FakeGcal())
    assert out["ok"] is True and out["created"] == "Dentist" and out["time"] == "14:30"
    out = app.execute_tool("gcal_create_event", {"title": "Off", "day": "tomorrow"},
                           gcal=FakeGcal())
    assert out["time"] == "all day"


def test_bad_day_and_time_reach_the_llm_as_errors(gcal, monkeypatch):
    signup(gcal)
    uid = store.get_user_by_name("tester")["id"]
    store.save_google_account(uid, "live", "REF", time.time() + 3600, "UTC")
    monkeypatch.setattr(app.requests, "post",
                        lambda *a, **kw: pytest.fail("bad input never reaches Google"))
    g = app.Gcal(uid)
    out = app.execute_tool("gcal_create_event", {"title": "x", "day": "next week"},
                           gcal=g)
    assert out["ok"] is False and "next week" in out["detail"]
    out = app.execute_tool("gcal_create_event",
                           {"title": "x", "day": "tomorrow", "time": "3pm"}, gcal=g)
    assert out["ok"] is False and "3pm" in out["detail"]


def test_prompt_lists_calendar_only_when_connected(gcal):
    no_cal = app.build_system_prompt(t=app.Trello("k", "t"))
    assert "gcal_list_events" not in no_cal
    assert "calendar isn't set up yet" in no_cal
    with_cal = app.build_system_prompt(t=app.Trello("k", "t"), gcal=FakeGcal())
    assert "gcal_list_events" in with_cal
    assert "Never guess a day or a time" in with_cal
    assert "timezone Europe/London" in with_cal


def test_google_disconnect_revokes_and_deletes(gcal, monkeypatch):
    signup(gcal)
    uid = store.get_user_by_name("tester")["id"]
    store.save_google_account(uid, "ACC", "REF", time.time() + 3600, "UTC")
    posts = []
    monkeypatch.setattr(app.requests, "post",
                        lambda url, data=None, **kw: posts.append((url, data)) or GResp({}))
    r = gcal.post("/app/google/disconnect")
    assert r.status_code == 200
    assert r.json()["calendar"]["connected"] is False
    assert posts[0][0] == "https://oauth2.googleapis.com/revoke"
    assert posts[0][1] == {"token": "REF"}
    assert store.get_google_account(uid) is None


def test_google_disconnect_survives_a_dead_revoke(gcal, monkeypatch):
    signup(gcal)
    uid = store.get_user_by_name("tester")["id"]
    store.save_google_account(uid, "ACC", "REF", time.time() + 3600, "UTC")

    def dead(url, data=None, **kw):
        raise app.requests.ConnectionError("down")

    monkeypatch.setattr(app.requests, "post", dead)
    assert gcal.post("/app/google/disconnect").status_code == 200
    assert store.get_google_account(uid) is None
