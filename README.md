# Hermes for Tern

**Your agent, at home in Tern.**

A native [Tern](https://stencil.so/) frontend for [Hermes Agent](https://github.com/NousResearch/hermes-agent).
Type **`hermes`**, just as you normally would. Inside Tern, your conversation is rendered as
native Markdown, tool cards, buttons and a docked composer. In other terminals, the original
Hermes interface starts normally.

Hermes owns the agent. Tern owns the drawing. This adapter connects their existing protocols;
it does not patch Hermes, scrape ANSI output or embed a web chat.

## Install

This repository is a private work in progress. Native subagent cards are planned but are not
implemented in this version.

Prerequisites:

- macOS or Linux, Python 3.11+, Git and [uv](https://docs.astral.sh/uv/).
- Tern with Surface Protocol v1, `dock`, and the native component vocabulary.
- A working Hermes Agent installation and model configuration.

From this repository:

```sh
git clone https://github.com/thefullctx/hermes-for-tern.git
cd hermes-for-tern
uv sync --frozen
uv run hermes-for-tern --doctor
uv run hermes-for-tern --install
```

Then open Tern in your project and run:

```sh
hermes
```

The installer saves your existing launcher beside it as `hermes.before-tern` and installs
a small wrapper. It never modifies the Hermes repository, dependencies, credentials or
approval configuration. Keep this checkout and its `.venv` in place while the wrapper is installed.

To try it without installing the wrapper, run `uv run hermes-for-tern` inside Tern.

## Preview

![Hermes for Tern: real terminal tool output and a native Markdown reply](assets/screenshot.png)

[Animated native replay](assets/demo.gif). These previews were rendered by Tern from a recorded
live Hermes session; they are not browser mockups. The account avatar in the replay is Tern's
headless test identity. Recorded project paths were generalized for portability. The replay uses the current design stylesheet.

To render the same demonstration locally from the repository root:

```sh
tern shot docs/design-demo.txt --out work/demo --size 1440x900 --theme dark
```

### Visual design

Hermes uses graphite cards with muted gold accents in dark mode, and warm ivory in light mode.
The startup screen is centered above the composer, with a staged logo/title/details entrance.
It includes artwork adapted from Hermes Agent, project/model details, and
three prompt suggestions. UI labels use a system sans-serif stack; prose, input, code, and paths
retain the terminal font. The dock includes a focused composer and Send/Stop controls.

The custom wing-and-orbit indicator is animated by Tern, without per-frame Python updates.
It becomes a static mark when a response from you is needed, stops under Reduce Motion, and
pauses in hidden or covered panes. Tool output remains expandable. Styling is scoped to this
surface, so the surrounding Tern interface follows your theme.

![Hermes startup](assets/startup.png)

[Startup entrance animation](assets/entrance.gif). Render it with
`tern shot docs/entrance-demo.txt --out work/entrance --size 1440x900 --theme dark`.

### Continuous motion and visual demo

During a turn, running tool cards shimmer and show an animated indeterminate bar and elapsed time.
Streamed answers have a shimmering label and a moving glimmer. A live work panel adds animated
activity bars and a graph sampled from **received response characters**, not estimated tokens or
invented model progress. These effects stop when the turn ends and respect Reduce Motion.

[Animated work preview](assets/motion-demo.gif). This recording is a labelled simulation.

For a five-minute simulation inside Tern:

```sh
hermes --demo
```

It starts automatically and is visibly labelled **SIMULATED DEMO**. It runs the same native frontend
with a deterministic backend: no model requests, shell commands, or file changes. Stop works normally.
For a shorter run, use `hermes --demo 30` (seconds). Ordinary `hermes` still uses the real Hermes backend.
Demo durations must be between 10 and 1,800 seconds. The demo requires Tern and does not fall back
to the real agent in another terminal.

### Backend compatibility

The default backend command uses Hermes's managed launcher:

```text
<original-hermes> --run-module tui_gateway.entry
```

For a conventional Hermes virtualenv installation without `--run-module`, specify its backend:

```sh
uv run hermes-for-tern --install --backend '/absolute/path/to/hermes-venv/bin/python -u -m tui_gateway.entry'
```

That interpreter must have Hermes's dependencies and its modules must be importable from your
project directory. For example, install Hermes into its own virtualenv as an editable package.
Do not install Tern dependencies into Hermes's managed environment.

`--target /path/to/bin/hermes` can instead create a wrapper in a different directory placed
earlier in `PATH`, preserving the existing launcher at its original location.

## What works

- Native conversation UI with streamed Markdown and code blocks.
- Multi-turn conversations using the real Hermes backend and existing tools.
- Native tool cards with folded output, duration, outcome and optional diffs.
- Composer typing, multiline paste, Unicode, native selection and basic undo.
- Tool approval buttons using the backend's offered choices.
- Clarification questions: options, multiple selections, free text and skipping.
- Stop and a new prompt after interruption.
- Backend error reporting and orderly terminal restoration.
- Existing model/provider/profile configuration; chat flags `--model`, `--provider`, `--profile` and `--tui`.
- Ordinary Hermes subcommands, including `hermes model`, `hermes doctor` and `hermes --version`,
  pass directly to the original command.

The adapter respects Hermes's approval policy. It does not enable approvals when your policy is off,
and it never automatically approves a request.

## Keys

| Key | Action |
| --- | --- |
| Enter | Send a prompt or answer a clarification |
| Shift+Enter / Alt+Enter | Insert a newline |
| Ctrl+C | Stop an active turn; clear a draft when idle; exit when idle with an empty draft |
| Escape | Stop the active turn |
| Ctrl+D with an empty draft | Exit |
| Native selection / undo shortcuts | Handled through Tern's edit and undo events |

The composer remains editable while Hermes works; it holds your next draft until the turn ends.
`/stop`, `/quit` and `/exit` are local commands. Other slash commands are not implemented yet.

## Restore the original command

```sh
uv run hermes-for-tern --uninstall
```

To temporarily use the original interface even inside Tern:

```sh
hermes --plain
```

`TERN_TSP=0 hermes` also disables surface detection. Before moving/removing this checkout or
updating an installation that replaces its outer `hermes` launcher, uninstall the wrapper.
The uninstaller refuses to overwrite a launcher that somebody changed after installation.

## Architecture

```text
Tern pane ⇄ Surface Protocol ⇄ Python adapter ⇄ JSON-RPC pipes ⇄ Hermes tui_gateway
```

- `launcher.py`: reversible installation, surface detection and original-command fallback.
- `rpc.py`: bidirectional transport, request matching and backend lifecycle.
- `state.py`: ordered transcript and pending-request state.
- `editor.py`: composer text/caret state and UTF-16 edit translation.
- `views.py`: semantic native components and small theme-aware stylesheet.
- `design.py` and `design/`: packaged artwork, theme styles and native motion.
- `demo_backend.py`: isolated simulation using the same JSON-RPC event path.
- `app.py`: UI event loop, streaming updates, approvals, clarification and interruption.

The UI polls Tern input continuously while a background reader queues Hermes events.
Render updates are batched at roughly 30 Hz; the SDK handles diffing, credits and terminal cleanup.
The backend runs in a separate process with private pipes. Its stdout never reaches the pane directly.

## MVP boundaries

One live conversation per process. Session browsing/resume, restart recovery, model pickers,
attachments, full slash-command support, voice and desktop browser bridges are not implemented.
Hermes delegation may appear as an ordinary tool call; dedicated subagent events, child transcripts,
agent hierarchy and individual-agent controls are not rendered yet. The visual demo currently shows
sequential work phases, not parallel subagents.
Unsupported Hermes launch flags fall back to the original interface rather than being silently discarded.
Unsupported server requests, including secret/sudo/vault entry, receive an explicit unsupported-method
error so they cannot leave the agent waiting indefinitely.

Tern's inline surface disappears when you return to the shell prompt. Hermes owns durable conversation
storage; the Tern transcript is not a persistence layer. Multiplexers such as tmux disable SDK detection.
Long tool output is visually truncated at 48,000 characters in this MVP.

## Validation

Tested locally on macOS with:

- Tern `0.5.0` (`ea43dbe`).
- Hermes `v0.21.5+7421.gffe73c3`.
- Tern Python SDK `0.1.0`, pinned to `46bd7df153d3135d84a0b5658dfb4705d0bddb81`.

Live checks exercised native rendering, streamed replies, follow-up context, real terminal tool use,
clarification through a native button, interruption during a tool call, a successful subsequent turn,
native Unicode selection/paste/undo, and clean exit. Allow and Deny buttons were exercised against the controlled JSON-RPC fixture because
the local Hermes approval policy was off. Linux and other Hermes installation methods are unverified.

```sh
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
```

The current suite contains 27 tests covering streaming/interim/final message ordering, tool
failure/interruption, Unicode edits, server-request answers, process disconnection, reversible launcher
installation, response activity sampling and demo completion/interruption. They use temporary
directories and a controlled backend, without touching your Hermes configuration or calling a model.

## License and attribution

MIT. This is an independent integration, not an official Nous Research or Stencil product.
The composer was adapted from Stencil's MIT-licensed Python chat example, and the startup artwork
from Hermes Agent; see `THIRD_PARTY_NOTICES.md`.
