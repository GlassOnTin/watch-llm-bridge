"""Unit tests for the Phase 2 LLM routing: tool dispatch, echo fallback,
and the spoken error the watch hears when a target doesn't resolve."""
import json

import pytest

import app

BOARDS = {"Home": "b1", "Plans": "b2"}
LISTS = {
    "b1": [("Shopping List", "l1"), ("Done", "l2"), ("Done", "l3")],
    "b2": [("Food", "l4"), ("Events", "l5")],
}


@pytest.fixture
def stocked_trello(monkeypatch):
    monkeypatch.setattr(app.trello, "boards", dict(BOARDS))
    monkeypatch.setattr(app.trello, "lists_by_board", {k: list(v) for k, v in LISTS.items()})


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_list_boards_tool_keeps_duplicate_names(stocked_trello):
    out = app.execute_tool("trello_list_boards", {})
    assert out == {
        "Home": ["Shopping List", "Done", "Done"],
        "Plans": ["Food", "Events"],
    }


def test_unknown_tool_raises_not_implemented(stocked_trello):
    with pytest.raises(NotImplementedError, match="unknown tool"):
        app.execute_tool("gcal_what", {})


def test_calendar_tool_without_a_connection_says_disconnected(stocked_trello):
    out = app.execute_tool("gcal_list_events", {"day": "today"})
    assert out == {"ok": False, "error": "calendar_disconnected"}
    out = app.execute_tool("gcal_create_event", {"title": "x", "day": "today"})
    assert out == {"ok": False, "error": "calendar_disconnected"}


def test_create_card_tool_defaults_desc_to_empty(monkeypatch, stocked_trello):
    seen = {}

    def fake_create(board, list_name, name, desc=""):
        seen.update(board=board, list_name=list_name, name=name, desc=desc)
        return {"name": name}

    monkeypatch.setattr(app.trello, "create_card", fake_create)
    out = app.execute_tool(
        "trello_create_card",
        {"board": "Home", "list": "Shopping List", "name": "Milk"},
    )
    assert out["ok"] is True
    assert seen == {"board": "Home", "list_name": "Shopping List", "name": "Milk", "desc": ""}


def test_route_echoes_without_llm_backend(monkeypatch):
    monkeypatch.setattr(app, "LLM_API_KEY", "")
    assert app.route("hello") == "echo: hello"


def test_route_executes_tool_and_replies(monkeypatch, stocked_trello):
    monkeypatch.setattr(app, "LLM_API_KEY", "k")
    monkeypatch.setattr(app, "LLM_BASE_URL", "https://llm.test/v1")
    monkeypatch.setattr(app, "LLM_MODEL", "test-model")
    monkeypatch.setattr(
        app.trello, "cards", lambda board, list_name: [{"name": "Milk"}, {"name": "Beans"}]
    )
    posts = []

    def fake_post(url, headers=None, **kw):
        posts.append(kw.get("json"))
        if len(posts) == 1:
            tool_call = {
                "id": "t1",
                "function": {
                    "name": "trello_list_cards",
                    "arguments": json.dumps({"board": "Home", "list": "Shopping List"}),
                },
            }
            return FakeResp({"choices": [{"message": {"role": "assistant", "tool_calls": [tool_call]}}]})
        return FakeResp({"choices": [{"message": {"content": "Two items: milk and beans."}}]})

    monkeypatch.setattr(app.requests, "post", fake_post)
    assert app.route("what's on my shopping list") == "Two items: milk and beans."
    tool_msg = posts[1]["messages"][3]
    assert tool_msg["role"] == "tool"
    assert json.loads(tool_msg["content"])["cards"] == ["Milk", "Beans"]


