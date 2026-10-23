"""Compose the main surface from transcript, command and sheet components."""

from __future__ import annotations

import time
import re as re
from pathlib import Path
from typing import Callable

from tern_sdk import ui

# Preserve existing component imports while their implementations live in focused modules.
from .view_helpers import button as button
from .slash_views import (
    COMMAND_ROWS as COMMAND_ROWS,
    ALIASES as ALIASES,
    HIDDEN as HIDDEN,
    ALIAS_OF as ALIAS_OF,
    SLASH_ROWS as SLASH_ROWS,
    slash_typed as slash_typed,
    local_rows as local_rows,
    slash_rows as slash_rows,
    usage_free as usage_free,
    slash_window as slash_window,
    slash_open as slash_open,
    slash_list as slash_list,
    slash_ghost as slash_ghost,
)
from .sheet_views import (
    model_rows as model_rows,
    by_provider as by_provider,
    ago as ago,
    session_label as session_label,
    session_rows as session_rows,
    sheet_rows as sheet_rows,
    sheet_cursor as sheet_cursor,
    current_model as current_model,
    model_sheet as model_sheet,
    sessions_sheet as sessions_sheet,
)
from .transcript_views import (
    DIGITS as DIGITS,
    MARKUP as MARKUP,
    LONG_TURN as LONG_TURN,
    CLIP as CLIP,
    trail as trail,
    subagent as subagent,
    delegation as delegation,
    ERRAND as ERRAND,
    GLYPH_TONE as GLYPH_TONE,
    took as took,
    progress as progress,
    thought as thought,
    errand_list as errand_list,
    hits as hits,
    search_tree as search_tree,
    surface_hint as surface_hint,
    undelivered as undelivered,
    hermes_wire as hermes_wire,
    effort_notice as effort_notice,
    dispatches as dispatches,
    clipped_output as clipped_output,
    attachment_strip as attachment_strip,
    transcript_row as transcript_row,
    fold as fold,
    approval_card as approval_card,
    clarification_card as clarification_card,
    brand_image as brand_image,
    mask as mask,
    secret_card as secret_card,
    subagent_sheet as subagent_sheet,
    signature as signature,
)
from .editor import Draft, utf16_len as utf16_len
from .messages import ImageRef as ImageRef
from .transcript import matches, searchable
from .state import (
    SECRET_METHODS,
    Conversation,
    Row,
    Agent as Agent,
    Dispatch as Dispatch,
    Question as Question,
)

