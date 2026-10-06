from pathlib import Path

from tern_sdk.reconcile import View

from hermes_for_tern.editor import Draft
from hermes_for_tern.state import Conversation
from hermes_for_tern.views import view


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
