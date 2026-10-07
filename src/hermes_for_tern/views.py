"""Native semantic components, using Tern's theme and layout."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable

from tern_sdk import ui

from .editor import Draft
from .state import Agent, Conversation, Dispatch, Question, Row

# Markup would split a mark across rendered elements, so such runs are left unmarked.
MARKUP = set("*_`[]()#<>|~\\\n")
# Seconds of work after which a delivered turn is celebrated, quietly.
LONG_TURN = 60
# Tool output past this is clipped for the pane; the row says so rather than lying by omission.
CLIP = 48000


def button(
    label: str,
    fn: Callable,
    *,
    primary: bool = False,
    disabled: bool = False,
    key: str | None = None,
    hint: str | None = None,
):
    """A key is a button; `hint` is the digit that answers it without the mouse."""
    children = [ui.html.span(label, key="label")]
    if hint:
        children.insert(0, ui.html.span(hint, class_="hft-hint", key="hint"))
    return ui.html.button(
        *children,
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


def hits(text: str, pattern: re.Pattern | None) -> list:
    """A line with each match as a mark, which the stylesheet underlines in gold ink."""
    if pattern is None:
        return [ui.span(text)]
    spans, at = [], 0
    for found in pattern.finditer(text):
        if found.end() > found.start():
            spans += [ui.span(text[at : found.start()]), ui.span(found.group(), "mark")]
            at = found.end()
    return spans + [ui.span(text[at:])] if spans else [ui.span(text)]


def search_tree(row: Row):
    """search_files results as tree rows: each file once, then its matching lines, matches inked."""
    result = row.result or {}
    try:
        pattern = re.compile(str(row.args.get("pattern", ""))) if row.args.get("target") != "files" else None
    except re.error:
        pattern = None
    files: dict[str, list[tuple[str, str]]] = {}
    for match in result.get("matches") or []:
        files.setdefault(str(match.get("path", "")), []).append(
            (str(match.get("line", "")), str(match.get("content", "")))
        )
    path = ""
    for line in str(result.get("matches_text") or "").splitlines():
        if line.startswith("  ") and ":" in line:
            number, _, content = line.strip().partition(": ")
            files.setdefault(path, []).append((number, content))
        elif line.strip():
            path = line.strip()
            files.setdefault(path, [])
    for name in result.get("files") or []:
        files.setdefault(str(name), [])
    counts = result.get("counts") or {}
    rows, names = [], list(files) or list(counts)
    for index, name in enumerate(names):
        last = index == len(names) - 1
        count = counts.get(name) or len(files.get(name, [])) or None
        rows.append(
            ui.text(
                [ui.span("└─ " if last else "├─ ", "dim"), ui.span(name, "path")]
                + ([ui.span(f"  {count}", "dim")] if count else []),
                role="search-file",
                key=f"f{index}",
            )
        )
        for hit, (number, content) in enumerate(files.get(name, [])[:20]):
            rows.append(
                ui.text(
                    [ui.span(("   " if last else "│  ") + f"{number:>4}  ", "dim")]
                    + hits(content.rstrip(), pattern),
                    role="search-hit",
                    key=f"f{index}h{hit}",
                )
            )
    return ui.col(*rows, role="search", key="output") if rows else None


def undelivered(row: Row, state: Conversation, retry: Callable | None):
    """A failed turn: what failed, the provider's code when Hermes gives one, and a retry key."""
    surface = row.result or {}
    code = str(surface.get("code") or "")
    status = re.search(r"\b([45]\d\d)\b", row.text)
    badge = status.group(1) if status else code.replace("_", " ")
    latest = state.can_retry and row is next((r for r in reversed(state.rows) if r.kind == "error"), None)
    return ui.card(
        ui.text(row.text.strip(), key="body"),
        ui.html.div(button("retry ⏎", retry, primary=True, key="retry"), class_="hft-actions", key="actions")
        if latest and retry
        else None,
        head=[ui.span("Undelivered"), ui.span(f"  {badge}", "dim")] if badge else "Undelivered",
        variant="bare",
        role="undelivered",
        key=row.key,
    )


def effort_notice(level: str, key: str):
    """The pressed effort, floating like a dispatch but with a lamp rather than a seal."""
    return ui.html.div(
        ui.icon("lightbulb", tone="accent", key="mark"),
        ui.html.span("thinking effort", class_="hft-dispatch-title", key="title"),
        ui.html.span(level, class_="hft-dispatch-text", key="level"),
        class_="hft-dispatch hft-effort",
        key=key,
    )


