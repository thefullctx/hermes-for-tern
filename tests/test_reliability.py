"""User-visible guarantees across ambiguous delivery, session changes, and process failure."""

import json
import sys
import time
from pathlib import Path

import pytest
from tern_sdk import Key
from tern_sdk.reconcile import View

from hermes_for_tern.editor import Draft
from hermes_for_tern.recovery import RecoveryStore
from hermes_for_tern.rpc import Backend
from hermes_for_tern.state import QUEUE, Question
from hermes_for_tern.transcript import matches
from hermes_for_tern.views import MAX_TURNS, view
from test_app import STORED, FakeBackend, answer, ready_app, switching_app


def event(instance, kind, payload):
    instance.backend.events.put(
        {
            "method": "event",
            "params": {
                "session_id": instance.state.session_id,
                "type": kind,
                "payload": payload,
            },
        }
    )
    instance.read_backend()


def prompts(instance):
    return [params for method, params, _ in instance.backend.requests if method == "prompt.submit"]


def build(instance):
    noop = lambda *args: None  # noqa: E731
    return View.build(
        view(
            instance.state,
            instance.draft,
            instance.answer_draft,
            Path("/p"),
            noop,
            noop,
            noop,
            noop,
            noop,
            action=instance.local_action,
        )
    )


def test_full_queue_keeps_new_draft_and_all_previous_messages():
    instance = ready_app()
    instance.state.busy = True
    for index in range(QUEUE):
        instance.submit(f"queued {index}")
    instance.submit("keep me 🪶")
    assert instance.draft.text == "keep me 🪶"
    assert [message.text for message in instance.state.queued] == [f"queued {i}" for i in range(QUEUE)]
    assert not prompts(instance)


@pytest.mark.parametrize("status", ["interrupted", "error"])
def test_unsuccessful_turns_pause_queue_until_explicit_continue(status):
    instance = ready_app()
    instance.state.begin("first")
    instance.submit("second")
    event(instance, "message.complete", {"status": status})
    assert instance.state.queue_paused and [message.text for message in instance.state.queued] == ["second"]
    assert not prompts(instance)
    assert "dock.queue-actions" in build(instance).nodes()
    instance.continue_queue()
    assert prompts(instance) == [{"session_id": "live", "text": "second"}]


def test_stop_pauses_queue_even_if_backend_reports_success_in_the_race():
    instance = ready_app()
    instance.state.begin("first")
    instance.submit("second")
    instance.stop()
    event(instance, "message.complete", {"status": "complete"})
    assert not prompts(instance) and [message.text for message in instance.state.queued] == ["second"]


def test_new_draft_is_not_eaten_when_queue_continues():
    instance = ready_app()
    instance.state.queue("queued")
    instance.draft = Draft("unfinished", 10)
    instance.continue_queue()
    assert instance.draft.text == "unfinished"
    assert prompts(instance)[0]["text"] == "queued"


def test_held_switch_messages_are_sent_only_to_confirmed_destination():
    instance = switching_app()
    instance.resume_session("destination")
    instance.submit("for destination")
    assert not prompts(instance)
    assert not build(instance).nodes()["dock.composer"].wire()["p"]["sendable"]
    answer(instance, "session.resume", STORED)
    assert prompts(instance) == [{"session_id": "live-2", "text": "for destination"}]


@pytest.mark.parametrize("timeout", [False, True])
def test_failed_switch_preserves_held_messages_and_new_draft(timeout):
    instance = switching_app()
    instance.resume_session("destination")
    instance.submit("for destination")
    instance.draft = Draft("still typing", 12)
    if timeout:
        instance.pending = [(m, f, 0) for m, f, _ in instance.pending]
        instance.check_requests()
    else:
        answer(instance, "session.resume", error={"message": "missing", "code": 4007})
    assert instance.state.session_id == "live"
    assert instance.draft.text == "for destination\n\nstill typing"
    assert not prompts(instance)
    # A late response cannot replace the old session after the timeout.
    if timeout:
        instance.backend.requests[0][2].set_result(STORED)
        instance.check_requests()
        assert instance.state.session_id == "live"