def test_route_teaches_on_unresolvable_list(monkeypatch, stocked_trello):
    monkeypatch.setattr(app, "LLM_API_KEY", "k")
    monkeypatch.setattr(app, "LLM_BASE_URL", "https://llm.test/v1")
    monkeypatch.setattr(app, "LLM_MODEL", "test-model")

    def fake_post(url, headers=None, **kw):
        tool_call = {
            "id": "t1",
            "function": {
                "name": "trello_list_cards",
                "arguments": json.dumps({"board": "Home", "list": "Freezer"}),
            },
        }
        return FakeResp({"choices": [{"message": {"tool_calls": [tool_call]}}]})

    monkeypatch.setattr(app.requests, "post", fake_post)
    reply = app.route("what's in the freezer")
    assert reply.startswith("I couldn't match that.")
    assert "Shopping List" in reply  # candidates are offered, not a guess


def test_route_replies_on_llm_outage(monkeypatch):
    monkeypatch.setattr(app, "LLM_API_KEY", "k")
    monkeypatch.setattr(app, "LLM_BASE_URL", "https://llm.test/v1")
    monkeypatch.setattr(app, "LLM_MODEL", "test-model")

    class Dead:
        def post(self, *a, **kw):
            raise app.requests.ConnectionError("down")

    monkeypatch.setattr(app.requests, "post", Dead().post)
    assert app.route("hello") == "The assistant brain is unreachable right now."


def test_system_prompt_carries_inventory_and_aliases(stocked_trello):
    prompt = app.build_system_prompt()
    assert "Shopping List" in prompt
    assert '"move board" means board "Move To A Nicer Spot"' in prompt


def test_spoken_error_truncates_long_candidate_lists(stocked_trello):
    try:
        app.resolve_target(BOARDS, LISTS, "Home", "Freezer")
    except KeyError as e:
        reply = app.spoken_error(e.args[0])
    assert "Shopping List" in reply
    assert "Freezer" in reply
    assert "(known:" not in reply


def test_spoken_error_without_candidates():
    reply = app.spoken_error("board 'Work' matches nothing")
    assert reply.startswith("I couldn't match that.")
    assert "Options include" not in reply

def test_create_card_without_board_resolves_unique_board(monkeypatch, stocked_trello):
    seen = {}

    def fake_create(board, list_name, name, desc=""):
        seen.update(board=board, list_name=list_name)
        return {"name": name}

    monkeypatch.setattr(app.trello, "create_card", fake_create)
    out = app.execute_tool(
        "trello_create_card", {"list": "Shopping List", "name": "Milk"}
    )
    assert out == {"ok": True, "created": "Milk", "list": "Shopping List", "board": "Home"}
    assert seen["board"] == "Home"


def test_create_card_without_board_and_ambiguous_list_asks(monkeypatch, stocked_trello):
    monkeypatch.setattr(
        app.trello, "lists_by_board",
        {"b1": [("Done", "l2"), ("Done", "l3")], "b2": [("Done", "l4")]})
    called = False

    def fake_create(*a, **kw):
        nonlocal called
        called = True
        return {"name": "x"}

    monkeypatch.setattr(app.trello, "create_card", fake_create)
    out = app.execute_tool("trello_create_card", {"list": "Done", "name": "x"})
    assert out == {"ok": False, "error": "ambiguous_board", "list": "Done", "boards": ["Home", "Plans"]}
    assert not called  # nothing was written before the ask


# --- deeper Trello surface: find_card and the card/list tools ---


def stub_cards(monkeypatch, cards):
    """find_card's _get: the open cards of the queried list or board."""
    seen = {}

    def fake_get(path, **params):
        seen["path"] = path
        return cards

    monkeypatch.setattr(app.trello, "_get", fake_get)
    return seen


def test_find_card_exact_then_substring(monkeypatch, stocked_trello):
    seen = stub_cards(monkeypatch, [
        {"id": "c1", "name": "Milk"},
        {"id": "c2", "name": "Bin day reminder"},
        {"id": "c3", "name": "Beans"},
    ])
    hit = app.trello.find_card("Home", "milk")
    assert hit["id"] == "c1"
    assert seen["path"] == "/boards/b1/cards"
    assert app.trello.find_card("Home", "bin day")["id"] == "c2"


