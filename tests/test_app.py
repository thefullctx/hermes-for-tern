import queue
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

from tern_sdk import Key

from hermes_for_tern.app import App
from hermes_for_tern.editor import Draft
from hermes_for_tern.state import QUEUE, Conversation, Question
from hermes_for_tern.views import sheet_rows


class FakeBackend:
    def __init__(self):
        self.requests = []
        self.answers = []
        self.events = queue.Queue()

    def request(self, method, params):
        future = Future()
        self.requests.append((method, params, future))
        return future

    def answer(self, rid, result=None, **kwargs):
        self.answers.append((rid, result, kwargs))


def app():
    instance = App(SimpleNamespace(caps=SimpleNamespace(cols=100)), [], Path("/project"), {})
    instance.backend = FakeBackend()
    return instance


def test_startup_schedules_session_creation_without_losing_its_future():
    instance = app()
    instance.request("client.capabilities", {"server_requests": True})
    instance.backend.requests[-1][2].set_result({"server_requests": ["approval", "clarify"]})
    instance.check_requests()
    assert instance.pending[0][0] == "session.create"
    instance.backend.requests[-1][2].set_result(
        {"session_id": "live", "stored_session_id": "stored", "info": {}}
    )
    instance.check_requests()
    assert instance.state.session_id == "live"
    assert instance.state.ready


def test_approval_can_only_send_an_offered_choice_with_original_rpc_id():
    instance = app()
    instance.state.session_id = "live"
    instance.server_request(
        {
            "id": "srq-1",
            "method": "approval",
            "params": {"session_id": "live", "request_id": "approval-1", "choices": ["once", "deny"]},
        }
    )
    instance.approve("srq-1", "always")
    assert not instance.backend.answers
    instance.approve("srq-1", "once")
    assert instance.backend.answers == [("srq-1", {"choice": "once"}, {})]
    assert not instance.state.questions


def test_multiple_clarifications_and_multiselect_answer_together():
    instance = app()
    question = Question(
        "srq-q",
        "clarify",
        {
            "questions": [
                {"qid": "a", "question": "Color?"},
                {"qid": "b", "question": "Sizes?", "multi_select": True, "choices": ["S", "M"]},
            ]
        },
    )
    instance.state.questions[question.rid] = question
    instance.answer_question(question, "Blue")
    assert not instance.backend.answers
    instance.choose(question.rid, "S")
    instance.choose(question.rid, "M")
    instance.choose(question.rid, None)
    assert instance.backend.answers == [("srq-q", {"answers": {"a": "Blue", "b": "M, S"}}, {})]


def test_unsupported_request_fails_promptly_and_busy_submit_is_held():
    instance = app()
    instance.state.session_id = "live"
    instance.server_request({"id": "srq-t", "method": "tour", "params": {"session_id": "live"}})
    assert instance.backend.answers[0][2]["error"]["code"] == -32601
    assert instance.state.rows[-1].text.startswith("This frontend does not support tour")
    instance.state.ready = True
    instance.state.busy = True
    instance.submit("Do something")
    assert not instance.backend.requests


def pending_approval(instance, choices=("once", "session", "always", "deny")):
    instance.state.session_id = "live"
    instance.server_request(
        {
            "id": "srq-k",
            "method": "approval",
            "params": {"session_id": "live", "request_id": "a-1", "choices": list(choices)},
        }
    )
    return "srq-k"


def test_a_pending_approval_answers_to_digits_enter_and_escape():
    instance = app()
    rid = pending_approval(instance)
    instance.input(Key(name="3"))
    assert instance.backend.answers[-1] == (rid, {"choice": "always"}, {})
    assert not instance.state.questions

    rid = pending_approval(instance)
    instance.input(Key(name="enter"))
    assert instance.backend.answers[-1] == (rid, {"choice": "once"}, {})

    rid = pending_approval(instance)
    instance.input(Key(name="escape"))
    assert instance.backend.answers[-1] == (rid, {"choice": "deny"}, {})


