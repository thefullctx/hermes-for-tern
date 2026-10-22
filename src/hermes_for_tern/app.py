"""Single-threaded UI loop; Hermes runs over private pipes in a separate process."""

from __future__ import annotations

import queue
import os
import shlex
import time
import threading
from concurrent.futures import Future
from pathlib import Path

from tern_sdk import EditEvent, ErrorEvent, FocusEvent, GoneEvent, Key, SendEvent, UndoEvent, VisibleEvent

from .attachment_app import Attachments
from .messages import ImageRef, Message
from .editor import Draft
from .recovery import RecoveryStore
from .transcript import markdown, matches
from .rpc import Backend
from .state import SECRET_METHODS, TAIL, Conversation, Question, hermes_effort
from .design import CSS, DARK, LIGHT, send_assets
from .views import ALIASES, COMMAND_ROWS, sheet_cursor, sheet_rows, slash_open, slash_rows, slash_typed, view

# Keys that pick a question's choice, in the order the card shows them.
DIGITS = "123456789"
# The commands this pane answers itself; every other `/command` is Hermes's. What each does lives
# beside the list that offers them, so `/help` cannot drift from the keyboard.
COMMANDS = tuple(name.split(" ")[0] for name, _ in COMMAND_ROWS)
HELP = "In this pane: " + " · ".join(COMMANDS)
# `slash.exec` refusals that mean "not mine, ask command.dispatch" (skills, bundles, pending input).
DISPATCH_CODES = (4011, 4018)
# Sessions `/resume` lists, newest first.
SESSIONS = 50
# Output longer than this folds into a row of its own instead of a one-line note.
NOTE_CHARS, NOTE_LINES = 180, 2


