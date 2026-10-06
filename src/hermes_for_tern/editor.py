"""Small composer state machine; Tern owns drawing, selection and soft wrapping."""

from __future__ import annotations

from dataclasses import dataclass, field

from tern_sdk import EditEvent, Key


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def codepoint_at(text: str, offset: int) -> int:
    units = 0
    for index, char in enumerate(text):
        if units >= offset:
            return index
        units += utf16_len(char)
    return len(text)


@dataclass
class Draft:
    text: str = ""
    cursor: int = 0
    history: list[tuple[str, int]] = field(default_factory=list)

    @property
    def caret(self) -> int:
        return utf16_len(self.text[: self.cursor])

    def replace(self, start: int, end: int, text: str) -> None:
        self.history.append((self.text, self.cursor))
        self.history = self.history[-100:]
        self.text = self.text[:start] + text + self.text[end:]
        self.cursor = start + len(text)

    def insert(self, text: str) -> None:
        self.replace(self.cursor, self.cursor, text)

    def undo(self) -> None:
        if self.history:
            self.text, self.cursor = self.history.pop()

    def edit(self, event: EditEvent) -> None:
        # Native edit events refer to the version of text Tern saw, in UTF-16 units.
        if event.len is not None and event.len != utf16_len(self.text):
            return
        start = codepoint_at(self.text, event.from_ or 0)
        end = codepoint_at(self.text, event.to or 0)
        if start != end or event.text:
            self.replace(start, end, event.text or "")
        if event.cursor is not None:
            self.cursor = codepoint_at(self.text, event.cursor)

    def key(self, key: Key) -> None:
        if key.name == "paste" and key.text:
            self.insert(key.text)
        elif key.name == "backspace" and self.cursor:
            start = self.cursor - 1
            if key.alt or key.ctrl:
                while start > 0 and self.text[start].isspace():
                    start -= 1
                while start > 0 and not self.text[start - 1].isspace():
                    start -= 1
            self.replace(start, self.cursor, "")
        elif key.name == "delete":
            self.replace(self.cursor, min(len(self.text), self.cursor + 1), "")
        elif key.name == "left" or (key.ctrl and key.name == "b"):
            self.cursor = max(0, self.cursor - 1)
        elif key.name == "right" or (key.ctrl and key.name == "f"):
            self.cursor = min(len(self.text), self.cursor + 1)
        elif key.name == "home" or (key.ctrl and key.name == "a"):
            self.cursor = 0
        elif key.name == "end" or (key.ctrl and key.name == "e"):
            self.cursor = len(self.text)
        elif key.ctrl and key.name == "u":
            self.replace(0, self.cursor, "")
        elif key.ctrl and key.name == "k":
            self.replace(self.cursor, len(self.text), "")
        elif key.name == "enter":
            self.insert("\n")
        elif key.text is not None and not (key.ctrl or key.alt or key.meta):
            self.insert(key.text)
