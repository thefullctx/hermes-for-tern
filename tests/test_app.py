from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

from hermes_for_tern.app import App
from hermes_for_tern.state import Question


class FakeBackend:
    def __init__(self):
        self.requests = []
        self.answers = []

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
