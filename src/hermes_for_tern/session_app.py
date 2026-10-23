"""Session creation, resume, switching and return hints."""

from __future__ import annotations

import shlex

from .editor import Draft

# Sessions `/resume` lists, newest first.
SESSIONS = 50


class Sessions:
    @property
    def cols(self) -> int:
        return self.session.caps.cols or 80

    def profiled(self, **params) -> dict:
        """Params for a call that reaches Hermes's session store, in the launch profile's home."""
        profile = self.options.get("profile")
        return {**params, **({"profile": profile} if profile else {})}

    def create_session(self) -> None:
        params = {
            "cwd": str(self.cwd),
            "cwd_explicit": True,
            "source": "tui",
            "title": "Hermes for Tern",
            "cols": self.cols,
            **self.options,
        }
        self.request("session.create", params)

    def start_fresh(self, why: str) -> None:
        """Initial CLI resume may fall back; recovery must never silently change sessions."""
        if self.recovering or self.restoring:
            self.fail(f"{why}. Retry recovery or explicitly start a new session; your text is retained.")
            self.recovery_error = True
            self.restoring = None
            return
        self.state.add("notice", f"{why}. This is a new session.")
        self.resume = None
        self.create_session()

    def session_ready(self, result: dict, *, stored: str | None) -> None:
        if not isinstance(result.get("session_id"), str) or not result["session_id"]:
            raise ValueError("Hermes returned an invalid session id.")
        self.state.session_id = result["session_id"]
        self.state.stored_session_id = stored if isinstance(stored, str) else None
        self.state.info.update(result.get("info") or {})
        self.state.ready = True
        self.state.failed = False
        self.state.connection = "ready"
        self.state.activity = "Ready"
        self.state.touch()

    def resume_hint(self) -> str:
        """What Hermes's own terminal says on the way out: how to come back to this session. Nothing
        when the session never held a conversation, since Hermes keeps no record of it then."""
        stored = self.state.stored_session_id
        if not stored or not (self.state.last_prompt or self.state.resumed):
            return ""
        profile = self.options.get("profile")
        flag = f" -p {shlex.quote(profile)}" if profile and profile not in ("default", "custom") else ""
        lines = ["Resume this session with:", f"  hermes --resume {shlex.quote(stored)}{flag}"]
        if self.state.title:
            title = self.state.title
            # Preserve the familiar quoted hint for ordinary titles, escape shell expansion.
            escaped = title.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
            lines.append(f'  hermes -c "{escaped}"{flag}')
        return "\n".join(lines)

    def open_sessions(self) -> None:
        """The sessions sheet, filled from Hermes's store as soon as it answers."""
        if not self.state.session_id:
            return
        self.request("session.list", self.profiled(limit=SESSIONS))
        self.state.open_sheet("sessions")

    def resume_session(self, target: str) -> None:
        """Pick up an earlier session in this pane: its transcript replaces this one, and this live
        session is closed in Hermes once the other is open (its history stays in Hermes's store)."""
        if not target or not self.state.session_id or self.switching:
            return
        if self.state.image_loading or self.state.draft_images:
            self.state.add(
                "notice", "Finish importing, then send or remove draft images before switching sessions."
            )
            return
        if self.state.busy:
            self.state.add("notice", "Finish or stop this turn before switching sessions.")
            return
        if target == self.state.stored_session_id:
            self.state.close_sheet()
            self.state.add("notice", "This is the session you are in.")
            return
        if self.state.queued and not all(message.destination == target for message in self.state.queued):
            self.state.add(
                "notice", "Continue or discard the current session's queue before switching sessions."
            )
            self.restoring = None
            return
        self.switching = target
        self.state.switching = True
        self.state.queue_paused = True
        self.state.touch()
        self.request("session.resume", self.profiled(session_id=target, cols=self.cols))

    def switch_failed(self) -> None:
        self.switching = None
        self.state.switching = False
        self.state.queue_paused = True
        if self.held_switch:
            if any(message.images for message in self.held_switch):
                self.state.add(
                    "notice",
                    "Session switch failed. Image messages remain paused for their destination; retry that switch or discard the queue.",
                )
                for message in self.held_switch:
                    self.state.queue(message)
            else:
                self.state.add(
                    "notice", "Session switch failed. Held messages were kept in your draft; none were sent."
                )
                text = "\n\n".join(
                    [message.text for message in self.held_switch]
                    + ([self.draft.text] if self.draft.text else [])
                )
                self.draft = Draft(text, len(text))
            self.held_switch.clear()
        self.restoring = None
        self.state.touch()
        self.save_drafts(force=True)
