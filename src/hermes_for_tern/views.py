"""Native semantic components, using Tern's theme and layout."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from tern_sdk import ui

from .editor import Draft
from .state import Agent, Conversation, Question, Row

# Markup would split a mark across rendered elements, so such runs are left unmarked.
MARKUP = set("*_`[]()#<>|~\\\n")
# Seconds of work after which a delivered turn is celebrated, quietly.
LONG_TURN = 60


def button(
    label: str, fn: Callable, *, primary: bool = False, disabled: bool = False, key: str | None = None
):
    return ui.html.button(
        label,
        class_="hft-button" + (" hft-primary" if primary else "") + (" hft-disabled" if disabled else ""),
        on_click=(lambda _: fn()) if not disabled else None,
        attrs={"aria-disabled": disabled},
        key=key,
    )


def trail(row: Row, now: float) -> list:
    """Fresh text as marks in gold ink that dries: drying, gold, then newest and faint.
    Tern marks every match of a run, so only unique runs are marked."""
    marks = []
    for text, token in zip(row.fresh(now), ("muted", "accent", "dim")):
        text = text.strip()
        if text and not MARKUP & set(text) and row.visible.count(text) == 1:
            marks.append(ui.span(text, token))
    return marks


def subagent(agent: Agent, children: dict[str | None, list[Agent]]):
    """One delegated child as Tern's agent row; its own children nest under it."""
    now = time.time()
    running = agent.status == "running"
    age = (agent.duration if agent.duration is not None else now - agent.started) * 1000
    stats = {"tools": agent.tools, "age": max(0, age)}
    if agent.tokens:
        stats["tokens"] = agent.tokens
    summary = agent.summary.strip().splitlines()[0][:200] if agent.summary.strip() else ""
    return ui.agent(
        ui.text(summary, tone="muted", role="subagent-summary", key="summary")
        if summary and not running
        else None,
        *(subagent(child, children) for child in children.get(agent.id, [])),
        name=f"#{agent.index + 1}",
        task=agent.goal[:200],
        status=agent.status,
        model=agent.model or None,
        stats=stats,
        tool={
            "name": agent.tool,
            "intent": agent.tool_preview[:120],
            "age": (now - agent.tool_started) * 1000,
        }
        if running and agent.tool
        else None,
        role="subagent",
        key=agent.id,
    )


def delegation(row: Row):
    ids = {agent.id for agent in row.agents}
    children: dict[str | None, list[Agent]] = {}
    for agent in row.agents:
        children.setdefault(agent.parent if agent.parent in ids else None, []).append(agent)
    return ui.col(
        *(subagent(agent, children) for agent in children.get(None, [])), role="agents", key="agents"
    )


# Errand glyphs by Hermes's todo status.
ERRAND = {"completed": "✓", "in_progress": "◆", "pending": "·", "cancelled": "–"}
GLYPH_TONE = {"completed": "success", "in_progress": "accent", "pending": "dim", "cancelled": "dim"}


def took(seconds: float) -> str:
    return f"{max(1, round(seconds))}s" if seconds < 60 else f"{int(seconds // 60)}m {int(seconds % 60):02d}s"


def progress(items: list[dict]) -> tuple[int, int, str]:
    """Done and total errands (cancelled ones drop out) and the one under way."""
    live = [i for i in items if i.get("status") != "cancelled"]
    done = sum(i.get("status") == "completed" for i in live)
    current = next((i for i in live if i.get("status") == "in_progress"), None) or next(
        (i for i in live if i.get("status") == "pending"), None
    )
    return done, len(live), str(current.get("content", "")) if current else ""


def thought(row: Row, state: Conversation):
    """Reasoning in faint gold ink; when the answer starts it fades, then folds to one line."""
    live = row.ended is None
    folding = row.folding(time.monotonic())
    if live:
        head = [ui.span("◇ ", "accent"), ui.span("pondering…", fx="shimmer")]
    else:
        head = [ui.span("◇ ", "accent"), ui.span(f"pondered for {took(row.duration or 0)}", "muted")]
    return ui.section(
        ui.md(row.visible, stream=live, key="body"),
        head=head,
        collapsible=not live,
        collapsed=False if live or folding else row.collapsed,
        role="thought-live" if live else "thought-folding" if folding else "thought",
        key=row.key,
        on_toggle=lambda e: fold(row, state, e.collapsed),
    )