# Tern diffs the whole view every frame, so a session that runs for hours is capped: the newest
# turns are drawn and the rest are counted in one line, never silently dropped.
MAX_TURNS = 60


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
    model: Callable | None = None,
    secret: Draft | None = None,
    watch: Callable | None = None,
    unqueue: Callable | None = None,
    commands: Callable | None = None,
    resume: Callable | None = None,
    action: Callable | None = None,
    image_path: Draft | None = None,
) -> dict:
    assets = assets or {}
    action = action or (lambda _: None)
    model_name = str(state.info.get("model") or "Connecting…")
    # Each turn is its own column, so a gold thread can run down its gutter.
    turns: list[tuple[str, list[Row]]] = []
    for row in state.rows:
        if row.visible or row.kind in ("tool", "errands"):
            if row.kind == "user" or not turns:
                turns.append((row.key, []))
            turns[-1][1].append(row)
    found = matches(state)
    selected = found[state.search_index % len(found)] if found else None
    start = max(0, len(turns) - MAX_TURNS)
    if selected:
        at = next((i for i, (_, rows) in enumerate(turns) if selected in rows), 0)
        start = max(0, min(at - MAX_TURNS // 2, len(turns) - MAX_TURNS))
    hidden = start
    later = max(0, len(turns) - start - MAX_TURNS)
    turns = turns[start : start + MAX_TURNS]
    turns = [(key, [transcript_row(row, state, retry, assets, watch) for row in rows]) for key, rows in turns]
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
                    ui.html.span(ui.html.span("MODEL", class_="hft-context-label"), model_name),
                    ui.html.span(
                        ui.html.span("SESSION", class_="hft-context-label"), state.title or "unnamed"
                    ),
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
                    # Picks the last conversation back up, when there is one to pick up.
                    button(
                        f"Continue “{session_label(state.earlier)[:32]}”",
                        lambda: resume(state.earlier["id"]),
                        primary=True,
                        key="continue",
                    )
                    if state.earlier and resume
                    else None,
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
    asks += [secret_card(q) for q in state.questions.values() if q.method in SECRET_METHODS]
    (rows if welcome else turns[-1][1]).extend(asks)
    secret_ask = state.secret()
    active_draft = (secret or Draft()) if secret_ask else (answer_draft if clarification else draft)
    dock = []
    if state.recovery_available:
        dock.append(
            ui.row(
                ui.text(f"Saved text for {state.recovery_label}", tone="muted", key="label"),
                button("Restore saved text", lambda: action("restore"), key="restore"),
                button("Discard saved text", lambda: action("forget-saved"), key="forget"),
                key="saved-text",
                role="recovery",
                wrap=True,
                gap="sm",
            )
        )
    if state.failed:
        dock.append(
            ui.row(
                button("Reconnect and resume", lambda: action("reconnect"), primary=True, key="reconnect"),
                button("Start new session", lambda: action("new-session"), key="new"),
                key="reconnect",
                role="recovery",
                wrap=True,
                gap="sm",
            )
        )
    if state.switching:
        dock.append(
            ui.text("Opening session · submitted messages are held for it", key="switching", role="activity")
        )
    if state.queue_paused and state.queued:
        dock.append(
            ui.row(
                ui.text("Queue paused", tone="muted", key="label"),
                button("Continue queue", lambda: action("continue-queue"), key="continue"),
                button("Discard queue", lambda: action("discard-queue"), key="discard"),
                key="queue-actions",
                role="queue-actions",
                wrap=True,
                gap="sm",
            )
        )
    if state.search_query:
        index = state.search_index % len(found) + 1 if found else 0
        dock.append(
            ui.col(
                ui.text(f"Find “{state.search_query}” · {index}/{len(found)} matches", key="query"),
                ui.text(
                    searchable(selected)[:240] if selected else "No matches in the loaded conversation.",
                    tone="muted",
                    key="preview",
                ),
                ui.row(
                    button("Previous", lambda: action("find-previous"), key="previous"),
                    button("Next", lambda: action("find-next"), key="next"),
                    button("Close search", lambda: action("find-close"), key="close"),
                    key="actions",
                    wrap=True,
                    gap="sm",
                ),
                key="search",
                role="transcript-search",
            )
        )
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
    if not secret_ask and not clarification:
        if state.held_message:
            held = state.held_message
            dock.append(
                ui.col(
                    ui.text(
                        "Previous send uncertain · review before resending"
                        if state.held_uncertain
                        else "Message not sent · retained for review",
                        key="title",
                    ),
                    ui.text(held.label[:160], tone="muted", key="text"),
                    attachment_strip(held.images, assets),
                    ui.row(
                        button("Restore message", lambda: action("image-held-restore"), key="restore"),
                        button("Discard message", lambda: action("image-held-discard"), key="discard"),
                        wrap=True,
                        gap="sm",
                        key="actions",
                    ),
                    key="held-message",
                    role="image-controls",
                    gap="sm",
                )
            )
        image_controls = []
        if state.image_loading:
            image_controls.append(ui.text("Importing image…", tone="muted", key="status"))
        if state.draft_images:
            image_controls.append(attachment_strip(state.draft_images, assets, action))
        if image_controls:
            dock.append(ui.col(*image_controls, role="image-controls", key="image-controls", gap="sm"))
        if image_path is not None:
            dock.append(
                ui.col(
                    ui.text("Image file path", key="label"),
                    ui.editor(
                        image_path.text,
                        cursor=image_path.caret,
                        placeholder="/path/to/image.png",
                        sendable=not state.image_loading,
                        readonly=state.image_loading,
                        key="attach-path",
                        role="attachment-path",
                    ),
                    ui.row(
                        button(
                            "Attach",
                            lambda: action("image-path-submit"),
                            disabled=state.image_loading,
                            key="attach",
                        ),
                        button("Cancel", lambda: action("image-path-close"), key="cancel"),
                        wrap=True,
                        gap="sm",
                        key="actions",
                    ),
                    role="image-controls",
                    key="image-path",
                    gap="sm",
                )
            )
        dock.append(slash_list(state, draft, commands))
    if state.exit_armed is not None:
        dock.append(
            ui.text([ui.span("Press Ctrl+C again to exit", "muted")], role="exit-hint", key="exit-hint")
        )
    dock.append(
        ui.editor(
            mask(active_draft.text) if secret_ask else active_draft.text,
            cursor=active_draft.caret,
            ghost=None if secret_ask or clarification else slash_ghost(state, draft),
            key="composer",
            role="composer",
            tone="pending" if state.busy else None,
            placeholder={
                "sudo": "Password…",
                "secret": "Value…",
                "vault.unlock_prompt": "Master password…",
                "vault.code": "Code…",
            }.get(secret_ask.method if secret_ask else "", "Your answer…")
            if secret_ask or clarification
            else "Draft your next message…"
            if state.busy
            else "What would you like to work on?",
            # Not readonly on failure: /doctor and /quit must still be typeable.
            readonly=False,
            sendable=state.ready
            and not state.image_loading
            and not state.failed
            and not state.switching
            and (not state.busy or bool(clarification)),
            max_lines=8,
            prompt=[ui.span("❯ ", "accent")],
        )
    )
    can_send = (
        (
            bool(active_draft.text.strip())
            or (bool(state.draft_images) and not secret_ask and not clarification)
        )
        and not state.image_loading
        and state.ready
        and not state.failed
        and (not state.held_message or bool(secret_ask) or bool(clarification))
        and not state.switching
        and (not state.busy or bool(clarification))
    )
    if state.busy and not clarification:
        send_action = ui.seg(
            "esc stop", side="right", role="stop", key="action", priority=100, on_click=lambda _: stop()
        )
        queueable = (
            (bool(active_draft.text.strip()) or bool(state.draft_images))
            and not state.image_loading
            and state.ready
            and not state.failed
        )
    else:
        queueable = False
        send = submit or (lambda: None)
        send_action = ui.seg(
            "⏎ answer" if clarification else "⏎ send",
            side="right",
            role="send" if can_send else "send-off",
            key="action",
            priority=100,
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
                    [
                        ui.span("queued · ", "accent"),
                        ui.span(
                            held.label[:80]
                            + (f" · {len(held.images)} image(s)" if held.images and held.text else ""),
                            "muted",
                        ),
                    ],
                    role="queued",
                    key="queued",
                ),
                ui.text(f"{index + 1}/{len(state.queued)}", tone="muted", role="depth", key="depth"),
                button("✕", lambda i=index: unqueue(i), key="drop"),
                role="queued-line",
                key=f"queued-{index}",
            ),
        )
    usage = state.usage.get("total") or 0
    dock.append(
        ui.status(
            ui.seg("hermes", role="brand", key="brand", priority=3),
            ui.seg("SIMULATED DEMO", role="demo", key="demo") if state.info.get("demo") else None,
            ui.seg(state.title, key="title", priority=2) if state.title else None,
            ui.seg(
                model_name,
                icon="brain",
                key="model",
                priority=6,
                on_click=(lambda _: model("open")) if model else None,
            ),
            # The ring fills as thinking gets deeper; press it to step on.
            ui.effort(
                state.effort,
                role="effort",
                key="effort",
                on_click=(lambda _: effort()) if effort else None,
                title=f"thinking effort: {state.effort}",
            ),
            ui.seg(cwd.name, icon="folder", key="project", priority=5),
            ui.seg(f"{usage:,} tokens", side="right", key="usage", priority=2)
            if isinstance(usage, int) and usage
            else None,
            ui.seg(f"{fuel:.0f}% context", side="right", role="fuel-seg", key="fuel", priority=3)
            if fuel is not None
            else None,
            ui.seg("⇧⏎ newline", side="right", key="newline", priority=1),
            # the send or stop key is the one thing the strip may never give up
            send_action,
            role="bar",
            key="bar",
        )
    )
    return {
        "main": ui.col()
        if welcome
        else ui.col(
            ui.col(
                ui.text(
                    f"… {hidden} earlier turns · /clear empties the transcript",
                    role="earlier",
                    key="earlier",
                )
                if hidden
                else None,
                ui.text(f"… {later} later turns · /find-close returns to latest", key="later", role="earlier")
                if later
                else None,
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
            ),
        ),
        "layer": ui.col(
            # The welcome fades while the command list is open, which would sit over it; only its
            # class changes, so its entrance does not play again when the list closes.
            ui.html.div(
                *rows,
                class_="hft-welcome-stage"
                + (
                    " hft-tucked"
                    if slash_open(state, draft) or state.draft_images or image_path is not None
                    else ""
                ),
                role="stage",
                key="stage",
            )
            if welcome
            else None,
            model_sheet(state, model),
            sessions_sheet(state, model),
            subagent_sheet(state, watch),
            dispatches(
                state.dispatches,
                (
                    state.effort,
                    f"effort-{state.effort_presses}",
                    hermes_wire(state),
                )
                if state.effort_notice_live(time.monotonic())
                else None,
            )
            if state.dispatches or state.effort_notice_live(time.monotonic())
            else None,
        ),
        "dock": ui.col(*dock, gap="none"),
    }
