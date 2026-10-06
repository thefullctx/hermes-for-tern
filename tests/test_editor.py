from tern_sdk import EditEvent, Key

from hermes_for_tern.editor import Draft


def edit(**values):
    return EditEvent(ev="edit", sf="s1", raw={}, id="dock.composer", **values)


def test_emoji_native_replacement_and_undo_use_utf16_offsets():
    draft = Draft("A😀B", 3)
    assert draft.caret == 4
    draft.edit(edit(from_=1, to=3, text="🌙", cursor=3, len=4))
    assert draft.text == "A🌙B"
    assert draft.cursor == 2
    draft.undo()
    assert draft.text == "A😀B"
    assert draft.cursor == 3


def test_stale_native_edit_is_ignored():
    draft = Draft("changed", 7)
    draft.edit(edit(from_=0, to=3, text="new", cursor=3, len=3))
    assert draft.text == "changed"


def test_multiline_paste_is_one_undo_step():
    draft = Draft()
    draft.key(Key(name="paste", text="one\ntwo 😀"))
    assert draft.text == "one\ntwo 😀"
    draft.undo()
    assert draft.text == ""
