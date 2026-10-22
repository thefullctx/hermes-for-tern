import base64
import io
import json
import os
import time

import pytest
from PIL import Image
from tern_sdk import Key

from hermes_for_tern.delivery import ImageDelivery
from hermes_for_tern.editor import Draft
from hermes_for_tern.images import RETENTION, ImageStore, clipboard_image
from hermes_for_tern.messages import ImageRef, Message
from hermes_for_tern.recovery import RecoveryStore
from test_app import FakeBackend, app


def picture(color="red", format="PNG"):
    stream = io.BytesIO()
    Image.new("RGB", (160, 100), color).save(stream, format=format)
    return stream.getvalue()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return ImageStore(tmp_path, None)


@pytest.mark.parametrize("format,media", [("PNG", "image/png"), ("JPEG", "image/jpeg")])
def test_owned_snapshot_preserves_bytes_and_has_private_thumbnail(store, tmp_path, format, media):
    original = tmp_path / "source"
    data = picture(format=format)
    original.write_bytes(data)
    image = store.add_path(original)
    original.write_bytes(b"changed after paste")
    assert store.read(image) == data
    assert image.media == media
    assert (image.width, image.height) == (160, 100)
    assert store.path(image).stat().st_mode & 0o777 == 0o600
    assert store.directory.stat().st_mode & 0o777 == 0o700
    with Image.open(store.preview_path(image)) as preview:
        assert preview.width <= 96 and preview.height <= 72
    assert original.read_bytes() == b"changed after paste"


def test_invalid_oversized_and_changed_images_are_rejected(store, monkeypatch):
    for data in (b"", b"not an image", picture(format="GIF")):
        with pytest.raises(ValueError):
            store.add(data)
    monkeypatch.setattr(store, "max_bytes", 20)
    with pytest.raises(ValueError, match="exceeds"):
        store.add(picture())
    monkeypatch.setattr(store, "max_bytes", 8 << 20)
    image = store.add(picture())
    store.path(image).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="changed"):
        store.read(image)
    monkeypatch.setattr("hermes_for_tern.images.PIXELS", 10)
    with pytest.raises(ValueError, match="megapixel"):
        store.add(picture())


def test_configuration_and_remote_clipboard_fail_with_actionable_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_TERN_IMAGE_MAX_COUNT", "0")
    with pytest.raises(ValueError, match="MAX_COUNT"):
        ImageStore(tmp_path, None)
    monkeypatch.setenv("SSH_CONNECTION", "remote")
    with pytest.raises(ValueError, match="SSH.*Attach image"):
        clipboard_image(100)


def test_linux_clipboard_helper_reads_bytes_and_rejects_oversize(tmp_path, monkeypatch):
    import sys

    source = tmp_path / "clipboard.png"
    data = picture()
    source.write_bytes(data)
    helper = tmp_path / "wl-paste"
    # Stand-in OS helper: real child process, private file, binary stdout; no desktop clipboard access.
    helper.write_text(
        f'#!{sys.executable}\nimport sys\nsys.stdout.buffer.write(open({str(source)!r}, "rb").read())\n'
    )
    helper.chmod(0o700)
    for key in ("SSH_CONNECTION", "SSH_TTY", "SSH_CLIENT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("hermes_for_tern.images.sys.platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "fixture")
    monkeypatch.setenv("PATH", str(tmp_path))
    assert clipboard_image(1 << 20) == data
    with pytest.raises(ValueError, match="size limit"):
        clipboard_image(10)


def test_cleanup_preserves_recovery_references_originals_and_unrecognized_files(store, tmp_path):
    kept = store.add(picture("red"))
    orphan = store.add(picture("blue"))
    original = tmp_path / "original.png"
    original.write_bytes(picture())
    unknown = store.directory / "unknown.txt"
    unknown.write_text("keep")
    symlink = store.directory / ("f" * 64 + ".png")
    symlink.symlink_to(original)
    record = RecoveryStore(tmp_path, None)
    record.save("", [Message("queued", (kept,))], "s", False)
    for path in store.directory.iterdir():
        if not path.is_symlink():
            os.utime(path, (time.time() - RETENTION - 1,) * 2)
    store.collect()
    assert store.path(kept).exists() and store.preview_path(kept).exists()
    assert not store.path(orphan).exists() and not store.preview_path(orphan).exists()
    assert original.exists() and symlink.is_symlink() and unknown.exists()
    record.path.write_text("broken recovery")
    os.utime(store.path(kept), (1, 1))
    store.collect()
    assert store.path(kept).exists()


