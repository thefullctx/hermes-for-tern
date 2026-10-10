import re
import time
from pathlib import Path

from tern_sdk.reconcile import View

from hermes_for_tern.editor import Draft
from hermes_for_tern.state import EFFORTS, Conversation, Question
from hermes_for_tern.views import CLIP, trail, view


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


def test_undelivered_turn_offers_retry_only_when_hermes_marks_it_retryable():
    state = Conversation()
    state.ready = True
    state.begin("hello")
    state.event(
        "message.complete",
        {"status": "error", "text": "HTTP 429 from the provider", "error_surface": {"retryable": True}},
    )
    built = View.build(
        view(state, Draft(), Draft(), Path("/p"), noop, noop, noop, noop, noop, None, None, noop)
    )
    card = built.nodes()["main.canvas.r1.r2"].wire()
    assert card["p"]["role"] == "undelivered" and card["p"]["head"][1]["t"].strip() == "429"
    assert "main.canvas.r1.r2.actions" in built.nodes()

    # A surface that says nothing about retryability earns no retry key.
    state.begin("hello")
    state.event("message.complete", {"status": "error", "text": "Boom.", "error_surface": {}})
    assert (
        "main.canvas.r3.r4.actions"
        not in View.build(
            view(state, Draft(), Draft(), Path("/p"), noop, noop, noop, noop, noop, None, None, noop)
        ).nodes()
    )


def test_an_undelivered_turn_names_the_fix_before_a_retry_that_cannot_work():
    state = Conversation()
    state.ready = True
    state.begin("hello")
    state.event(
        "message.complete",
        {
            "status": "error",
            "text": "Anthropic rejected the key.",
            "error_surface": {
                "layer": "auth",
                "code": "auth_failed",
                "retryable": False,
                "auth_kind": "api_key",
                "api_key_env": "ANTHROPIC_API_KEY",
                "provider": "anthropic",
                "provider_label": "Anthropic",
            },
        },
    )
    built = View.build(
        view(state, Draft(), Draft(), Path("/p"), noop, noop, noop, noop, noop, None, None, noop)
    )
    assert ids_with_role(built, "cause") == ["main.canvas.r1.r2.cause"]
    hint = next(n.wire()["p"]["text"] for n in built.nodes().values() if n.wire()["id"].endswith(".cause"))
    assert hint == "set ANTHROPIC_API_KEY"
    assert "main.canvas.r1.r2.actions" not in built.nodes()


def test_an_undelivered_turn_says_when_the_rate_limit_lifts():
    state = Conversation()
    state.ready = True
    state.begin("hello")
    state.event(
        "message.complete",
        {
            "status": "error",
            "text": "Rate limited (429).",
            "error_surface": {
                "layer": "provider",
                "code": "rate_limit",
                "retryable": True,
                "resets_at": time.time() + 200,
            },
        },
    )
    built = View.build(
        view(state, Draft(), Draft(), Path("/p"), noop, noop, noop, noop, noop, None, None, noop)
    )
    hint = next(n.wire()["p"]["text"] for n in built.nodes().values() if n.wire()["id"].endswith(".cause"))
    # a second may tick between the payload and the render, so the seconds are a range
    assert re.fullmatch(r"the limit lifts in 3m (19|20)s", hint)
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


def build(state, draft=None, effort=None):
    return View.build(
        view(
            state,
            draft or Draft(),
            Draft(),
            Path("/project"),
            noop,
            noop,
            noop,
            noop,
            noop,
            effort=effort,
        )
    )


def ids_with_role(built, role):
    return [
        node["id"]
        for node in (n.wire() for n in built.nodes().values())
        if node.get("p", {}).get("role") == role
    ]


def lamps(built):
    """The floating lamp that names the pressed effort level."""
    return [
        node["id"]
        for node in (n.wire() for n in built.nodes().values())
        if "hft-effort" in node.get("p", {}).get("class", "")
    ]


def icon_name(built):
    return next(
        node["p"]["name"]
        for node in (n.wire() for n in built.nodes().values())
        if node["k"] == "icon" and node["id"].startswith("layer.dispatches")
    )


