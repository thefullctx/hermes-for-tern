"""Search and export only displayable conversation data, never transport or secret drafts."""

from __future__ import annotations

import re

from .state import Conversation, Row


def searchable(row: Row) -> str:
    parts = [row.text, row.name, row.target, row.diff, *(image.name for image in row.images)]
    parts += [str(item.get("content") or "") for item in row.items]
    parts += [f"{agent.goal} {agent.summary}" for agent in row.agents]
    return "\n".join(parts)


def matches(state: Conversation) -> list[Row]:
    query = state.search_query.casefold()
    return [row for row in state.rows if query and query in searchable(row).casefold()]


def fenced(text: str, language: str = "text") -> str:
    # Tool output and Markdown may themselves contain fences.
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{text}\n{fence}"


def markdown(state: Conversation) -> str:
    lines = [
        "# Hermes conversation",
        "",
        f"Session: {state.stored_session_id or 'not yet stored'}",
        "",
        "Export of the conversation loaded in this pane, including turns outside its visible window. "
        "Cleared or unloaded history is unavailable. Tool output is included in full when available; "
        "the UI's display limit does not trim this export. Resumed tool output may be unavailable. "
        "Secret answers and connection diagnostics are excluded.",
        "",
    ]
    for row in state.rows:
        if row.kind in ("user", "assistant"):
            lines += [f"## {'You' if row.kind == 'user' else 'Hermes'}", "", row.text, ""]
            for image in row.images:
                lines += [
                    f"Image: {image.name} ({image.media}, {image.width} × {image.height}, {image.size} bytes). Image bytes are not embedded in this Markdown export.",
                    "",
                ]
        elif row.kind == "tool":
            lines += [f"### Tool: {row.name} — {row.status}", ""]
            if row.target:
                lines += [fenced(row.target), ""]
            lines += [fenced(row.text) if row.text else "Output unavailable or empty.", ""]
            if row.diff:
                lines += [fenced(row.diff, "diff"), ""]
            for agent in row.agents:
                lines += [f"Subagent: {agent.goal} — {agent.status}", "", agent.summary, ""]
        elif row.kind == "errands":
            lines += [
                f"- [{'x' if item.get('status') == 'completed' else ' '}] {item.get('content', '')}"
                for item in row.items
            ] + [""]
        elif row.kind == "thought":
            lines += ["### Reasoning", "", row.text, ""]
        elif row.kind == "delivered" or (row.kind == "error" and row.name == "turn"):
            lines += [row.text, ""]
    return "\n".join(lines)
