# Changelog

## Unreleased

### Image attachments

- Handle Tern's empty image-paste signal with a bounded local clipboard snapshot; add Paste image,
  Attach image and `/attach` / `/paste-image` controls for PNG/JPEG.
- Preview, remove and send images with or without text. Keep immutable image snapshots with queued
  messages and their session destination; queue overflow retains both text and images.
- Upload through Hermes's existing attachment API immediately before the associated prompt, with
  acknowledged staging, rollback on cancellation/rejection, and reconnection after uncertain sends.
- Extend private recovery to image drafts, queues and retained sends, preserving old text records.
  Detect missing snapshots and clean up only old, unreferenced adapter-owned copies.
- Include image filenames in search and metadata in Markdown export; do not embed image bytes.

### Reliability and recovery

- Preserve all queued messages when the queue reaches eight; leave overflow in the composer.
- Hold messages during session switches and route them only to the confirmed destination.
- Pause the queue after Stop, interruption, failed turns, or a backend disconnect.
- Offer reconnect-and-resume without replaying uncertain work; keep new sessions an explicit
  choice if recovery fails.
- Save unsent ordinary drafts and queues privately by project/profile/session. Offer explicit
  restoration after restart, and leave restored queues paused. Exclude secret and clarification
  answers and ignore records owned by another live pane.
- Ignore stale session responses and release timed-out transport requests.
- Compare animation and interaction timers against deadlines so exact boundaries do not depend on
  floating-point rounding of the runner's uptime.
- Keep blocked backend pipe writes off the UI thread and terminate owned backend processes on exit.

### Conversation tools

- Search all loaded turns with `/find`, navigate matches and return to the latest transcript.
- Export loaded conversation and available tool output as Markdown without overwriting files.
- Expand `/doctor` and `--doctor` to check launcher integrity and a bounded, session-free backend
  handshake. Do not print arbitrary backend errors, stderr or credentials in the doctor report.

### Validation and documentation

- Expand CI to macOS/Linux on Python 3.11, 3.13 and 3.14. Run once per PR update and on pushes to
  main, avoiding duplicate branch-push and PR checks.
- Add regression coverage for failure, recovery, persistence, search/export and transport hangs.
- Separate the quick-start README from feature and visual-design references; add a release checklist.