def test_a_digit_past_the_last_choice_is_ordinary_typing_and_esc_denies_the_ask():
    instance = app()
    instance.state.session_id = "live"
    instance.state.ready = True
    rid = pending_approval(instance, choices=("once", "deny"))
    instance.input(Key(name="7", text="7"))
    assert not instance.backend.answers
    assert instance.draft.text == "7"
    instance.input(Key(name="escape"))
    assert instance.backend.answers == [(rid, {"choice": "deny"}, {})]


def test_clarification_digits_pick_a_choice_until_the_answer_draft_has_text():
    instance = app()
    instance.state.session_id = "live"
    question = Question(
        "srq-c", "clarify", {"questions": [{"qid": "a", "question": "Size?", "choices": ["S", "M"]}]}
    )
    instance.state.questions[question.rid] = question
    instance.input(Key(name="2"))
    assert instance.backend.answers == [("srq-c", {"answers": {"a": "M"}}, {})]

    instance.state.questions[question.rid] = question
    instance.answer_draft = Draft("42", 2)
    instance.input(Key(name="2", text="2"))
    assert instance.answer_draft.text == "422"
    assert len(instance.backend.answers) == 1


def test_a_message_composed_while_hermes_works_is_held_then_sent_when_the_turn_ends():
    instance = app()
    instance.state.session_id = "live"
    instance.state.ready = True
    instance.state.begin("first")
    instance.submit("and then this")
    assert instance.state.queued == ["and then this"]
    assert not [r for r in instance.backend.requests if r[0] == "prompt.submit"]

    instance.backend.events.put(
        {"method": "event", "params": {"session_id": "live", "type": "message.complete", "payload": {}}}
    )
    instance.read_backend()
    assert not instance.state.queued
    assert [r[1]["text"] for r in instance.backend.requests if r[0] == "prompt.submit"] == ["and then this"]


def test_the_queue_never_outgrows_its_limit_and_drops_the_oldest():
    state = Conversation()
    for index in range(QUEUE + 3):
        state.queue(f"note {index}")
    assert len(state.queued) == QUEUE
    assert state.queued[0] == "note 3"


def test_clear_empties_the_transcript_but_keeps_the_live_session():
    state = Conversation()
    state.session_id = "live"
    state.ready = True
    state.begin("hello")
    state.event("tool.complete", {"tool_id": "t", "name": "terminal", "result": {"output": "x"}})
    state.queue("later")
    state.clear()
    assert not state.rows and not state.tools
    # A held follow-up is pending work, not transcript: /clear must not eat what was typed.
    assert state.queued == ["later"]
    assert state.session_id == "live" and state.ready


def test_doctor_reports_the_launcher_without_leaving_the_pane():
    instance = app()
    instance.state.session_id = "live"
    instance.run_command("/doctor")
    said = " ".join(row.text for row in instance.state.rows)
    assert "project: /project" in said
    assert "session: live" in said


CATALOG = {
    "model": "claude-sonnet-4",
    "provider": "anthropic",
    "providers": [
        {
            "slug": "anthropic",
            "name": "Anthropic",
            "models": ["claude-sonnet-4", "claude-haiku-4"],
            "authenticated": True,
            "pricing": {"claude-sonnet-4": {"input": "$3", "output": "$15"}},
        },
        {
            "slug": "openai",
            "name": "OpenAI",
            "models": ["gpt-5"],
            "authenticated": False,
            "key_env": "OPENAI_API_KEY",
        },
        {"slug": "copilot", "name": "GitHub Copilot", "models": ["claude-sonnet-4"], "authenticated": True},
    ],
}


def ready(instance):
    instance.state.session_id = "live"
    instance.state.ready = True


def settled(instance, result):
    """Answer the newest request and let the loop pick the result up."""
    instance.backend.requests[-1][2].set_result(result)
    instance.check_requests()


