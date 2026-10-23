"""Model and session sheet keyboard handling and model switching."""

from __future__ import annotations

from .sheet_views import sheet_rows, sheet_cursor


class Sheets:
    def model_event(self, action: str, value: str = "") -> None:
        """The model sheet's own events: `open` from the model segment, `pick` from a row,
        `confirm` from its switch key, `cancel` from its cancel key or the backdrop."""
        if self.state.failed or self.switching:
            return
        if action == "open":
            if self.state.session_id:
                self.request("model.options", self.scoped(refresh=True))
            self.state.open_sheet()
        elif action == "pick":
            if value and self.state.sheet and self.state.sheet.kind == "sessions":
                self.resume_session(value)
            elif value:
                self.state.choose_row(value)
                self.switch_model(value)
        elif action == "confirm":
            self.confirm_model()
        elif action == "cancel":
            self.state.close_sheet()

    def sheet_key(self, item) -> bool:
        """The model sheet owns the keyboard while it is open: type to filter, arrows to move the
        cursor, Enter to switch, Escape to put it away. True when the key was the sheet's."""
        sheet = self.state.sheet
        if self.state.secret() or self.state.clarify():
            return False
        if sheet is None or item.ctrl or item.alt or item.meta:
            return False
        if item.name == "escape":
            self.state.close_sheet()
        elif item.name == "enter":
            self.confirm_model()
        elif item.name == "backspace":
            self.state.filter_sheet(sheet.query[:-1])
        elif item.name in ("up", "down"):
            self.move_sheet(item.name == "down")
        elif item.text:
            self.state.filter_sheet(sheet.query + item.text)
        else:
            return False
        return True

    def move_sheet(self, down: bool) -> None:
        rows = [row["id"] for row in sheet_rows(self.state)]
        if not rows:
            return
        at = rows.index(self.state.sheet.chosen) if self.state.sheet.chosen in rows else -1
        self.state.choose_row(rows[max(0, min(len(rows) - 1, at + (1 if down else -1)))])

    def confirm_model(self) -> None:
        """The switch key: the chosen row, or the session's own model when nothing was chosen."""
        if not self.state.sheet:
            return
        if self.state.sheet.kind == "sessions":
            self.resume_session(sheet_cursor(self.state))
            return
        if self.state.sheet.confirming:
            self.switch_model(self.state.sheet.chosen, confirmed=True)
            return
        chosen = sheet_cursor(self.state)
        if chosen:
            self.switch_model(chosen)

    def switch_model(self, value: str, *, confirmed: bool = False) -> None:
        """Ask Hermes to run another model. Mid-turn it holds the pick for the next turn start and
        reports it in session.info; an expensive model asks first, which is what `confirmed` is."""
        if self.state.failed or self.switching:
            return
        if not value or not self.state.session_id:
            return
        if self.state.sheet:
            # The sheet's row is the pending pick, so a confirmation repeats the same one.
            self.state.choose_row(value)
        params: dict = {"key": "model", "value": value}
        if confirmed:
            params["confirm_expensive_model"] = True
        self.request("config.set", self.scoped(**params))

    def model_set(self, result: dict) -> None:
        """Hermes answered a model switch: either it asks whether the expensive pick is worth it,
        or it holds the pick for the next turn because this one is still running."""
        if result.get("confirm_required"):
            if self.state.sheet:
                self.state.sheet.confirming = True
            self.state.add(
                "notice",
                str(result.get("confirm_message") or "Hermes asks whether this model is worth it."),
            )
            return
        self.state.close_sheet()
        if result.get("deferred"):
            self.state.dispatch("model", "model switches when this turn ends", str(result.get("value") or ""))
        if result.get("warning"):
            self.state.add("notice", str(result["warning"]))
