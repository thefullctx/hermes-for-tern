"""Single-threaded UI loop; Hermes runs over private pipes in a separate process."""

from __future__ import annotations

import queue
import time
from concurrent.futures import Future
from pathlib import Path

from tern_sdk import EditEvent, ErrorEvent, FocusEvent, GoneEvent, Key, SendEvent, UndoEvent, VisibleEvent

from .editor import Draft
from .rpc import Backend
from .state import Conversation, Question
from .design import CSS, DARK, LIGHT, send_assets
from .views import view


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
                        self.fail("Hermes took too long to start. Exit and run hermes-for-tern --doctor.")
                    item = self.session.poll(0.02)
                    if item is not None:
                        self.input(item)
                    now = time.monotonic()
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
            )
        )
        if self.backend:
            for rid in self._ack_approvals:
                question = self.state.questions.get(rid)
                if question:
                    self.request("approval.received", self.scoped(request_id=question.params["request_id"]))
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
                    self.state.event(kind, params.get("payload") or {})
                    if old_question and old_question is not self.state.clarify():
                        self.answer_draft = Draft()
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
        question = self.state.clarify()
        if question:
            answer = self.answer_draft.text if text is None else text
            if answer.strip():
                self.answer_question(question, answer)
            return
        text = self.draft.text if text is None else text
        if not text.strip() and self.state.can_retry:
            self.retry()
            return
        if text.strip() in ("/quit", "/exit"):
            self.exit = True
        elif text.strip() == "/stop":
            self.stop()
            self.draft = Draft()
        elif self.state.ready and not self.state.busy and not self.state.failed and text.strip():
            if text.startswith("/"):
                self.state.add(
                    "notice", "Slash commands are not supported yet. Use plain prompts, /stop or /quit."
                )
                return
            self.state.begin(text)
            self.draft = Draft()
            self.request("prompt.submit", self.scoped(text=text))

    def input(self, item) -> None:
        draft = self.answer_draft if self.state.clarify() else self.draft
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
            elif item.name == "escape":
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
