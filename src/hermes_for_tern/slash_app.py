"""Slash command keyboard handling, dispatch and local command results."""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import Future
from pathlib import Path

from .editor import Draft
from .messages import Message
from .transcript import markdown, matches
from .slash_views import ALIASES, COMMAND_ROWS, slash_open, slash_rows, slash_typed

# The commands this pane answers itself; every other `/command` is Hermes's. What each does lives
# beside the list that offers them, so `/help` cannot drift from the keyboard.
COMMANDS = tuple(name.split(" ")[0] for name, _ in COMMAND_ROWS)
HELP = "In this pane: " + " · ".join(COMMANDS)
# `slash.exec` refusals that mean "not mine, ask command.dispatch" (skills, bundles, pending input).
DISPATCH_CODES = (4011, 4018)
# Output longer than this folds into a row of its own instead of a one-line note.
NOTE_CHARS, NOTE_LINES = 180, 2


class Commands:
    def slash_key(self, item) -> bool:
        """The command list answers the keyboard while it shows: arrows move, Tab completes the
        highlighted command, Enter runs it (or completes one that still needs its argument), and
        Escape puts the list away until the draft changes. True when the key was the list's."""
        if item.ctrl or item.alt or item.meta or self.state.secret() or self.state.clarify():
            return False
        if self.state.sheet or self.state.watch or not slash_open(self.state, self.draft):
            return False
        rows = slash_rows(self.state, self.draft.text)
        at = min(self.state.slash.cursor, len(rows) - 1)
        if item.name in ("up", "down"):
            self.state.slash.cursor = (at + (1 if item.name == "down" else -1)) % len(rows)
        elif item.name == "tab":
            self.complete_command(rows[at])
        elif item.name == "enter" and not item.shift:
            self.complete_command(rows[at], run=True)
        elif item.name == "escape":
            self.state.slash.put_away = self.draft.text
        else:
            return False
        self.state.touch()
        return True

    def complete_command(self, row: dict, *, run: bool = False) -> None:
        """Put the row into the draft. Run it too when asked, unless it is one of this pane's
        commands that needs its argument (`/btw`, `/bg`, `/name`): those wait for it."""
        start = row.get("from")
        if start is None:
            text = row["insert"] + " "
        else:
            text = self.draft.text[:start] + row["insert"]
        self.draft = Draft(text, len(text))
        self.state.slash.cursor = 0
        needs_argument = row.get("local") and "<" in row.get("label", "")
        if run and not needs_argument:
            self.submit(text)
        elif self.surface:
            self.surface.focus("dock.composer")

    def pick_command(self, index: int) -> None:
        """A click on a row of the command list completes it, ready for an argument."""
        rows = slash_rows(self.state, self.draft.text)
        if 0 <= index < len(rows):
            self.complete_command(rows[index])
            self.state.touch()

    def ask_completions(self) -> None:
        """Ask Hermes what completes the draft, once per draft, while a command is being typed."""
        text = self.draft.text
        if self.state.slash.put_away is not None and self.state.slash.put_away != text:
            self.state.slash.put_away = None
        if (
            slash_typed(text) is None
            or not self.backend
            or not self.state.session_id
            or not self.state.ready
            or self.state.failed
            or self.switching
            or text in self.asked.values()
            or self.state.slash.answered == text
        ):
            return
        if self.completing is not None and self.completing in self.asked:
            self.asked.pop(self.completing)  # a newer draft makes the older answer moot
        self.completing = self.request("complete.slash", self.scoped(text=text))
        self.asked[self.completing] = text

    def run_command(self, text: str) -> None:
        """One of COMMANDS, answered here; every other `/command` is Hermes's to run."""
        name, space, rest = text.partition(" ")
        if ALIASES.get(name.lower()):
            text = ALIASES[name.lower()] + space + rest
        local = {
            "/reconnect": "reconnect",
            "/recover": "restore",
            "/queue-continue": "continue-queue",
            "/queue-discard": "discard-queue",
            "/find-next": "find-next",
            "/find-previous": "find-previous",
            "/find-close": "find-close",
        }
        if name == "/attach":
            if rest.strip():
                self.load_image(rest)
            else:
                self.attachment_action("image-path")
        elif text == "/paste-image":
            self.load_image()
        elif text in local:
            self.local_action(local[text])
        elif name == "/find":
            self.state.search_query = rest.strip()
            self.state.search_index = 0
            found = matches(self.state)
            if found:
                found[0].collapsed = False
            self.state.touch()
        elif name == "/export":
            self.export_transcript(rest.strip())
        elif self.switching and name not in ("/quit", "/exit", "/doctor", "/stop"):
            self.state.add("notice", "Wait for the session switch before running this command.")
        elif text in ("/quit", "/exit"):
            self.exit = True
        elif text == "/model":
            self.model_event("open")
        elif text.startswith("/model "):
            self.switch_model(text[len("/model ") :].strip())
        elif text == "/resume":
            self.open_sessions()
        elif text.startswith("/resume "):
            self.resume_session(text[len("/resume ") :].strip())
        elif text == "/name":
            self.state.add("notice", "Name the session with /name <title>.")
        elif text.startswith("/name "):
            self.rename_session(text[len("/name ") :].strip())
        elif text.startswith("/btw "):
            self.side_question(text[len("/btw ") :].strip())
        elif text.startswith("/bg "):
            self.background_task(text[len("/bg ") :].strip())
        elif text == "/exit":
            self.exit = True
        elif text == "/stop":
            self.stop()
            self.draft = Draft()
        elif text == "/help":
            self.hermes_command("/help")
        elif text == "/clear":
            self.state.clear()
            self.state.add("notice", "Transcript cleared. Hermes still remembers this session.")
        elif text == "/cost":
            self.state.add("notice", self.state.cost())
        elif text == "/doctor":
            self.doctor()
        else:
            self.hermes_command(text)

    def hermes_command(self, text: str) -> None:
        """Hand a slash command to Hermes, the way its own terminal does: `slash.exec` first, which
        answers with output; it refers skills, bundles and commands that need the live session to
        `command.dispatch` (see command_result). Hermes refuses what may not run mid-turn itself."""
        if not self.backend or not self.state.session_id:
            self.state.add("notice", f"{text.split(' ')[0]} needs Hermes, which is still starting.")
            return
        future = self.request("slash.exec", self.scoped(command=text[1:]))
        self.asked[future] = text

    def dispatch(self, text: str) -> None:
        name, _, arg = text[1:].partition(" ")
        future = self.request("command.dispatch", self.scoped(name=name.lower(), arg=arg.strip()))
        self.asked[future] = text

    def command_result(self, text: str, result: dict) -> None:
        """What Hermes made of a slash command: output to show, a message to send, a draft to put
        back in the composer, or another command it stands for."""
        name, _, arg = text.partition(" ")
        kind = result.get("type")
        if kind == "alias" and result.get("target"):
            self.run_command(f"/{result['target']}" + (f" {arg}" if arg else ""))
        elif kind in ("skill", "send"):
            if result.get("notice"):
                self.state.add("notice", str(result["notice"]))
            message = str(result.get("message") or "").strip()
            if message:
                self.send_command(message, str(result.get("display") or "").strip() or text)
        elif kind == "prefill":
            if result.get("notice"):
                self.state.add("notice", str(result["notice"]))
            message = str(result.get("message") or "")
            self.draft = Draft(message, len(message))
            self.state.touch()
        else:
            output = str(result.get("output") or "").strip("\n") or f"{name}: no output"
            if result.get("warning"):
                output = f"warning: {result['warning']}\n{output}"
            if name == "/help":
                output += "\n\n" + HELP
            self.show_output(name, output)

    def show_output(self, name: str, output: str) -> None:
        """A short answer is a note; a long one folds into a row of its own, a few lines showing."""
        lines = [line for line in output.splitlines() if line.strip()]
        if len(output) <= NOTE_CHARS and len(lines) <= NOTE_LINES:
            self.state.add("notice", output)
        else:
            self.state.add("command", output, name=name)

    def send_command(self, message: str, shown: str) -> None:
        """Send what a command expanded to, showing the command as it was typed rather than the
        expansion, which is written for the model."""
        if not self.state.ready or self.state.failed or self.switching:
            self.state.add("notice", "Reconnect or finish switching sessions before sending this command.")
            return
        if self.state.held_message:
            self.state.add("notice", "Restore or discard the retained message before sending this command.")
            return
        if self.state.busy:
            self.state.add("notice", f"{shown.split(' ')[0]} can run when this turn ends.")
            return
        self.state.begin(shown)
        self.state.last_prompt = message
        self.state.last_message = Message(message)
        self.request("prompt.submit", self.scoped(text=message))

    def rename_session(self, title: str) -> None:
        """Name this session; Hermes owns the title and echoes it back on `session.title`."""
        if title and self.state.session_id:
            self.request("session.title", self.scoped(title=title))

    def side_question(self, text: str) -> None:
        """Ask Hermes something beside the work it is doing; the answer lands as a dispatch."""
        if not text or not self.state.session_id:
            return
        self.request("prompt.btw", self.scoped(text=text))

    def background_task(self, text: str) -> None:
        """Hand Hermes a task on a fresh agent; the answer lands as a dispatch when it is done."""
        if not text or not self.state.session_id:
            return
        self.request("prompt.background", self.scoped(text=text))

    def export_transcript(self, destination: str) -> None:
        path = Path(destination).expanduser() if destination else self.cwd / "hermes-conversation.md"
        if not path.is_absolute():
            path = self.cwd / path
        try:
            # Exclusive creation prevents accidental overwrite, including through symlinks.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(markdown(self.state))
            self.state.add("notice", f"Exported loaded conversation to {path}")
        except OSError:
            self.state.add(
                "notice",
                f"Could not export to {path}. Choose a new writable filename; existing files are never overwritten.",
            )

    def doctor(self) -> None:
        from .diagnostics import diagnose

        if any(method == "local.doctor" for method, _, _ in self.pending):
            return
        future = Future()

        def check():
            try:
                future.set_result(diagnose(self.cwd, surface_caps=self.session.caps))
            except Exception:
                future.set_result(
                    ["Doctor could not finish. Check the launcher settings and backend installation."]
                )

        worker = threading.Thread(target=check, daemon=True)
        worker.start()
        self._retired.append(worker)
        self.pending.append(("local.doctor", future, time.monotonic() + 60))
        self.state.add("notice", f"project: {self.cwd}")
        self.state.add("notice", f"session: {self.state.session_id or 'none'}")
        self.state.add(
            "notice", "Checking launcher integrity and backend handshake (no agent session or prompt)."
        )