def test_find_card_in_a_named_list_hits_the_list(monkeypatch, stocked_trello):
    seen = stub_cards(monkeypatch, [{"id": "c1", "name": "Milk"}])
    assert app.trello.find_card("Home", "Milk", "Shopping List")["id"] == "c1"
    assert seen["path"] == "/lists/l1/cards"


def test_find_card_not_found_offers_candidates(monkeypatch, stocked_trello):
    stub_cards(monkeypatch, [{"name": "Milk"}, {"name": "Beans"}])
    with pytest.raises(KeyError) as e:
        app.trello.find_card("Home", "Dentist")
    assert "Beans" in str(e.value) and "Milk" in str(e.value)


def test_find_card_two_matches_asks(monkeypatch, stocked_trello):
    stub_cards(monkeypatch, [{"name": "Milk"}, {"name": "milk"}])
    with pytest.raises(KeyError, match="matches 2"):
        app.trello.find_card("Home", "Milk")


def test_move_card_tool_targets_the_other_list(monkeypatch, stocked_trello):
    monkeypatch.setattr(app.trello, "lists_by_board", {
        **LISTS, "b1": LISTS["b1"] + [("In Basket", "l6")]})
    monkeypatch.setattr(app.trello, "find_card",
                        lambda board, name, list_name="": {"id": "c1", "name": "Milk"})
    seen = {}

    def fake_update(card_id, **fields):
        seen.update(card_id=card_id, **fields)
        return {"id": card_id}

    monkeypatch.setattr(app.trello, "update_card", fake_update)
    out = app.execute_tool("trello_move_card", {
        "board": "Home", "list": "Shopping List", "name": "Milk", "to_list": "In Basket"})
    assert out == {"ok": True, "moved": "Milk", "to_list": "In Basket",
                   "to_board": "Home"}
    assert seen == {"card_id": "c1", "idList": "l6"}


def test_move_card_bad_target_names_the_lists(monkeypatch, stocked_trello):
    monkeypatch.setattr(app.trello, "find_card",
                        lambda board, name, list_name="": {"id": "c1", "name": "Milk"})
    out = app.execute_tool("trello_move_card", {
        "board": "Home", "name": "Milk", "to_list": "Food"})
    assert out["error"] == "bad_target"


def test_cross_board_move_passes_both_ids(monkeypatch, stocked_trello):
    monkeypatch.setattr(app.trello, "find_card",
                        lambda board, name, list_name="": {"id": "c1", "name": "Milk"})
    seen = {}
    monkeypatch.setattr(app.trello, "update_card",
                        lambda card_id, **fields: seen.update(card_id=card_id, **fields)
                        or {"id": card_id})
    app.execute_tool("trello_move_card", {
        "board": "Home", "name": "Milk", "to_list": "Food", "to_board": "Plans"})
    assert seen == {"card_id": "c1", "idList": "l4", "idBoard": "b2"}


def test_archive_card_with_no_match_asks(monkeypatch, stocked_trello):
    stub_cards(monkeypatch, [{"name": "Milk"}, {"name": "Beans"}])
    out = app.execute_tool("trello_archive_card", {"board": "Home", "name": "Dentist"})
    assert out == {"ok": False, "error": "no_card",
                   "candidates": ["Beans", "Milk"]}


def test_archive_card_with_two_matches_asks(monkeypatch, stocked_trello):
    stub_cards(monkeypatch, [{"name": "Milk"}, {"name": "milk"}])
    out = app.execute_tool("trello_archive_card", {"board": "Home", "name": "Milk"})
    assert out["error"] == "ambiguous_card"
    assert out["candidates"] == ["Milk", "milk"]


def test_card_actions_find_the_board_from_the_card_name_alone(monkeypatch, stocked_trello):
    def fake_find(board, name, list_name=""):
        if board == "Home":
            return {"id": "c1", "name": "Milk"}
        raise KeyError(f"card '{name}' matches nothing on board '{board}' "
                       f"(known: Beans, Milk)")

    monkeypatch.setattr(app.trello, "find_card", fake_find)
    seen = {}

    def fake_details(board, name, list_name=""):
        seen["board"] = board
        return {"name": "Milk", "desc": "", "due": None, "labels": [], "comments": []}

    monkeypatch.setattr(app.trello, "card_details", fake_details)
    out = app.execute_tool("trello_card_details", {"name": "Milk"})
    assert out["ok"] is True
    assert seen["board"] == "Home"


