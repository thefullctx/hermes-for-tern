# Contributing

Thanks for wanting to improve Hermes for Tern. Bug reports, ideas and pull requests are all welcome.
Every change is reviewed before it is merged, and not every idea will fit; opening an issue first
for anything large saves everyone time.

## Set up

You need macOS or Linux, Python 3.11+, [uv](https://docs.astral.sh/uv/), and Tern to see your changes.

```sh
git clone https://github.com/thefullctx/hermes-for-tern.git
cd hermes-for-tern
uv sync --frozen
```

Run it inside Tern without installing the wrapper:

```sh
uv run hermes-for-tern            # with your real Hermes
uv run hermes-for-tern --demo 30  # a labelled simulation; no model calls, commands or file changes
```

## Before you open a pull request

```sh
uv run pytest -q
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
```

For visual changes, attach a screenshot or GIF. `./scripts/render_previews.sh` re-records the README
scenes through the real views and renders them with a headless Tern (it needs Tern and ffmpeg);
`scripts/record_motion.py` holds the scripted session if you want to show a new state.

## House rules for the interface

- **The console idiom.** One monospace face, the glyph gutter (`❯` you, `●` tools, `✗` errors,
  `☤` delivered), frameless tools, color as the only hierarchy. Hermes's color is muted gold.
- **Motion only when something happens.** Arrivals, outcomes and work in progress move; idle stays
  still. Every animation must pause in hidden panes (`.sf-paused`, `.sf-covered`) and stop under
  `.sf-still` and `prefers-reduced-motion`.
- **Never change the size of code blocks or agent rows.** Tern measures them itself; restyle their
  colors and lines only, or text after them can disappear.
- **Check the stylesheet loads.** Tern reports unknown properties as errors, which Hermes for Tern
  shows in the transcript. Start the real app once inside Tern after editing `hermes.css`.
- **State changes need tests.** `state.py` is a pure reducer over Hermes events; cover new events in
  `tests/test_state.py`, and new components in `tests/test_views.py`.
- **Do not patch Hermes.** This adapter only speaks Hermes's existing JSON-RPC protocol.

## Commits and pull requests

Keep pull requests focused on one change, describe what it does and why, and link the issue it
addresses. By contributing you agree that your work is released under the MIT license.