def test_image_recovery_roundtrip_and_legacy_schema(store, tmp_path):
    image = store.add(picture())
    record = RecoveryStore(tmp_path, None)
    message = Message("describe", (image,), "stored")
    record.save("", [message, "old text"], "stored", True, images=(image,), pending=message)
    data = json.loads(record.path.read_text())
    data.pop("pid")
    record.path.write_text(json.dumps(data))
    restored = record.records()[0]
    assert Message.read(restored["queue"][0]) == message
    assert ImageRef.read(restored["draft_images"][0]) == image
    assert Message.read(restored["pending"]) == message
    assert restored["queue"][1] == "old text"
    data.update(version=1, draft="legacy", queue=["old"], draft_images=[], pending=None)
    record.path.write_text(json.dumps(data))
    assert record.records()[0]["draft"] == "legacy"
    data["draft_images"] = [dict(image.wire(), id="../../outside")]
    record.path.write_text(json.dumps(data))
    assert not record.records()


def resolve(backend, result=None, error=None):
    future = backend.requests[-1][2]
    if error:
        future.set_exception(RuntimeError(error))
    else:
        future.set_result(result)


def staged(store, count=1):
    images = tuple(store.add(picture(color)) for color in ("red", "blue")[:count])
    backend = FakeBackend()
    delivery = ImageDelivery(backend, "live", Message("describe", images), store)
    return delivery, backend, images


def test_delivery_waits_for_each_ack_and_keeps_original_bytes(store):
    delivery, backend, images = staged(store, 2)
    assert backend.requests[-1][0] == "image.detach"
    resolve(backend, {"detached": False, "count": 0})
    delivery.tick()
    for index, image in enumerate(images):
        method, params, _ = backend.requests[-1]
        assert method == "image.attach_bytes" and params["session_id"] == "live"
        assert base64.b64decode(params["content_base64"]) == store.read(image)
        assert not any(method == "prompt.submit" for method, _, _ in backend.requests)
        resolve(backend, {"attached": True, "path": f"/hermes/{index}", "count": index + 1})
        delivery.tick()
    assert backend.requests[-1][:2] == ("prompt.submit", {"session_id": "live", "text": "describe"})
    resolve(backend, {"status": "streaming"})
    delivery.tick()
    assert delivery.done and delivery.accepted and not delivery.tainted


@pytest.mark.parametrize("stop", [True, False])
def test_partial_upload_cancel_or_rejection_rolls_back_before_releasing_queue(store, stop):
    delivery, backend, _ = staged(store, 2)
    resolve(backend, {"count": 0})
    delivery.tick()
    if stop:
        delivery.cancelled = True
        resolve(backend, {"attached": True, "path": "/hermes/1", "count": 1})
        delivery.tick()
    else:
        resolve(backend, {"attached": True, "path": "/hermes/1", "count": 1})
        delivery.tick()
        resolve(backend, error="rejected second image")
        delivery.tick()
    assert backend.requests[-1][:2] == ("image.detach", {"session_id": "live", "path": "/hermes/1"})
    assert not delivery.done
    resolve(backend, {"detached": True, "count": 0})
    delivery.tick()
    assert delivery.done and not delivery.accepted and not delivery.tainted
    assert not any(method == "prompt.submit" for method, _, _ in backend.requests)


@pytest.mark.parametrize("phase", ["probe", "attach", "rollback", "submit"])
def test_timeout_in_any_phase_requires_reconnect_and_never_replays(store, phase, monkeypatch):
    delivery, backend, _ = staged(store)
    if phase != "probe":
        resolve(backend, {"count": 0})
        delivery.tick()
    if phase in ("rollback", "submit"):
        delivery.cancelled = phase == "rollback"
        resolve(backend, {"attached": True, "path": "/hermes/1", "count": 1})
        delivery.tick()
    assert delivery.phase == phase
    monkeypatch.setattr("hermes_for_tern.delivery.time.monotonic", lambda: delivery.deadline + 1)
    delivery.tick()
    count = len(backend.requests)
    delivery.tick()
    assert delivery.done and delivery.tainted and not delivery.accepted
    assert len(backend.requests) == count


@pytest.mark.parametrize("reply", [{"count": 1}, None, {"count": "0"}])
def test_unknown_staging_or_invalid_probe_never_attaches(store, reply):
    delivery, backend, _ = staged(store)
    resolve(backend, reply)
    delivery.tick()
    assert delivery.tainted and delivery.done
    assert len(backend.requests) == 1


