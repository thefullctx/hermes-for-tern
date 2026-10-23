"""Slash command catalog, completion rows and composer command list."""

from __future__ import annotations

import re
from typing import Callable

from tern_sdk import ui

from .editor import Draft
from .state import Conversation

# The commands this pane answers itself, because they draw or change something in Tern, with what
# each does. Every other `/command` is Hermes's own and goes to it. One list: the composer's command
# list and `/help` both read it, so they cannot drift apart.
COMMAND_ROWS = (
    ("/model", "switch the model over Hermes's catalog; /model <name> skips the sheet"),
    ("/btw <question>", "ask beside the running turn"),
    ("/bg <task>", "hand Hermes a task on a fresh agent"),
    ("/attach [path]", "attach a PNG or JPEG image to this draft"),
    ("/paste-image", "attach a clipboard image to this draft"),
    ("/name <title>", "name this session"),
    ("/resume [session]", "pick up an earlier session, by its id or title or from the list"),
    ("/cost", "what this session has spent"),
    ("/doctor", "check launcher integrity, backend startup and compatibility"),
    ("/reconnect", "reconnect and resume without resending work"),
    ("/recover", "restore saved text for this project and session"),
    ("/queue-continue", "continue paused messages"),
    ("/queue-discard", "discard queued messages"),
    ("/find <text>", "search the loaded conversation, including older turns"),
    ("/find-next", "show the next transcript match"),
    ("/find-previous", "show the previous transcript match"),
    ("/find-close", "return to the newest turns"),
    ("/export [path]", "save loaded conversation as Markdown; never overwrite"),
    ("/clear", "empty the transcript; Hermes still remembers"),
    ("/stop", "stop the running turn"),
    ("/help", "Hermes's commands, and these"),
    ("/quit", "leave Hermes for Tern"),
)
# Other spellings of this pane's commands: Hermes's `/title` is `/name` here.
ALIASES = {"/exit": "/quit", "/title": "/name", "/sessions": "/resume"}
# Hermes commands the list leaves out: they would swap the session out from under this pane, or they
# drive Hermes's own terminal display, which Tern draws instead.
HIDDEN = frozenset(
    {
        "/new", "/reset", "/session", "/switch", "/branch", "/fork", "/rename",
        "/redraw", "/skin", "/theme", "/theme-info", "/statusbar", "/sb", "/footer", "/indicator",
        "/battery", "/timestamps", "/density", "/details", "/detail", "/mouse", "/scroll", "/palette",
        "/copy", "/paste", "/image", "/prompt", "/compose", "/voice", "/terminal-setup", "/busy",
        "/verbose", "/pet", "/hatch", "/fortune", "/heapdump", "/mem", "/widgets-reload",
    }
)  # fmt: skip
# Hermes describes an alias as "(alias for /name)"; the list offers each command once.
ALIAS_OF = re.compile(r"\(alias for /")
# Rows the command list shows at once; the rest scroll into view under the arrow keys.
SLASH_ROWS = 8


def slash_typed(text: str) -> tuple[str, bool] | None:
    """The draft as a command being typed: its `/name`, and whether its argument has begun. None when
    it is prose: no leading slash, a second slash (`/usr/local`), or more than one line."""
    if not text.startswith("/") or "\n" in text:
        return None
    name, space, _ = text.partition(" ")
    if "/" in name[1:]:
        return None
    return name.lower(), bool(space)


def local_rows() -> list[dict]:
    return [
        {
            "name": name.split(" ")[0],
            "label": name,
            "detail": detail,
            "insert": name.split(" ")[0],
            "local": True,
        }
        for name, detail in COMMAND_ROWS
    ]