def test_the_model_command_asks_hermes_for_its_catalog_once_and_opens_the_sheet():
    instance = app()
    ready(instance)
    instance.draft = Draft("/model", len("/model"))
    instance.submit()
    assert instance.state.sheet is not None
    assert instance.backend.requests[-1][0] == "model.options"
    settled(instance, CATALOG)
    assert instance.state.models is CATALOG
    # the sheet is already open, so a second press only refreshes it
    instance.model_event("open")
    assert [method for method, *_ in instance.backend.requests].count("model.options") == 2


def test_typing_while_the_sheet_is_open_filters_it_instead_of_the_composer():
    instance = app()
    ready(instance)
    instance.model_event("open")
    instance.sheet_key(Key(name="m", text="m"))
    assert instance.draft.text == ""
    assert instance.state.sheet.query == "m"
    for name, text in (("o", "o"), ("d", "d"), ("e", "e"), ("l", "l")):
        instance.input(Key(name=name, text=text))
    assert instance.state.sheet.query == "model"
    assert instance.draft.text == ""
    instance.input(Key(name="backspace"))
    assert instance.state.sheet.query == "mode"
    instance.input(Key(name="escape"))
    assert instance.state.sheet is None


def test_enter_switches_the_row_the_cursor_sits_on_and_arrows_move_it():
    instance = app()
    ready(instance)
    instance.state.models = CATALOG
    instance.state.info = {"model": "claude-sonnet-4", "provider": "anthropic"}
    instance.model_event("open")
    assert [row["id"] for row in sheet_rows(instance.state)] == [
        "anthropic/claude-sonnet-4",
        "claude-haiku-4",
        "gpt-5",
        "copilot/claude-sonnet-4",
    ]
    instance.confirm_model()
    method, params, _ = instance.backend.requests[-1]
    # two providers carry this name, so its value names the one this session is on
    assert method == "config.set" and params["value"] == "anthropic/claude-sonnet-4"
    instance.input(Key(name="down"))
    instance.confirm_model()
    assert instance.backend.requests[-1][1]["value"] == "claude-haiku-4"
    instance.input(Key(name="up"))
    instance.input(Key(name="up"))
    instance.confirm_model()
    assert instance.backend.requests[-1][1]["value"] == "anthropic/claude-sonnet-4"


def test_an_expensive_model_asks_before_switching():
    instance = app()
    ready(instance)
    instance.state.models = CATALOG
    instance.state.info = {"model": "gpt-5", "provider": "openai"}
    instance.model_event("open")
    instance.switch_model("anthropic/claude-sonnet-4")
    settled(instance, {"confirm_required": True, "confirm_message": "This model is expensive."})
    assert instance.state.sheet.confirming
    assert instance.state.rows[-1].text == "This model is expensive."
    instance.confirm_model()
    method, params, _ = instance.backend.requests[-1]
    assert method == "config.set" and params["confirm_expensive_model"] is True


def test_a_switch_mid_turn_is_held_for_the_next_turn_and_says_so():
    instance = app()
    ready(instance)
    instance.state.begin("work")
    instance.state.models = CATALOG
    instance.state.info = {"model": "gpt-5", "provider": "openai"}
    instance.model_event("open")
    instance.switch_model("anthropic/claude-sonnet-4")
    settled(instance, {"value": "anthropic/claude-sonnet-4", "deferred": True})
    assert instance.state.sheet is None
    assert [d.text for d in instance.state.dispatches] == ["model switches when this turn ends"]


def test_a_row_pick_switches_straight_away():
    instance = app()
    ready(instance)
    instance.state.models = CATALOG
    instance.state.info = {"model": "gpt-5", "provider": "openai"}
    instance.model_event("open")
    instance.model_event("pick", "anthropic/claude-sonnet-4")
    assert instance.backend.requests[-1][1]["value"] == "anthropic/claude-sonnet-4"
    instance.state.sheet = None
    instance.model_event("open")
    instance.model_event("pick", "copilot/claude-sonnet-4")
    assert instance.backend.requests[-1][1]["value"] == "copilot/claude-sonnet-4"