def test_unsupported_backend_and_missing_owned_file_retain_message_without_submit(store):
    delivery, backend, _ = staged(store)
    resolve(backend, error="method unsupported")
    delivery.tick()
    assert delivery.done and not delivery.tainted and "Update Hermes" in delivery.error
    delivery, backend, images = staged(store)
    store.path(images[0]).unlink()
    resolve(backend, {"count": 0})
    delivery.tick()
    assert delivery.done and "missing" in delivery.error
    assert len(backend.requests) == 1


def image_app(store):
    instance = app()
    instance.image_store = store
    instance.store = RecoveryStore(store.directory.parent, None)
    instance.state.ready = True
    instance.state.session_id = "live"
    instance.state.stored_session_id = "stored"
    return instance


def test_native_empty_paste_captures_snapshot_and_queue_does_not_stage(store):
    instance = image_app(store)
    original = picture("red")
    instance.clipboard_reader = lambda _: original
    instance.input(Key(name="paste", text=""))
    instance.image_load.result(timeout=2)
    instance.poll_images()
    images = instance.state.draft_images
    assert len(images) == 1 and store.read(images[0]) == original
    instance.state.busy = True
    instance.submit("queued image")
    assert instance.state.queued == [Message("queued image", images, "stored")]
    assert not instance.state.draft_images and not instance.backend.requests
    instance.clipboard_reader = lambda _: picture("blue")
    assert store.read(images[0]) == original
    instance.state.busy = False
    instance.send_queued()
    assert instance.delivery.message.images == images


def test_queue_overflow_keeps_text_and_images_and_secrets_do_not_read_clipboard(store):
    instance = image_app(store)
    image = store.add(picture())
    instance.state.draft_images = (image,)
    instance.state.busy = True
    for i in range(8):
        instance.state.queue(str(i))
    instance.submit("full queue")
    assert instance.draft.text == "full queue" and instance.state.draft_images == (image,)
    from hermes_for_tern.state import Question

    instance.state.questions["secret"] = Question("secret", "secret", {})
    instance.clipboard_reader = lambda _: pytest.fail("secret answer must never read the clipboard")
    instance.load_image()
    assert instance.image_load is None


def test_image_only_send_and_disconnect_are_retained_for_explicit_review(store):
    instance = image_app(store)
    image = store.add(picture())
    instance.state.draft_images = (image,)
    instance.submit()
    assert instance.inflight.images == (image,) and not instance.state.draft_images
    assert instance.inflight.prompt == "[User attached images]"
    instance.fail("connection lost")
    assert instance.state.held_message.images == (image,) and instance.state.held_uncertain
    assert instance.delivery is None
    instance.attachment_action("image-held-restore")
    assert instance.state.draft_images == (image,) and not instance.state.held_message
    assert not instance.state.held_uncertain
    assert len(instance.backend.requests) == 1


def test_retained_message_cannot_be_overwritten_or_auto_sent_to_another_session(store):
    instance = image_app(store)
    held = Message("other session", (store.add(picture()),), "other")
    instance.state.queue(held)
    instance.send_queued()
    assert instance.state.held_message == held and not instance.backend.requests
    instance.draft = Draft("new draft", 9)
    instance.submit()
    assert instance.state.held_message == held and instance.draft.text == "new draft"
    instance.state.queue("another")
    instance.continue_queue()
    assert not instance.backend.requests and len(instance.state.queued) == 1
    instance.attachment_action("image-held-discard")
    assert instance.state.held_message is None and not instance.state.held_uncertain


def test_failed_image_upload_holds_message_and_pauses_followups(store):
    instance = image_app(store)
    image = store.add(picture())
    instance.state.draft_images = (image,)
    instance.submit("first")
    instance.submit("second")
    resolve(instance.backend, error="unsupported")
    instance.poll_images()
    assert instance.state.held_message.text == "first"
    assert instance.state.queue_paused and not instance.state.busy
    assert instance.state.queued[0].text == "second"
    assert len(instance.backend.requests) == 1


def test_completion_before_submit_ack_cannot_drain_queue_early(store):
    instance = image_app(store)
    instance.state.draft_images = (store.add(picture()),)
    instance.submit("image")
    resolve(instance.backend, {"count": 0})
    instance.poll_images()
    resolve(instance.backend, {"attached": True, "path": "/hermes/1", "count": 1})
    instance.poll_images()
    instance.submit("followup")
    instance.backend.events.put(
        {
            "method": "event",
            "params": {
                "session_id": "live",
                "type": "message.complete",
                "payload": {"status": "complete", "text": "answer"},
            },
        }
    )
    instance.read_backend()
    assert len(instance.state.queued) == 1
    assert instance.inflight is not None
    resolve(instance.backend, {"status": "streaming"})
    instance.poll_images()
    assert not instance.state.queued and instance.inflight is None
    assert instance.backend.requests[-1][:2] == ("prompt.submit", {"session_id": "live", "text": "followup"})


