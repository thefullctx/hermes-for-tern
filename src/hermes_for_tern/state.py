"""A transcript reducer independent of transport and rendering."""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


def readable(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2) if value is not None else ""


@dataclass
class Row:
    key: str
    kind: str
    text: str = ""
    name: str = ""
    target: str = ""
    status: str = "done"
    started: float = field(default_factory=time.time)
    duration: float | None = None
    collapsed: bool = True
    diff: str = ""
    exit_code: int | None = None


@dataclass
class Question:
    rid: str
    method: str
    params: dict
    index: int = 0
    answers: dict[str, str | None] = field(default_factory=dict)
    selected: set[str] = field(default_factory=set)

    @property
    def current(self) -> dict:
        questions = self.params.get("questions", [])
        return questions[self.index] if self.index < len(questions) else {}


class Conversation:
    def __init__(self):
        self.rows: list[Row] = []
        self.tools: dict[str, Row] = {}
        self.questions: dict[str, Question] = {}
        self.session_id: str | None = None
        self.stored_session_id: str | None = None
        self.info: dict = {}
        self.busy = False
        self.ready = False
        self.failed = False
        self.activity = "Connecting to Hermes"
        self.usage: dict = {}
        self._counter = 0
        self._assistant: Row | None = None
        self._turn_assistants: list[Row] = []
        self.revision = 0
        self.turn_started: float | None = None
        self.stream_chars = 0
        self.activity_samples: deque[float] = deque([0.0] * 24, maxlen=24)
        self._sample_at = 0.0
        self._sample_chars = 0
        self.demo_progress: float | None = None

    def touch(self) -> None:
        self.revision += 1

    def add(self, kind: str, text: str = "", **kwargs) -> Row:
        self._counter += 1
        row = Row(f"r{self._counter}", kind, text, **kwargs)
        self.rows.append(row)
        self.touch()
        return row

    def begin(self, text: str) -> None:
        self.add("user", text)
        self._assistant = None
        self._turn_assistants = []
        self.busy = True
        self.activity = "Thinking"
        self.turn_started = time.monotonic()
        self.stream_chars = 0
        self._sample_chars = 0
        self._sample_at = self.turn_started
        self.activity_samples = deque([0.0] * 24, maxlen=24)
        self.demo_progress = 0.0 if self.info.get("demo") else None
        self.touch()

    def sample_activity(self, now: float) -> None:
        """Sample received text, not model tokens or estimated task progress."""
        if not self.busy or self.questions or now - self._sample_at < 0.5:
            return
        self.activity_samples.append(float(self.stream_chars - self._sample_chars))
        self._sample_chars = self.stream_chars
        self._sample_at = now
        self.touch()

    def assistant(self) -> Row:
        if self._assistant is None:
            self._assistant = self.add("assistant")
            self._turn_assistants.append(self._assistant)
        return self._assistant

    def event(self, kind: str, payload: dict) -> None:
        if kind == "session.info":
            self.info.update(payload)
            self.ready = True
            self.activity = "Ready" if not self.busy else self.activity
        elif kind == "message.start":
            self.busy = True
            self.activity = "Writing"
        elif kind == "message.delta":
            delta = str(payload.get("text", ""))
            self.assistant().text += delta
            self.stream_chars += len(delta)
            self.activity = "Writing"
        elif kind == "message.interim":
            text = str(payload.get("text", ""))
            if text:
                if self._assistant is not None:
                    self._assistant.text = text
                elif not self._turn_assistants or self._turn_assistants[-1].text != text:
                    self.assistant().text = text
            self._assistant = None
        elif kind == "tool.start":
            self._assistant = None
            tid = str(payload.get("tool_id", ""))
            args = payload.get("args") or {}
            target = next(
                (str(args[k]) for k in ("command", "path", "file_path", "query", "url") if args.get(k)), ""
            )
            row = self.add(
                "tool",
                readable(args),
                name=payload.get("name", "Tool"),
                target=target[:240],
                status="running",
            )
            self.tools[tid] = row
            self.activity = row.name.replace("_", " ")
        elif kind == "tool.complete":
            tid = str(payload.get("tool_id", ""))
            row = self.tools.get(tid)
            if row is None:
                row = self.add("tool", name=payload.get("name", "Tool"))
                self.tools[tid] = row
            result = payload.get("result")
            row.exit_code = result.get("exit_code") if isinstance(result, dict) else None
            is_error = isinstance(result, dict) and (
                result.get("is_error")
                or result.get("error")
                or result.get("success") is False
                or row.exit_code not in (None, 0)
            )
            row.status = "error" if is_error else "done"
            output = result
            if isinstance(result, dict):
                output = next(
                    (result[k] for k in ("output", "stdout", "content", "text") if k in result), result
                )
                if not output and result.get("error"):
                    output = result["error"]
            row.text = readable(output) or payload.get("result_text") or str(payload.get("summary", ""))
            row.duration = payload.get("duration_s")
            row.diff = payload.get("inline_diff") or ""
            self.activity = "Thinking"
        elif kind == "message.complete":
            text = readable(payload.get("text", ""))
            # Final text replaces the streamed segment; reused text adds no new row.
            if text and not payload.get("response_reused"):
                if self._assistant is not None:
                    self._assistant.text = text
                elif not self._turn_assistants or self._turn_assistants[-1].text != text:
                    self.assistant().text = text
            status = payload.get("status", "complete")
            if status == "interrupted":
                self.add("notice", "Stopped")
            elif status == "error" or payload.get("error"):
                error = payload.get("error") or payload.get("failure_reason") or "The turn failed. Try again."
                self.add("error", str(error))
            for tool in self.tools.values():
                if tool.status == "running":
                    tool.status = "cancelled" if status == "interrupted" else "error"
            self.usage = payload.get("usage") or self.usage
            self.busy = False
            self.activity = "Ready"
            self._assistant = None
        elif kind == "session.usage":
            self.usage = payload.get("usage") or {}
        elif kind == "demo.progress" and self.info.get("demo"):
            self.demo_progress = max(0.0, min(1.0, float(payload.get("value", 0))))
        elif kind == "status.update":
            self.activity = str(payload.get("text") or "Working")[:140]
        elif kind == "error":
            self.add("error", str(payload.get("message", "Hermes reported an error")))
            if not self.ready:
                self.failed = True
                self.busy = False
                self.activity = "Could not start Hermes"
        elif kind == "notice":
            self.add("notice", str(payload.get("message", "")))
        elif kind == "request.cancel":
            self.questions.pop(str(payload.get("id")), None)
        self.touch()

    def clarify(self) -> Question | None:
        return next((q for q in self.questions.values() if q.method == "clarify"), None)
