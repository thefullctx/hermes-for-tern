"""Single-threaded UI loop; Hermes runs over private pipes in a separate process."""

from __future__ import annotations

import queue
import os as os
import shlex as shlex
import time
import threading
from concurrent.futures import Future
from pathlib import Path

from tern_sdk import EditEvent, ErrorEvent, FocusEvent, GoneEvent, Key, SendEvent, UndoEvent, VisibleEvent

from .attachment_app import Attachments
from .slash_app import (
    Commands,
    COMMANDS as COMMANDS,
    HELP as HELP,
    DISPATCH_CODES as DISPATCH_CODES,
    NOTE_CHARS as NOTE_CHARS,
    NOTE_LINES as NOTE_LINES,
)
from .sheet_app import Sheets
from .session_app import Sessions, SESSIONS as SESSIONS
from .recovery_app import Recovery
from .messages import ImageRef as ImageRef, Message
from .editor import Draft
from .images import dropped_image_path
from .recovery import RecoveryStore
from .transcript import markdown as markdown, matches as matches
from .rpc import Backend
from .state import SECRET_METHODS, TAIL, Conversation, Question, hermes_effort
from .design import CSS, DARK, LIGHT, send_assets
from .views import (
    ALIASES as ALIASES,
    COMMAND_ROWS as COMMAND_ROWS,
    sheet_cursor as sheet_cursor,
    sheet_rows as sheet_rows,
    slash_open as slash_open,
    slash_rows as slash_rows,
    slash_typed as slash_typed,
    view,
)

# Keys that pick a question's choice, in the order the card shows them.
DIGITS = "123456789"


