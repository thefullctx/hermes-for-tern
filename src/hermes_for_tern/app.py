"""Single-threaded UI loop; Hermes runs over private pipes in a separate process."""

from __future__ import annotations

import queue
import shlex
import time
from concurrent.futures import Future
from pathlib import Path

import tern_sdk
from tern_sdk import EditEvent, ErrorEvent, FocusEvent, GoneEvent, Key, SendEvent, UndoEvent, VisibleEvent

from .editor import Draft
from .rpc import Backend
from .state import SECRET_METHODS, TAIL, Conversation, Question, hermes_effort
from .design import CSS, DARK, LIGHT, send_assets
from .views import sheet_cursor, sheet_rows, view

# Keys that pick a question's choice, in the order the card shows them.
DIGITS = "123456789"
# What the composer can be told to do, all handled here rather than sent to the model.
COMMANDS = (
    "/help",
    "/clear",
    "/cost",
    "/retry",
    "/doctor",
    "/stop",
    "/quit",
    "/exit",
    "/model",
    "/btw",
    "/bg",
)
HELP = "Commands: /help · /clear · /cost · /retry · /doctor · /stop · /quit · /model · /btw · /bg"


class App:
    def __init__(self, session, command: list[str], cwd: Path, options: dict):
        self.session = session
        self.command = command
        self.cwd = cwd
        self.options = {key: value for key, value in options.items() if key != "demo"}
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

    def request(self, method: str, params: dict | None = None) -> None:
        assert self.backend
        self.pending.append((method, self.backend.request(method, params), time.monotonic() + 60))

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
                self.backend = Backend(self.command, self.cwd)
                last_render = 0.0
                revision = -1
                while not self.exit and not self.session.closed and not surface.closed:
                    self.read_backend()
                    self.check_requests()
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
                    if self.state.revision != revision and now - last_render >= 0.033:
                        self.render()
                        revision = self.state.revision
                        last_render = now
            except (OSError, RuntimeError) as exc:
                self.fail(str(exc))
                self.render()
                # Keep a failed launch readable; allow the user to leave normally.
                while not self.exit and not self.session.closed and not surface.closed:
                    item = self.session.poll(0.1)
                    if item is not None:
                        self.input(item)
            finally:
                if self.backend:
                    self.backend.close(self.state.session_id)
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
        self.state.add("error", message)
        self.state.failed = True
        self.state.ready = False
        self.state.busy = False
        self.state.activity = "Disconnected"
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
                if (
                    params.get("session_id")
                    and self.state.session_id
                    and params["session_id"] != self.state.session_id
                ):
                    continue
                kind = params.get("type", "")
                if kind == "gateway.ready":
                    self.request("client.capabilities", {"server_requests": True})
                else:
                    old_question = self.state.clarify()
                    busy = self.state.busy
                    self.state.event(kind, params.get("payload") or {})
                    if old_question and old_question is not self.state.clarify():
                        self.answer_draft = Draft()
                    if busy and not self.state.busy and self.state.queued:
                        held = self.state.take_queued()
                        if held:
                            self.submit(held)
            elif "method" in frame and "id" in frame:
                self.server_request(frame)

    def check_requests(self) -> None:
        batch, self.pending = self.pending, []
        remaining = []
        for method, future, deadline in batch:
            if not future.done():
                if time.monotonic() > deadline:
                    if method in ("session.create", "client.capabilities"):
                        self.fail(f"Timed out waiting for {method}.")
                    else:
                        self.state.add(
                            "error", f"Hermes has not acknowledged {method}. Use Stop before retrying."
                        )
                    continue
                remaining.append((method, future, deadline))
                continue
            try:
                result = future.result()
                if method == "client.capabilities":
                    params = {
                        "cwd": str(self.cwd),
                        "cwd_explicit": True,
                        "source": "tui",
                        "title": "Hermes for Tern",
                        "cols": self.session.caps.cols or 80,
                        **self.options,
                    }
                    self.request("session.create", params)
                elif method == "session.create":
                    self.state.session_id = result["session_id"]
                    self.state.stored_session_id = result.get("stored_session_id")
                    self.state.info.update(result.get("info") or {})
                    self.state.ready = True
                    self.state.activity = "Ready"
                    self.state.touch()
                    if self.demo_duration is not None:
                        self.submit(f"Run a {self.demo_duration:g}-second simulated visual demo.")
                elif method == "session.interrupt":
                    self.state.activity = "Stopping" if self.state.busy else "Ready"
                    self.state.touch()
                elif method == "model.options":
                    self.state.models = result
                    self.state.touch()
                elif method == "config.set":
                    self.model_set(result)
                elif method == "subagent.tail":
                    self.state.watch_tail(result)
                elif method in ("subagent.interrupt", "subagent.steer"):
                    self.steer_result(method, result)
            except Exception as exc:
                if method in ("client.capabilities", "session.create"):
                    self.fail(str(exc))
                else:
                    self.state.add("error", str(exc))
                    if method == "prompt.submit":
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
        if self.state.busy and self.state.session_id:
            self.request("session.interrupt", self.scoped())
            self.state.activity = "Stopping"
            self.state.touch()

    def cycle_effort(self) -> None:
        """The ring was pressed: ask Hermes to think a little deeper, or not, from now on."""
        level = self.state.cycle_effort()
        if self.state.session_id:
            self.request("config.set", self.scoped(key="reasoning", value=hermes_effort(level)))

    def watch_event(self, action: str) -> None:
        """The watched child's own buttons: interrupt it, steer it, or stop watching."""
        if action == "interrupt":
            self.interrupt_agent()
        elif action == "steer":
            self.steer_agent(self.state.watch.steer if self.state.watch else "")
        elif action == "close":
            self.state.close_watch()

    def model_event(self, action: str, value: str = "") -> None:
        """The model sheet's own events: `open` from the model segment, `pick` from a row,
        `confirm` from its switch key, `cancel` from its cancel key or the backdrop."""
        if action == "open":
            if self.state.session_id:
                self.request("model.options", self.scoped(refresh=True))
            self.state.open_sheet()
        elif action == "pick":
            if value:
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
        if self.state.sheet.confirming:
            self.switch_model(self.state.sheet.chosen, confirmed=True)
            return
        chosen = sheet_cursor(self.state)
        if chosen:
            self.switch_model(chosen)

    def switch_model(self, value: str, *, confirmed: bool = False) -> None:
        """Ask Hermes to run another model. Mid-turn it holds the pick for the next turn start and
        reports it in session.info; an expensive model asks first, which is what `confirmed` is."""
        if not value or not self.state.session_id:
            return
        if self.state.sheet:
            # The sheet's row is the pending pick, so a confirmation repeats the same one.
            self.state.choose_row(value)
        params: dict = {"key": "model", "value": value}
        if confirmed:
            params["confirm_expensive_model"] = True
        self.request("config.set", self.scoped(**params))

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

    def watch_key(self, item) -> bool:
        """The watched child owns the keyboard while it is open: a line to steer it, Enter to
        send, Escape to stop watching. True when the key was the watch's."""
        watch = self.state.watch
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
        if self.state.can_retry:
            prompt = self.state.last_prompt
            self.state.begin(prompt)
            self.request("prompt.submit", self.scoped(text=prompt))

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
        text = self.draft.text if text is None else text
        if not text.strip():
            if self.state.can_retry:
                self.retry()
            return
        if text.strip().startswith("/"):
            self.run_command(text.strip())
            if text.strip() != "/stop":
                self.draft = Draft()
            return
        if not self.state.ready or self.state.failed:
            return
        if self.state.busy:
            # Hermes is still working; hold the thought rather than dropping it on the floor.
            self.state.queue(text)
            self.draft = Draft()
            return
        self.state.begin(text)
        self.draft = Draft()
        self.request("prompt.submit", self.scoped(text=text))

    def run_command(self, text: str) -> None:
        """One of COMMANDS, answered here; anything else starting with `/` is not one."""
        if text in ("/quit", "/exit"):
            self.exit = True
        elif text == "/model":
            self.model_event("open")
        elif text.startswith("/model "):
            self.switch_model(text[len("/model ") :].strip())
        elif text.startswith("/btw "):
            self.side_question(text[len("/btw ") :].strip())
        elif text.startswith("/bg "):
            self.background_task(text[len("/bg ") :].strip())
        elif text == "/stop":
            self.stop()
            self.draft = Draft()
        elif text == "/help":
            self.state.add("notice", HELP)
        elif text == "/clear":
            self.state.clear()
            self.state.add("notice", "Transcript cleared. Hermes still remembers this session.")
        elif text == "/cost":
            self.state.add("notice", self.state.cost())
        elif text == "/doctor":
            self.doctor()
        elif text == "/retry":
            if self.state.can_retry:
                self.retry()
            else:
                self.state.add("notice", "Nothing to retry.")
        else:
            self.state.add("notice", f"{text} is not a command. Try {', '.join(COMMANDS[:6])} or /help.")

    def doctor(self) -> None:
        """What `--doctor` prints on the terminal, said here so it is never a dead end."""
        from . import launcher

        lines = []
        try:
            config = launcher.settings()
            original = launcher.original_command(config)
            lines.append(("hermes", original))
            lines.append(("backend", shlex.join(launcher.backend_command(original, config))))
            lines.append(("wrapper", str(config.get("target", "not installed"))))
        except (OSError, RuntimeError, ValueError) as exc:
            lines.append(("launcher", str(exc)))
        lines.append(("protocol", str(getattr(tern_sdk.wire, "VERSION", "?"))))
        lines.append(("session", self.state.session_id or "none"))
        lines.append(("project", str(self.cwd)))
        lines.append(("state", f"ready={self.state.ready} busy={self.state.busy}"))
        for name, value in lines:
            self.state.add("notice", f"{name}: {value}")

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
            if item.ctrl and item.name == "d" and not draft.text:
                self.exit = True
            elif item.ctrl and item.name == "c":
                if self.state.busy:
                    self.stop()
                elif draft.text:
                    draft.replace(0, len(draft.text), "")
                else:
                    self.exit = True
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
            if item.id == "dock.composer":
                draft.edit(item)
                self.state.touch()
        elif isinstance(item, UndoEvent):
            draft.undo()
            self.state.touch()
        elif isinstance(item, SendEvent) and item.id == "dock.composer":
            self.submit(item.text or "")
        elif isinstance(item, FocusEvent):
            self.surface.focus("dock.composer")
        elif isinstance(item, ErrorEvent):
            self.state.add("error", f"Tern: {item.msg}")
        elif isinstance(item, VisibleEvent) and item.visible is not None:
            self.state.show(item.visible)
        elif isinstance(item, GoneEvent) and self.surface.id in item.ids:
            self.exit = True