def test_a_masked_ask_is_answered_from_the_composer_and_never_shown():
    instance = app()
    instance.state.session_id = "live"
    instance.surface = SimpleNamespace(focus=lambda _: None)
    instance.server_request(
        {
            "id": "srq-s",
            "method": "secret",
            "params": {"session_id": "live", "env_var": "GITHUB_TOKEN", "prompt": "Token?"},
        }
    )
    assert instance.state.secret() is not None
    # typing is masked in the view and the real value never reaches the wire as a row
    from hermes_for_tern.views import mask

    instance.input(Key(name="g", text="g"))
    for ch in "hp_abc123":
        instance.input(Key(name=ch, text=ch))
    assert mask(instance.secret.text) == "••••••••••"
    editors = [node.wire() for node in built_nodes(instance) if node.wire().get("k") == "editor"]
    assert [editor["p"]["text"] for editor in editors] == ["••••••••••"]
    # and the answer itself appears nowhere in what the pane is sent
    shown = [node.wire().get("p", {}).get("text") for node in built_nodes(instance)]
    assert "ghp_abc123" not in [value for value in shown if isinstance(value, str)]
    instance.submit()
    assert instance.backend.answers == [("srq-s", {"value": "ghp_abc123"}, {})]
    assert not instance.state.questions
    assert instance.secret.text == ""  # the answer is not kept once it is sent


def test_escape_skips_a_masked_ask_the_way_hermes_words_it():
    instance = app()
    instance.state.session_id = "live"
    instance.surface = SimpleNamespace(focus=lambda _: None)
    instance.server_request(
        {"id": "srq-v", "method": "vault.code", "params": {"session_id": "live", "site": "bank"}}
    )
    instance.input(Key(name="escape"))
    assert instance.backend.answers == [("srq-v", {"value": ""}, {})]


def test_a_side_question_goes_to_hermes_and_its_answer_arrives_as_a_dispatch():
    instance = app()
    instance.state.session_id = "live"
    instance.state.ready = True
    instance.side_question("what is the capital of France?")
    method, params, _ = instance.backend.requests[-1]
    assert method == "prompt.btw" and params["text"] == "what is the capital of France?"
    instance.backend.events.put(
        {
            "method": "event",
            "params": {
                "session_id": "live",
                "type": "btw.complete",
                "payload": {"task_id": "t1", "question": "what is the capital of France?", "text": "Paris."},
            },
        }
    )
    instance.read_backend()
    assert [d.text for d in instance.state.dispatches] == ["what is the capital of France?"]
    assert [d.sub for d in instance.state.dispatches] == ["Paris."]


def test_a_watched_child_reports_its_tail_and_can_be_interrupted_or_steered():
    instance = app()
    instance.state.session_id = "live"
    instance.state.begin("audit")
    instance.state.event(
        "subagent.start", {"subagent_id": "a1", "goal": "check the tests", "task_index": 0, "model": "small"}
    )
    instance.watch_agent("a1")
    assert instance.state.watch is not None
    method, params, _ = instance.backend.requests[-1]
    assert method == "subagent.tail" and params["subagent_id"] == "a1"
    instance.backend.requests[-1][2].set_result(
        {"subagent_id": "a1", "available": True, "text": "reading tests…", "truncated": True}
    )
    instance.check_requests()
    assert instance.state.watch.tail == "reading tests…" and instance.state.watch.truncated

    instance.input(Key(name="r", text="r"))
    for ch in "un faster":
        instance.input(Key(name=ch, text=ch))
    assert instance.state.watch.steer == "run faster"
    instance.input(Key(name="enter"))
    method, params, _ = instance.backend.requests[-1]
    assert method == "subagent.steer" and params["text"] == "run faster"
    assert instance.state.watch.steer == ""
    instance.backend.requests[-1][2].set_result(
        {"status": "queued", "subagent_id": "a1", "text": "run faster"}
    )
    instance.check_requests()
    assert [d.text for d in instance.state.dispatches] == ["steered the child"]

    instance.interrupt_agent()
    assert instance.backend.requests[-1][0] == "subagent.interrupt"
    instance.input(Key(name="escape"))
    assert instance.state.watch is None