class App(Attachments, Commands, Sheets, Sessions, Recovery):
    def __init__(self, session, command: list[str], cwd: Path, options: dict):
        self.session = session
        self.command = command
        self.cwd = cwd
        self.options = {
            key: value for key, value in options.items() if key not in ("demo", "resume", "continue_last")
        }
        # The session to pick up instead of starting one: an id or title, or "latest", Hermes's word
        # for the newest (what a bare `-c` continues).
        continued = options.get("continue_last")
        self.resume: str | None = options.get("resume") or (
            continued if isinstance(continued, str) else "latest" if continued else None
        )
        self.demo_duration = options.get("demo")
        self.state = Conversation()
        self.draft = Draft()
        self.answer_draft = Draft()
        self.secret = Draft()
        self.watched_at = 0.0
        self.backend: Backend | None = None
        self.surface = None
        self.assets: dict[str, str] = {}
        self.exit = False
        self.pending: list[tuple[str, Future, float]] = []
        self.start_deadline = time.monotonic() + 60
        self._ack_approvals: list[str] = []
        # What each in-flight slash request was for: the draft a completion answers, or the command.
        self.asked: dict[Future, str] = {}
        self.completing: Future | None = None
        self.typed = ""  # the draft the command list's cursor was placed for
        self.switching: str | None = None  # the session `/resume` is picking up in this pane
        self.held_switch: list[Message] = []
        self.request_sessions: dict[Future, str] = {}
        self.recovering = False
        self.recovery_error = False
        self.restoring: dict | None = None
        self.store: RecoveryStore | None = None
        self.saved_records: list[dict] = []
        self._saved_at = 0.0
        self._save_failed = False
        self._retired: list[threading.Thread] = []
        self.gateway: dict = {}
        self.uncertain_work = False
        self.init_images()

    def request(self, method: str, params: dict | None = None) -> Future:
        assert self.backend
        future = self.backend.request(method, params)
        self.pending.append((method, future, time.monotonic() + 60))
        if params and params.get("session_id") and method not in ("session.resume", "session.close"):
            self.request_sessions[future] = params["session_id"]
        return future

    def scoped(self, **params) -> dict:
        return {"session_id": self.state.session_id, **params}

    def run(self) -> int:
        with self.session, self.session.open(mode="inline", title="Hermes", role="hermes.session") as surface:
            self.surface = surface
            surface.stylesheet("hermes-for-tern", CSS)
            if self.session.caps.has("program-palette"):
                surface.palette(
                    dark=DARK, light=LIGHT, name={"dark": "Hermes graphite", "light": "Hermes ivory"}
                )
            self.assets = send_assets(self.session)
            self.render()
            surface.focus("dock.composer")
            try:
                if self.demo_duration is None:
                    self.store = RecoveryStore(self.cwd, self.options.get("profile"))
                    self.saved_records = self.store.records()
                    self.update_saved()
                    if self.image_store:
                        try:
                            self.image_store.collect()
                        except OSError:
                            pass
                try:
                    self.backend = Backend(self.command, self.cwd)
                except (OSError, RuntimeError) as exc:
                    self.fail(str(exc))
                last_render = 0.0
                revision = -1
                while not self.exit and not self.session.closed and not surface.closed:
                    self.read_backend()
                    self.check_requests()
                    self.poll_images()
                    if (
                        not self.state.ready
                        and not self.state.failed
                        and time.monotonic() > self.start_deadline
                    ):
                        self.fail("Hermes took too long to start. Type /doctor for the details.")
                    item = self.session.poll(0.02)
                    if item is not None:
                        self.input(item)
                    now = time.monotonic()
                    if self.state.watch and now - self.watched_at >= TAIL:
                        self.read_tail()
                    self.state.pace(now)
                    self.save_drafts()
                    if self.state.revision != revision and now - last_render >= 0.033:
                        self.render()
                        revision = self.state.revision
                        last_render = now
            except (OSError, RuntimeError) as exc:
                self.fail(str(exc))
                self.render()
                # Keep a failed launch readable; allow the user to leave normally.
                while not self.exit and not self.session.closed and not surface.closed:
                    self.read_backend()
                    self.check_requests()
                    self.poll_images()
                    self.state.pace(time.monotonic())
                    self.save_drafts()
                    self.render()
                    item = self.session.poll(0.1)
                    if item is not None:
                        self.input(item)
            finally:
                if self.image_load:
                    try:
                        self.image_load.result(timeout=8)
                    except Exception:
                        pass
                    self.poll_images()
                if self.inflight:
                    self.state.held_message = self.inflight
                    self.state.held_uncertain = True
                self.save_drafts(force=True)
                if self.backend:
                    self.backend.close(self.state.session_id)
                for worker in self._retired:
                    worker.join(timeout=10)
        # The surface is gone; the pane's own terminal keeps this in its scrollback.
        hint = self.resume_hint()
        if hint:
            print(hint, flush=True)
        return 1 if self.state.failed else 0

    def render(self) -> None:
        assert self.surface
        self.surface.render(
            view(
                self.state,
                self.draft,
                self.answer_draft,
                self.cwd,
                self.stop,
                self.approve,
                self.choose,
                self.skip,
                self.suggest,
                self.submit,
                self.assets,
                self.retry,
                self.cycle_effort,
                self.model_event,
                self.secret,
                self.watch_event,
                self.state.unqueue,
                self.pick_command,
                self.resume_session,
                self.local_action,
                self.image_path if self.image_path_open else None,
            )
        )
        if self.backend:
            for rid in self._ack_approvals:
                question = self.state.questions.get(rid)
                if question:
                    request_id = question.params.get("request_id")
                    if request_id:
                        self.request("approval.received", self.scoped(request_id=request_id))
            self._ack_approvals.clear()

    def reconnect(self, *, fresh: bool = False) -> None:
        if self.state.connection in ("connecting", "recovering") and not self.state.failed:
            return
        if not fresh and not self.state.stored_session_id and self.state.rows:
            self.state.add(
                "notice", "There is no stored session to resume. Choose Start new session to keep your draft."
            )
            self.recovery_error = True
            return
        self.resume = None if fresh else self.state.stored_session_id
        self.recovering = not fresh
        self.recovery_error = False
        self.state.failed = False
        self.state.ready = False
        self.state.questions.clear()
        self.secret = Draft()
        self.answer_draft = Draft()
        self.state.connection = "recovering" if self.recovering else "connecting"
        self.state.activity = "Reconnecting to Hermes"
        self.state.queue_paused = True
        self.state.session_id = None
        if fresh:
            self.uncertain_work = False
            queued = list(self.state.queued)
            self.state.switched()
            self.state.queued = queued
            self.state.stored_session_id = None
        self.pending.clear()
        self.request_sessions.clear()
        self.asked.clear()
        self.completing = None
        self.gateway = {}
        self.start_deadline = time.monotonic() + 60
        if self.backend:
            worker = threading.Thread(target=self.backend.close, daemon=True)
            worker.start()
            self._retired.append(worker)
        try:
            self.backend = Backend(self.command, self.cwd)
        except (OSError, RuntimeError) as exc:
            self.fail(str(exc))
        self.state.touch()

    def read_backend(self) -> None:
        if not self.backend:
            return
        # Bounded draining keeps a flood of tool events from starving the keyboard.
        for _ in range(200):
            try:
                frame = self.backend.events.get_nowait()
            except queue.Empty:
                break
            if "local_error" in frame:
                self.fail(frame["local_error"])
            elif frame.get("method") == "event":
                params = frame.get("params", {})
                if self.state.failed:
                    continue
                if (
                    params.get("session_id")
                    and self.state.session_id
                    and params["session_id"] != self.state.session_id
                ):
                    continue
                kind = params.get("type", "")
                if kind == "gateway.ready":
                    self.gateway = params.get("payload") or {}
                    self.request("client.capabilities", {"server_requests": True})
                else:
                    old_question = self.state.clarify()
                    old_secret = self.state.secret()
                    busy = self.state.busy
                    self.state.event(kind, params.get("payload") or {})
                    if old_question and old_question is not self.state.clarify():
                        self.answer_draft = Draft()
                    if old_secret and old_secret is not self.state.secret():
                        self.secret = Draft()
                    if kind == "message.complete" and self.delivery is None:
                        self.inflight = None
                    if busy and not self.state.busy:
                        self.send_queued()
            elif "method" in frame and "id" in frame and not self.state.failed:
                self.server_request(frame)

    def check_requests(self) -> None:
        batch, self.pending = self.pending, []
        remaining = []
        for method, future, deadline in batch:
            scope = self.request_sessions.get(future)
            if scope and scope != self.state.session_id:
                self.request_sessions.pop(future, None)
                self.asked.pop(future, None)
                continue
            if self.state.failed and method != "local.doctor":
                self.request_sessions.pop(future, None)
                self.asked.pop(future, None)
                continue
            if not future.done():
                if time.monotonic() > deadline:
                    self.request_sessions.pop(future, None)
                    self.asked.pop(future, None)
                    if self.backend and hasattr(self.backend, "forget"):
                        self.backend.forget(future)
                    if method == "complete.slash":
                        self.asked.pop(future, None)  # the list keeps this pane's own commands
                    elif method == "session.resume" and self.switching:
                        self.state.add("notice", f"Hermes did not pick up {self.switching!r} in time.")
                        self.switch_failed()
                    elif method in ("session.resume", "session.most_recent"):
                        self.start_fresh(f"Hermes did not find {self.resume!r} in time")
                    elif method in ("session.create", "client.capabilities"):
                        self.fail(f"Timed out waiting for {method}.")
                    else:
                        self.state.add(
                            "error", f"Hermes has not acknowledged {method}. Use Stop before retrying."
                        )
                    continue
                remaining.append((method, future, deadline))
                continue
            self.request_sessions.pop(future, None)
            try:
                result = future.result()
                if method == "client.capabilities":
                    if self.resume == "latest":
                        self.request("session.most_recent", self.profiled())
                    elif self.resume:
                        self.request("session.resume", self.profiled(session_id=self.resume, cols=self.cols))
                    else:
                        self.create_session()
                elif method == "session.most_recent":
                    if result.get("session_id"):
                        self.request(
                            "session.resume", self.profiled(session_id=result["session_id"], cols=self.cols)
                        )
                    else:
                        self.start_fresh("There is no earlier session to continue")
                elif method == "session.resume":
                    if not isinstance(result, dict) or not isinstance(result.get("session_id"), str):
                        raise ValueError("Hermes returned an invalid session; no messages were sent.")
                    previous = self.state.session_id if self.switching else None
                    held = list(self.held_switch)
                    self.held_switch.clear()
                    recovering = self.recovering
                    if previous or recovering:
                        queued = (
                            list(self.state.queued)
                            if recovering
                            or (
                                self.switching
                                and self.state.queued
                                and all(
                                    message.destination == self.switching for message in self.state.queued
                                )
                            )
                            else []
                        )
                        self.state.switched()
                        self.state.queued = queued
                        self.state.close_sheet()
                    self.switching = None
                    self.state.switching = False
                    self.secret = Draft()
                    self.answer_draft = Draft()
                    self.session_ready(
                        result,
                        stored=result.get("session_key")
                        or result.get("stored_session_id")
                        or result.get("resumed"),
                    )
                    self.state.restore(result.get("messages") or [], result.get("info") or {})
                    if recovering:
                        self.uncertain_work = False
                        self.state.queue_paused = True
                        self.state.add(
                            "notice",
                            "Reconnected. Previous work may already have run; no prompt was resent. Review paused messages before continuing.",
                        )
                    self.recovering = False
                    if previous and previous != self.state.session_id:
                        self.request("session.close", {"session_id": previous})
                    if self.restoring:
                        self.apply_restored()
                    for message in held:
                        self.state.queue(Message(message.text, message.images, self.state.stored_session_id))
                    if not recovering and self.state.queued:
                        self.state.queued = [
                            Message(message.text, message.images, self.state.stored_session_id)
                            for message in self.state.queued
                        ]
                    if held:
                        self.continue_queue()
                    self.save_drafts(force=True)
                elif method == "session.list":
                    self.state.listed(result.get("sessions") or [])
                elif method == "session.create":
                    self.session_ready(result, stored=result.get("stored_session_id"))
                    if self.demo_duration is None:
                        self.request("session.list", self.profiled(limit=SESSIONS))
                    if self.demo_duration is not None:
                        self.submit(f"Run a {self.demo_duration:g}-second simulated visual demo.")
                elif method == "local.doctor":
                    for line in result:
                        self.state.add("notice", line)
                elif method == "session.interrupt":
                    self.state.activity = "Stopping" if self.state.busy else "Ready"
                    self.state.touch()
                elif method == "model.options":
                    self.state.models = result
                    self.state.touch()
                elif method == "config.set":
                    self.model_set(result)
                elif method == "session.title":
                    if result.get("title"):
                        self.state.title = str(result["title"])
                        self.state.touch()
                elif method == "subagent.tail":
                    self.state.watch_tail(result)
                elif method in ("subagent.interrupt", "subagent.steer"):
                    self.steer_result(method, result)
                elif method == "complete.slash":
                    text = self.asked.pop(future, None)
                    if text is not None and future is self.completing:
                        self.state.completions(text, result)
                elif method in ("slash.exec", "command.dispatch"):
                    self.command_result(self.asked.pop(future, ""), result)
            except Exception as exc:
                if method == "complete.slash":
                    self.asked.pop(future, None)
                elif method == "slash.exec" and getattr(exc, "code", None) in DISPATCH_CODES:
                    self.dispatch(self.asked.pop(future, ""))
                elif method in ("slash.exec", "command.dispatch"):
                    command = self.asked.pop(future, "")
                    self.state.add("notice", f"{command.split(' ')[0]}: {exc}")
                elif method == "session.resume" and self.switching:
                    self.state.add("notice", f"Could not resume {self.switching!r}: {exc}")
                    self.switch_failed()
                elif method in ("session.resume", "session.most_recent"):
                    self.start_fresh(f"Could not resume {self.resume!r}: {exc}")
                elif method in ("session.list", "session.close"):
                    pass  # the welcome and the sheet simply offer nothing; a close is best effort
                elif method in ("client.capabilities", "session.create"):
                    self.fail(str(exc))
                else:
                    self.state.add("error", str(exc))
                    if method == "prompt.submit":
                        self.state.queue_paused = True
                        self.state.busy = False
                        self.state.activity = "Ready"
        # Requests scheduled by the handlers above must survive this iteration.
        self.pending = remaining + self.pending

    def server_request(self, frame: dict) -> None:
        assert self.backend
        rid, method, params = str(frame["id"]), frame["method"], frame.get("params") or {}
        if params.get("session_id") != self.state.session_id:
            self.backend.answer(rid, error={"code": -32601, "message": "Session is not displayed here"})
        elif method in ("approval", "clarify"):
            if method == "clarify" and not params.get("questions"):
                self.backend.answer(rid, {"answers": {}})
                return
            self.state.questions[rid] = Question(rid, method, params)
            if method == "approval":
                self._ack_approvals.append(rid)
            self.state.touch()
        elif method in SECRET_METHODS:
            # A one-string ask: a password, a key or a code. It is answered from the composer,
            # masked while it is typed, and the empty answer is Hermes's word for "skipped".
            self.state.questions[rid] = Question(rid, method, params)
            self.state.touch()
            if self.surface:
                self.surface.focus("dock.composer")
        else:
            self.backend.answer(
                rid, error={"code": -32601, "message": f"{method} is not supported by Hermes for Tern yet"}
            )
            self.state.add("notice", f"This frontend does not support {method} yet.")

    def approve(self, rid: str, choice: str) -> None:
        assert self.backend
        question = self.state.questions.get(rid)
        if not question or choice not in (question.params.get("choices") or ["deny"]):
            return
        self.backend.answer(rid, {"choice": choice})
        self.state.questions.pop(rid, None)
        self.state.add("notice", "Permission denied" if choice == "deny" else "Permission granted")

    def choose(self, rid: str, choice: str | None) -> None:
        question = self.state.questions.get(rid)
        if not question:
            return
        if question.current.get("multi_select") and choice is not None:
            if choice in question.selected:
                question.selected.remove(choice)
            else:
                question.selected.add(choice)
            self.state.touch()
        else:
            self.answer_question(
                question, choice if choice is not None else ", ".join(sorted(question.selected))
            )

    def answer_secret(self, question: Question, value: str) -> None:
        assert self.backend
        self.backend.answer(question.rid, {"value": value})
        self.state.questions.pop(question.rid, None)
        # The answer is Hermes's the moment it is sent; keep no copy of it here.
        self.secret = Draft()
        self.state.touch()

    def skip(self, rid: str) -> None:
        question = self.state.questions.get(rid)
        if question:
            self.answer_question(question, None)

    def answer_question(self, question: Question, answer: str | None) -> None:
        assert self.backend
        question.answers[question.current["qid"]] = answer
        question.index += 1
        question.selected.clear()
        self.answer_draft = Draft()
        if question.index >= len(question.params["questions"]):
            self.backend.answer(question.rid, {"answers": question.answers})
            self.state.questions.pop(question.rid, None)
        self.state.touch()

    def stop(self) -> None:
        if self.delivery and self.delivery.phase != "submit":
            self.delivery.cancelled = True
            self.state.queue_paused = True
            self.state.activity = "Cancelling image send"
            self.state.touch()
            return
        self.state.queue_paused = True
        self.state.touch()
        if self.state.busy and self.state.session_id and not self.state.failed:
            self.request("session.interrupt", self.scoped())
            self.state.activity = "Stopping"
            self.state.touch()

    def cycle_effort(self) -> None:
        """The ring was pressed: ask Hermes to think a little deeper, or not, from now on."""
        if self.state.failed or self.switching:
            return
        level = self.state.cycle_effort()
        if self.state.session_id:
            self.request("config.set", self.scoped(key="reasoning", value=hermes_effort(level)))

    def watch_event(self, action: str) -> None:
        """The watched child's own buttons: interrupt it, steer it, or stop watching."""
        if self.state.failed or self.switching:
            return
        if action == "interrupt":
            self.interrupt_agent()
        elif action == "steer":
            self.steer_agent(self.state.watch.steer if self.state.watch else "")
        elif action == "close":
            self.state.close_watch()

    def watch_agent(self, subagent_id: str) -> None:
        """Open a delegated child: its live transcript, refreshed, with interrupt and steer."""
        self.state.open_watch(subagent_id)
        self.read_tail()

    def read_tail(self) -> None:
        watch = self.state.watch
        if watch and self.state.session_id:
            self.watched_at = time.monotonic()
            self.request("subagent.tail", self.scoped(subagent_id=watch.id))

    def interrupt_agent(self) -> None:
        watch = self.state.watch
        if watch and self.state.session_id:
            self.request("subagent.interrupt", self.scoped(subagent_id=watch.id))

    def steer_agent(self, text: str) -> None:
        watch = self.state.watch
        if not watch or not text.strip() or not self.state.session_id:
            return
        self.request("subagent.steer", self.scoped(subagent_id=watch.id, text=text))
        self.state.steer_watch("")

    def watch_key(self, item) -> bool:
        """The watched child owns the keyboard while it is open: a line to steer it, Enter to
        send, Escape to stop watching. True when the key was the watch's."""
        watch = self.state.watch
        if self.state.secret() or self.state.clarify():
            return False
        if watch is None or item.ctrl or item.alt or item.meta:
            return False
        if item.name == "escape":
            self.state.close_watch()
        elif item.name == "enter":
            self.steer_agent(watch.steer)
        elif item.name == "backspace":
            self.state.steer_watch(watch.steer[:-1])
        elif item.text:
            self.state.steer_watch(watch.steer + item.text)
        else:
            return False
        return True

    def steer_result(self, method: str, result: dict) -> None:
        """Steering is queued, not delivered: a child past its last tool batch misses it."""
        if method == "subagent.interrupt":
            if result.get("found") is False:
                self.state.add("notice", "That child already finished.")
            else:
                self.state.add("notice", "Child interrupted.")
            return
        status = str(result.get("status") or "")
        detail = str(result.get("text") or "")
        if status == "queued":
            self.state.dispatch(f"steer-{result.get('subagent_id', '')}", "steered the child", detail[:120])
        else:
            self.state.add("notice", detail[:200] or "The child will not take that.")

    def suggest(self, text: str) -> None:
        self.draft = Draft(text, len(text))
        self.state.touch()
        self.surface.focus("dock.composer")

    def retry(self) -> None:
        """Send the last prompt again after a retryable failure."""
        if self.state.can_retry and not self.switching and not self.state.held_message:
            self.send_message(self.state.last_message or Message(self.state.last_prompt))

    def submit(self, text: str | None = None) -> None:
        question = self.state.secret()
        if question:
            # Enter sends what was typed; an empty answer is Hermes's word for "skipped".
            self.answer_secret(question, self.secret.text if text is None else text)
            return
        question = self.state.clarify()
        if question:
            answer = self.answer_draft.text if text is None else text
            if answer.strip():
                self.answer_question(question, answer)
            return
        if self.state.image_loading:
            self.state.add("notice", "Wait for the image to finish importing before sending.")
            return
        text = self.draft.text if text is None else text
        if not text.strip() and not self.state.draft_images:
            if self.state.can_retry:
                self.retry()
            return
        # Terminal file drops arrive as text paths. Resolve them before slash-command dispatch.
        if dropped_image_path(text) is not None:
            self.draft = Draft(text, len(text))
            if self.import_dropped_image():
                return
        if text.strip().startswith("/"):
            # Consume the command first: /recover may replace it with a restored draft.
            self.draft = Draft()
            self.run_command(text.strip())
            self.save_drafts(force=True)
            return
        if not self.state.ready or self.state.failed:
            return
        if self.restoring:
            self.state.add("notice", "Wait for saved work to finish restoring. Your draft is unchanged.")
            return
        if self.state.held_message:
            self.state.add(
                "notice",
                "Restore or discard the retained message before sending another. Your draft is unchanged.",
            )
            return
        message = Message(text, self.state.draft_images, self.switching or self.state.stored_session_id)
        if self.switching:
            if len(self.held_switch) + len(self.state.queued) >= 8:
                self.state.add("notice", "The switch queue is full. Your message stays in the composer.")
                self.draft = Draft(text, len(text))
            else:
                self.held_switch.append(message)
                self.draft = Draft()
                self.state.draft_images = ()
                self.state.add("notice", "Message held for the session being opened.")
            self.save_drafts(force=True)
            return
        if self.state.busy or (self.state.queue_paused and self.state.queued):
            if self.state.queue(message):
                self.draft = Draft()
                self.state.draft_images = ()
            else:
                self.state.add(
                    "notice",
                    "The queue is full (8 messages). Remove a queued message or keep editing this draft.",
                )
                self.draft = Draft(text, len(text))
            self.save_drafts(force=True)
            return
        self.draft = Draft()
        self.state.draft_images = ()
        self.send_message(message)
        self.save_drafts(force=True)

    def choose_by_key(self, item) -> bool:
        """A pending question answers to the keyboard: digits pick, Enter takes the likely one,
        Esc denies. Returns True when the key was a choice and so must not reach the draft."""
        if item.ctrl or item.alt or item.meta:
            return False
        approval = self.state.approval()
        if approval is not None:
            choices = approval.params.get("choices") or ["deny"]
            if item.name in DIGITS and DIGITS.index(item.name) < len(choices):
                self.approve(approval.rid, choices[DIGITS.index(item.name)])
            elif item.name == "enter":
                self.approve(approval.rid, "once" if "once" in choices else choices[0])
            elif item.name == "escape":
                self.approve(approval.rid, "deny" if "deny" in choices else choices[-1])
            else:
                return False
            return True
        question = self.state.clarify()
        # Digits type a number into the answer draft once one is started, so only claim
        # them while the draft is still empty.
        if question is not None and not self.answer_draft.text and item.name in DIGITS:
            choices = question.current.get("choices") or []
            if DIGITS.index(item.name) < len(choices):
                self.choose(question.rid, choices[DIGITS.index(item.name)])
                return True
        return False

    def input(self, item) -> None:
        draft = (
            self.secret
            if self.state.secret()
            else (self.answer_draft if self.state.clarify() else self.draft)
        )
        if isinstance(item, Key):
            if self.image_key(item):
                pass
            elif (
                item.ctrl
                and item.name == "d"
                and not draft.text
                and not self.state.draft_images
                and not self.state.image_loading
            ):
                self.exit = True
            elif item.ctrl and item.name == "c":
                # One press stops the turn or clears the draft; leaving takes a second press within
                # EXIT_WINDOW, so a stray Ctrl+C never closes Hermes.
                if self.state.exit_armed is not None:
                    self.exit = True
                else:
                    if self.state.busy:
                        self.stop()
                    elif draft.text or self.state.draft_images:
                        draft.replace(0, len(draft.text), "")
                        if draft is self.draft:
                            self.state.draft_images = ()
                    self.state.arm_exit(time.monotonic())
            elif self.slash_key(item):
                pass
            elif self.sheet_key(item):
                pass
            elif self.watch_key(item):
                pass
            elif self.choose_by_key(item):
                pass
            elif item.name == "escape":
                question = self.state.secret()
                if question:
                    self.answer_secret(question, "")
                else:
                    self.stop()
            elif item.name == "enter" and not (item.shift or item.alt):
                self.submit()
            else:
                draft.key(item)
            self.state.touch()
        elif isinstance(item, EditEvent):
            if item.id == "dock.image-path.attach-path" and self.image_path_open:
                self.image_path.edit(item)
                self.state.touch()
            elif item.id == "dock.composer":
                draft.edit(item)
                if draft is self.draft:
                    self.import_dropped_image()
                self.state.touch()
        elif isinstance(item, UndoEvent):
            if item.id == "dock.image-path.attach-path" and self.image_path_open:
                self.image_path.undo()
            elif item.id in (None, "dock.composer"):
                draft.undo()
            self.state.touch()
        elif (
            isinstance(item, SendEvent) and item.id == "dock.image-path.attach-path" and self.image_path_open
        ):
            self.image_path = Draft(item.text or "", len(item.text or ""))
            self.attachment_action("image-path-submit")
        elif isinstance(item, SendEvent) and item.id == "dock.composer":
            # Tern's own send key on a command being typed runs the highlighted row, as Enter does.
            if not self.slash_key(Key(name="enter")):
                self.submit(item.text or "")
        elif isinstance(item, FocusEvent):
            self.surface.focus(
                "dock.image-path.attach-path"
                if self.image_path_open and item.id == "dock.image-path.attach-path"
                else "dock.composer"
            )
        elif isinstance(item, ErrorEvent):
            self.state.add("error", f"Tern: {item.msg}")
        elif isinstance(item, VisibleEvent) and item.visible is not None:
            self.state.show(item.visible)
        elif isinstance(item, GoneEvent) and self.surface.id in item.ids:
            self.exit = True
        if self.draft.text != self.typed:
            self.typed = self.draft.text
            self.state.slash.cursor = 0
        self.ask_completions()