def errand_list(row: Row):
    done, total, _ = progress(row.items)
    lines = [
        ui.text(
            [
                ui.span(
                    ERRAND.get(str(item.get("status")), "·") + " ", GLYPH_TONE.get(str(item.get("status")))
                ),
                ui.span(
                    str(item.get("content", "")),
                    fx="shimmer" if item.get("status") == "in_progress" else None,
                ),
            ],
            role=f"errand-{item.get('status', 'pending')}",
            key=f"e{index}",
        )
        for index, item in enumerate(row.items)
    ]
    return ui.tool(
        ui.col(*lines, role="errands", key="items"),
        name="errands",
        title="Errands",
        target=f"{done}/{total}",
        status="done" if total and done == total else "running",
        collapsible=False,
        key=row.key,
        role="tool",
    )


def transcript_row(row: Row, state: Conversation):
    if row.kind == "thought":
        return thought(row, state)
    if row.kind == "errands":
        return errand_list(row)
    if row.kind == "tool":
        body = (
            ui.diff(row.diff, key="diff") if row.diff else ui.code(row.text[:48000], wrap=True, key="output")
        )
        running = row.status == "running"
        if row.agents:
            body = delegation(row)
        elif running:
            # The head carries the live timer; the body speaks only when Hermes waits on you.
            body = ui.text("waiting for your answer", tone="muted", key="output") if state.questions else None
        return ui.tool(
            body,
            name=row.name.replace("_", " "),
            title=row.name.replace("_", " ").capitalize(),
            target=row.target
            or (f"{len(row.agents)} subagent{'s' * (len(row.agents) != 1)}" if row.agents else None),
            target_kind="command" if row.name == "terminal" else "text",
            status=row.status,
            exit=row.exit_code,
            age=max(0, (time.time() - row.started) * 1000) if running else None,
            took=row.duration,
            collapsible=True,
            collapsed=row.collapsed,
            preview=None if row.agents else {"lines": 5},
            key=row.key,
            role="tool",
            on_toggle=lambda e: fold(row, state, e.collapsed),
        )
    if row.kind == "user":
        return ui.card(ui.text(row.text, key="body"), variant="bare", role="message-user", key=row.key)
    if row.kind == "assistant":
        live = (state.busy and row is state._assistant) or row.pending
        return ui.card(
            ui.md(row.visible, stream=live, marks=trail(row, time.monotonic()) or None, key="body"),
            variant="bare",
            role="message-assistant",
            key=row.key,
        )
    if row.kind == "error":
        return ui.card(
            ui.text(row.text, key="body"),
            head="Something went wrong",
            variant="bare",
            role="error",
            key=row.key,
        )
    if row.kind == "delivered":
        # A long turn earns a few sparks; the stylesheet keys them on the tone.
        long = (row.duration or 0) >= LONG_TURN
        return ui.text(row.text, role="delivered", tone="accent" if long else None, key=row.key)
    return ui.text(row.text, role="notice", key=row.key)


def fold(row: Row, state: Conversation, collapsed: bool | None):
    row.collapsed = bool(collapsed)
    state.touch()


def approval_card(question: Question, answer: Callable):
    p = question.params
    choices = p.get("choices") or ["deny"]
    labels = {
        "once": "Allow once",
        "session": "Allow for this session",
        "always": "Always allow",
        "deny": "Deny",
    }
    return ui.card(
        ui.text(p.get("description") or "Hermes needs your permission to continue.", key="description"),
        ui.code(p.get("command") or p.get("tool_name") or "Tool request", wrap=True, key="command"),
        ui.html.div(
            *(
                button(
                    labels.get(c, c),
                    lambda choice=c: answer(question.rid, choice),
                    primary=c == "once",
                    key=c,
                )
                for c in choices
            ),
            class_="hft-actions",
            key="buttons",
        ),
        head="Permission needed",
        variant="bare",
        role="approval",
        key=question.rid,
    )


