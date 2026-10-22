# Release checklist and validation record

This project is public. These checks guide updates to the existing release; incomplete live
coverage must be reported honestly rather than implied by passing controlled tests.

## Before merging

- [ ] All six CI combinations pass: macOS/Linux × Python 3.11/3.13/3.14.
- [ ] Tests, lint and formatting pass for the final commit.
- [ ] Review recovery, paused queue and search screenshots from native Tern.
- [ ] Confirm no changes to Hermes configuration, credentials, dependencies or approval policy.
- [ ] Review the changelog and recovery-storage documentation.

## Automated failure checks

- [x] Queue overflow keeps the draft and all previously queued messages.
- [x] Stop/interruption/failure pause queued work, including a completion racing with Stop.
- [x] A session switch holds messages for its destination; failure/timeout keeps them unsent.
- [x] Old-session replies cannot change the new session's UI.
- [x] Disconnect clears stale questions and secrets and marks unfinished tools interrupted.
- [x] Reconnect resumes without resending work; failed recovery does not create a session silently.
- [x] Saved text is private, isolated by project/profile/session, and restored with its queue paused.
- [x] Recovery ignores corrupt files and records still owned by a live pane.
- [x] Search reveals older turns; exports retain full available output and never overwrite files.
- [x] Doctor performs a bounded handshake without creating an agent session or printing raw errors.
- [x] A backend that stops reading cannot block UI request submission or orderly termination.

## Local validation — 22 October 2026

- macOS, Python 3.11.15 / 3.13.14 / 3.14.6: automated suite exercised locally.
- Lint, formatting, and source/wheel builds passed locally.
- Tern 0.8.3 (`e2b8656`), Hermes `v0.21.7+64.ga6c72dd`, pinned Tern SDK 0.1.0.
- Live Hermes: backend startup, advertised approval/clarification support, session creation,
  and resume through a fresh backend succeeded. A minimal model turn returned an error, so a
  successful live model reply, real tool execution and live delegation remain unverified in this run.
- The real frontend was started inside Tern, and `/doctor` confirmed required native components,
  dock support, launcher integrity and the backend handshake without stylesheet errors.
- Approvals/clarification, interruption with a paused queue, delegation display, crash/reconnect,
  search, and frontend restart with restored draft/queue passed in actual headless Tern using the
  controlled backend. Light/dark appearances and a 560px pane were inspected. These checks do not
  establish real-provider behavior.
- Linux runs are covered by CI, not this macOS workstation.

## Repeatable native checks

Run `uv run python scripts/check_native.py` with Tern installed. It starts an isolated headless
Tern, drives the real frontend, and writes screenshots to `work/native-qa/serve`. It uses the
controlled backend in `tests/fixtures/recovery_backend.py`, makes no model calls and runs no tools. `approval` opens an approval, `clarify` opens a
question, `wait` holds a turn until Stop, `crash` terminates the gateway, and any other prompt
completes a scripted delegation. Reconnect resumes its fixed QA session.

The new recovery/search controls use the existing palette, button styles and composer width,
with wrapping in narrow panes. No new animation is added. Existing reduced-motion and hidden-pane
rules remain in effect.

## Follow-up live checks when the model provider is available

- [ ] Stream a real answer, then ask a follow-up using prior context.
- [ ] Exercise a harmless real tool with the existing approval policy; check allow/deny if offered.
- [ ] Exercise real delegation, child watching and interruption.
- [ ] Stop during a real tool call with a queued follow-up; confirm no follow-up starts.
- [ ] Disconnect the backend and resume the same stored session without replaying a prompt.
- [ ] Restart the frontend with an unsent ordinary draft and queue; restore them without sending.

## Native screenshots

These use the controlled backend and the real frontend, not a browser mockup.

[Backend recovery](../assets/reliability/recovery.png) ·
[Paused queue](../assets/reliability/queue-paused.png) ·
[Narrow search in dark appearance](../assets/reliability/search-narrow-dark.png) ·
[Draft and queue restored after frontend termination](../assets/reliability/draft-restored.png)

## Image milestone validation — 22 October 2026

- 197 automated tests pass locally on Python 3.11, 3.13 and 3.14. Coverage includes immutable image
  snapshots, private storage, limits, missing files, old/new recovery, queue capacity, destination
  routing, staged upload acknowledgement, reverse rollback, timeout fencing, and completion races.
- Real Tern 0.8.3 (`e2b8656`) with the pinned SDK was traced: image-only clipboard paste produces
  `Key(name="paste", text="")`. The SDK still carries no image bytes, so no SDK bump was needed.