def test_typing_in_the_composer_is_the_watchs_line_while_a_child_is_watched():
    instance = app()
    instance.state.session_id = "live"
    instance.state.watch = None
    instance.watch_agent("a1")
    instance.draft = Draft("a thought of my own")
    instance.input(Key(name="x", text="x"))
    assert instance.state.watch.steer == "x"
    assert instance.draft.text == "a thought of my own"


def built_nodes(instance):
    from hermes_for_tern.views import view

    from pathlib import Path as _Path

    tree = view(
        instance.state,
        instance.draft,
        instance.answer_draft,
        _Path("/project"),
        lambda: None,
        lambda *a: None,
        lambda *a: None,
        lambda *a: None,
        lambda *a: None,
        secret=instance.secret,
    )
    from tern_sdk.reconcile import View

    return View.build(tree).nodes().values()


def test_the_session_can_be_named_and_hermes_names_it_back():
    instance = app()
    instance.state.session_id = "live"
    instance.rename_session("Hunting a flaky test")
    method, params, _ = instance.backend.requests[-1]
    assert method == "session.title" and params["title"] == "Hunting a flaky test"
    settled(instance, {"title": "Hunting a flaky test", "session_key": "stored"})
    assert instance.state.title == "Hunting a flaky test"
    # and the auto-titler's own rename arrives as an event
    instance.backend.events.put(
        {
            "method": "event",
            "params": {
                "session_id": "live",
                "type": "session.title",
                "payload": {"session_id": "stored", "title": "Tern surface polish"},
            },
        }
    )
    instance.read_backend()
    assert instance.state.title == "Tern surface polish"


def test_a_held_follow_up_can_be_taken_back():
    instance = app()
    instance.state.ready = True
    instance.state.begin("work")
    instance.state.queue("first")
    instance.state.queue("second")
    instance.state.unqueue(0)
    assert instance.state.queued == ["second"]
    instance.state.unqueue(5)
    assert instance.state.queued == ["second"]


def ready_app():
    instance = app()
    instance.state.session_id = "live"
    instance.state.ready = True
    return instance


def answer(instance, method, result=None, error=None):
    """Answer the newest request for `method` and let the app read it."""
    future = next(f for m, _, f in reversed(instance.backend.requests) if m == method)
    if error:
        from hermes_for_tern.rpc import RpcError

        future.set_exception(RpcError(error))
    else:
        future.set_result(result)
    instance.check_requests()


def type_text(instance, text):
    for char in text:
        instance.input(Key(name=char, text=char))


HERMES_ITEMS = {
    "items": [
        {"text": "model", "display": "/model", "meta": "Switch model (usage: /model [m])", "kind": "command"},
        {"text": "moa", "display": "/moa", "meta": "Mixture of agents", "kind": "command"},
        {"text": "compress", "display": "/compress", "meta": "Compress context", "kind": "command"},
        {"text": "new", "display": "/new", "meta": "Start a new session", "kind": "command"},
        {"text": "work", "display": "/work", "meta": "A skill", "kind": "skill"},
    ],
    "replace_from": 1,
}