def test_switching_with_paused_work_requires_resolving_old_session_queue():
    instance = switching_app()
    instance.state.queue("old session only")
    instance.resume_session("destination")
    assert not instance.switching
    assert [message.text for message in instance.state.queued] == ["old session only"]


def test_late_old_session_model_result_does_not_close_new_session_sheet():
    instance = switching_app()
    old = instance.request("config.set", instance.scoped(key="model", value="old"))
    instance.resume_session("destination")
    answer(instance, "session.resume", STORED)
    instance.state.open_sheet()
    old.set_result({})
    instance.check_requests()
    assert instance.state.sheet is not None


def test_disconnect_clears_questions_and_sensitive_drafts_and_marks_unfinished_tools():
    instance = ready_app()
    instance.state.begin("work")
    instance.state.event("tool.start", {"tool_id": "t", "name": "terminal"})
    instance.state.questions["secret"] = Question("secret", "secret", {})
    instance.secret = Draft("DO-NOT-KEEP", 11)
    instance.answer_draft = Draft("answer", 6)
    instance.draft = Draft("ordinary", 8)
    instance.state.queue("later")
    instance.fail("connection lost")
    assert instance.state.connection == "disconnected"
    assert instance.state.tools["t"].status == "cancelled"
    assert not instance.state.questions and not instance.secret.text and not instance.answer_draft.text
    assert instance.draft.text == "ordinary" and [message.text for message in instance.state.queued] == [
        "later"
    ]
    assert "dock.reconnect" in build(instance).nodes()


def test_reconnect_resumes_without_resending_previous_work(monkeypatch):
    instance = ready_app()
    instance.state.stored_session_id = "stored"
    instance.state.begin("already may have run")
    instance.draft = Draft("unsent", 6)
    instance.state.queue("later")
    instance.fail("crashed")
    fresh = FakeBackend()
    monkeypatch.setattr("hermes_for_tern.app.Backend", lambda *args: fresh)
    monkeypatch.setattr(instance.backend, "close", lambda: None, raising=False)
    instance.reconnect()
    event(instance, "gateway.ready", {})
    answer(instance, "client.capabilities", {})
    assert fresh.requests[-1][:2] == ("session.resume", {"session_id": "stored", "cols": 100})
    answer(instance, "session.resume", STORED)
    assert instance.state.ready and instance.state.connection == "ready"
    assert instance.draft.text == "unsent" and [message.text for message in instance.state.queued] == [
        "later"
    ]
    assert instance.state.queue_paused and not prompts(instance)


def test_failed_recovery_never_silently_creates_new_session(monkeypatch):
    instance = ready_app()
    instance.state.stored_session_id = "stored"
    instance.fail("crashed")
    fresh = FakeBackend()
    monkeypatch.setattr("hermes_for_tern.app.Backend", lambda *args: fresh)
    monkeypatch.setattr(instance.backend, "close", lambda: None, raising=False)
    instance.reconnect()
    event(instance, "gateway.ready", {})
    answer(instance, "client.capabilities", {})
    answer(instance, "session.resume", error={"message": "missing", "code": 4007})
    assert instance.state.failed
    assert not any(m == "session.create" for m, _, _ in fresh.requests)
    instance.reconnect(fresh=True)
    event(instance, "gateway.ready", {})
    answer(instance, "client.capabilities", {})
    assert fresh.requests[-1][0] == "session.create"


def dead_record(store, draft="unsent", queued=None, session="stored"):
    store.save(draft, queued or [], session, True)
    data = json.loads(store.path.read_text())
    data["pid"] = None
    store.path.write_text(json.dumps(data))
    return store.records()[0]