def dispatches(notes: list[Dispatch], effort: tuple[str, str] | None = None):
    """Notes above the composer: a ☤, what happened, and a quiet detail; they rise, then fade.
    `effort` is (level, key) for the lamp that shows the pressed thinking effort."""
    now = time.monotonic()
    return ui.html.div(
        *((effort_notice(*effort),) if effort else ()),
        *(
            ui.html.div(
                ui.html.span("☤", class_="hft-dispatch-mark", key="mark"),
                ui.html.span("Dispatch", class_="hft-dispatch-title", key="title"),
                ui.html.span(note.text, class_="hft-dispatch-text", key="text"),
                ui.html.span(note.sub, class_="hft-dispatch-sub", key="sub") if note.sub else None,
                class_=f"hft-dispatch tone-{note.tone}" + (" leaving" if note.leaving(now) else ""),
                key=note.key,
            )
            for note in notes
        ),
        class_="hft-dispatches",
        role="dispatches",
        key="dispatches",
    )


def clipped_output(row: Row):
    """A tool's output, with a line saying how much was left out when there is any."""
    code = ui.code(row.text[:CLIP], wrap=True, key="output")
    if len(row.text) <= CLIP:
        return code
    return ui.col(
        code,
        ui.text(
            f"└─ {len(row.text) - CLIP:,} more characters not shown",
            tone="muted",
            role="clipped",
            key="clip",
        ),
        key="clipped",
    )