def slash_rows(state: Conversation, text: str) -> list[dict]:
    """The command list for the draft `text`: this pane's own commands, then Hermes's commands and
    skills (`complete.slash`), or, once an argument has begun, Hermes's suggestions for it. Each row
    knows what replaces the draft from where. Until Hermes answers this very draft, its last answer
    stands, narrowed to what is still typed, so the list never blinks between keystrokes."""
    typed = slash_typed(text)
    if typed is None:
        return []
    name, arguing = typed
    slash = state.slash
    fresh = slash.answered == text
    if arguing:
        if name in {row["name"] for row in local_rows()} or ALIASES.get(name):
            return []  # this pane's commands take free text
        start = slash.replace_from if 0 < slash.replace_from <= len(text) else len(text)
        fragment = text[start:].lower()
        rows = []
        for item in slash.items:
            insert = str(item.get("text") or "")
            if not insert or (not fresh and fragment not in insert.lower()):
                continue
            rows.append(
                {
                    "name": str(item.get("display") or insert).strip(),
                    "detail": usage_free(str(item.get("meta") or "")),
                    "insert": insert,
                    "from": start,
                    "skill": item.get("kind") == "skill",
                }
            )
        return rows
    fragment = name[1:]
    # Names outrank descriptions, as in Hermes's own ranking: this pane's commands named like the
    # draft, then Hermes's, then this pane's that only mention it.
    rows = [row for row in local_rows() if fragment in row["name"][1:]]
    mentions = [
        row
        for row in local_rows()
        if fragment and fragment not in row["name"][1:] and fragment in row["detail"].lower()
    ]
    taken = {row["name"] for row in local_rows()} | set(ALIASES) | HIDDEN
    for item in slash.items if not slash.answered.partition(" ")[1] else ():
        insert = str(item.get("text") or "").strip()
        shown = "/" + insert
        if not insert or shown.lower() in taken or (not fresh and fragment not in insert.lower()):
            continue
        if ALIAS_OF.search(str(item.get("meta") or "")):
            continue  # one row per command: typing an alias still works, Hermes resolves it
        taken.add(shown.lower())
        rows.append(
            {
                "name": shown,
                "detail": usage_free(str(item.get("meta") or "")),
                "insert": shown,
                "skill": item.get("kind") == "skill",
            }
        )
    return rows + mentions


def usage_free(meta: str) -> str:
    """Hermes's description of a command, without the `(usage: …)` tail the composer cannot show."""
    return re.sub(r"\s*\(usage:.*\)\s*$", "", meta).strip()


def slash_window(rows: list, cursor: int) -> tuple[int, int]:
    """The SLASH_ROWS rows to show, keeping the highlighted one in view."""
    if len(rows) <= SLASH_ROWS:
        return 0, len(rows)
    top = min(max(0, cursor - SLASH_ROWS + 1), len(rows) - SLASH_ROWS)
    top = min(top, cursor)
    return top, top + SLASH_ROWS


def slash_open(state: Conversation, draft: Draft) -> bool:
    """Whether the command list shows: the draft is a command being typed, it has rows, and Escape
    has not put the list away for this very draft."""
    return state.slash.put_away != draft.text and bool(slash_rows(state, draft.text))


def slash_list(state: Conversation, draft: Draft, pick: Callable | None):
    """The composer's live command list, drawn right above it: one line per command with what it
    does, the highlighted one marked. It grows and shrinks with every keystroke (see App.slash_key)."""
    if not slash_open(state, draft):
        return None
    rows = slash_rows(state, draft.text)
    cursor = min(state.slash.cursor, len(rows) - 1)
    top, bottom = slash_window(rows, cursor)
    lines = []
    for index in range(top, bottom):
        row = rows[index]
        current = index == cursor
        lines.append(
            ui.row(
                ui.text(row["name"], key="name", role="slash-name"),
                ui.text("skill", key="kind", role="slash-kind") if row.get("skill") else None,
                ui.text(row["detail"], key="detail", role="slash-detail") if row["detail"] else None,
                role="slash-current" if current else "slash-row",
                key=f"row-{row['name']}",
                on_click=(lambda _, at=index: pick(at)) if pick else None,
            )
        )
    more = len(rows) - bottom
    if top or more:
        shown = f"{top + 1}–{bottom} of {len(rows)}"
        lines.append(ui.text(f"{shown} · ↑↓ to see more", role="slash-more", key="more"))
    return ui.col(*lines, gap="none", role="slash-list", key="slash")


def slash_ghost(state: Conversation, draft: Draft) -> str | None:
    """The rest of the highlighted command, shown faintly after the caret while its name is typed."""
    if not slash_open(state, draft) or draft.cursor != len(draft.text):
        return None
    typed = slash_typed(draft.text)
    if typed is None or typed[1]:
        return None
    rows = slash_rows(state, draft.text)
    name = rows[min(state.slash.cursor, len(rows) - 1)]["name"]
    if name.lower().startswith(typed[0]) and len(name) > len(typed[0]):
        return name[len(typed[0]) :]
    return None