def test_recovery_is_private_atomic_scoped_and_ignores_live_panes(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    store = RecoveryStore(Path("/project"), "work")
    store.save("draft 🪶", ["later"], "stored", False)
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.directory.stat().st_mode & 0o777 == 0o700
    assert not store.records()  # this process still owns it
    record = dead_record(store, "draft 🪶", ["later"])
    assert record["session"] == "stored" and record["queue"] == ["later"]
    assert not RecoveryStore(Path("/other"), "work").records()
    assert not RecoveryStore(Path("/project"), "personal").records()
    store.save("", [], "stored", False)
    assert not store.path.exists()


def test_corrupt_recovery_files_do_not_prevent_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    store = RecoveryStore(tmp_path, None)
    store.directory.mkdir(parents=True)
    for i, value in enumerate(["broken", "[]", '{"version":1}', '{"version":999}']):
        (store.directory / f"{i}.json").write_text(value)
    assert store.records() == []


def test_draft_restore_resumes_its_own_session_and_never_sends_text(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    old = RecoveryStore(Path("/project"), None)
    record = dead_record(old, "draft 🪶", ["queued"], "20261011_abc")
    instance = switching_app()
    instance.store = RecoveryStore(Path("/project"), None)
    instance.saved_records = [record]
    instance.restore_drafts()
    assert instance.backend.requests[-1][1]["session_id"] == "20261011_abc"
    answer(instance, "session.resume", STORED)
    assert instance.draft.text == "draft 🪶" and [message.text for message in instance.state.queued] == [
        "queued"
    ]
    assert instance.state.queue_paused and not prompts(instance)
    assert not old.path.exists() and instance.store.path.exists()


def test_recovery_storage_excludes_secret_answers(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    instance = ready_app()
    instance.store = RecoveryStore(tmp_path, None)
    instance.secret = Draft("secret-value", 12)
    instance.answer_draft = Draft("secret-answer", 13)
    instance.draft = Draft("ordinary", 8)
    instance.save_drafts(force=True)
    saved = instance.store.path.read_text()
    assert "ordinary" in saved and "secret-value" not in saved and "secret-answer" not in saved


def test_crash_after_submit_retains_uncertain_work_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    instance = ready_app()
    instance.store = RecoveryStore(tmp_path, None)
    instance.state.begin("submitted")
    instance.fail("lost")
    assert json.loads(instance.store.path.read_text())["uncertain"]


def test_saved_text_is_not_overwritten_by_restoring_over_current_draft(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    instance = switching_app()
    store = RecoveryStore(tmp_path, None)
    instance.saved_records = [dead_record(store)]
    instance.draft = Draft("current", 7)
    instance.restore_drafts()
    assert instance.draft.text == "current" and store.path.exists()
    assert not instance.backend.requests


def test_search_reveals_old_turns_and_close_returns_to_latest():
    instance = ready_app()
    for i in range(MAX_TURNS + 10):
        instance.state.add("user", f"prompt {i}")
        instance.state.add("assistant", "needle 🪶" if i == 0 else f"response {i}")
    first_key = instance.state.rows[0].key
    assert f"main.canvas.{first_key}" not in build(instance).nodes()
    instance.run_command("/find NEEDLE")
    assert len(matches(instance.state)) == 1
    assert f"main.canvas.{first_key}" in build(instance).nodes()
    assert "dock.search" in build(instance).nodes()
    instance.local_action("find-close")
    assert f"main.canvas.{first_key}" not in build(instance).nodes()


def test_search_navigates_and_expands_tool_output():
    instance = ready_app()
    instance.state.add("user", "needle")
    tool = instance.state.add("tool", "needle in output", name="terminal")
    instance.run_command("/find needle")
    instance.local_action("find-next")
    assert instance.state.search_index == 1 and not tool.collapsed
    instance.local_action("find-next")
    assert instance.state.search_index == 0


def test_export_includes_full_hidden_output_but_no_secrets_or_diagnostics(tmp_path):
    instance = ready_app()
    instance.state.add("user", "question")
    instance.state.add("assistant", "answer")
    instance.state.add("tool", "x" * 49000, name="terminal", status="done", diff="+ change")
    instance.state.add("notice", "PRIVATE diagnostic")
    instance.secret = Draft("PRIVATE secret", 14)
    destination = tmp_path / "export.md"
    instance.export_transcript(str(destination))
    text = destination.read_text()
    assert "x" * 49000 in text and "+ change" in text
    assert "question" in text and "answer" in text
    assert "PRIVATE" not in text and destination.stat().st_mode & 0o777 == 0o600
    instance.export_transcript(str(destination))
    assert destination.read_text() == text
    assert "never overwritten" in instance.state.rows[-1].text


def test_secret_input_outranks_open_sheets_and_watched_child():
    instance = ready_app()
    instance.state.open_sheet()
    instance.state.open_watch("child")
    instance.state.questions["s"] = Question("s", "secret", {})
    instance.input(Key(name="x", text="x"))
    assert instance.secret.text == "x" and not instance.state.watch.steer
    assert not instance.state.sheet.query


def test_transport_cannot_block_ui_when_backend_stops_reading(tmp_path):
    backend = Backend([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path)
    try:
        began = time.monotonic()
        future = backend.request("big", {"text": "x" * 1_000_000})
        assert time.monotonic() - began < 1
        backend.forget(future)
        assert future.cancelled() and not backend._pending
    finally:
        backend.close()
    assert backend.process.poll() is not None


def test_malformed_response_id_does_not_kill_reader_with_unresolved_future(tmp_path):
    source = 'import sys; sys.stdin.readline(); print(\'{"id": [], "result": {}}\', flush=True)'
    backend = Backend([sys.executable, "-c", source], tmp_path)
    future = backend.request("go")
    try:
        assert "invalid RPC response id" in backend.events.get(timeout=2)["local_error"]
        with pytest.raises(BrokenPipeError):
            future.result(timeout=2)
    finally:
        backend.close()


def test_recovery_and_queue_button_callbacks_remain_callable_after_render():
    instance = ready_app()
    instance.state.queue("held")
    instance.state.queue_paused = True
    nodes = build(instance).nodes()
    nodes["dock.queue-actions.discard"].handlers["action:click"](None)
    assert not instance.state.queued
    instance.fail("lost")
    actions = []
    noop = lambda *args: None  # noqa: E731
    built = View.build(
        view(
            instance.state, Draft(), Draft(), Path("/p"), noop, noop, noop, noop, noop, action=actions.append
        )
    )
    built.nodes()["dock.reconnect.reconnect"].handlers["action:click"](None)
    assert actions == ["reconnect"]


def test_cancelled_secret_is_cleared_before_the_next_secret_question():
    instance = ready_app()
    instance.state.questions["old"] = Question("old", "secret", {})
    instance.secret = Draft("do-not-reuse", 12)
    event(instance, "request.cancel", {"id": "old"})
    instance.state.questions["new"] = Question("new", "secret", {})
    assert not instance.secret.text and not instance.secret.history


def test_backend_shutdown_kills_descendants_holding_pipes_open(tmp_path):
    import signal
    import threading

    child = 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print(\'{"method":"child.ready"}\',flush=True); time.sleep(30)'
    source = f"import subprocess,sys; subprocess.Popen([sys.executable, '-u', '-c', {child!r}])"
    backend = Backend([sys.executable, "-c", source], tmp_path)
    worker = threading.Thread(target=backend.close, daemon=True)
    try:
        assert backend.events.get(timeout=3)["method"] == "child.ready"
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert not backend._reader_thread.is_alive() and not backend._stderr_thread.is_alive()
    finally:
        backend._terminate_group(signal.SIGKILL)
        if worker.ident:
            worker.join(timeout=2)


def test_saved_text_survives_its_owning_process_exit(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    source = "from pathlib import Path; from hermes_for_tern.recovery import RecoveryStore; RecoveryStore(Path('/p'),'work').save('unsent 🪶',['later'],'stored',False)"
    subprocess.run([sys.executable, "-c", source], check=True)
    records = RecoveryStore(Path("/p"), "work").records()
    assert len(records) == 1 and records[0]["draft"] == "unsent 🪶"
    assert records[0]["queue"] == ["later"] and records[0]["session"] == "stored"


def test_recover_slash_command_consumes_itself_without_erasing_restored_draft(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    old = RecoveryStore(tmp_path, None)
    record = dead_record(old, "restore this", ["held"], "20261011_now")
    instance = switching_app()
    instance.store = RecoveryStore(tmp_path, None)
    instance.saved_records = [record]
    instance.draft = Draft("/recover", 8)
    instance.submit()
    assert instance.draft.text == "restore this"
    assert [message.text for message in instance.state.queued] == ["held"] and instance.state.queue_paused
    assert not prompts(instance)
