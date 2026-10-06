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
