"""Conversation rows, attachments, activity and pending-input cards."""

from __future__ import annotations

import re
import time
from typing import Callable

from tern_sdk import ui

from .editor import utf16_len
from .messages import ImageRef
from .state import Agent, Conversation, Dispatch, Question, Row
from .view_helpers import button

# Keys that pick a question's choice, in the order the card shows them.
DIGITS = "123456789"
# Markup would split a mark across rendered elements, so such runs are left unmarked.
MARKUP = set("*_`[]()#<>|~\\\n")
# Seconds of work after which a delivered turn is celebrated, quietly.
LONG_TURN = 60
# Tool output past this is clipped for the pane; the row says so rather than lying by omission.
CLIP = 48000


def trail(row: Row, now: float) -> list:
    """Fresh text as marks in gold ink that dries: drying, gold, then newest and faint.
    Tern marks every match of a run, so only unique runs are marked."""
    marks = []
    for text, token in zip(row.fresh(now), ("muted", "accent", "dim")):
        text = text.strip()
        if text and not MARKUP & set(text) and row.visible.count(text) == 1:
            marks.append(ui.span(text, token))
    return marks


def subagent(agent: Agent, children: dict[str | None, list[Agent]], watch: Callable | None = None):
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
        *(subagent(child, children, watch) for child in children.get(agent.id, [])),
        on_click=(lambda _: watch(agent.id)) if watch else None,
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


