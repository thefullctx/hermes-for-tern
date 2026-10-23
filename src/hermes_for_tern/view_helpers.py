"""Shared button component for the native surface."""

from __future__ import annotations

from typing import Callable

from tern_sdk import ui


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
