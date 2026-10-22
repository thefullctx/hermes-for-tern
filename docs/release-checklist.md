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
