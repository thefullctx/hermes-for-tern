"""A transcript reducer independent of transport and rendering."""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

# Streamed text is released at an even pace: a backlog drains in about DRAIN seconds,
# never slower than MIN_RATE characters per second.
DRAIN = 0.35
MIN_RATE = 120.0
# Newly revealed text is written in gold ink that dries: each FADE age ends one stage.
FADE = (0.12, 0.32, 0.6)


def delivered(seconds: float, usage: dict) -> str:
    """The quiet line that closes a turn: how long it took and what it used."""
    took = f"{seconds:.1f}s" if seconds < 60 else f"{int(seconds // 60)}m {int(seconds % 60):02d}s"
    tokens = usage.get("total")
    if isinstance(tokens, int) and tokens:
        return f"delivered · {took} · " + (
            f"{tokens / 1000:.1f}K tokens" if tokens >= 1000 else f"{tokens} tokens"
        )
    return f"delivered · {took}"


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
    started: float = field(default_factory=lambda: time.time())
    duration: float | None = None
    collapsed: bool = True
    diff: str = ""
    exit_code: int | None = None
    shown: int | None = None  # characters revealed so far; None shows all of `text`
    carry: float = 0.0
    reveals: deque[tuple[float, int]] = field(default_factory=deque)  # (time, shown) steps
    agents: list[Agent] = field(default_factory=list)  # subagents a delegate_task call spawned

    @property
    def visible(self) -> str:
        return self.text if self.shown is None else self.text[: self.shown]

    @property
    def pending(self) -> bool:
        return self.shown is not None and self.shown < len(self.text)

    def replace(self, text: str) -> None:
        """New text keeps what is already shown only where it still agrees."""
        if self.shown is not None:
            pairs = enumerate(zip(self.text[: self.shown], text))
            self.shown = next((i for i, (old, new) in pairs if old != new), min(self.shown, len(text)))
            self.reveals.clear()
        self.text = text

    def reveal(self, count: int, now: float) -> None:
        if not self.reveals:
            self.reveals.append((float("-inf"), self.shown))
        self.shown = min(len(self.text), self.shown + count)
        self.reveals.append((now, self.shown))
        while len(self.reveals) > 1 and now - self.reveals[1][0] > FADE[-1]:
            self.reveals.popleft()

    def fading(self, now: float) -> bool:
        return bool(self.reveals) and now - self.reveals[-1][0] < FADE[-1]

    def fresh(self, now: float) -> tuple[str, ...]:
        """The text revealed within each FADE stage, the driest first and the newest last."""
        if self.shown is None or not self.fading(now):
            return ("",) * len(FADE)

        def shown_at(moment: float) -> int:
            return ([count for at, count in self.reveals if at <= moment] or [self.reveals[0][1]])[-1]

        bounds = [shown_at(now - age) for age in reversed(FADE)] + [self.shown]
        return tuple(self.text[a:b] for a, b in zip(bounds, bounds[1:]))


# Hermes's terminal states for a delegated child, as Tern's agent states.
AGENT_STATUS = {
    "queued": "pending",
    "running": "running",
    "completed": "done",
    "failed": "failed",
    "error": "failed",
    "timeout": "failed",
    "interrupted": "aborted",
}