def test_typing_a_slash_shows_the_list_and_each_letter_narrows_it():
    from hermes_for_tern.views import slash_rows

    instance = ready_app()
    type_text(instance, "/")
    assert instance.draft.text == "/"  # the slash stays in the composer
    # this pane's commands show at once, before Hermes answers
    assert [row["name"] for row in slash_rows(instance.state, "/")][:3] == ["/model", "/btw", "/bg"]
    assert instance.backend.requests[-1][:2] == ("complete.slash", {"session_id": "live", "text": "/"})
    answer(instance, "complete.slash", HERMES_ITEMS)
    names = [row["name"] for row in slash_rows(instance.state, "/")]
    # Hermes's own commands and skills follow; its /model is this pane's, and /new is hidden
    assert names[-3:] == ["/moa", "/compress", "/work"] and names.count("/model") == 1 and "/new" not in names
    type_text(instance, "mo")
    # until Hermes answers "/mo", its last answer is narrowed to what is typed
    assert [row["name"] for row in slash_rows(instance.state, "/mo")] == ["/model", "/moa"]
    assert instance.backend.requests[-1][1]["text"] == "/mo"


def test_the_arrows_move_tab_completes_and_enter_runs_through_hermes():
    instance = ready_app()
    type_text(instance, "/")
    answer(instance, "complete.slash", HERMES_ITEMS)
    type_text(instance, "com")
    answer(instance, "complete.slash", {"items": HERMES_ITEMS["items"][2:3], "replace_from": 1})
    instance.input(Key(name="tab"))
    assert instance.draft.text == "/compress "
    instance.draft = Draft("/com", 4)
    instance.input(Key(name="enter"))
    # not one of this pane's commands: Hermes runs it
    assert instance.backend.requests[-1][:2] == ("slash.exec", {"session_id": "live", "command": "compress"})
    answer(instance, "slash.exec", {"output": "Compressed 12 turns."})
    assert instance.state.rows[-1].kind == "notice" and instance.state.rows[-1].text == "Compressed 12 turns."


def test_escape_puts_the_list_away_until_the_draft_changes():
    from hermes_for_tern.views import slash_open

    instance = ready_app()
    type_text(instance, "/mo")
    assert slash_open(instance.state, instance.draft)
    instance.input(Key(name="escape"))
    assert not slash_open(instance.state, instance.draft)
    assert instance.draft.text == "/mo"
    type_text(instance, "d")
    assert slash_open(instance.state, instance.draft)


def test_this_panes_commands_run_here_and_ones_needing_an_argument_wait_for_it():
    instance = ready_app()
    type_text(instance, "/bt")
    instance.input(Key(name="enter"))
    assert instance.draft.text == "/btw "  # /btw needs its question: Enter completes, nothing runs
    assert not any(method == "prompt.btw" for method, _, _ in instance.backend.requests)
    instance.run_command("/cost")
    instance.run_command("/clear")
    instance.run_command("/title Flaky test hunt")  # Hermes's spelling of /name
    assert instance.backend.requests[-1][:2] == (
        "session.title",
        {"session_id": "live", "title": "Flaky test hunt"},
    )
    instance.run_command("/exit")
    assert instance.exit


def test_long_output_folds_into_a_row_and_help_adds_this_panes_commands():
    instance = ready_app()
    instance.run_command("/help")
    answer(
        instance, "slash.exec", {"output": "Available Commands\n" + "\n".join(f"/c{i}" for i in range(20))}
    )
    row = instance.state.rows[-1]
    assert row.kind == "command" and row.name == "/help" and row.collapsed
    assert "In this pane: /model" in row.text


def test_skills_go_through_command_dispatch_and_send_what_they_expand_to():
    instance = ready_app()
    instance.run_command("/work fix the leak")
    answer(instance, "slash.exec", error={"code": 4018, "message": "skill command"})
    assert instance.backend.requests[-1][:2] == (
        "command.dispatch",
        {"session_id": "live", "name": "work", "arg": "fix the leak"},
    )
    answer(
        instance,
        "command.dispatch",
        {"type": "skill", "name": "work", "message": "SKILL BODY", "display": "/work fix the leak"},
    )
    assert instance.backend.requests[-1][:2] == (
        "prompt.submit",
        {"session_id": "live", "text": "SKILL BODY"},
    )
    # the transcript shows the command as typed, not the expansion written for the model
    assert instance.state.rows[-1].kind == "user" and instance.state.rows[-1].text == "/work fix the leak"