@pytest.mark.parametrize("bad", [{"status": "queued"}, {"status": "steered"}, {}])
def test_unexpected_submit_status_fences_all_followups(store, bad):
    instance = image_app(store)
    instance.state.draft_images = (store.add(picture()),)
    instance.submit("image")
    resolve(instance.backend, {"count": 0})
    instance.poll_images()
    resolve(instance.backend, {"attached": True, "path": "/hermes/1", "count": 1})
    instance.poll_images()
    instance.submit("followup")
    resolve(instance.backend, bad)
    instance.poll_images()
    assert instance.state.failed and instance.state.held_uncertain
    assert instance.state.held_message.text == "image"
    assert instance.state.queued[0].text == "followup"
    assert instance.delivery is None


def test_rejected_submit_clears_all_staged_images_in_reverse_order(store):
    delivery, backend, _ = staged(store, 2)
    resolve(backend, {"count": 0})
    delivery.tick()
    for index in range(2):
        resolve(backend, {"attached": True, "path": f"/hermes/{index}", "count": index + 1})
        delivery.tick()
    resolve(backend, error="prompt rejected")
    delivery.tick()
    for index in (1, 0):
        assert backend.requests[-1][1]["path"] == f"/hermes/{index}"
        resolve(backend, {"detached": True, "count": index})
        delivery.tick()
    assert delivery.done and not delivery.tainted and not delivery.accepted


def test_failed_rollback_or_unknown_attach_ack_requires_reconnect(store):
    delivery, backend, _ = staged(store)
    resolve(backend, {"count": 0})
    delivery.tick()
    resolve(backend, {"attached": True, "count": 1})
    delivery.tick()
    assert delivery.tainted and delivery.done
    delivery, backend, _ = staged(store)
    resolve(backend, {"count": 0})
    delivery.tick()
    delivery.cancelled = True
    resolve(backend, {"attached": True, "path": "/hermes/1", "count": 1})
    delivery.tick()
    resolve(backend, {"detached": False, "count": 1})
    delivery.tick()
    assert delivery.tainted and delivery.done


def test_recovery_missing_file_shows_notice_and_cannot_submit(store):
    instance = image_app(store)
    image = store.add(picture())
    store.path(image).unlink()
    instance.restoring = {"draft": "", "queue": [], "draft_images": [image.wire()], "pending": None}
    instance.store = None
    instance.apply_restored()
    assert instance.state.draft_images == (image,)
    assert any("saved image unavailable" in row.text for row in instance.state.rows)
    instance.submit()
    resolve(instance.backend, {"count": 0})
    instance.poll_images()
    assert instance.state.held_message.images == (image,)
    assert len(instance.backend.requests) == 1


def test_retrying_switch_cannot_overflow_existing_destination_queue(store):
    instance = image_app(store)
    image = store.add(picture())
    for i in range(8):
        instance.state.queue(Message(str(i), (image,), "other"))
    instance.resume_session("other")
    assert instance.switching == "other"
    instance.state.draft_images = (image,)
    instance.submit("overflow")
    assert instance.draft.text == "overflow" and instance.state.draft_images == (image,)
    assert not instance.held_switch
    instance.switch_failed()
    assert len(instance.state.queued) == 8


def test_editing_during_async_restore_preserves_new_draft_and_original_record(store):
    instance = image_app(store)
    record = {"draft": "saved text", "queue": [], "draft_images": []}
    instance.restoring = record
    instance.saved_records = [record]
    instance.draft = Draft("typed while resuming", 20)
    instance.submit()
    assert not instance.backend.requests
    instance.apply_restored()
    assert instance.draft.text == "typed while resuming"
    assert instance.saved_records == [record] and instance.restoring is None


def test_path_field_undo_and_stale_send_never_change_the_message(store):
    from tern_sdk import SendEvent, UndoEvent

    instance = image_app(store)
    instance.draft = Draft("message", 7)
    instance.draft.insert(" retained")
    instance.image_path_open = True
    instance.image_path.insert("/tmp/image.png")
    instance.input(UndoEvent(raw={}, ev="undo", sf="dock", id="dock.image-path.attach-path"))
    assert instance.image_path.text == "" and instance.draft.text == "message retained"
    instance.image_path_open = False
    instance.input(
        SendEvent(raw={}, ev="send", sf="dock", id="dock.image-path.attach-path", text="/tmp/missing.png")
    )
    assert instance.image_load is None and instance.draft.text == "message retained"