class App(Attachments):
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

    def fail(self, message: str) -> None:
        if self.state.failed:
            return
        if self.inflight:
            self.state.held_message = self.inflight
            self.state.held_uncertain = True
            self.inflight = None
        self.delivery = None
        self.state.image_preparing = False
        self.state.add("error", message)
        self.state.failed = True
        self.state.ready = False
        self.state.connection = "disconnected"
        self.state.queue_paused = True
        self.state.questions.clear()
        self.state.close_watch()
        self._ack_approvals.clear()
        self.secret = Draft()
        self.answer_draft = Draft()
        for tool in self.state.tools.values():
            if tool.status == "running":
                tool.status = "cancelled"
        for agent in self.state.agents.values():
            if agent.status in ("running", "pending"):
                agent.status = "aborted"
        if self.state.busy:
            self.uncertain_work = True
            self.state.add(
                "notice", "Work was interrupted. It may already have changed files; it will not be resent."
            )
        self.state.busy = False
        self.state.activity = "Disconnected — reconnect to resume"
        self.recovering = False
        if self.switching:
            self.switch_failed()
        self.switching = None
        self.state.switching = False
        self.state.touch()
        self.save_drafts(force=True)

    def update_saved(self) -> None:
        self.state.recovery_available = bool(self.saved_records)
        record = self.saved_records[0] if self.saved_records else {}
        self.state.recovery_label = str(record.get("session") or "a session that did not finish starting")
        self.state.touch()

    def save_drafts(self, *, force: bool = False) -> None:
        if not self.store or (not force and time.monotonic() - self._saved_at < 0.5):
            return
        self._saved_at = time.monotonic()
        try:
            self.store.save(
                self.draft.text,
                self.state.queued + self.held_switch,
                self.switching or self.state.stored_session_id,
                self.state.busy or self.uncertain_work or self.state.held_uncertain,
                images=self.state.draft_images,
                pending=self.inflight or self.state.held_message,
            )
            self._save_failed = False
        except OSError:
            if not self._save_failed:
                self.state.add(
                    "notice", "Draft recovery could not be saved. Keep this pane open to retain your text."
                )
                self._save_failed = True

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

    def restore_drafts(self) -> None:
        if not self.saved_records or self.restoring:
            return
        if not self.state.ready and not self.state.failed:
            self.state.add("notice", "Wait for Hermes to finish connecting before restoring saved text.")
            return
        if (
            self.state.busy
            or self.switching
            or self.draft.text
            or self.state.queued
            or self.state.draft_images
            or self.state.image_loading
            or self.state.held_message
        ):
            self.state.add(
                "notice",
                "Finish this turn and send or clear your current draft and queue before restoring saved text.",
            )
            return
        self.restoring = self.saved_records[0]
        target = self.restoring.get("session")
        if target and target != self.state.stored_session_id:
            if self.state.ready:
                self.resume_session(target)
            else:
                self.state.stored_session_id = target
                self.reconnect()
        else:
            self.apply_restored()

    def apply_restored(self) -> None:
        record = self.restoring
        if not record:
            return
        if self.draft.text or self.state.draft_images or self.state.image_loading or self.state.queued:
            self.restoring = None
            self.state.add(
                "notice",
                "Your current draft changed while restoring. Saved work is still available; clear this draft and restore again.",
            )
            return
        self.draft = Draft(record["draft"], len(record["draft"]))
        self.state.queued = [Message.read(message) for message in record["queue"]]
        self.state.draft_images = tuple(ImageRef.read(image) for image in record.get("draft_images", []))
        self.state.held_message = (
            Message.read(record["pending"]) if record.get("pending") is not None else None
        )
        self.state.held_uncertain = bool(record.get("uncertain"))
        images = list(self.state.draft_images)
        images += [image for message in self.state.queued for image in message.images]
        if self.state.held_message:
            images += list(self.state.held_message.images)
        for image in images:
            self.register_image(image)
        self.state.queue_paused = True
        # Write our replacement before removing the original recovery file.
        self.save_drafts(force=True)
        if self.store and not self._save_failed:
            try:
                self.store.forget(record)
                self.saved_records.remove(record)
            except OSError:
                self.state.add("notice", "Text restored, but its old recovery file could not be removed.")
        self.restoring = None
        self.update_saved()
        self.state.add(
            "notice",
            "Saved text restored; nothing was sent. Queued messages are paused. Previously submitted work may already have run.",
        )

    def continue_queue(self) -> None:
        if not self.state.ready or self.state.failed or self.switching or self.state.busy:
            return
        if self.state.held_message:
            self.state.add("notice", "Restore or discard the retained message before continuing the queue.")
            return
        self.state.queue_paused = False
        self.send_queued()
        self.state.touch()

    def send_queued(self) -> None:
        if (
            self.state.queued
            and not self.state.queue_paused
            and not self.switching
            and self.delivery is None
            and self.state.held_message is None
            and not self.state.busy
            and self.state.ready
            and not self.state.failed
        ):
            message = self.state.take_queued()
            self.send_message(message)

    def local_action(self, action: str) -> None:
        if action.startswith("image-"):
            self.attachment_action(action)
            return
        if action == "reconnect":
            self.reconnect()
        elif action == "new-session":
            self.reconnect(fresh=True)
        elif action == "restore":
            self.restore_drafts()
        elif action == "forget-saved" and self.saved_records and self.store:
            try:
                self.store.forget(self.saved_records.pop(0))
                self.update_saved()
            except OSError:
                self.state.add("notice", "Could not remove the saved draft.")
        elif action == "continue-queue":
            self.continue_queue()
        elif action == "discard-queue":
            self.state.queued.clear()
            self.state.touch()
        elif action in ("find-next", "find-previous"):
            found = matches(self.state)
            if found:
                self.state.search_index = (
                    self.state.search_index + (1 if action == "find-next" else -1)
                ) % len(found)
                found[self.state.search_index].collapsed = False
                self.state.touch()
        elif action == "find-close":
            self.state.search_query = ""
            self.state.touch()
        self.save_drafts(force=True)

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

    @property
    def cols(self) -> int:
        return self.session.caps.cols or 80

    def profiled(self, **params) -> dict:
        """Params for a call that reaches Hermes's session store, in the launch profile's home."""
        profile = self.options.get("profile")
        return {**params, **({"profile": profile} if profile else {})}

    def create_session(self) -> None:
        params = {
            "cwd": str(self.cwd),
            "cwd_explicit": True,
            "source": "tui",
            "title": "Hermes for Tern",
            "cols": self.cols,
            **self.options,
        }
        self.request("session.create", params)

    def start_fresh(self, why: str) -> None:
        """Initial CLI resume may fall back; recovery must never silently change sessions."""
        if self.recovering or self.restoring:
            self.fail(f"{why}. Retry recovery or explicitly start a new session; your text is retained.")
            self.recovery_error = True
            self.restoring = None
            return
        self.state.add("notice", f"{why}. This is a new session.")
        self.resume = None
        self.create_session()

    def session_ready(self, result: dict, *, stored: str | None) -> None:
        if not isinstance(result.get("session_id"), str) or not result["session_id"]:
            raise ValueError("Hermes returned an invalid session id.")
        self.state.session_id = result["session_id"]
        self.state.stored_session_id = stored if isinstance(stored, str) else None
        self.state.info.update(result.get("info") or {})
        self.state.ready = True
        self.state.failed = False
        self.state.connection = "ready"
        self.state.activity = "Ready"
        self.state.touch()

    def resume_hint(self) -> str:
        """What Hermes's own terminal says on the way out: how to come back to this session. Nothing
        when the session never held a conversation, since Hermes keeps no record of it then."""
        stored = self.state.stored_session_id
        if not stored or not (self.state.last_prompt or self.state.resumed):
            return ""
        profile = self.options.get("profile")
        flag = f" -p {shlex.quote(profile)}" if profile and profile not in ("default", "custom") else ""
        lines = ["Resume this session with:", f"  hermes --resume {shlex.quote(stored)}{flag}"]
        if self.state.title:
            title = self.state.title
            # Preserve the familiar quoted hint for ordinary titles, escape shell expansion.
            escaped = title.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
            lines.append(f'  hermes -c "{escaped}"{flag}')
        return "\n".join(lines)

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

    def model_event(self, action: str, value: str = "") -> None:
        """The model sheet's own events: `open` from the model segment, `pick` from a row,
        `confirm` from its switch key, `cancel` from its cancel key or the backdrop."""
        if self.state.failed or self.switching:
            return
        if action == "open":
            if self.state.session_id:
                self.request("model.options", self.scoped(refresh=True))
            self.state.open_sheet()
        elif action == "pick":
            if value and self.state.sheet and self.state.sheet.kind == "sessions":
                self.resume_session(value)
            elif value:
                self.state.choose_row(value)
                self.switch_model(value)
        elif action == "confirm":
            self.confirm_model()
        elif action == "cancel":
            self.state.close_sheet()

    def sheet_key(self, item) -> bool:
        """The model sheet owns the keyboard while it is open: type to filter, arrows to move the
        cursor, Enter to switch, Escape to put it away. True when the key was the sheet's."""
        sheet = self.state.sheet
        if self.state.secret() or self.state.clarify():
            return False
        if sheet is None or item.ctrl or item.alt or item.meta:
            return False
        if item.name == "escape":
            self.state.close_sheet()
        elif item.name == "enter":
            self.confirm_model()
        elif item.name == "backspace":
            self.state.filter_sheet(sheet.query[:-1])
        elif item.name in ("up", "down"):
            self.move_sheet(item.name == "down")
        elif item.text:
            self.state.filter_sheet(sheet.query + item.text)
        else:
            return False
        return True

    def move_sheet(self, down: bool) -> None:
        rows = [row["id"] for row in sheet_rows(self.state)]
        if not rows:
            return
        at = rows.index(self.state.sheet.chosen) if self.state.sheet.chosen in rows else -1
        self.state.choose_row(rows[max(0, min(len(rows) - 1, at + (1 if down else -1)))])

    def confirm_model(self) -> None:
        """The switch key: the chosen row, or the session's own model when nothing was chosen."""
        if not self.state.sheet:
            return
        if self.state.sheet.kind == "sessions":
            self.resume_session(sheet_cursor(self.state))
            return
        if self.state.sheet.confirming:
            self.switch_model(self.state.sheet.chosen, confirmed=True)
            return
        chosen = sheet_cursor(self.state)
        if chosen:
            self.switch_model(chosen)

    def switch_model(self, value: str, *, confirmed: bool = False) -> None:
        """Ask Hermes to run another model. Mid-turn it holds the pick for the next turn start and
        reports it in session.info; an expensive model asks first, which is what `confirmed` is."""
        if self.state.failed or self.switching:
            return
        if not value or not self.state.session_id:
            return
        if self.state.sheet:
            # The sheet's row is the pending pick, so a confirmation repeats the same one.
            self.state.choose_row(value)
        params: dict = {"key": "model", "value": value}
        if confirmed:
            params["confirm_expensive_model"] = True
        self.request("config.set", self.scoped(**params))

    def rename_session(self, title: str) -> None:
        """Name this session; Hermes owns the title and echoes it back on `session.title`."""
        if title and self.state.session_id:
            self.request("session.title", self.scoped(title=title))

    def side_question(self, text: str) -> None:
        """Ask Hermes something beside the work it is doing; the answer lands as a dispatch."""
        if not text or not self.state.session_id:
            return
        self.request("prompt.btw", self.scoped(text=text))

    def background_task(self, text: str) -> None:
        """Hand Hermes a task on a fresh agent; the answer lands as a dispatch when it is done."""
        if not text or not self.state.session_id:
            return
        self.request("prompt.background", self.scoped(text=text))

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

    def slash_key(self, item) -> bool:
        """The command list answers the keyboard while it shows: arrows move, Tab completes the
        highlighted command, Enter runs it (or completes one that still needs its argument), and
        Escape puts the list away until the draft changes. True when the key was the list's."""
        if item.ctrl or item.alt or item.meta or self.state.secret() or self.state.clarify():
            return False
        if self.state.sheet or self.state.watch or not slash_open(self.state, self.draft):
            return False
        rows = slash_rows(self.state, self.draft.text)
        at = min(self.state.slash.cursor, len(rows) - 1)
        if item.name in ("up", "down"):
            self.state.slash.cursor = (at + (1 if item.name == "down" else -1)) % len(rows)
        elif item.name == "tab":
            self.complete_command(rows[at])
        elif item.name == "enter" and not item.shift:
            self.complete_command(rows[at], run=True)
        elif item.name == "escape":
            self.state.slash.put_away = self.draft.text
        else:
            return False
        self.state.touch()
        return True

    def complete_command(self, row: dict, *, run: bool = False) -> None:
        """Put the row into the draft. Run it too when asked, unless it is one of this pane's
        commands that needs its argument (`/btw`, `/bg`, `/name`): those wait for it."""
        start = row.get("from")
        if start is None:
            text = row["insert"] + " "
        else:
            text = self.draft.text[:start] + row["insert"]
        self.draft = Draft(text, len(text))
        self.state.slash.cursor = 0
        needs_argument = row.get("local") and "<" in row.get("label", "")
        if run and not needs_argument:
            self.submit(text)
        elif self.surface:
            self.surface.focus("dock.composer")

    def pick_command(self, index: int) -> None:
        """A click on a row of the command list completes it, ready for an argument."""
        rows = slash_rows(self.state, self.draft.text)
        if 0 <= index < len(rows):
            self.complete_command(rows[index])
            self.state.touch()

    def ask_completions(self) -> None:
        """Ask Hermes what completes the draft, once per draft, while a command is being typed."""
        text = self.draft.text
        if self.state.slash.put_away is not None and self.state.slash.put_away != text:
            self.state.slash.put_away = None
        if (
            slash_typed(text) is None
            or not self.backend
            or not self.state.session_id
            or not self.state.ready
            or self.state.failed
            or self.switching
            or text in self.asked.values()
            or self.state.slash.answered == text
        ):
            return
        if self.completing is not None and self.completing in self.asked:
            self.asked.pop(self.completing)  # a newer draft makes the older answer moot
        self.completing = self.request("complete.slash", self.scoped(text=text))
        self.asked[self.completing] = text

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

    def model_set(self, result: dict) -> None:
        """Hermes answered a model switch: either it asks whether the expensive pick is worth it,
        or it holds the pick for the next turn because this one is still running."""
        if result.get("confirm_required"):
            if self.state.sheet:
                self.state.sheet.confirming = True
            self.state.add(
                "notice",
                str(result.get("confirm_message") or "Hermes asks whether this model is worth it."),
            )
            return
        self.state.close_sheet()
        if result.get("deferred"):
            self.state.dispatch("model", "model switches when this turn ends", str(result.get("value") or ""))
        if result.get("warning"):
            self.state.add("notice", str(result["warning"]))

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

    def run_command(self, text: str) -> None:
        """One of COMMANDS, answered here; every other `/command` is Hermes's to run."""
        name, space, rest = text.partition(" ")
        if ALIASES.get(name.lower()):
            text = ALIASES[name.lower()] + space + rest
        local = {
            "/reconnect": "reconnect",
            "/recover": "restore",
            "/queue-continue": "continue-queue",
            "/queue-discard": "discard-queue",
            "/find-next": "find-next",
            "/find-previous": "find-previous",
            "/find-close": "find-close",
        }
        if name == "/attach":
            if rest.strip():
                self.load_image(rest)
            else:
                self.attachment_action("image-path")
        elif text == "/paste-image":
            self.load_image()
        elif text in local:
            self.local_action(local[text])
        elif name == "/find":
            self.state.search_query = rest.strip()
            self.state.search_index = 0
            found = matches(self.state)
            if found:
                found[0].collapsed = False
            self.state.touch()
        elif name == "/export":
            self.export_transcript(rest.strip())
        elif self.switching and name not in ("/quit", "/exit", "/doctor", "/stop"):
            self.state.add("notice", "Wait for the session switch before running this command.")
        elif text in ("/quit", "/exit"):
            self.exit = True
        elif text == "/model":
            self.model_event("open")
        elif text.startswith("/model "):
            self.switch_model(text[len("/model ") :].strip())
        elif text == "/resume":
            self.open_sessions()
        elif text.startswith("/resume "):
            self.resume_session(text[len("/resume ") :].strip())
        elif text == "/name":
            self.state.add("notice", "Name the session with /name <title>.")
        elif text.startswith("/name "):
            self.rename_session(text[len("/name ") :].strip())
        elif text.startswith("/btw "):
            self.side_question(text[len("/btw ") :].strip())
        elif text.startswith("/bg "):
            self.background_task(text[len("/bg ") :].strip())
        elif text == "/exit":
            self.exit = True
        elif text == "/stop":
            self.stop()
            self.draft = Draft()
        elif text == "/help":
            self.hermes_command("/help")
        elif text == "/clear":
            self.state.clear()
            self.state.add("notice", "Transcript cleared. Hermes still remembers this session.")
        elif text == "/cost":
            self.state.add("notice", self.state.cost())
        elif text == "/doctor":
            self.doctor()
        else:
            self.hermes_command(text)

    def open_sessions(self) -> None:
        """The sessions sheet, filled from Hermes's store as soon as it answers."""
        if not self.state.session_id:
            return
        self.request("session.list", self.profiled(limit=SESSIONS))
        self.state.open_sheet("sessions")

    def resume_session(self, target: str) -> None:
        """Pick up an earlier session in this pane: its transcript replaces this one, and this live
        session is closed in Hermes once the other is open (its history stays in Hermes's store)."""
        if not target or not self.state.session_id or self.switching:
            return
        if self.state.image_loading or self.state.draft_images:
            self.state.add(
                "notice", "Finish importing, then send or remove draft images before switching sessions."
            )
            return
        if self.state.busy:
            self.state.add("notice", "Finish or stop this turn before switching sessions.")
            return
        if target == self.state.stored_session_id:
            self.state.close_sheet()
            self.state.add("notice", "This is the session you are in.")
            return
        if self.state.queued and not all(message.destination == target for message in self.state.queued):
            self.state.add(
                "notice", "Continue or discard the current session's queue before switching sessions."
            )
            self.restoring = None
            return
        self.switching = target
        self.state.switching = True
        self.state.queue_paused = True
        self.state.touch()
        self.request("session.resume", self.profiled(session_id=target, cols=self.cols))

    def hermes_command(self, text: str) -> None:
        """Hand a slash command to Hermes, the way its own terminal does: `slash.exec` first, which
        answers with output; it refers skills, bundles and commands that need the live session to
        `command.dispatch` (see command_result). Hermes refuses what may not run mid-turn itself."""
        if not self.backend or not self.state.session_id:
            self.state.add("notice", f"{text.split(' ')[0]} needs Hermes, which is still starting.")
            return
        future = self.request("slash.exec", self.scoped(command=text[1:]))
        self.asked[future] = text

    def dispatch(self, text: str) -> None:
        name, _, arg = text[1:].partition(" ")
        future = self.request("command.dispatch", self.scoped(name=name.lower(), arg=arg.strip()))
        self.asked[future] = text

    def command_result(self, text: str, result: dict) -> None:
        """What Hermes made of a slash command: output to show, a message to send, a draft to put
        back in the composer, or another command it stands for."""
        name, _, arg = text.partition(" ")
        kind = result.get("type")
        if kind == "alias" and result.get("target"):
            self.run_command(f"/{result['target']}" + (f" {arg}" if arg else ""))
        elif kind in ("skill", "send"):
            if result.get("notice"):
                self.state.add("notice", str(result["notice"]))
            message = str(result.get("message") or "").strip()
            if message:
                self.send_command(message, str(result.get("display") or "").strip() or text)
        elif kind == "prefill":
            if result.get("notice"):
                self.state.add("notice", str(result["notice"]))
            message = str(result.get("message") or "")
            self.draft = Draft(message, len(message))
            self.state.touch()
        else:
            output = str(result.get("output") or "").strip("\n") or f"{name}: no output"
            if result.get("warning"):
                output = f"warning: {result['warning']}\n{output}"
            if name == "/help":
                output += "\n\n" + HELP
            self.show_output(name, output)

    def show_output(self, name: str, output: str) -> None:
        """A short answer is a note; a long one folds into a row of its own, a few lines showing."""
        lines = [line for line in output.splitlines() if line.strip()]
        if len(output) <= NOTE_CHARS and len(lines) <= NOTE_LINES:
            self.state.add("notice", output)
        else:
            self.state.add("command", output, name=name)

    def send_command(self, message: str, shown: str) -> None:
        """Send what a command expanded to, showing the command as it was typed rather than the
        expansion, which is written for the model."""
        if not self.state.ready or self.state.failed or self.switching:
            self.state.add("notice", "Reconnect or finish switching sessions before sending this command.")
            return
        if self.state.held_message:
            self.state.add("notice", "Restore or discard the retained message before sending this command.")
            return
        if self.state.busy:
            self.state.add("notice", f"{shown.split(' ')[0]} can run when this turn ends.")
            return
        self.state.begin(shown)
        self.state.last_prompt = message
        self.state.last_message = Message(message)
        self.request("prompt.submit", self.scoped(text=message))

    def switch_failed(self) -> None:
        self.switching = None
        self.state.switching = False
        self.state.queue_paused = True
        if self.held_switch:
            if any(message.images for message in self.held_switch):
                self.state.add(
                    "notice",
                    "Session switch failed. Image messages remain paused for their destination; retry that switch or discard the queue.",
                )
                for message in self.held_switch:
                    self.state.queue(message)
            else:
                self.state.add(
                    "notice", "Session switch failed. Held messages were kept in your draft; none were sent."
                )
                text = "\n\n".join(
                    [message.text for message in self.held_switch]
                    + ([self.draft.text] if self.draft.text else [])
                )
                self.draft = Draft(text, len(text))
            self.held_switch.clear()
        self.restoring = None
        self.state.touch()
        self.save_drafts(force=True)

    def export_transcript(self, destination: str) -> None:
        path = Path(destination).expanduser() if destination else self.cwd / "hermes-conversation.md"
        if not path.is_absolute():
            path = self.cwd / path
        try:
            # Exclusive creation prevents accidental overwrite, including through symlinks.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(markdown(self.state))
            self.state.add("notice", f"Exported loaded conversation to {path}")
        except OSError:
            self.state.add(
                "notice",
                f"Could not export to {path}. Choose a new writable filename; existing files are never overwritten.",
            )

    def doctor(self) -> None:
        from .diagnostics import diagnose

        if any(method == "local.doctor" for method, _, _ in self.pending):
            return
        future = Future()

        def check():
            try:
                future.set_result(diagnose(self.cwd, surface_caps=self.session.caps))
            except Exception:
                future.set_result(
                    ["Doctor could not finish. Check the launcher settings and backend installation."]
                )

        worker = threading.Thread(target=check, daemon=True)
        worker.start()
        self._retired.append(worker)
        self.pending.append(("local.doctor", future, time.monotonic() + 30))
        self.state.add("notice", f"project: {self.cwd}")
        self.state.add("notice", f"session: {self.state.session_id or 'none'}")
        self.state.add(
            "notice", "Checking launcher integrity and backend handshake (no agent session or prompt)."
        )

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
