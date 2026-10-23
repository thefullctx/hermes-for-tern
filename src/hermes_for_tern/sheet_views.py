"""Model and session picker rows, filtering and sheet components."""

from __future__ import annotations

import time
from typing import Callable

from tern_sdk import ui

from .editor import utf16_len
from .state import Conversation


def model_rows(state: Conversation) -> list[dict]:
    """Every model Hermes offers, as picker rows. A row's id is the value a switch sends: the bare
    name when only one provider carries it (Hermes resolves the provider itself), otherwise
    `provider/model`. Providers without credentials keep their rows, disabled with the reason."""
    catalog = state.models or {}
    offered = [
        (
            str(provider.get("slug") or ""),
            str(provider.get("name") or provider.get("slug") or ""),
            model,
            provider,
        )
        for provider in catalog.get("providers") or []
        if isinstance(provider, dict)
        for model in provider.get("models") or []
    ]
    names = [model for _, _, model, _ in offered]
    rows = []
    for slug, provider_name, model, provider in offered:
        row = {
            "id": model if names.count(model) == 1 else f"{slug}/{model}",
            "label": model,
            "detail": provider_name,
            "mono": True,
        }
        pricing = (provider.get("pricing") or {}).get(model) or {}
        facts = {
            key: str(value)
            for key, value in (("in", pricing.get("input")), ("out", pricing.get("output")))
            if value
        }
        if facts:
            row["facts"] = facts
        if provider.get("authenticated") is False:
            row["disabled"] = f"set {provider.get('key_env') or 'a key'}"
        rows.append(row)
    return rows


def by_provider(rows: list[dict]) -> list:
    """The sheet's order: each provider's models under a header naming it, in Hermes's order."""
    providers: dict[str, list[str]] = {}
    for row in rows:
        providers.setdefault(str(row["detail"]), []).append(row["id"])
    order: list = []
    for name, ids in providers.items():
        order.append({"group": name, "label": name, "count": len(ids)})
        order.extend(ids)
    return order


def ago(started: float) -> str:
    """How long ago, the way a list of sessions says it: `just now`, `5 min`, `3 h`, `2 d`."""
    seconds = max(0, time.time() - started)
    for size, unit in ((86400, "d"), (3600, "h"), (60, "min")):
        if seconds >= size:
            return f"{int(seconds // size)} {unit} ago"
    return "just now"


def session_label(row: dict) -> str:
    """A session by its title, else by how it began."""
    return (
        str(row.get("title") or "").strip()
        or " ".join(str(row.get("preview") or "").split())[:72]
        or row["id"]
    )


def session_rows(state: Conversation) -> list[dict]:
    """Hermes's recent sessions as sheet rows, newest first; a row's id is what a resume sends."""
    rows = []
    for row in state.sessions or []:
        count = int(row.get("message_count") or 0)
        detail = f"{count} message{'s' * (count != 1)}"
        if row.get("started_at"):
            detail += f" · {ago(float(row['started_at']))}"
        if row["id"] == state.stored_session_id:
            detail += " · this session"
        rows.append({"id": row["id"], "label": session_label(row), "detail": detail})
    return rows


def sheet_rows(state: Conversation) -> list[dict]:
    """The rows the sheet is showing: the catalog or the sessions, filtered by what was typed."""
    rows = session_rows(state) if state.sheet and state.sheet.kind == "sessions" else model_rows(state)
    query = (state.sheet.query if state.sheet else "").strip().lower()
    if not query:
        return rows
    return [row for row in rows if query in f"{row['label']} {row['detail']}".lower()]


def sheet_cursor(state: Conversation) -> str:
    """The row the cursor sits on — what was chosen, else the session's own model while it is in
    view, else the top row the sheet is showing. The sheet draws it and the switch key acts on it,
    so both read this one answer."""
    rows = sheet_rows(state)
    if not rows:
        return ""
    ids = [row["id"] for row in rows]
    if state.sheet and state.sheet.kind == "sessions":
        # The newest session other than this one: the likeliest to pick up.
        here = state.sheet.chosen or next((i for i in ids if i != state.stored_session_id), "")
        return here if here in ids else ids[0]
    here = (state.sheet.chosen if state.sheet else "") or current_model(state)
    return here if here in ids else ids[0]