def test_a_held_follow_up_is_shown_in_the_dock_and_counted():
    state = Conversation()
    state.ready = True
    state.begin("first")
    state.queue("and then this")
    state.queue("last thing")
    built = build(state)
    assert ids_with_role(built, "queued") == ["dock.queued-0.queued", "dock.queued-1.queued"]
    counts = [
        node["p"]["text"]
        for node in (n.wire() for n in built.nodes().values())
        if node.get("p", {}).get("role") == "depth"
    ]
    assert sorted(counts) == ["1/2", "2/2"]


def test_the_queue_hint_only_appears_while_hermes_is_actually_working():
    state = Conversation()
    state.ready = True
    state.begin("first")
    assert not ids_with_role(build(state), "queued-hint")
    built = build(state, Draft("another thought"))
    assert ids_with_role(built, "queued-hint") == ["dock.queued-hint"]


def test_every_offered_choice_is_numbered_on_its_button():
    state = Conversation()
    state.session_id = "live"
    state.questions["r1"] = Question("r1", "approval", {"choices": ["once", "session", "deny"]})
    built = build(state)
    hints = sorted(
        node["p"]["text"]
        for node in (n.wire() for n in built.nodes().values())
        if node.get("p", {}).get("class") == "hft-hint"
    )
    assert hints == ["1", "2", "3"]


def test_a_choice_no_key_can_reach_is_not_numbered():
    state = Conversation()
    state.session_id = "live"
    many = [f"choice {index}" for index in range(11)]
    state.questions["r1"] = Question(
        "r1", "clarify", {"questions": [{"qid": "a", "question": "Pick one.", "choices": many}]}
    )
    built = build(state)
    hints = sorted(
        node["p"]["text"]
        for node in (n.wire() for n in built.nodes().values())
        if node.get("p", {}).get("class") == "hft-hint"
    )
    assert hints == [str(index) for index in range(1, 10)]  # "10" and "11" no key can answer
    assert len(ids_with_role(built, "clarification")) == 1


def test_a_tool_body_past_the_clip_says_how_much_it_left_out():
    state = Conversation()
    state.begin("go")
    state.event("tool.complete", {"tool_id": "t", "name": "terminal", "result": {"output": "x" * 12}})
    assert not ids_with_role(build(state), "clipped")
    state.event(
        "tool.complete", {"tool_id": "u", "name": "terminal", "result": {"output": "y" * (CLIP + 250)}}
    )
    note = ids_with_role(build(state), "clipped")
    assert note == ["main.canvas.r1.r3.clipped.clip"]
    text = next(n.wire()["p"]["text"] for n in build(state).nodes().values() if n.wire()["id"] == note[0])
    assert "250 more characters" in text


def test_the_effort_ring_sits_beside_the_model_and_steps_through_every_level():
    state = Conversation()
    assert EFFORTS == ("off", "minimal", "low", "medium", "high", "xhigh", "max")
    seen = []
    for _ in range(len(EFFORTS) + 1):
        ring = next(
            n.wire()
            for n in build(state, effort=lambda: None).nodes().values()
            if n.wire().get("p", {}).get("role") == "effort"
        )
        assert ring["k"] == "effort"
        assert ring["p"]["actions"] == {"click": "click"}
        seen.append(ring["p"]["level"])
        state.cycle_effort()
    # One full loop and back to where it started.
    assert seen[1:] == list(EFFORTS[1:]) + [EFFORTS[0]]
    assert seen[-1] == EFFORTS[0]


def test_the_pressed_level_floats_with_a_lamp_then_goes_away_itself():
    state = Conversation()
    assert lamps(build(state)) == []
    state.cycle_effort()
    assert lamps(build(state)) == ["layer.dispatches.effort-1"]
    assert icon_name(build(state)) == "lightbulb"
    # A second press re-keys it, so the same level can show again.
    state.cycle_effort()
    assert lamps(build(state)) == ["layer.dispatches.effort-2"]
    state.effort_notice = time.monotonic() - 1
    state.pace(time.monotonic())
    assert state.effort_notice is None
    assert lamps(build(state)) == []