def delegation(row: Row, watch: Callable | None = None):
    ids = {agent.id for agent in row.agents}
    children: dict[str | None, list[Agent]] = {}
    for agent in row.agents:
        children.setdefault(agent.parent if agent.parent in ids else None, []).append(agent)
    return ui.col(
        *(subagent(agent, children, watch) for agent in children.get(None, [])),
        role="agents",
        key="agents",
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


def surface_hint(surface: dict) -> str:
    """What Hermes's error surface says will fix it, said before a retry that cannot work: the
    credential to repair, or when the limit lifts."""
    hints = []
    provider = str(surface.get("provider_label") or surface.get("provider") or "")
    env = str(surface.get("api_key_env") or "")
    if surface.get("auth_kind") == "oauth":
        hints.append(f"sign in to {provider} again" if provider else "sign in again")
    elif env:
        hints.append(f"set {env}")
    elif surface.get("auth_kind") == "api_key":
        hints.append(f"fix {provider}'s credentials" if provider else "fix the credentials")
    resets_at = surface.get("resets_at")
    if isinstance(resets_at, (int, float)) and not isinstance(resets_at, bool):
        left = resets_at - time.time()
        if left > 1:
            hints.append(f"the limit lifts in {took(left)}")
    return " · ".join(hints)


def undelivered(row: Row, state: Conversation, retry: Callable | None):
    """A failed turn: what failed, what Hermes's error surface says will fix it, the provider's code
    when Hermes gives one, and a retry key only when Hermes marked the failure retryable."""
    surface = row.result or {}
    code = str(surface.get("code") or "")
    status = re.search(r"\b([45]\d\d)\b", row.text)
    badge = status.group(1) if status else code.replace("_", " ")
    latest = state.can_retry and row is next((r for r in reversed(state.rows) if r.kind == "error"), None)
    hint = surface_hint(surface)
    return ui.card(
        ui.text(row.text.strip(), key="body"),
        ui.text(hint, tone="muted", role="cause", key="cause") if hint else None,
        ui.html.div(button("retry ⏎", retry, primary=True, key="retry"), class_="hft-actions", key="actions")
        if latest and retry
        else None,
        head=[ui.span("Undelivered"), ui.span(f"  {badge}", "dim")] if badge else "Undelivered",
        variant="bare",
        role="undelivered",
        key=row.key,
    )


def hermes_wire(state: Conversation) -> str | None:
    """What Hermes says it put on the wire, when that differs from what the ring asked for."""
    wire = state.info.get("reasoning_effort_wire")
    return wire if isinstance(wire, str) and wire else None


def effort_notice(level: str, key: str, wire: str | None = None):
    """The pressed effort, floating like a dispatch but with a lamp rather than a seal. When
    Hermes clamps the level to something this route actually honours, say so."""
    clamped = wire and wire not in (level, "" if level == "off" else None)
    return ui.html.div(
        ui.icon("lightbulb", tone="accent", key="mark"),
        ui.html.span("thinking effort", class_="hft-dispatch-title", key="title"),
        ui.html.span(level, class_="hft-dispatch-text", key="level"),
        ui.html.span(f"sends {wire} here", class_="hft-dispatch-sub", key="wire") if clamped else None,
        class_="hft-dispatch hft-effort",
        key=key,
    )


def dispatches(notes: list[Dispatch], effort: tuple[str, str, str | None] | None = None):
    """Notes above the composer: a ☤, what happened, and a quiet detail; they rise, then fade.
    `effort` is (level, key, wire) for the lamp showing the pressed thinking effort."""
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


def attachment_strip(images: tuple[ImageRef, ...], assets: dict, action: Callable | None = None):
    if not images:
        return None
    return ui.row(
        *(
            ui.col(
                ui.image(assets["attachment:" + image.id], alt=image.name, w=96, h=72, key="preview")
                if assets.get("attachment:" + image.id)
                else ui.text("Preview unavailable", tone="muted", key="preview"),
                ui.text(image.name[:40], key="name"),
                ui.text(
                    f"{image.width} × {image.height} · {image.size / 1024:.1f} KiB", tone="muted", key="size"
                ),
                button("Remove", lambda i=index: action(f"image-remove:{i}"), key="remove")
                if action
                else None,
                key=f"image-{index}",
                role="attachment",
                gap="sm",
            )
            for index, image in enumerate(images)
        ),
        key="attachments",
        role="attachments",
        wrap=True,
        gap="sm",
    )


def transcript_row(
    row: Row,
    state: Conversation,
    retry: Callable | None = None,
    assets: dict[str, str] | None = None,
    watch: Callable | None = None,
):
    if row.kind == "thought":
        return thought(row, state)
    if row.kind == "errands":
        return errand_list(row)
    if row.kind == "tool":
        # No output, no well: a tool that printed nothing, or one redrawn from a resumed session.
        body = ui.diff(row.diff, key="diff") if row.diff else clipped_output(row) if row.text else None
        running = row.status == "running"
        if row.agents:
            body = delegation(row, watch)
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
    if row.kind == "command":
        # A slash command's answer from Hermes, drawn like a finished tool: its name, then its output
        # folded to a few lines that open in place.
        return ui.tool(
            ui.text(row.text, key="output", role="command-output"),
            name="command",
            title=row.name,
            status="done",
            collapsible=True,
            collapsed=row.collapsed,
            preview={"lines": 5},
            key=row.key,
            role="tool",
            on_toggle=lambda e: fold(row, state, e.collapsed),
        )
    if row.kind == "user":
        return ui.card(
            ui.text(row.text, key="body"),
            attachment_strip(row.images, assets or {}),
            variant="bare",
            role="message-user",
            key=row.key,
        )
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
                # A digit only answers while one key can reach it; the rest stay clickable.
                hint=f"{index + 1}" if index < len(DIGITS) else None,
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


def mask(text: str) -> str:
    """A secret as the pane sees it: one dot per UTF-16 unit, so the caret still lines up."""
    return "•" * utf16_len(text)


def secret_card(question: Question):
    """Hermes is waiting for one typed answer — a password, a key, a code. The answer is typed in
    the composer, masked; Enter sends it, Escape skips, which is Hermes's own word for declining."""
    p = question.params
    method = question.method
    if method == "sudo":
        title, body = "Hermes needs your password", p.get("command") or "A command needs elevated rights."
    elif method == "secret":
        title = f"Hermes needs {p.get('env_var') or 'a value'}"
        body = p.get("prompt") or "A tool asked for a value."
    elif method == "vault.unlock_prompt":
        title = f"Unlock {p.get('display_name') or p.get('backend') or 'the vault'}"
        body = "Hermes needs the master password to reach your saved credentials."
    else:  # vault.code
        title = "Hermes needs a one-time code"
        body = p.get("hint") or p.get("site") or "Read the code from your device."
    return ui.card(
        ui.code(str(body), wrap=True, key="command"),
        ui.text("Type it in the composer; Enter sends it, Escape skips.", tone="muted", key="hint"),
        head=[ui.span(title), ui.span("  masked", "dim")],
        variant="bare",
        role="secret",
        key=question.rid,
    )


def subagent_sheet(state: Conversation, events: Callable | None):
    """A delegated child, watched: its live transcript, refreshed while it runs, with interrupt
    and steer. It owns the keyboard while it is open (see App.watch_key)."""
    watch = state.watch
    if watch is None:
        return None
    agent = state.agents.get(watch.id)
    running = agent.status == "running" if agent else False
    facts = []
    if agent:
        facts += [
            ui.text([ui.span("goal   ", "dim"), ui.span(agent.goal[:120] or "—")], key="goal"),
            ui.text([ui.span("model  ", "dim"), ui.span(agent.model or "—")], key="model"),
            ui.text(
                [ui.span("tool   ", "dim"), ui.span(agent.tool or "—", fx="shimmer" if running else None)],
                key="tool",
            ),
            ui.text(
                [
                    ui.span("counts ", "dim"),
                    ui.span(f"{agent.tools} tools · {agent.tokens or 0} tokens"),
                ],
                key="counts",
            ),
        ]
    body = (
        ui.code(watch.tail, wrap=True, key="tail")
        if watch.available
        else ui.text("This child has no transcript yet.", tone="muted", key="empty")
    )
    return ui.card(
        ui.col(*facts, gap="sm", key="facts"),
        body,
        ui.text("└─ earlier output left out" if watch.truncated else None, tone="muted", key="truncated"),
        ui.text([ui.span("steer › ", "accent"), ui.span(watch.steer)], key="steer"),
        ui.html.div(
            button("interrupt", lambda: events("interrupt"), key="interrupt"),
            button("steer ⏎", lambda: events("steer"), primary=True, key="steer"),
            button("close", lambda: events("close"), key="close"),
            class_="hft-actions",
            key="actions",
        )
        if events
        else None,
        head=[
            ui.span("Watching"),
            ui.span(f"  #{agent.index + 1}" if agent else f"  {watch.id}", "dim"),
            ui.span(f"  {watch.id}", "dim"),
        ],
        variant="bare",
        role="watch",
        key="watch",
    )


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
