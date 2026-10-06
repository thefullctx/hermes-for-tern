from hermes_for_tern.state import Conversation


def assistant_texts(state):
    return [row.text for row in state.rows if row.kind == "assistant"]


def test_streamed_final_is_replaced_not_appended():
    state = Conversation()
    state.begin("hello")
    state.event("message.delta", {"text": "Hello"})
    state.event("message.complete", {"text": "Hello!", "status": "complete"})
    assert assistant_texts(state) == ["Hello!"]
    assert not state.busy


def test_interim_tool_final_preserves_order_and_does_not_repeat_commentary():
    state = Conversation()
    state.begin("inspect")
    state.event("message.delta", {"text": "I'll inspect the project."})
    state.event("message.interim", {"text": "I'll inspect the project.", "already_streamed": True})
    state.event("tool.start", {"tool_id": "t1", "name": "terminal", "args": {"command": "pwd"}})
    state.event("tool.complete", {"tool_id": "t1", "result": {"output": "/project"}, "duration_s": 0.2})
    state.event("message.delta", {"text": "This is a Python project."})
    state.event("message.complete", {"text": "This is a Python project.", "status": "complete"})
    assert [row.kind for row in state.rows] == ["user", "assistant", "tool", "assistant"]
    assert assistant_texts(state) == ["I'll inspect the project.", "This is a Python project."]
    assert state.tools["t1"].status == "done"


def test_reused_completion_and_next_turn_do_not_duplicate_text():
    state = Conversation()
    state.begin("first")
    state.event("message.interim", {"text": "Already delivered.", "already_streamed": False})
    state.event("message.complete", {"text": "Already delivered.", "response_reused": True})
    state.begin("second")
    state.event("message.complete", {"text": "New reply."})
    assert assistant_texts(state) == ["Already delivered.", "New reply."]


def test_interrupted_tool_turn_can_be_followed_by_a_successful_turn():
    state = Conversation()
    state.begin("long job")
    state.event("tool.start", {"tool_id": "t", "name": "terminal"})
    state.event("message.complete", {"status": "interrupted"})
    assert state.tools["t"].status == "cancelled"
    assert not state.busy
    state.begin("try again")
    state.event("message.complete", {"text": "OK", "status": "complete"})
    assert assistant_texts(state) == ["OK"]


def test_tool_failure_is_visible():
    state = Conversation()
    state.event("tool.start", {"tool_id": "t", "name": "read_file"})
    state.event("tool.complete", {"tool_id": "t", "result": {"error": "File missing"}})
    assert state.tools["t"].status == "error"
    assert "File missing" in state.tools["t"].text
