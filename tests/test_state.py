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
    assert [row.kind for row in state.rows] == ["user", "assistant", "tool", "assistant", "delivered"]
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


def test_streamed_text_is_released_evenly_and_completely():
    state = Conversation()
    state.begin("hello")
    state.event("message.delta", {"text": "x" * 300})
    row = state.rows[-1]
    shown, now = [], state._paced_at
    for _ in range(60):
        now += 1 / 30
        state.pace(now)
        shown.append(row.shown)
    assert 0 < shown[0] < 300
    assert shown == sorted(shown)
    assert max(b - a for a, b in zip(shown, shown[1:])) < 60
    assert row.visible == row.text


def test_replaced_text_keeps_only_the_agreeing_revealed_prefix():
    state = Conversation()
    state.begin("hello")
    state.event("message.delta", {"text": "Hello wrld"})
    row = state.rows[-1]
    row.shown = 10
    state.event("message.complete", {"text": "Hello world!"})
    assert row.text == "Hello world!"
    assert row.shown == 7


def test_fresh_text_fades_and_then_settles():
    state = Conversation()
    state.begin("hello")
    state.event("message.delta", {"text": "Smooth words arrive gently."})
    row, now = state.rows[-1], state._paced_at
    for _ in range(3):
        now += 1 / 30
        state.pace(now)
    *_, newest = row.fresh(now)
    assert newest and row.visible.endswith(newest)
    assert "".join(row.fresh(now)) == row.visible
    assert row.fresh(now + 1) == ("", "", "")


def subagent(kind, aid, **extra):
    return f"subagent.{kind}", {"subagent_id": aid, "goal": f"goal {aid}", "task_index": 0, **extra}


def test_subagents_join_their_delegation_and_settle():
    state = Conversation()
    state.begin("audit")
    state.event("tool.start", {"tool_id": "d", "name": "delegate_task", "args": {}})
    state.event(*subagent("spawn_requested", "a"))
    assert state.tools["d"].agents[0].status == "pending"
    state.event(*subagent("start", "a", model="haiku"))
    state.event(*subagent("tool", "a", tool_name="read_file", tool_preview="x.py"))
    agent = state.tools["d"].agents[0]
    assert (agent.status, agent.model, agent.tool, agent.tools) == ("running", "haiku", "read_file", 1)
    state.event(*subagent("complete", "a", status="timeout", summary="Too slow", duration_seconds=2))
    assert (agent.status, agent.summary, agent.tool) == ("failed", "Too slow", "")
    state.event(*subagent("tool", "a", tool_name="late"))
    assert agent.status == "failed"
    assert not state.tools["d"].collapsed


def test_subagents_without_a_delegate_call_get_their_own_row():
    state = Conversation()
    state.begin("audit")
    state.event(*subagent("start", "a"))
    state.event(*subagent("start", "b", parent_id="a", depth=2))
    row = state.rows[-1]
    assert row.name == "delegate_task" and row.status == "running" and len(row.agents) == 2
    state.event(*subagent("complete", "b", status="completed"))
    assert row.status == "running"
    state.event(*subagent("complete", "a", status="completed"))
    assert row.status == "done"


def test_unknown_or_idle_subagent_events_are_ignored_and_interrupt_aborts():
    state = Conversation()
    state.event(*subagent("start", "a"))
    assert not state.agents
    state.begin("audit")
    state.event(*subagent("tool", "ghost"))
    assert not state.agents
    state.event(*subagent("start", "a"))
    state.event("message.complete", {"status": "interrupted"})
    assert state.agents["a"].status == "aborted"


def test_a_completed_turn_is_signed_with_its_time_and_tokens_but_a_stopped_one_is_not():
    state = Conversation()
    state.begin("hello")
    state.turn_started -= 75
    state.event("message.complete", {"text": "Hi", "usage": {"total": 12480}})
    assert state.rows[-1].kind == "delivered"
    assert state.rows[-1].text == "delivered · 1m 15s · 12.5K tokens"
    state.begin("again")
    state.event("message.complete", {"status": "interrupted"})
    assert state.rows[-1].kind == "notice"