def clarification_card(question: Question, choose: Callable, skip: Callable):
    current = question.current
    controls = []
    for index, choice in enumerate(current.get("choices") or []):
        selected = choice in question.selected
        label = ("✓ " if selected else "") + choice
        controls.append(
            button(label, lambda c=choice: choose(question.rid, c), primary=selected, key=f"c{index}")
        )
    if current.get("multi_select"):
        controls.append(button("Continue", lambda: choose(question.rid, None), primary=True, key="continue"))
    controls.append(button("Skip question", lambda: skip(question.rid), key="skip"))
    return ui.card(
        ui.md(str(current.get("question", "")), key="question"),
        ui.html.div(*controls, class_="hft-actions", key="buttons"),
        ui.text("Choose an option, or type your answer below.", tone="muted", key="hint"),
        head=f"Question {question.index + 1} of {len(question.params.get('questions', []))}",
        variant="bare",
        role="clarification",
        key=question.rid,
    )


def brand_image(assets: dict[str, str], name: str, size: int, *, key: str, role: str | None = None):
    if name in assets:
        return ui.image(
            assets[name],
            w=size,
            h=size,
            alt="Hermes",
            key=key,
            role=role,
            on_click=lambda _: None,
        )
    return ui.icon("sparkle", tone="accent", key=key, role=role)


def errand_pill(todos: list[dict]):
    """The turn's errands as a pill: a gold ring that fills as they are done."""
    done, total, current = progress(todos)
    if not total:
        return None
    finished = done == total
    return ui.row(
        ui.meter(done / total, style="ring", size="sm", key="ring"),
        ui.text([ui.span("errands ", "muted"), ui.span(f"{done}/{total}")], key="count"),
        ui.text("all delivered" if finished else current[:60], key="now"),
        gap="sm",
        role="errands-done" if finished else "errands",
        key="errands",
    )


def working(state: Conversation, label: str):
    """One console line: a spinner, what Hermes is doing, how long the turn has run, its errands."""
    waiting = bool(state.questions)
    started = state.turn_started or time.monotonic()
    return ui.row(
        ui.text("◆", key="mark", role="working-still")
        if waiting
        else ui.spinner(style="braille", key="mark"),
        ui.text(label, key="label") if waiting else ui.shimmer(label, key="label"),
        ui.elapsed(max(0, (time.monotonic() - started) * 1000), key="clock"),
        errand_pill(state.todos) if state.busy else None,
        gap="sm",
        role="activity",
        key="working",
    )


