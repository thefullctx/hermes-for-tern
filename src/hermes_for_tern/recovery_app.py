"""Draft persistence, restoration and paused queue actions."""

from __future__ import annotations

import time

from .editor import Draft
from .messages import ImageRef, Message
from .transcript import matches


class Recovery:
    def fail(self, message: str) -> None:
        if self.state.failed:
            return
        if self.inflight:
            self.state.held_message = self.inflight
            self.state.held_uncertain = True
            self.inflight = None
        self.delivery = None
        self.state.image_preparing = False
        self.state.add("error", message)
        self.state.failed = True
        self.state.ready = False
        self.state.connection = "disconnected"
        self.state.queue_paused = True
        self.state.questions.clear()
        self.state.close_watch()
        self._ack_approvals.clear()
        self.secret = Draft()
        self.answer_draft = Draft()
        for tool in self.state.tools.values():
            if tool.status == "running":
                tool.status = "cancelled"
        for agent in self.state.agents.values():
            if agent.status in ("running", "pending"):
                agent.status = "aborted"
        if self.state.busy:
            self.uncertain_work = True
            self.state.add(
                "notice", "Work was interrupted. It may already have changed files; it will not be resent."
            )
        self.state.busy = False
        self.state.activity = "Disconnected — reconnect to resume"
        self.recovering = False
        if self.switching:
            self.switch_failed()
        self.switching = None
        self.state.switching = False
        self.state.touch()
        self.save_drafts(force=True)

    def update_saved(self) -> None:
        self.state.recovery_available = bool(self.saved_records)
        record = self.saved_records[0] if self.saved_records else {}
        self.state.recovery_label = str(record.get("session") or "a session that did not finish starting")
        self.state.touch()

    def save_drafts(self, *, force: bool = False) -> None:
        if not self.store or (not force and time.monotonic() - self._saved_at < 0.5):
            return
        self._saved_at = time.monotonic()
        try:
            self.store.save(
                self.draft.text,
                self.state.queued + self.held_switch,
                self.switching or self.state.stored_session_id,
                self.state.busy or self.uncertain_work or self.state.held_uncertain,
                images=self.state.draft_images,
                pending=self.inflight or self.state.held_message,
            )
            self._save_failed = False
        except OSError:
            if not self._save_failed:
                self.state.add(
                    "notice", "Draft recovery could not be saved. Keep this pane open to retain your text."
                )
                self._save_failed = True

    def restore_drafts(self) -> None:
        if not self.saved_records or self.restoring:
            return
        if not self.state.ready and not self.state.failed:
            self.state.add("notice", "Wait for Hermes to finish connecting before restoring saved text.")
            return
        if (
            self.state.busy
            or self.switching
            or self.draft.text
            or self.state.queued
            or self.state.draft_images
            or self.state.image_loading
            or self.state.held_message
        ):
            self.state.add(
                "notice",
                "Finish this turn and send or clear your current draft and queue before restoring saved text.",
            )
            return
        self.restoring = self.saved_records[0]
        target = self.restoring.get("session")
        if target and target != self.state.stored_session_id:
            if self.state.ready:
                self.resume_session(target)
            else:
                self.state.stored_session_id = target
                self.reconnect()
        else:
            self.apply_restored()

    def apply_restored(self) -> None:
        record = self.restoring
        if not record:
            return
        if self.draft.text or self.state.draft_images or self.state.image_loading or self.state.queued:
            self.restoring = None
            self.state.add(
                "notice",
                "Your current draft changed while restoring. Saved work is still available; clear this draft and restore again.",
            )
            return
        self.draft = Draft(record["draft"], len(record["draft"]))
        self.state.queued = [Message.read(message) for message in record["queue"]]
        self.state.draft_images = tuple(ImageRef.read(image) for image in record.get("draft_images", []))
        self.state.held_message = (
            Message.read(record["pending"]) if record.get("pending") is not None else None
        )
        self.state.held_uncertain = bool(record.get("uncertain"))
        images = list(self.state.draft_images)
        images += [image for message in self.state.queued for image in message.images]
        if self.state.held_message:
            images += list(self.state.held_message.images)
        for image in images:
            self.register_image(image)
        self.state.queue_paused = True
        # Write our replacement before removing the original recovery file.
        self.save_drafts(force=True)
        if self.store and not self._save_failed:
            try:
                self.store.forget(record)
                self.saved_records.remove(record)
            except OSError:
                self.state.add("notice", "Text restored, but its old recovery file could not be removed.")
        self.restoring = None
        self.update_saved()
        self.state.add(
            "notice",
            "Saved text restored; nothing was sent. Queued messages are paused. Previously submitted work may already have run.",
        )

    def continue_queue(self) -> None:
        if not self.state.ready or self.state.failed or self.switching or self.state.busy:
            return
        if self.state.held_message:
            self.state.add("notice", "Restore or discard the retained message before continuing the queue.")
            return
        self.state.queue_paused = False
        self.send_queued()
        self.state.touch()

    def send_queued(self) -> None:
        if (
            self.state.queued
            and not self.state.queue_paused
            and not self.switching
            and self.delivery is None
            and self.state.held_message is None
            and not self.state.busy
            and self.state.ready
            and not self.state.failed
        ):
            message = self.state.take_queued()
            self.send_message(message)

    def local_action(self, action: str) -> None:
        if action.startswith("image-"):
            self.attachment_action(action)
            return
        if action == "reconnect":
            self.reconnect()
        elif action == "new-session":
            self.reconnect(fresh=True)
        elif action == "restore":
            self.restore_drafts()
        elif action == "forget-saved" and self.saved_records and self.store:
            try:
                self.store.forget(self.saved_records.pop(0))
                self.update_saved()
            except OSError:
                self.state.add("notice", "Could not remove the saved draft.")
        elif action == "continue-queue":
            self.continue_queue()
        elif action == "discard-queue":
            self.state.queued.clear()
            self.state.touch()
        elif action in ("find-next", "find-previous"):
            found = matches(self.state)
            if found:
                self.state.search_index = (
                    self.state.search_index + (1 if action == "find-next" else -1)
                ) % len(found)
                found[self.state.search_index].collapsed = False
                self.state.touch()
        elif action == "find-close":
            self.state.search_query = ""
            self.state.touch()
        self.save_drafts(force=True)
