"""Native semantic components, using Tern's theme and layout."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from tern_sdk import ui

from .editor import Draft
from .state import Conversation, Question, Row


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


def transcript_row(row: Row, state: Conversation):
    if row.kind == "tool":
        body = (
            ui.diff(row.diff, key="diff") if row.diff else ui.code(row.text[:48000], wrap=True, key="output")
        )
        if row.status == "running":
            body = (
                ui.text("Waiting for your answer…", tone="muted", key="output")
                if state.questions
                else ui.col(
                    ui.row(
                        ui.shimmer("Working", mode="kitt", key="label"),
                        ui.elapsed(max(0, (time.time() - row.started) * 1000), key="elapsed"),
                        gap="sm",
                    ),
                    ui.progress(key="scan", role="tool-scan"),
                    key="output",
                    gap="sm",
                )
            )
        return ui.tool(
            body,
            name=row.name.replace("_", " "),
            title=row.name.replace("_", " ").capitalize(),
            target=row.target or None,
            target_kind="command" if row.name == "terminal" else "text",
            status=row.status,
            exit=row.exit_code,
            took=row.duration,
            collapsible=True,
            collapsed=row.collapsed,
            preview={"lines": 5},
            key=row.key,
            role="tool",
            on_toggle=lambda e: fold(row, state, e.collapsed),
        )
    if row.kind == "user":
        return ui.card(
            ui.text(row.text, key="body"), head="You", tone="user", role="message-user", key=row.key
        )
    if row.kind == "assistant":
        live = state.busy and row is state._assistant
        return ui.card(
            ui.md(row.text, stream=live, key="body"),
            ui.html.div(class_="hft-stream-glimmer", key="glimmer") if live else None,
            head=[ui.span("Hermes", "accent", fx="shimmer" if live else None)],
            variant="bare",
            role="message-assistant",
            key=row.key,
        )
    return ui.card(
        ui.text(row.text, key="body"),
        head="Something went wrong" if row.kind == "error" else None,
        tone="error" if row.kind == "error" else "muted",
        variant="bare",
        role="error" if row.kind == "error" else "notice",
        key=row.key,
    )


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
        head=[ui.span("Permission needed", "warning")],
        tone="warning",
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
        ui.text("Choose an option or type your answer below.", tone="muted", key="hint"),
        head=f"Question {question.index + 1} of {len(question.params.get('questions', []))}",
        tone="accent",
        role="clarification",
        key=question.rid,
    )


def brand_image(assets: dict[str, str], name: str, size: int, *, key: str, role: str | None = None):
    if name in assets:
        return ui.image(
            assets[name],
            w=size,
            h=size,
            alt="Hermes" if name == "hermes" else "Hermes wing",
            key=key,
            role=role,
            on_click=lambda _: None,
        )
    return ui.icon("sparkle", tone="accent", key=key, role=role)


def activity_indicator(assets: dict[str, str], label: str, *, moving: bool):
    return ui.html.div(
        ui.html.div(
            ui.html.div(class_="hft-orbit-ring", key="ring") if moving else None,
            brand_image(assets, "wing", 20, key="wing"),
            class_="hft-orbit" if moving else "hft-still-mark",
            key="mark",
        ),
        ui.html.span(label, class_="hft-activity-label", key="label"),
        class_="hft-activity-content",
        key="indicator",
    )


def live_work(state: Conversation):
    waiting = bool(state.questions)
    return ui.html.div(
        ui.row(
            ui.badge("SIMULATED DEMO", tone="warning", key="demo") if state.info.get("demo") else None,
            ui.text("Waiting for you", key="label")
            if waiting
            else ui.shimmer("Hermes is working", mode="kitt", key="label"),
            ui.elapsed(
                max(0, (time.monotonic() - (state.turn_started or time.monotonic())) * 1000), key="clock"
            ),
            gap="sm",
            key="head",
        ),
        ui.html.div(
            *(ui.html.div(class_=f"hft-wave-bar hft-wave-{i}", key=f"b{i}") for i in range(7)),
            class_="hft-wave",
            attrs={"aria-hidden": True},
            key="wave",
        )
        if not waiting
        else None,
        ui.chart(
            "spark",
            size="sm",
            token="accent",
            series=[{"value": n} for n in state.activity_samples],
            aria="Response activity: characters received per half-second",
            role="response-activity",
            key="graph",
        ),
        ui.html.div(
            ui.html.span("Response activity", key="label"),
            ui.html.span(f"{state.stream_chars:,} characters received", key="count"),
            class_="hft-live-meta",
            key="meta",
        ),
        ui.progress(state.demo_progress, label="Simulated demo progress", key="progress")
        if state.info.get("demo")
        else ui.progress(key="progress"),
        class_="hft-live-panel hft-waiting" if waiting else "hft-live-panel hft-live-active",
        role="live-work",
        key="live-work",
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
    rows = [transcript_row(row, state) for row in state.rows if row.text or row.kind == "tool"]
    welcome = not rows
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
    for question in state.questions.values():
        if question.method == "approval":
            rows.append(approval_card(question, approve))
    clarification = state.clarify()
    if clarification:
        rows.append(clarification_card(clarification, choose, skip))
    if state.busy:
        rows.append(live_work(state))
    active_draft = answer_draft if clarification else draft
    dock = []
    if state.busy or not state.ready:
        activity = {"Thinking": "Working", "Writing": "Writing response", "terminal": "Running terminal"}.get(
            state.activity, state.activity.replace("_", " ")
        )
        label = "Waiting for your answer" if state.questions else activity
        dock.append(
            ui.row(
                ui.text(state.activity, tone="error", key="activity")
                if state.failed
                else activity_indicator(assets, label, moving=not bool(state.questions)),
                gap="sm",
                role="activity",
                key="working",
            )
        )
    dock.append(
        ui.editor(
            active_draft.text,
            cursor=active_draft.caret,
            key="composer",
            role="composer",
            placeholder="Your answer…"
            if clarification
            else "Draft your next message…"
            if state.busy
            else "What would you like to work on?",
            readonly=state.failed,
            sendable=state.ready and not state.busy,
            max_lines=8,
            prompt=[ui.span("› ", "accent")],
        )
    )
    can_send = (
        bool(active_draft.text.strip())
        and state.ready
        and not state.failed
        and (not state.busy or bool(clarification))
    )
    dock.append(
        ui.html.div(
            ui.html.span("Enter send · Shift+Enter newline · Ctrl+C stop", class_="hft-hints", key="hints"),
            button("Stop", stop, key="action")
            if state.busy and not clarification
            else button(
                "Send answer" if clarification else "Send ↑",
                submit or (lambda: None),
                primary=True,
                disabled=not can_send,
                key="action",
            ),
            role="composer-footer",
            key="footer",
        )
    )
    usage = state.usage.get("total") or 0
    dock.append(
        ui.status(
            ui.seg("Hermes", icon="sparkle", tone="accent", key="brand"),
            ui.seg(model, icon="brain", key="model"),
            ui.seg(cwd.name, icon="folder", key="project"),
            ui.seg(f"{usage:,} tokens", side="right", key="usage")
            if isinstance(usage, int) and usage
            else None,
            ui.seg("Ctrl+D exit", side="right", key="keys"),
            role="status",
            key="status",
        )
    )
    return {
        "main": ui.col() if welcome else ui.col(ui.col(*rows, gap="lg", role="canvas", key="canvas")),
        "layer": ui.col(ui.html.div(*rows, class_="hft-welcome-stage", role="canvas", key="stage"))
        if welcome
        else None,
        "dock": ui.col(*dock, gap="sm"),
    }