def view(
    state: Conversation,
    draft: Draft,
    answer_draft: Draft,
    cwd: Path,
    stop: Callable,
    approve: Callable,
    choose: Callable,
    skip: Callable,
    suggest: Callable,
    submit: Callable | None = None,
    assets: dict[str, str] | None = None,
) -> dict:
    assets = assets or {}
    model = str(state.info.get("model") or "Connecting…")
    # Each turn is its own column, so a gold thread can run down its gutter.
    turns: list[tuple[str, list]] = []
    for row in state.rows:
        if row.visible or row.kind in ("tool", "errands"):
            if row.kind == "user" or not turns:
                turns.append((row.key, []))
            turns[-1][1].append(transcript_row(row, state))
    welcome = not turns
    rows: list = []
    if welcome:
        rows = [
            ui.col(
                ui.row(
                    brand_image(assets, "hermes", 84, key="logo", role="welcome-logo"),
                    ui.col(
                        ui.html.div("HERMES FOR TERN", class_="hft-eyebrow", key="eyebrow"),
                        ui.html.h1("Hermes", class_="hft-wordmark", key="wordmark"),
                        gap="sm",
                        key="name",
                    ),
                    role="welcome-brand",
                    key="brand",
                ),
                ui.html.p("Your agent, at home in Tern.", class_="hft-tagline", key="intro"),
                ui.html.div(
                    ui.html.span(ui.html.span("PROJECT", class_="hft-context-label"), cwd.name),
                    ui.html.span(ui.html.span("MODEL", class_="hft-context-label"), model),
                    class_="hft-context",
                    key="context",
                ),
                ui.html.div(
                    button(
                        "Explore this project",
                        lambda: suggest(
                            "Inspect this project and explain its structure. Do not modify files."
                        ),
                        key="explore",
                    ),
                    button("Help me plan", lambda: suggest("Help me plan "), key="plan"),
                    button(
                        "Review my code",
                        lambda: suggest(
                            "Review this project's code for bugs and explain your findings. Do not modify files."
                        ),
                        key="review",
                    ),
                    class_="hft-actions",
                    key="suggestions",
                ),
                gap="lg",
                role="welcome",
                key="welcome",
            )
        ]
    asks = [approval_card(q, approve) for q in state.questions.values() if q.method == "approval"]
    clarification = state.clarify()
    if clarification:
        asks.append(clarification_card(clarification, choose, skip))
    (rows if welcome else turns[-1][1]).extend(asks)
    active_draft = answer_draft if clarification else draft
    dock = []
    if state.failed:
        dock.append(ui.text(state.activity, tone="error", role="activity", key="working"))
    elif state.busy or not state.ready:
        activity = {"Thinking": "Working", "Writing": "Writing", "terminal": "Running terminal"}.get(
            state.activity, state.activity.replace("_", " ")
        )
        dock.append(working(state, "Waiting for your answer" if state.questions else activity))
    fuel = state.usage.get("context_percent")
    fuel = fuel if isinstance(fuel, (int, float)) and not isinstance(fuel, bool) else None
    if fuel is not None:
        # Context use as a gold hairline on the composer's top edge; amber, then red, as it fills.
        dock.append(
            ui.meter(min(1.0, fuel / 100), thresholds={"warn": 0.8, "bad": 0.95}, role="fuel", key="fuel")
        )
    dock.append(
        ui.editor(
            active_draft.text,
            cursor=active_draft.caret,
            key="composer",
            role="composer",
            tone="pending" if state.busy else None,
            placeholder="Your answer…"
            if clarification
            else "Draft your next message…"
            if state.busy
            else "What would you like to work on?",
            readonly=state.failed,
            sendable=state.ready and not state.busy,
            max_lines=8,
            prompt=[ui.span("❯ ", "accent")],
        )
    )
    can_send = (
        bool(active_draft.text.strip())
        and state.ready
        and not state.failed
        and (not state.busy or bool(clarification))
    )
    if state.busy and not clarification:
        action = ui.seg("esc stop", side="right", role="stop", key="action", on_click=lambda _: stop())
    else:
        send = submit or (lambda: None)
        action = ui.seg(
            "⏎ answer" if clarification else "⏎ send",
            side="right",
            role="send" if can_send else "send-off",
            key="action",
            on_click=(lambda _: send()) if can_send else None,
        )
    usage = state.usage.get("total") or 0
    dock.append(
        ui.status(
            ui.seg("hermes", role="brand", key="brand"),
            ui.seg("SIMULATED DEMO", role="demo", key="demo") if state.info.get("demo") else None,
            ui.seg(model, icon="brain", key="model"),
            ui.seg(cwd.name, icon="folder", key="project"),
            ui.seg(f"{usage:,} tokens", side="right", key="usage")
            if isinstance(usage, int) and usage
            else None,
            ui.seg(f"{fuel:.0f}% context", side="right", role="fuel-seg", key="fuel")
            if fuel is not None
            else None,
            ui.seg("⇧⏎ newline", side="right", key="newline"),
            action,
            role="bar",
            key="bar",
        )
    )
    return {
        "main": ui.col()
        if welcome
        else ui.col(
            ui.col(
                *(
                    ui.col(
                        *nodes,
                        role="turn",
                        tone="pending" if state.busy and index == len(turns) - 1 else None,
                        key=key,
                    )
                    for index, (key, nodes) in enumerate(turns)
                ),
                role="canvas",
                key="canvas",
            )
        ),
        "layer": ui.col(ui.html.div(*rows, class_="hft-welcome-stage", role="stage", key="stage"))
        if welcome
        else None,
        "dock": ui.col(*dock, gap="none"),
    }
