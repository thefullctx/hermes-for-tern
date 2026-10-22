"""One image-bearing turn at a time: fence staging, attach, submit, or roll back."""

from __future__ import annotations

import base64
import time
import uuid
from concurrent.futures import Future

from .images import ImageStore
from .messages import Message


class ImageDelivery:
    def __init__(self, backend, session_id: str, message: Message, store: ImageStore):
        self.backend, self.session_id, self.message, self.store = backend, session_id, message, store
        self.paths: list[str] = []
        self.phase = "probe"
        self.future: Future | None = None
        self.deadline = 0.0
        self.index = 0
        self.cancelled = False
        self.done = False
        self.accepted = False
        self.tainted = False
        self.error = ""
        # Verify detach support and that no older staged images could leak into this message.
        self._request("image.detach", path=f"/nonexistent/hermes-tern-probe-{uuid.uuid4().hex}")

    def _request(self, method: str, **params) -> None:
        self.future = self.backend.request(method, {"session_id": self.session_id, **params})
        self.deadline = time.monotonic() + 15

    def _next(self) -> None:
        if self.cancelled:
            self._rollback("Image send stopped; your message was retained.")
        elif self.index < len(self.message.images):
            image = self.message.images[self.index]
            try:
                data = self.store.read(image)
            except (OSError, ValueError):
                self._rollback(
                    f"{image.name}: saved image is missing or changed. Remove it and attach it again."
                )
                return
            self.phase = "attach"
            self._request(
                "image.attach_bytes",
                content_base64=base64.b64encode(data).decode("ascii"),
                filename=image.name,
            )
        else:
            self.phase = "submit"
            self._request("prompt.submit", text=self.message.prompt)

    def _rollback(self, error: str) -> None:
        self.error = error
        if self.paths:
            self.phase = "rollback"
            self._request("image.detach", path=self.paths[-1])
        else:
            self.done = True

    def tick(self) -> None:
        if self.done or self.future is None:
            return
        if not self.future.done():
            if time.monotonic() >= self.deadline:
                if hasattr(self.backend, "forget"):
                    self.backend.forget(self.future)
                self.error = "Hermes did not confirm the image operation. Reconnect before sending again."
                # Even a probe timeout is fenced by reconnect; an attachment might be outstanding elsewhere.
                self.tainted = True
                self.done = True
            return
        try:
            result = self.future.result()
        except Exception:
            if self.phase == "probe":
                self.error = "This Hermes backend does not support safe image attachments. Update Hermes; your message is retained."
                self.done = True
            elif self.phase == "rollback":
                self.error = "Hermes could not clear staged images. Reconnect before sending again."
                self.tainted = self.done = True
            else:
                self._rollback("Hermes rejected the image send. Your message is retained for review.")
            return
        if not isinstance(result, dict):
            self.tainted = self.done = True
            self.error = "Hermes returned an invalid image response. Reconnect before sending again."
            return
        if self.phase == "probe":
            if result.get("count") != 0:
                self.tainted = self.done = True
                self.error = "Hermes has older staged images. Reconnect before sending this message."
            else:
                self._next()
        elif self.phase == "attach":
            path = result.get("path")
            if (
                result.get("attached") is not True
                or not isinstance(path, str)
                or not path
                or result.get("count") != self.index + 1
            ):
                self.tainted = self.done = True
                self.error = "Hermes did not confirm which image was staged. Reconnect before sending again."
                return
            self.paths.append(path)
            self.index += 1
            self._next()
        elif self.phase == "rollback":
            if result.get("detached") is not True or result.get("count") != len(self.paths) - 1:
                self.tainted = self.done = True
                self.error = "Hermes did not confirm image removal. Reconnect before sending again."
                return
            self.paths.pop()
            self._rollback(self.error)
        elif self.phase == "submit":
            if result.get("status") != "streaming":
                self.tainted = self.done = True
                self.error = (
                    "Hermes did not confirm this image turn started. Reconnect and review before resending."
                )
                return
            self.accepted = self.done = True