def transcript_row(
    row: Row, state: Conversation, retry: Callable | None = None, assets: dict[str, str] | None = None
):
    if row.kind == "thought":
        return thought(row, state)
    if row.kind == "errands":
        return errand_list(row)
    if row.kind == "tool":
        body = ui.diff(row.diff, key="diff") if row.diff else clipped_output(row)
        running = row.status == "running"
        if row.agents:
            body = delegation(row)
        elif row.name == "search_files" and row.result and not running:
            body = search_tree(row) or body
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
    if row.kind == "error" and row.name == "turn":
        return undelivered(row, state, retry)
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
        note = ui.text(row.text, role="delivered", tone="accent" if long else None, key=row.key)
        # Hermes signs the delivered turn: its H writes itself in as the glint passes, and its ink
        # dries to faint once the next turn begins.
        latest = not state.busy and row is next(
            (r for r in reversed(state.rows) if r.kind == "delivered"), None
        )
        signed = signature(assets, "signed" if latest else "dry", 12, role="signature")
        if not signed:
            return note
        return ui.row(note, signed, role="delivered-line", key=f"{row.key}-signed")
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
                    hint=f"{index + 1}",
                )
                for index, c in enumerate(choices)
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
            button(
                label,
                lambda c=choice: choose(question.rid, c),
                primary=selected,
                key=f"c{index}",
                hint=f"{index + 1}",
            )
        )
    if current.get("multi_select"):
        controls.append(button("Continue", lambda: choose(question.rid, None), primary=True, key="continue"))
    controls.append(button("Skip question", lambda: skip(question.rid), key="skip"))
    return ui.card(
        ui.md(str(current.get("question", "")), key="question"),
        ui.html.div(*controls, class_="hft-actions", key="buttons"),
        ui.text("Choose with a number key, or type your answer below.", tone="muted", key="hint"),
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


def signature(assets: dict[str, str] | None, use: str, height: int, *, role: str, key: str = "signature"):
    """Hermes's H in the wordmark's pen, in its copy for `use` (see design.MONOGRAM_USES); none
    without images."""
    if not assets or f"monogram-{use}" not in assets:
        return None
    return ui.image(
        assets[f"monogram-{use}"],
        w=round(height * 0.75),
        h=height,
        alt="",
        key=key,
        role=role,
        on_click=lambda _: None,
    )


def wordmark(assets: dict[str, str]):
    """HERMES, written in stroke by stroke over its own faint ghost; plain text without images."""
    if "wordmark" in assets:
        return ui.image(
            assets["wordmark"], w=163, h=34, alt="Hermes", key="wordmark", role="welcome-wordmark"
        )
    return ui.html.h1("Hermes", class_="hft-wordmark", key="wordmark")


def errand_pill(todos: list[dict], assets: dict[str, str] | None = None):
    """The turn's errands as a pill: a gold ring that fills as they are done, signed once all are."""
    done, total, current = progress(todos)
    if not total:
        return None
    finished = done == total
    return ui.row(
        ui.meter(done / total, style="ring", size="sm", key="ring"),
        ui.text([ui.span("errands ", "muted"), ui.span(f"{done}/{total}")], key="count"),
        ui.text("all delivered" if finished else current[:60], key="now"),
        signature(assets, "errands", 11, role="errands-signature") if finished else None,
        gap="sm",
        role="errands-done" if finished else "errands",
        key="errands",
    )


def working(state: Conversation, label: str, assets: dict[str, str] | None = None):
    """One console line: a spinner, what Hermes is doing, how long the turn has run, its errands.
    While Hermes muses in silence the spinner gives way to the pen, writing its H over and over."""
    waiting = bool(state.questions)
    started = state.turn_started or time.monotonic()
    done, total, _ = progress(state.todos)
    # one write-on at a time: the errands' signature outranks the working mark
    pen = signature(assets, "pen", 13, role="working-pen", key="pen")
    if waiting:
        mark = ui.text("◆", key="mark", role="working-still")
    elif pen and state.musing and not (total and done == total):
        mark = pen
    else:
        mark = ui.spinner(style="braille", key="mark")
    return ui.row(
        mark,
        ui.text(label, key="label") if waiting else ui.shimmer(label, key="label"),
        ui.elapsed(max(0, (time.monotonic() - started) * 1000), key="clock"),
        errand_pill(state.todos, assets) if state.busy else None,
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
    retry: Callable | None = None,
    effort: Callable | None = None,
) -> dict:
    assets = assets or {}
    model = str(state.info.get("model") or "Connecting…")
    # Each turn is its own column, so a gold thread can run down its gutter.
    turns: list[tuple[str, list]] = []
    for row in state.rows:
        if row.visible or row.kind in ("tool", "errands"):
            if row.kind == "user" or not turns:
                turns.append((row.key, []))
            turns[-1][1].append(transcript_row(row, state, retry, assets))
    welcome = not turns
    rows: list = []
    if welcome:
        rows = [
            ui.col(
                ui.row(
                    brand_image(assets, "hermes", 84, key="logo", role="welcome-logo"),
                    ui.col(
                        ui.html.div("HERMES FOR TERN", class_="hft-eyebrow", key="eyebrow"),
                        wordmark(assets),
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
        dock.append(working(state, "Waiting for your answer" if state.questions else activity, assets))
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
            # Not readonly on failure: /doctor and /quit must still be typeable.
            readonly=False,
            sendable=state.ready and (not state.busy or bool(clarification)),
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
        queueable = bool(active_draft.text.strip()) and state.ready and not state.failed
    else:
        queueable = False
        send = submit or (lambda: None)
        action = ui.seg(
            "⏎ answer" if clarification else "⏎ send",
            side="right",
            role="send" if can_send else "send-off",
            key="action",
            on_click=(lambda _: send()) if can_send else None,
        )
    if queueable:
        dock.insert(
            0,
            ui.text(
                [ui.span("⏎ holds this for when the turn ends", "muted")],
                role="queued-hint",
                key="queued-hint",
            ),
        )
    for index, held in enumerate(state.queued):
        dock.insert(
            index,
            ui.row(
                ui.text(
                    [ui.span("queued · ", "accent"), ui.span(held[:80], "muted")],
                    role="queued",
                    key="queued",
                ),
                ui.text(f"{index + 1}/{len(state.queued)}", tone="muted", role="depth", key="depth"),
                role="queued-line",
                key=f"queued-{index}",
            ),
        )
    usage = state.usage.get("total") or 0
    dock.append(
        ui.status(
            ui.seg("hermes", role="brand", key="brand"),
            ui.seg("SIMULATED DEMO", role="demo", key="demo") if state.info.get("demo") else None,
            ui.seg(model, icon="brain", key="model"),
            # The ring fills as thinking gets deeper; press it to step on.
            ui.effort(
                state.effort,
                role="effort",
                key="effort",
                on_click=(lambda _: effort()) if effort else None,
                title=f"thinking effort: {state.effort}",
            ),
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
        "layer": ui.col(
            ui.html.div(*rows, class_="hft-welcome-stage", role="stage", key="stage") if welcome else None,
            dispatches(
                state.dispatches,
                (state.effort, f"effort-{state.effort_presses}")
                if state.effort_notice_live(time.monotonic())
                else None,
            )
            if state.dispatches or state.effort_notice_live(time.monotonic())
            else None,
        ),
        "dock": ui.col(*dock, gap="none"),
    }