def test_set_due_bare_day_becomes_noon(monkeypatch, stocked_trello):
    monkeypatch.setattr(app.trello, "find_card",
                        lambda board, name, list_name="": {"id": "c1", "name": "Milk"})
    seen = {}
    monkeypatch.setattr(app.trello, "update_card",
                        lambda card_id, **fields: seen.update(**fields) or {"id": card_id})
    out = app.execute_tool("trello_set_due", {
        "board": "Home", "name": "Milk", "day": "2030-06-05"})
    assert out["ok"] is True
    assert seen == {"due": "2030-06-05T12:00:00"}


def test_set_due_with_a_time_and_a_bad_day(monkeypatch, stocked_trello):
    monkeypatch.setattr(app.trello, "find_card",
                        lambda board, name, list_name="": {"id": "c1", "name": "Milk"})
    seen = {}
    monkeypatch.setattr(app.trello, "update_card",
                        lambda card_id, **fields: seen.update(**fields) or {"id": card_id})
    app.execute_tool("trello_set_due", {
        "board": "Home", "name": "Milk", "day": "tomorrow", "time": "14:30"})
    assert seen["due"].endswith("T14:30:00")
    bad = app.execute_tool("trello_set_due", {
        "board": "Home", "name": "Milk", "day": "someday"})
    assert bad == {"ok": False, "error": "bad_day",
                   "detail": "calendar day 'someday' is not today, tomorrow, "
                             "yesterday or YYYY-MM-DD"}


def test_edit_card_needs_something_to_change(monkeypatch, stocked_trello):
    monkeypatch.setattr(app.trello, "find_card",
                        lambda board, name, list_name="": {"id": "c1", "name": "Milk"})
    out = app.execute_tool("trello_edit_card", {"board": "Home", "name": "Milk"})
    assert out == {"ok": False, "error": "nothing_to_edit"}
    seen = {}
    monkeypatch.setattr(app.trello, "update_card",
                        lambda card_id, **fields: seen.update(**fields) or {"id": card_id})
    out = app.execute_tool("trello_edit_card", {
        "board": "Home", "name": "Milk", "new_name": "Oat milk", "desc": "from the market"})
    assert out["ok"] is True and out["renamed_to"] == "Oat milk"
    assert seen == {"name": "Oat milk", "desc": "from the market"}


def test_comment_card_posts_the_text(monkeypatch, stocked_trello):
    monkeypatch.setattr(app.trello, "find_card",
                        lambda board, name, list_name="": {"id": "c1", "name": "Milk"})
    seen = {}
    monkeypatch.setattr(app.trello, "add_comment",
                        lambda card_id, text: seen.update(card_id=card_id, text=text)
                        or {})
    out = app.execute_tool("trello_comment_card", {
        "board": "Home", "name": "Milk", "text": "picked up"})
    assert out == {"ok": True, "commented": "picked up"}
    assert seen == {"card_id": "c1", "text": "picked up"}


def test_board_overview_maps_lists_to_card_names(monkeypatch, stocked_trello):
    def fake_get(path, **params):
        assert path == "/boards/b1/lists"
        return [{"name": "Shopping List", "cards": [{"name": "Milk"}]},
                {"name": "Done", "cards": []}]

    monkeypatch.setattr(app.trello, "_get", fake_get)
    out = app.execute_tool("trello_board_overview", {"board": "Home"})
    assert out == {"ok": True, "board": "Home",
                   "lists": {"Shopping List": ["Milk"], "Done": []}}


