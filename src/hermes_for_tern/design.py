"""Packaged visual assets, independent of Hermes and its configuration."""

from importlib.resources import files


ASSETS = files("hermes_for_tern").joinpath("design")
CSS = ASSETS.joinpath("hermes.css").read_text()
DARK = {
    "accent": "#d5b475",
    "text": "#ece8df",
    "muted": "#aaa397",
    "toolTitle": "#d5b475",
    "mdHeading": "#d5b475",
    "statusLineModel": "#aaa397",
    # Code: Hermes gold for keywords, then cool and warm notes that sit well beside it.
    "syntaxKeyword": "#d5b475",
    "syntaxFunction": "#8fb8de",
    "syntaxString": "#a9c98c",
    "syntaxNumber": "#e39b7b",
    "syntaxType": "#7fc4bf",
    "syntaxVariable": "#ece8df",
    "syntaxOperator": "#c3a6e0",
    "syntaxPunctuation": "#8a847a",
    "syntaxComment": "#77716a",
}
LIGHT = {
    "accent": "#805c20",
    "text": "#292722",
    "muted": "#686359",
    "toolTitle": "#805c20",
    "mdHeading": "#805c20",
    "statusLineModel": "#686359",
    "syntaxKeyword": "#805c20",
    "syntaxFunction": "#2f6a9a",
    "syntaxString": "#4f7a2e",
    "syntaxNumber": "#a8502c",
    "syntaxType": "#2f7f78",
    "syntaxVariable": "#292722",
    "syntaxOperator": "#6b4f9a",
    "syntaxPunctuation": "#8f897d",
    "syntaxComment": "#9a958b",
}


def send_assets(session) -> dict[str, str]:
    if "image" not in session.caps.kinds or not session.caps.has("blobs"):
        return {}
    return {
        name: session.blob(ASSETS.joinpath(f"{name}.svg").read_bytes(), "image/svg+xml")
        for name in ("hermes", "wordmark")
    }