@dataclass
class Agent:
    """A delegated child, from Hermes's `subagent.*` events."""

    id: str
    goal: str
    index: int = 0
    depth: int = 0
    parent: str | None = None
    model: str = ""
    status: str = "pending"
    tool: str = ""
    tool_preview: str = ""
    tool_started: float = 0.0
    tools: int = 0
    tokens: int = 0
    started: float = field(default_factory=lambda: time.time())
    duration: float | None = None
    summary: str = ""

    def update(self, kind: str, payload: dict) -> None:
        if payload.get("model"):
            self.model = str(payload["model"])
        tokens = sum(int(payload.get(k) or 0) for k in ("input_tokens", "output_tokens"))
        self.tokens = max(self.tokens, tokens)
        self.tools = max(self.tools, int(payload.get("tool_count") or 0))
        finished = self.status in ("done", "failed", "aborted")
        if kind == "subagent.spawn_requested" and not finished:
            self.status = "pending"
        elif kind == "subagent.complete":
            self.status = AGENT_STATUS.get(str(payload.get("status") or "completed"), "done")
            self.duration = payload.get("duration_seconds") or self.duration
            self.summary = str(payload.get("summary") or payload.get("text") or self.summary)
            self.tool = ""
        elif not finished:
            self.status = "running"
            if kind == "subagent.tool":
                self.tool = str(payload.get("tool_name") or "tool")
                self.tool_preview = str(payload.get("tool_preview") or payload.get("text") or "")
                self.tool_started = time.time()
                self.tools = max(self.tools, int(payload.get("tool_count") or 0) or self.tools + 1)


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
        self.agents: dict[str, Agent] = {}
        self._paced_at = time.monotonic()

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
        self.touch()

    def pace(self, now: float) -> None:
        """Reveal streamed text smoothly, however bursty its arrival."""
        dt = min(0.1, max(0.0, now - self._paced_at))
        self._paced_at = now
        for row in self.rows:
            if row.fading(now):
                self.touch()
            if not row.pending:
                continue
            backlog = len(row.text) - row.shown
            row.carry += max(MIN_RATE, backlog / DRAIN) * dt
            step = int(row.carry)
            if step:
                row.carry -= step
                row.reveal(step, now)
                self.touch()

    def assistant(self) -> Row:
        if self._assistant is None:
            self._assistant = self.add("assistant", shown=0)
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
            self.activity = "Writing"
        elif kind == "message.interim":
            text = str(payload.get("text", ""))
            if text:
                if self._assistant is not None:
                    self._assistant.replace(text)
                elif not self._turn_assistants or self._turn_assistants[-1].text != text:
                    self.assistant().replace(text)
            self._assistant = None
        elif kind.startswith("subagent."):
            self.subagent(kind, payload)
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
                collapsed=payload.get("name") != "delegate_task",  # its children stay in view
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
                    self._assistant.replace(text)
                elif not self._turn_assistants or self._turn_assistants[-1].text != text:
                    self.assistant().replace(text)
            status = payload.get("status", "complete")
            if status == "interrupted":
                self.add("notice", "Stopped")
            elif status == "error" or payload.get("error"):
                error = payload.get("error") or payload.get("failure_reason") or "The turn failed. Try again."
                self.add("error", str(error))
            for tool in self.tools.values():
                if tool.status == "running":
                    tool.status = "cancelled" if status == "interrupted" else "error"
            for agent in self.agents.values():
                if agent.status in ("pending", "running"):
                    agent.status = "aborted" if status == "interrupted" else "failed"
            self.usage = payload.get("usage") or self.usage
            if status == "complete" and not payload.get("error") and self.turn_started is not None:
                self.add(
                    "delivered",
                    delivered(time.monotonic() - self.turn_started, self.usage),
                    duration=time.monotonic() - self.turn_started,
                )
            self.busy = False
            self.activity = "Ready"
            self._assistant = None
        elif kind == "session.usage":
            self.usage = payload.get("usage") or {}
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

    def subagent(self, kind: str, payload: dict) -> None:
        """Children join the delegate_task call that spawned them; late events never revive one."""
        aid = str(payload.get("subagent_id") or f"{payload.get('task_index', 0)}:{payload.get('goal', '')}")
        agent = self.agents.get(aid)
        if agent is None:
            if kind not in ("subagent.spawn_requested", "subagent.start") or not self.busy:
                return
            agent = Agent(
                aid,
                str(payload.get("goal") or "Subagent"),
                index=int(payload.get("task_index") or 0),
                depth=int(payload.get("depth") or 0),
                parent=payload.get("parent_id"),
            )
            self.agents[aid] = agent
            self.delegation().agents.append(agent)
        agent.update(kind, payload)
        # A delegation Hermes never announced as a tool settles when its last child does.
        for key, row in self.tools.items():
            if key.startswith("delegation-") and agent in row.agents and row.status == "running":
                if all(a.status in ("done", "failed", "aborted") for a in row.agents):
                    row.status = "error" if any(a.status == "failed" for a in row.agents) else "done"

    def delegation(self) -> Row:
        running = [r for r in self.tools.values() if r.status == "running" and r.name == "delegate_task"]
        if running:
            return running[-1]
        self._assistant = None
        row = self.add("tool", name="delegate_task", status="running", collapsed=False)
        self.tools[f"delegation-{row.key}"] = row
        return row

    def clarify(self) -> Question | None:
        return next((q for q in self.questions.values() if q.method == "clarify"), None)