def test_create_and_rename_list_update_the_cache(monkeypatch, stocked_trello):
    def fake_post(url, params=None, timeout=None):
        assert "/lists" in url
        return FakeResp({"id": "l9", "name": "Freezer"})

    monkeypatch.setattr(app.requests, "post", fake_post)
    out = app.execute_tool("trello_create_list", {"board": "Home", "name": "Freezer"})
    assert out == {"ok": True, "created_list": "Freezer", "board": "Home"}
    assert ("Freezer", "l9") in app.trello.lists_by_board["b1"]

    monkeypatch.setattr(app.trello, "_put", lambda path, **params: {"id": "l1"})
    out = app.execute_tool("trello_rename_list", {
        "board": "Home", "list": "Shopping List", "new_name": "Groceries"})
    assert out["ok"] is True
    assert ("Groceries", "l1") in app.trello.lists_by_board["b1"]
    assert ("Shopping List", "l1") not in app.trello.lists_by_board["b1"]


def test_card_details_tool_reads_the_full_body(monkeypatch, stocked_trello):
    def fake_get(path, **params):
        if path == "/boards/b1/cards":
            return [{"id": "c1", "name": "Milk"}]
        return {"id": "c1", "name": "Milk", "desc": "2 pints", "due": "2030-06-05T12:00:00",
                "url": "https://trello.com/c/c1",
                "labels": [{"name": "", "color": "green"}],
                "actions": [{"data": {"text": "bought it"}}]}

    monkeypatch.setattr(app.trello, "_get", fake_get)
    out = app.execute_tool("trello_card_details", {"board": "Home", "name": "Milk"})
    assert out == {"ok": True, "name": "Milk", "desc": "2 pints",
                   "due": "2030-06-05T12:00:00", "labels": ["green"],
                   "comments": ["bought it"]}


# --- calendar edit / delete tools ---

def test_calendar_edit_delete_without_a_connection_says_disconnected(stocked_trello):
    out = app.execute_tool("gcal_edit_event",
                           {"day": "tomorrow", "name": "Dentist", "new_time": "09:00"})
    assert out == {"ok": False, "error": "calendar_disconnected"}
    out = app.execute_tool("gcal_delete_event", {"day": "tomorrow", "name": "Dentist"})
    assert out == {"ok": False, "error": "calendar_disconnected"}


class FakeGcal:
    connected = True
    timezone = "Europe/London"
    _hit = {"id": "e1", "summary": "Dentist",
            "start": {"dateTime": "2030-01-15T14:30:00+00:00"},
            "end": {"dateTime": "2030-01-15T15:00:00+00:00"}}

    def __init__(self):
        self.updates = []
        self.deleted = None

    def find_events(self, name, day=""):
        return [self._hit]

    def update_event(self, event_id, **body):
        self.updates.append((event_id, body))
        return {"id": event_id}

    def delete_event(self, event_id):
        self.deleted = event_id
        return {"id": event_id}


def test_calendar_edit_moves_day_and_time_sends_both_start_and_end(stocked_trello):
    from datetime import timedelta
    g = FakeGcal()
    out = app.execute_tool("gcal_edit_event",
                           {"day": "tomorrow", "name": "Dentist",
                            "new_day": "2030-01-20", "new_time": "09:15",
                            "duration": 45}, gcal=g)
    assert out["ok"] is True and out["edited"] == "Dentist"
    assert out["moved_to"] == "09:15"
    assert g.updates == [("e1", {
        "start": {"dateTime": "2030-01-20T09:15:00+00:00",
                  "timeZone": "Europe/London"},
        "end": {"dateTime": "2030-01-20T10:00:00+00:00",
                "timeZone": "Europe/London"}})]
    assert timedelta(hours=1)  # keeps the duration import used below


def test_calendar_edit_with_no_changes_is_refused(stocked_trello):
    out = app.execute_tool("gcal_edit_event",
                           {"day": "tomorrow", "name": "Dentist"},
                           gcal=FakeGcal())
    assert out == {"ok": False, "error": "nothing_to_edit"}


def test_calendar_delete_removes_the_matched_event(stocked_trello):
    g = FakeGcal()
    out = app.execute_tool("gcal_delete_event",
                           {"day": "tomorrow", "name": "Dentist"}, gcal=g)
    assert out == {"ok": True, "deleted": "Dentist"}
    assert g.deleted == "e1"
