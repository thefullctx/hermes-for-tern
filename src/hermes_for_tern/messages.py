"""Immutable message snapshots: images travel with their text through queues and recovery."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ImageRef:
    id: str
    name: str
    media: str
    width: int
    height: int
    size: int

    def wire(self) -> dict:
        return vars(self).copy()

    @classmethod
    def read(cls, value: dict) -> ImageRef:
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("id"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", value["id"])
        ):
            raise ValueError("Invalid saved image reference")
        if value.get("media") not in ("image/png", "image/jpeg"):
            raise ValueError("Unsupported saved image type")
        if not isinstance(value.get("name"), str) or not value["name"]:
            raise ValueError("Invalid saved image name")
        if any(type(value.get(k)) is not int or value[k] <= 0 for k in ("width", "height", "size")):
            raise ValueError("Invalid saved image dimensions or size")
        return cls(**{key: value[key] for key in cls.__dataclass_fields__})


@dataclass(frozen=True)
class Message:
    text: str
    images: tuple[ImageRef, ...] = ()
    destination: str | None = None

    @property
    def label(self) -> str:
        return self.text or f"{len(self.images)} image{'s' if len(self.images) != 1 else ''} attached"

    @property
    def prompt(self) -> str:
        return self.text or "[User attached images]"

    def wire(self) -> str | dict:
        # Text-only records remain readable by old tools; recovery v2 adds image records.
        return (
            {
                "text": self.text,
                "images": [image.wire() for image in self.images],
                "destination": self.destination,
            }
            if self.images or self.destination
            else self.text
        )

    @classmethod
    def read(cls, value: str | dict | Message) -> Message:
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            return cls(value)
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("text"), str)
            or not isinstance(value.get("images"), list)
        ):
            raise ValueError("Invalid saved message")
        destination = value.get("destination")
        if destination is not None and not isinstance(destination, str):
            raise ValueError("Invalid saved message destination")
        return cls(value["text"], tuple(ImageRef.read(image) for image in value["images"]), destination)