def test_hermes_refusals_and_prefills_are_shown_as_hermes_says_them():
    instance = ready_app()
    instance.run_command("/undo")
    answer(instance, "slash.exec", error={"code": 4018, "message": "pending input"})
    answer(
        instance,
        "command.dispatch",
        {"type": "prefill", "message": "my last message", "notice": "Undid one turn."},
    )
    assert instance.draft.text == "my last message"
    assert instance.state.rows[-1].text == "Undid one turn."
    instance.run_command("/compress")
    answer(instance, "slash.exec", error={"code": 4009, "message": "Hermes is busy; try when the turn ends."})
    assert instance.state.rows[-1].text == "/compress: Hermes is busy; try when the turn ends."


def resuming_app(**options):
    instance = App(SimpleNamespace(caps=SimpleNamespace(cols=100)), [], Path("/project"), options)
    instance.backend = FakeBackend()
    instance.request("client.capabilities", {"server_requests": True})
    answer(instance, "client.capabilities", {})
    return instance


STORED = {
    "session_id": "live-2",
    "session_key": "20261011_abc",
    "info": {"title": "Flaky test hunt", "model": "m"},
    "messages": [
        {"role": "user", "text": "Why does CI fail?"},
        {"role": "assistant", "text": "Let me look."},
        {"role": "tool", "name": "terminal", "context": "uv run pytest -q"},
        {"role": "assistant", "text": "The fixture closes the pipe early."},
    ],
}


def test_resume_picks_up_the_stored_session_and_redraws_it():
    instance = resuming_app(resume="20261011_abc")
    assert instance.backend.requests[-1][:2] == (
        "session.resume",
        {"session_id": "20261011_abc", "cols": 100},
    )
    answer(instance, "session.resume", STORED)
    state = instance.state
    assert state.session_id == "live-2" and state.stored_session_id == "20261011_abc" and state.ready
    assert [row.kind for row in state.rows] == ["user", "assistant", "tool", "assistant", "notice"]
    assert state.rows[2].target == "uv run pytest -q" and state.rows[2].status == "done"
    assert state.rows[-1].text == "Resumed “Flaky test hunt” · 3 messages before this."
    assert instance.resume_hint() == (
        'Resume this session with:\n  hermes --resume 20261011_abc\n  hermes -c "Flaky test hunt"'
    )


def test_continue_takes_the_newest_session_and_a_missing_one_starts_fresh():
    instance = resuming_app(continue_last=True, profile="work")
    assert instance.backend.requests[-1][:2] == ("session.most_recent", {"profile": "work"})
    answer(instance, "session.most_recent", {"session_id": "20261010_xyz"})
    assert instance.backend.requests[-1][1]["session_id"] == "20261010_xyz"
    answer(instance, "session.resume", error={"code": 4007, "message": "session not found"})
    assert instance.backend.requests[-1][0] == "session.create"
    assert "session.create" != instance.backend.requests[-1][1].get("resume")  # no resume flag leaks in
    assert "This is a new session." in instance.state.rows[-1].text

    nothing = resuming_app(continue_last=True)
    answer(nothing, "session.most_recent", {"session_id": None})
    assert nothing.backend.requests[-1][0] == "session.create"


def test_a_session_that_never_spoke_leaves_no_resume_hint():
    instance = ready_app()
    instance.state.stored_session_id = "20261011_new"
    assert instance.resume_hint() == ""
    instance.state.begin("hello")
    instance.options["profile"] = "work"
    assert instance.resume_hint() == "Resume this session with:\n  hermes --resume 20261011_new -p work"