- The real App passed native path import/removal, paste handling, image-only send, queued image
  association, a subsequent text-only message, four previews in a 560px dark pane, and forced
  frontend restart with an image draft restored without sending. Styles loaded without errors.
  Headless Tern uses a process-local clipboard; these repeatable checks inject fixture bytes into
  the OS clipboard reader while exercising the native paste signal. They do not replace the
  user's system clipboard or establish real OS clipboard extraction.
- Live Hermes `v0.21.7+64.ga6c72dd` acknowledged an image upload (160×100), successfully detached it,
  accepted a second image turn, and reported empty staging afterward. The model turn returned a
  provider error, so successful vision interpretation remains unverified. Credentials, provider
  settings, approvals and the Hermes installation were not changed.
- Pillow is locked as an adapter dependency; the wheel includes the new modules and stylesheet.
  Source/wheel builds, lint and formatting pass. Linux OS clipboard behavior needs manual testing;
  CI covers the Python implementation on macOS/Linux.

Before treating issue #20 as fully verified:

- [ ] Copy an image in a desktop app, paste it into regular Tern, and verify the preview reflects
  the copied image. Repeat after changing the clipboard while an earlier image is queued.
- [ ] Send it to a working vision model and confirm an answer about its actual contents.
- [ ] Exercise OS clipboard import on Linux Wayland/X11, or confirm the visible path fallback.

The image PR addresses #20 without automatically closing it while these checks remain open.

[Image draft in a narrow pane](../assets/images/draft-narrow.png) ·
[Four previews in dark appearance](../assets/images/multiple-narrow-dark.png) ·
[Image draft restored after frontend termination](../assets/images/draft-restored.png)

## Image drop fix — 23 October 2026

- 215 automated tests pass, including native edit/paste/send paths, shell-escaped screenshot
  filenames, quoted paths, local file URLs, failed imports and captions typed during import.
- Actual headless Tern file drag-and-drop onto the composer imported a screenshot with spaces
  in its name. Sending the preview delivered one image through the controlled backend, without
  command dispatch. Pasting the equivalent escaped path also passed.
- The native image/queue/restart checks continue to pass. This confirms local file drops;
  the earlier OS bitmap clipboard and successful vision-provider checks remain separate.

## Shared gateway startup fix — 23 October 2026

- Reproduced the startup disconnect on Hermes 0.22.4: the standalone stdio entry refused to open
  a profile already owned by a running gateway. No gateway was stopped and no lock was removed.
- The default backend now uses the official `ui-tui.scripts.gateway_bootstrap` through the original
  managed launcher. Bootstrap tickets remain private; the adapter connects to the authenticated
  loopback WebSocket. Explicit backend overrides are unchanged. Only a missing bootstrap module
  permits the older stdio path; discovery, authentication and dependency failures do not.
- The 236-test suite includes launch policy preservation, unique session creation, history resume,
  profile binding, viewer-only detach, image admission/rollback, owner confirmation tokens, exact
  prompt/generation responses, queued-input cancellation, event gaps and connection loss. Real
  socket tests verify the shared server still accepts viewers after frontend close, and that no
  stdin reader pins a disconnected bridge process.
- Tern 0.9.5 with the real Hermes 0.22.4 gateway: native startup, `/doctor`, orderly frontend exit,
  and unchanged gateway instance identity afterward passed. Fresh-viewer resume and two-image
  upload/rollback also passed. The controlled native image/recovery suite continues to pass.
- A bounded, tool-free live text prompt was admitted and its error outcome persisted/resumed.
  The provider still returned an error; successful model output remains unverified. Credentials,
  provider configuration, approval policy and the Hermes installation were not changed.

## Full-resolution image preview fix — 23 October 2026

- Clickable images now register the validated original PNG/JPEG and its media type with Tern,
  rather than the 96×72 thumbnail. Inline chat images keep their existing size.
- All 239 tests pass on Python 3.11, 3.13 and 3.14, including original bytes/dimensions/media type,
  missing-thumbnail support, missing-original rejection and unchanged inline dimensions.
  Lint, formatting and source/wheel builds pass.
- The real App in headless Tern opens a detailed 1800×1200 fixture at fit-to-window and 100% zoom.
  Viewer state confirms the original dimensions, and screenshots show readable detail. The native
  image, queue, approval, clarification, crash/resume, search and draft restoration checks pass.

[Fit-to-window preview](../assets/images/image-preview-fit.png) ·
[Preview at 100%](../assets/images/image-preview-100.png)
