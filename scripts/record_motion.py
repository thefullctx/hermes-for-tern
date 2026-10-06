"""Record the README scenes through the real state and views, frame by frame, for `tern shot`.

    uv run python scripts/record_motion.py assets

writes `startup.jsonl` (connecting, then the welcome) and `session.jsonl` (a scripted turn with
bursty streaming, a failing test run, subagents and a fix). Frames are taken at 30 Hz on a
simulated clock, so recordings are deterministic and replay at real pace. No Hermes runs.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from tern_sdk import wire
from tern_sdk.reconcile import View

from hermes_for_tern.design import ASSETS, CSS, DARK, LIGHT
from hermes_for_tern.editor import Draft
from hermes_for_tern.state import Conversation
from hermes_for_tern.views import view

EPOCH = 1_800_000_000.0
FPS = 30
MODEL = "hermes-4-405b"
# The handshake a dark Tern 0.5 pane answers with, so a replay opens like a live session.
HELLO = [
    {"verb": "q", "dir": "out", "body": {"q": "hello", "v": [1], "app": "hermes", "ver": "0.1.0",
                                         "features": ["edit", "undo", "send"]}},
    {"verb": "r", "dir": "in", "body": {
        "r": "hello", "v": 1, "term": "tern", "ver": "0.5.0",
        "kinds": ["col", "row", "card", "section", "rule", "spacer", "text", "md", "code", "diff", "ansi",
                  "math", "image", "kv", "table", "tree", "badge", "kbd", "icon", "spinner", "shimmer",
                  "elapsed", "progress", "rate", "list", "item", "tabs", "editor", "input", "status", "seg",
                  "overlay", "toast", "rows", "picker", "prefs", "tool", "checklist", "agent", "chart",
                  "meter", "block", "effort", "el"],
        "features": ["blobs", "settle", "adopt", "dock", "program-palette", "reduce-motion", "aside",
                     "scroll", "styles", "flow"],
        "apc": 65536, "credits": 2, "cols": 229, "cell": {"w": 8, "h": 16}, "dark": True,
        "reduceMotion": False, "hour12": False}},
]  # fmt: skip


class Clock:
    now = 0.0

    def wall(self) -> float:
        return EPOCH + self.now

    def mono(self) -> float:
        return self.now


def bursts(
    text: str, start: float, rate: float = 70.0, kind: str = "message.delta"
) -> list[tuple[float, str, dict]]:
    """Deltas the way models send them: uneven chunks at uneven intervals."""
    events, at, i, n = [], start, 0, 0
    while i < len(text):
        size = (18, 7, 31, 12, 44, 9, 26)[n % 7]
        chunk = text[i : i + size]
        events.append((at, kind, {"text": chunk}))
        at += len(chunk) / rate * (0.6, 1.5, 0.8, 1.2)[n % 4]
        i, n = i + size, n + 1
    return events


INTRO = "I'll check the test configuration first, then run the suite to see exactly what fails."
FOLLOW = "One test times out on CI. Let me read how it sets its deadline."
FINAL = (
    "## Fixed: a timing assumption in one test\n\n"
    "The failure came from `test_timeout`, which waited a fixed 50 ms for the backend. "
    "CI machines are slower, so the wait expired before the reply arrived.\n\n"
    "- The test now waits on the reply itself, with a generous upper bound.\n"
    "- Nothing in the application changed.\n"
    "- All 27 tests pass locally in under a second.\n\n"
    "```python\n"
    'reply = backend.request("session.create", {})\n'
    'assert reply.result(timeout=2)["session_id"]\n'
    "```\n\n"
    "Push the branch and CI should be green again."
)


SEARCH = {"pattern": r"timeout=0\.\d+", "target": "content", "path": "tests"}
FOUND = {
    "total_count": 3,
    "matches": [
        {"path": "tests/test_rpc.py", "line": 41, "content": "    reply = future.result(timeout=0.05)"},
        {"path": "tests/test_rpc.py", "line": 58, "content": "    backend.wait(timeout=0.05)"},
        {"path": "tests/test_app.py", "line": 22, "content": "    app.poll(timeout=0.02)"},
    ],
}
FAILURE = (
    "Rate limited by the provider (429). Too many requests in the last minute.\n"
    "Details: retry after a few seconds."
)

THOUGHT = (
    "CI fails but local runs pass, so something depends on timing or environment. "
    "Start with the test configuration, then run the suite and read the failure."
)
SECOND_THOUGHT = "A fixed 50 ms wait is fragile on slow runners; the test should wait on the reply itself."
ERRANDS = ["Read the test configuration", "Run the test suite", "Find the failing test", "Fix it and verify"]


def errands(done: int, revision: int) -> tuple[str, dict]:
    todos = [
        {
            "id": str(i),
            "content": c,
            "status": "completed" if i < done else "in_progress" if i == done else "pending",
        }
        for i, c in enumerate(ERRANDS)
    ]
    return "todo.updated", {"todos": todos, "revision": revision}


def timeline() -> list[tuple[float, str, dict]]:
    """The turn: a thought, errands, tools that fail and pass, subagents and the fix."""
    work = [(at + 1.6, kind, payload) for at, kind, payload in steps()]
    extra = bursts(THOUGHT, 0.4, rate=220.0, kind="reasoning.delta")
    extra += bursts(SECOND_THOUGHT, 8.02, rate=220.0, kind="reasoning.delta")
    extra += [
        (at, *errands(done, revision))
        for revision, (at, done) in enumerate([(2.9, 0), (5.5, 1), (8.0, 2), (11.0, 3), (18.4, 4)], 1)
    ]
    extra += [
        (at, "session.usage", {"usage": {"total": tokens, "context_percent": pct}})
        for at, pct, tokens in [(2.0, 8, 2100), (6.0, 12, 4800), (11.5, 16, 8200), (17.0, 21, 11600)]
    ]
    return sorted(work + extra, key=lambda event: event[0])


def steps() -> list[tuple[float, str, dict]]:
    events: list[tuple[float, str, dict]] = [(0.8, "message.start", {})]
    events += bursts(INTRO, 1.0)
    events += [
        (2.6, "message.interim", {"text": INTRO}),
        (2.7, "tool.start", {"tool_id": "t1", "name": "read_file", "args": {"path": "pyproject.toml"}}),
        (
            3.9,
            "tool.complete",
            {
                "tool_id": "t1",
                "result": {"output": '[tool.pytest.ini_options]\ntestpaths = ["tests"]'},
                "duration_s": 1.2,
            },
        ),
        (4.2, "tool.start", {"tool_id": "t2", "name": "terminal", "args": {"command": "uv run pytest -q"}}),
        (
            6.4,
            "tool.complete",
            {
                "tool_id": "t2",
                "duration_s": 2.2,
                "result": {
                    "output": "FAILED tests/test_rpc.py::test_timeout - TimeoutError\n1 failed, 26 passed in 0.71s",
                    "exit_code": 1,
                },
            },
        ),
    ]
    events += bursts(FOLLOW, 6.9)
    events += [
        (8.2, "message.interim", {"text": FOLLOW}),
        (8.3, "tool.start", {"tool_id": "t3", "name": "search_files", "args": SEARCH}),
        (9.3, "tool.complete", {"tool_id": "t3", "result": FOUND, "duration_s": 1.0}),
        (9.6, "tool.start", {"tool_id": "t4", "name": "terminal", "args": {"command": "uv run pytest -q"}}),
        (
            11.6,
            "tool.complete",
            {"tool_id": "t4", "result": {"output": "27 passed in 0.64s", "exit_code": 0}, "duration_s": 2.0},
        ),
    ]

    def agent(at, kind, aid, goal, index, **extra):
        payload = {"subagent_id": aid, "goal": goal, "task_index": index, "task_count": 2, **extra}
        return (at, f"subagent.{kind}", payload)

    scan, docs, deep = (
        "Find other tests with fixed sleeps",
        "Check the CI timeout settings",
        "Read test_app.py",
    )
    events += [
        (12.0, "tool.start", {"tool_id": "t5", "name": "delegate_task", "args": {"goal": "Audit timing"}}),
        agent(12.2, "spawn_requested", "a1", scan, 0, depth=1),
        agent(12.3, "spawn_requested", "a2", docs, 1, depth=1),
        agent(12.6, "start", "a1", scan, 0, depth=1, model="haiku"),
        agent(12.8, "start", "a2", docs, 1, depth=1, model="haiku"),
        agent(13.2, "tool", "a1", scan, 0, tool_name="search_files", tool_preview="sleep(", tool_count=1),
        agent(
            13.6, "tool", "a2", docs, 1, tool_name="read_file", tool_preview=".github/workflows/checks.yml"
        ),
        agent(14.0, "start", "a3", deep, 0, depth=2, parent_id="a1", model="haiku"),
        agent(14.3, "tool", "a3", deep, 0, tool_name="read_file", tool_preview="tests/test_app.py"),
        agent(14.6, "tool", "a1", scan, 0, tool_name="search_files", tool_preview="timeout=0", tool_count=2),
        agent(
            15.4,
            "complete",
            "a3",
            deep,
            0,
            status="completed",
            duration_seconds=1.4,
            summary="No fixed sleeps in test_app.py.",
            input_tokens=2100,
            output_tokens=180,
        ),
        agent(
            15.8,
            "complete",
            "a2",
            docs,
            1,
            status="completed",
            duration_seconds=3.0,
            summary="CI uses the default 10-minute job timeout.",
            input_tokens=3400,
            output_tokens=240,
        ),
        agent(
            16.4,
            "complete",
            "a1",
            scan,
            0,
            status="completed",
            duration_seconds=3.8,
            tool_count=3,
            summary="test_timeout was the only fixed wait.",
            input_tokens=5200,
            output_tokens=410,
        ),
        (
            16.6,
            "tool.complete",
            {"tool_id": "t5", "result": {"output": "2 subagents finished"}, "duration_s": 4.6},
        ),
    ]
    events += bursts(FINAL, 17.0, rate=110.0)
    end = events[-1][0] + 0.4
    events.append(
        (
            end,
            "message.complete",
            {"text": FINAL, "status": "complete", "usage": {"total": 12480, "context_percent": 22}},
        )
    )
    return events


def record(out: Path, script) -> None:
    """Run `script(state, step)` on the simulated clock; `step(seconds)` advances it, framing."""
    clock = Clock()
    time.time, time.monotonic = clock.wall, clock.mono
    t0 = int(EPOCH * 1000)
    lines = [{"t": t0, "params": {}, **line} for line in HELLO]

    def send(msg) -> None:
        lines.append(
            {
                "t": t0 + int(clock.now * 1000),
                "dir": "out",
                "verb": msg.verb,
                "params": dict(msg.params),
                "body": msg.payload,
            }
        )

    send(wire.open_surface("s1", mode="inline", title="Hermes", role="hermes.session"))
    send(wire.stylesheet("s1", "hermes-for-tern", CSS))
    send(
        wire.palette("s1", dark=DARK, light=LIGHT, name={"dark": "Hermes graphite", "light": "Hermes ivory"})
    )
    assets = {}
    for name in ("hermes", "wordmark"):
        blob = wire.blob(ASSETS.joinpath(f"{name}.svg").read_bytes(), "image/svg+xml")
        send(blob)
        assets[name] = blob.params[0][1]

    state = Conversation()
    state._paced_at = 0.0
    noop = lambda *args: None  # noqa: E731
    sent, seq, revision = View(), 0, -1

    def frame() -> None:
        nonlocal sent, seq, revision
        built = View.build(
            view(
                state,
                Draft(),
                Draft(),
                Path("hermes-for-tern"),
                noop,
                noop,
                noop,
                noop,
                noop,
                noop,
                assets,
                noop,
            )
        )
        ops = sent.ops(built, "s1")
        if ops:
            seq += 1
            send(wire.frame("s1", seq, ops))
        sent, revision = built, state.revision

    def step(seconds: float, events: list | None = None) -> None:
        end = clock.now + seconds
        events = list(events or [])
        while clock.now < end:
            clock.now = round(clock.now + 1 / FPS, 6)
            while events and events[0][0] <= clock.now:
                _, kind, payload = events.pop(0)
                state.event(kind, payload)
            state.pace(clock.now)
            if state.revision != revision:
                frame()

    frame()
    script(state, step)
    out.write_text("".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines))
    print(f"{out}: {seq} frames over {clock.now:.1f}s")


def startup(state: Conversation, step) -> None:
    step(0.8)
    state.ready = True
    state.activity = "Ready"
    state.info["model"] = MODEL
    state.touch()
    step(3.0)


def session(state: Conversation, step) -> None:
    """A first attempt the provider rate-limits, a retry, then the turn."""
    prompt = "Why are the tests failing on CI?"
    state.ready = True
    state.info["model"] = MODEL
    step(0.4)
    state.begin(prompt)
    surface = {"layer": "provider", "code": "rate_limit", "retryable": True}
    step(2.6, [(1.4, "message.complete", {"status": "error", "text": FAILURE, "error_surface": surface})])
    state.begin(prompt)  # what the retry key does
    events = [(at + 3.0, kind, payload) for at, kind, payload in timeline()]
    events.append(
        (
            17.5,
            "background.complete",
            {"task_id": "ci-log", "text": "Fetched the last CI log: 1 failure, 26 passed."},
        )
    )
    events.sort(key=lambda event: event[0])
    step(events[-1][0] - 3.0 + 2.0, events)


def main(folder: Path) -> None:
    record(folder / "startup.jsonl", startup)
    record(folder / "session.jsonl", session)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
