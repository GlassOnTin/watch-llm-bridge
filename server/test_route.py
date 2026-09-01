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
        app.execute_tool("gcal_list_events", {})


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
