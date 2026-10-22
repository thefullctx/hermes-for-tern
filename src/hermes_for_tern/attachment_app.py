"""Image composition and delivery hooks for the UI loop."""

from __future__ import annotations

import shlex
import threading
from concurrent.futures import Future
from pathlib import Path

from tern_sdk import Key

from .delivery import ImageDelivery
from .editor import Draft
from .images import ImageStore, clipboard_image, dropped_image_path
from .messages import ImageRef, Message


class Attachments:
    def init_images(self) -> None:
        self.image_store = None
        self.image_error = ""
        try:
            self.image_store = ImageStore(self.cwd, self.options.get("profile"))
        except ValueError as exc:
            self.image_error = str(exc)
        self.clipboard_reader = clipboard_image
        self.image_load: Future | None = None
        self.image_drop_draft: str | None = None
        self.image_path = Draft()
        self.image_path_open = False
        self.delivery: ImageDelivery | None = None
        self.inflight: Message | None = None

    def register_image(self, image: ImageRef) -> None:
        try:
            if self.image_store is None:
                raise ValueError(self.image_error)
            self.image_store.read(image)
            data = self.image_store.preview_path(image).read_bytes()
            if hasattr(self.session, "blob"):
                self.assets["attachment:" + image.id] = self.session.blob(data, "image/png")
        except (OSError, ValueError):
            self.state.add(
                "notice",
                f"{image.name}: saved image unavailable. Remove it and attach it again before sending.",
            )

    def import_dropped_image(self) -> bool:
        if (
            self.state.secret()
            or self.state.clarify()
            or self.image_path_open
            or self.state.sheet
            or self.state.watch
        ):
            return False
        path = dropped_image_path(self.draft.text)
        if path is None:
            return False
        self.load_image(path, from_draft=self.draft.text)
        return True

    def load_image(self, path: str | None = None, *, from_draft: str | None = None) -> None:
        if self.restoring:
            self.state.add("notice", "Wait for saved work to finish restoring before importing an image.")
            return
        if self.state.secret() or self.state.clarify():
            self.state.add("notice", "Images belong to a message, not to this answer.")
            return
        if self.demo_duration is not None:
            self.state.add("notice", "Image attachments are unavailable in the simulated demo.")
            return
        if not self.image_store:
            self.state.add("notice", self.image_error)
            return
        if self.image_load or self.state.image_preparing:
            self.state.add("notice", "Wait for the current image operation to finish.")
            return
        if len(self.state.draft_images) >= self.image_store.max_count:
            self.state.add(
                "notice",
                f"A message can hold {self.image_store.max_count} images. Remove one before adding another.",
            )
            return
        future = self.image_load = Future()
        self.image_drop_draft = from_draft
        self.state.image_loading = True
        self.state.touch()
        store, reader = self.image_store, self.clipboard_reader

        def load():
            try:
                if path is None:
                    image = store.add(reader(store.max_bytes))
                else:
                    raw = path.strip()
                    if raw[:1] in ("'", '"'):
                        parts = shlex.split(raw)
                        if len(parts) != 1:
                            raise ValueError("Choose one image path at a time.")
                        raw = parts[0]
                    source = Path(raw).expanduser()
                    if not source.is_absolute():
                        source = self.cwd / source
                    image = store.add_path(source)
                future.set_result(image)
            except Exception as exc:
                future.set_exception(exc)

        worker = threading.Thread(target=load, daemon=True)
        worker.start()
        self._retired.append(worker)

    def poll_images(self) -> None:
        if self.image_load and self.image_load.done():
            future, self.image_load = self.image_load, None
            drop_draft, self.image_drop_draft = self.image_drop_draft, None
            self.state.image_loading = False
            try:
                image = future.result()
                if len(self.state.draft_images) < self.image_store.max_count:
                    self.state.draft_images += (image,)
                    if drop_draft is not None:
                        if self.draft.text.startswith(drop_draft):
                            self.draft.replace(0, len(drop_draft), "")
                        self.state.add("notice", "Image attached. Review the preview, then press Send.")
                    self.register_image(image)
                    self.image_path_open = False
                    self.image_path = Draft()
                    if self.surface:
                        self.surface.focus("dock.composer")
            except (ValueError, OSError) as exc:
                self.state.add(
                    "notice",
                    str(exc)
                    if isinstance(exc, ValueError)
                    else "Could not read the image. Choose a readable PNG or JPEG.",
                )
            except Exception:
                self.state.add(
                    "notice", "Could not import the image. Use Attach image to choose a PNG or JPEG."
                )
            self.state.touch()
            self.save_drafts(force=True)
        if self.delivery:
            self.delivery.tick()
            if self.delivery.done:
                delivery, self.delivery = self.delivery, None
                self.state.image_preparing = False
                if not delivery.accepted:
                    self.state.held_message = delivery.message
                    self.state.held_uncertain = delivery.tainted
                    self.state.queue_paused = True
                    self.state.busy = False
                    self.state.activity = "Ready"
                    self.inflight = None
                    if delivery.tainted:
                        self.fail(delivery.error)
                    else:
                        self.state.add("notice", delivery.error)
                elif not self.state.busy:
                    self.inflight = None
                    self.send_queued()
                self.state.touch()
                self.save_drafts(force=True)

    def send_message(self, message: Message | str) -> None:
        message = Message.read(message)
        if message.destination and message.destination != self.state.stored_session_id:
            self.state.held_message = message
            self.state.held_uncertain = False
            self.state.queue_paused = True
            self.state.add(
                "notice",
                "This message belongs to another session. Switch to its destination or restore it for review; nothing was sent.",
            )
            return
        if message.images and (not self.image_store or len(message.images) > self.image_store.max_count):
            self.state.held_message = message
            self.state.add("notice", "Restore this message and remove excess images before sending.")
            return
        self.state.begin(message.label)
        self.state.last_message = message
        self.state.last_prompt = message.prompt
        self.state.rows[-1].images = message.images
        if message.images:
            self.inflight = message
            self.state.image_preparing = True
            self.state.activity = "Preparing images"
            self.save_drafts(force=True)
            self.delivery = ImageDelivery(self.backend, self.state.session_id, message, self.image_store)
        else:
            self.request("prompt.submit", self.scoped(text=message.text))

    def attachment_action(self, action: str) -> None:
        if action == "image-path":
            self.image_path_open = True
            self.state.touch()
            if self.surface:
                self.render()
                self.surface.focus("dock.image-path.attach-path")
        elif action == "image-path-submit":
            if self.image_path.text.strip():
                self.load_image(self.image_path.text)
        elif action == "image-path-close":
            self.image_path_open = False
            self.state.touch()
            if self.surface:
                self.surface.focus("dock.composer")
        elif action == "image-paste":
            self.load_image()
        elif action.startswith("image-remove:"):
            index = int(action.partition(":")[2])
            self.state.draft_images = tuple(
                image for i, image in enumerate(self.state.draft_images) if i != index
            )
            self.state.touch()
        elif action == "image-held-discard":
            self.state.held_message = None
            self.state.held_uncertain = False
            self.state.touch()
        elif action == "image-held-restore" and self.state.held_message:
            if (
                self.draft.text
                or self.state.draft_images
                or self.state.image_loading
                or self.state.busy
                or self.switching
            ):
                self.state.add(
                    "notice", "Finish the current turn and clear your draft before restoring this message."
                )
            else:
                message = self.state.held_message
                self.draft = Draft(message.text, len(message.text))
                self.state.draft_images = message.images
                self.state.held_message = None
                self.state.held_uncertain = False
                self.state.add(
                    "notice", "Message restored for review; press Send only if you want to submit it again."
                )
                for image in message.images:
                    self.register_image(image)
                self.state.touch()
        self.save_drafts(force=True)

    def image_key(self, item: Key) -> bool:
        if self.image_path_open and not (self.state.secret() or self.state.clarify()):
            if item.name == "escape":
                self.attachment_action("image-path-close")
            elif item.name == "enter" and not item.shift:
                self.attachment_action("image-path-submit")
            else:
                self.image_path.key(item)
                self.state.touch()
            return True
        if item.name == "paste" and not item.text:
            self.load_image()
            return True
        if (
            item.name == "paste"
            and item.text
            and not self.draft.text
            and not (self.state.secret() or self.state.clarify() or self.state.sheet or self.state.watch)
            and dropped_image_path(item.text) is not None
        ):
            self.draft.key(item)
            return self.import_dropped_image()
        return False