def test_one_ctrl_c_never_leaves_two_in_a_row_do():
    import time

    instance = ready_app()
    instance.input(Key(name="c", ctrl=True))
    assert not instance.exit and instance.state.exit_armed is not None
    instance.input(Key(name="c", ctrl=True))
    assert instance.exit

    # the first press stops a running turn or clears the draft, and the window closes after a while
    instance = ready_app()
    instance.state.begin("hello")
    instance.input(Key(name="c", ctrl=True))
    assert instance.backend.requests[-1][0] == "session.interrupt" and not instance.exit
    instance.state.pace(time.monotonic() + 3)
    assert instance.state.exit_armed is None
    instance.state.busy = False  # the turn has ended
    instance.draft = Draft("half a thought", 14)
    instance.input(Key(name="c", ctrl=True))
    assert instance.draft.text == "" and not instance.exit


LISTED = {
    "sessions": [
        {"id": "20261011_now", "title": "", "preview": "", "message_count": 0, "started_at": 0},
        {"id": "20261010_ci", "title": "Flaky test hunt", "message_count": 12, "started_at": 1},
        {
            "id": "20261009_old",
            "title": "",
            "preview": "Plan the launch",
            "message_count": 4,
            "started_at": 1,
        },
    ]
}


def switching_app():
    instance = ready_app()
    instance.state.stored_session_id = "20261011_now"
    instance.state.begin("hello")
    instance.state.busy = False
    return instance


def test_resume_lists_sessions_and_picks_one_up_in_place():
    instance = switching_app()
    instance.run_command("/resume")
    assert instance.state.sheet.kind == "sessions"
    assert instance.backend.requests[-1][:2] == ("session.list", {"limit": 50})
    answer(instance, "session.list", LISTED)
    instance.input(Key(name="enter"))  # the newest session other than this one
    assert instance.backend.requests[-1][:2] == ("session.resume", {"session_id": "20261010_ci", "cols": 100})
    answer(instance, "session.resume", STORED)
    state = instance.state
    assert state.sheet is None and state.session_id == "live-2" and state.stored_session_id == "20261011_abc"
    assert [row.kind for row in state.rows][0] == "user" and state.rows[0].text == "Why does CI fail?"
    assert "hello" not in [row.text for row in state.rows]  # the old transcript is gone
    assert instance.backend.requests[-1][:2] == ("session.close", {"session_id": "live"})


def test_resume_refuses_mid_turn_and_says_when_you_are_already_there():
    instance = switching_app()
    instance.state.busy = True
    instance.run_command("/resume 20261010_ci")
    assert instance.state.rows[-1].text == "Finish or stop this turn before switching sessions."
    instance.state.busy = False
    instance.run_command("/resume 20261011_now")
    assert instance.state.rows[-1].text == "This is the session you are in."
    instance.run_command("/sessions")  # Hermes's spelling
    assert instance.state.sheet.kind == "sessions"


def test_a_failed_switch_leaves_this_session_as_it_was():
    instance = switching_app()
    instance.run_command("/resume nowhere")
    answer(instance, "session.resume", error={"code": 4007, "message": "session not found"})
    assert instance.state.session_id == "live" and instance.state.rows[0].text == "hello"
    assert "Could not resume 'nowhere'" in instance.state.rows[-1].text
    assert not any(method == "session.create" for method, _, _ in instance.backend.requests)


def test_a_new_session_offers_the_last_one_on_the_welcome():
    from tern_sdk.reconcile import View

    from hermes_for_tern.views import view

    instance = resuming_app()
    answer(instance, "session.create", {"session_id": "live", "stored_session_id": "20261011_now"})
    assert instance.backend.requests[-1][:2] == ("session.list", {"limit": 50})
    answer(instance, "session.list", LISTED)
    assert instance.state.earlier["id"] == "20261010_ci"  # not this new one, and not an empty one
    picked = []
    built = View.build(
        view(instance.state, Draft(), Draft(), Path("/p"), *[lambda *a: None] * 5, resume=picked.append)
    )
    node = built.nodes()["layer.stage.welcome.suggestions.continue"]
    assert built.nodes()["layer.stage.welcome.suggestions.continue.label"].wire()["p"]["text"] == (
        "Continue “Flaky test hunt”"
    )
    node.handlers["action:click"](None)
    assert picked == ["20261010_ci"]