def current_model(state: Conversation) -> str:
    """The row of the model this session is running, when the catalog lists it."""
    model = str(state.info.get("model") or "")
    if not model:
        return ""
    provider = str(state.info.get("provider") or "")
    wanted = {model, f"{provider}/{model}"}
    rows = model_rows(state)
    for row in rows:
        if row["id"] in wanted:
            return row["id"]
    # A name only one provider carries is its own row; Hermes resolves a bare name itself.
    named = [row for row in rows if row["label"] == model]
    return named[0]["id"] if len(named) == 1 else ""


def model_sheet(state: Conversation, events: Callable | None):
    """The model sheet over the dock: Hermes's catalog, filtered as you type, with the switch and
    cancel keys in its own bar. It owns the keyboard while it is open (see App.sheet_key)."""
    sheet = state.sheet
    if sheet is None or sheet.kind != "model":
        return None
    rows = sheet_rows(state)
    chosen = sheet_cursor(state)
    return ui.picker(
        ui.col(
            ui.text(
                [
                    ui.span("provider  ", "dim"),
                    ui.span(next((row["detail"] for row in rows if row["id"] == chosen), "")),
                ],
                key="detail",
            ),
            ui.text(
                [ui.span("session   ", "dim"), ui.span(str(state.info.get("model") or "unknown"))],
                key="current",
            ),
            gap="sm",
            key="preview",
        ),
        title="Model",
        subtitle=f"{len(rows)} of {len(model_rows(state))} matches"
        if sheet.query.strip()
        else (f"{len(rows)} models" if rows else "asking Hermes…"),
        # The query is drawn as the sheet's search field; typing reaches it through App.sheet_key.
        query=sheet.query,
        cursor=utf16_len(sheet.query),
        items=rows,
        order=by_provider(rows),
        current=[chosen] if chosen else None,
        selected=current_model(state) or None,
        total=len(rows),
        empty="no model matches" if state.models else "asking Hermes…",
        placeholder="type to filter",
        noun="model",
        size="md",
        preview="side",
        state="ready",
        actions=[
            {"id": "pick", "label": "Switch model", "keys": ["enter"], "primary": True},
            {"id": "close", "label": "Cancel", "keys": ["escape"], "end": True},
        ],
        confirm={
            "text": "Hermes asks whether this model is worth it.",
            "act": "pick",
            "label": "Switch anyway",
        }
        if sheet.confirming
        else None,
        on_select=(lambda event: events("pick", event.item)) if events else None,
        on_action={"pick": (lambda _: events("confirm")), "close": (lambda _: events("cancel"))}
        if events
        else None,
        role="model-sheet",
        key="sheet",
    )


def sessions_sheet(state: Conversation, events: Callable | None):
    """The sessions sheet `/resume` opens over the dock: Hermes's recent sessions, newest first,
    filtered as you type; Enter picks the highlighted one up in this pane (see App.sheet_key)."""
    sheet = state.sheet
    if sheet is None or sheet.kind != "sessions":
        return None
    rows = sheet_rows(state)
    chosen = sheet_cursor(state)
    total = len(session_rows(state))
    return ui.picker(
        title="Resume a session",
        query=sheet.query,
        cursor=utf16_len(sheet.query),
        subtitle=f"{len(rows)} of {total} match"
        if sheet.query.strip()
        else (f"{total} recent sessions" if state.sessions is not None else "asking Hermes…"),
        items=rows,
        order=[row["id"] for row in rows],
        current=[chosen] if chosen else None,
        selected=state.stored_session_id or None,
        total=len(rows),
        empty="no session matches" if state.sessions else "asking Hermes…",
        placeholder="type to filter",
        noun="session",
        size="md",
        preview="none",
        state="ready",
        actions=[
            {"id": "pick", "label": "Resume", "keys": ["enter"], "primary": True},
            {"id": "close", "label": "Cancel", "keys": ["escape"], "end": True},
        ],
        on_select=(lambda event: events("pick", event.item)) if events else None,
        on_action={"pick": (lambda _: events("confirm")), "close": (lambda _: events("cancel"))}
        if events
        else None,
        role="sessions-sheet",
        key="sessions",
    )
