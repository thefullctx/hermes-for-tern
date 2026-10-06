"""Packaged visual assets, independent of Hermes and its configuration."""

from importlib.resources import files


ASSETS = files("hermes_for_tern").joinpath("design")
CSS = ASSETS.joinpath("hermes.css").read_text()
DARK = {
    "accent": "#d5b475",
    "text": "#ece8df",
    "muted": "#aaa397",
    "toolTitle": "#d5b475",
    "mdHeading": "#ece8df",
    "statusLineModel": "#aaa397",
}
LIGHT = {
    "accent": "#805c20",
    "text": "#292722",
    "muted": "#686359",
    "toolTitle": "#805c20",
    "mdHeading": "#292722",
    "statusLineModel": "#686359",
}


def send_assets(session) -> dict[str, str]:
    if "image" not in session.caps.kinds or not session.caps.has("blobs"):
        return {}
    return {
        name: session.blob(ASSETS.joinpath(f"{name}.svg").read_bytes(), "image/svg+xml")
        for name in ("hermes", "wing")
    }
