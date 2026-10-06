from pathlib import Path

from tern_sdk.reconcile import View

from hermes_for_tern.editor import Draft
from hermes_for_tern.state import Conversation
from hermes_for_tern.views import trail, view


def noop(*args):
    pass


def test_completed_terminal_output_is_readable_without_json_envelope():
    state = Conversation()
    state.event("tool.start", {"tool_id": "t", "name": "terminal", "args": {"command": "printf hello"}})
    state.event(
        "tool.complete", {"tool_id": "t", "result": {"output": "hello", "exit_code": 0, "error": None}}
    )
    assert state.tools["t"].text == "hello"
    assert state.tools["t"].exit_code == 0


def test_tool_nonzero_exit_is_a_failure_even_without_error_message():
    state = Conversation()
    state.event(
        "tool.complete", {"tool_id": "t", "name": "terminal", "result": {"output": "", "exit_code": 2}}
    )
    assert state.tools["t"].status == "error"


def test_dock_composer_retains_identity_across_busy_and_idle_views():
    state = Conversation()
    state.ready = True

    def build():
        return View.build(view(state, Draft(), Draft(), Path("/project"), noop, noop, noop, noop, noop))

    idle = build()
    state.begin("hello")
    busy = build()
    ops = idle.ops(busy, "s1")
    # A draft's focused node must not be deleted/recreated when a status indicator appears.
    assert not any(op[0] == "del" and op[1] == "dock.composer" for op in ops)


def test_assistant_shows_only_revealed_text_with_fading_marks():
    state = Conversation()
    state.begin("hello")
    state.event("message.delta", {"text": "First words, then more words."})
    row = state.rows[-1]
    row.reveal(12, 0.0)
    assert trail(row, 0.0) == [{"t": "First words,", "s": "dim"}]
    built = View.build(view(state, Draft(), Draft(), Path("/project"), noop, noop, noop, noop, noop))
    md = built.nodes()["main.canvas.r1.r2.body"].wire()
    assert md["p"]["text"] == "First words,"
    assert md["p"]["stream"] is True


def test_subagents_nest_under_their_parent_inside_the_delegation():
    state = Conversation()
    state.begin("audit")
    state.event("tool.start", {"tool_id": "d", "name": "delegate_task", "args": {}})
    for aid, parent in (("a", None), ("b", "a"), ("c", None)):
        state.event("subagent.start", {"subagent_id": aid, "goal": aid, "task_index": 0, "parent_id": parent})
    nodes = View.build(view(state, Draft(), Draft(), Path("/p"), noop, noop, noop, noop, noop)).nodes()
    assert "main.canvas.r1.r2.agents.a.b" in nodes
    assert "main.canvas.r1.r2.agents.c" in nodes
    assert nodes["main.canvas.r1.r2"].wire()["p"]["target"] == "3 subagents"
    assert nodes["main.canvas.r1"].wire()["p"]["tone"] == "pending"


def test_errand_progress_pill_and_fuel_line():
    from hermes_for_tern.views import progress

    items = [
        {"content": "a", "status": "completed"},
        {"content": "b", "status": "in_progress"},
        {"content": "c", "status": "cancelled"},
        {"content": "d", "status": "pending"},
    ]
    assert progress(items) == (1, 3, "b")
    state = Conversation()
    state.ready = True
    state.begin("work")
    state.event("todo.updated", {"todos": items, "revision": 1})
    state.event("session.usage", {"usage": {"context_percent": 42}})
    nodes = View.build(view(state, Draft(), Draft(), Path("/p"), noop, noop, noop, noop, noop)).nodes()
    assert nodes["dock.working.errands"].wire()["p"]["role"] == "errands"
    assert nodes["dock.fuel"].wire()["p"]["value"] == 0.42
    assert nodes["dock.bar.fuel"].wire()["p"]["text"] == "42% context"


def test_search_results_render_as_a_tree_with_inked_matches():
    state = Conversation()
    state.begin("find")
    args = {"pattern": r"timeout=\d", "target": "content"}
    state.event("tool.start", {"tool_id": "s", "name": "search_files", "args": args})
    result = {"matches_text": "a.py\n  3: x(timeout=5)\nb.py\n  7: timeout=1, timeout=2"}
    state.event("tool.complete", {"tool_id": "s", "name": "search_files", "result": result})
    nodes = View.build(view(state, Draft(), Draft(), Path("/p"), noop, noop, noop, noop, noop)).nodes()
    hit = nodes["main.canvas.r1.r2.output.f1h0"].wire()["p"]["spans"]
    assert [s["t"] for s in hit if s.get("s") == "mark"] == ["timeout=1", "timeout=2"]
    assert nodes["main.canvas.r1.r2.output.f0"].wire()["p"]["spans"][0]["t"] == "├─ "
    assert nodes["main.canvas.r1.r2"].wire()["p"]["target"] == r"timeout=\d"


def test_undelivered_turn_offers_retry_only_when_it_can():
    state = Conversation()
    state.ready = True
    state.begin("hello")
    state.event(
        "message.complete", {"status": "error", "text": "HTTP 429 from the provider", "error_surface": {}}
    )
    built = View.build(
        view(state, Draft(), Draft(), Path("/p"), noop, noop, noop, noop, noop, None, None, noop)
    )
    card = built.nodes()["main.canvas.r1.r2"].wire()
    assert card["p"]["role"] == "undelivered" and card["p"]["head"][1]["t"].strip() == "429"
    assert "main.canvas.r1.r2.actions" in built.nodes()


def signatures(state, assets):
    built = View.build(
        view(state, Draft(), Draft(), Path("/project"), noop, noop, noop, noop, noop, assets=assets)
    )
    return {
        node["p"]["role"]: node["p"]["blob"]
        for node in (n.wire() for n in built.nodes().values())
        if node["k"] == "image" and node["p"].get("role") in ("signature", "working-pen", "errands-signature")
    }


def test_hermes_signs_its_latest_delivered_turn_and_the_ink_dries_with_the_next():
    assets = {f"monogram-{use}": use for use in ("signed", "dry", "errands", "pen")}
    state = Conversation()
    state.begin("hello")
    state.event("message.complete", {"text": "Hi.", "status": "complete"})
    assert signatures(state, assets) == {"signature": "signed"}
    state.begin("again")
    assert signatures(state, assets) == {"signature": "dry"}
    assert signatures(state, {}) == {}


def test_the_pen_replaces_the_spinner_only_while_hermes_muses_and_errands_are_unfinished():
    assets = {f"monogram-{use}": use for use in ("signed", "dry", "errands", "pen")}
    state = Conversation()
    state.begin("hello")
    assert signatures(state, assets) == {}
    state.musing = True
    assert signatures(state, assets) == {"working-pen": "pen"}
    state.event("todo.updated", {"todos": [{"id": "1", "content": "Look", "status": "completed"}]})
    state.musing = True
    assert signatures(state, assets) == {"errands-signature": "errands"}
