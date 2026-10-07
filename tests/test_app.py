import queue
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

from tern_sdk import Key

from hermes_for_tern.app import App
from hermes_for_tern.editor import Draft
from hermes_for_tern.state import QUEUE, Conversation, Question


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
    instance.server_request({"id": "srq-s", "method": "secret", "params": {"session_id": "live"}})
    assert instance.backend.answers[0][2]["error"]["code"] == -32601
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


def test_slash_commands_are_answered_here_and_never_reach_the_model():
    instance = app()
    instance.state.session_id = "live"
    instance.state.ready = True
    instance.run_command("/help")
    instance.run_command("/cost")
    instance.run_command("/nonsense")
    assert not instance.backend.requests
    assert "/help" in instance.state.rows[-3].text
    assert "is not a command" in instance.state.rows[-1].text

    instance.run_command("/clear")
    assert len(instance.state.rows) == 1
    assert "cleared" in instance.state.rows[0].text

    instance.run_command("/quit")
    assert instance.exit


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
