# Feature reference

- Native conversation UI with streamed Markdown and code blocks.
- Multi-turn conversations using the real Hermes backend and existing tools.
- Native tool rows with folded output, duration, outcome and optional diffs.
- Reasoning as a thought in faint gold ink that fades and folds to `◇ pondered for 6s` when the answer
  starts (Hermes sends reasoning when its `display.show_reasoning` setting is on).
- Hermes's todo list as the turn's errands: one row whose items dry from gold as they are done, and a
  pill above the composer whose gold ring fills and seals, signed with Hermes's H, when all are
  delivered.
- Context use as a gold hairline on the composer's top edge, amber from 80% and red from 95%.
- Search results (`search_files`) as a file tree with line numbers, each match underlined in gold ink
  that draws itself in.
- Failed turns as `✗ Undelivered`, underlined in red ink, with the provider's code, and a `retry ⏎` key when Hermes marks the
  failure retryable (Enter in an empty composer retries too). Hermes's error surface names the fix
  first — sign in again, the environment variable to set, or when the limit lifts — so a failure it
  did not mark retryable says so rather than offering a key that cannot work.
- Dispatches: short notes above the composer for Hermes's notices, finished background tasks, and a
  reply that was delivered (or failed) while the pane was hidden, shown when you come back.
- Subagents from `delegate_task` as live rows under the call: goal, model, current tool, tool count,
  tokens, time and a one-line summary, with grandchildren nested under their parent.
- Composer typing, multiline paste, Unicode, native selection and basic undo.
- Tool approval cards using the backend's offered choices, answered with the mouse or the keyboard:
  number keys pick a choice, `Enter` allows once, `Escape` denies. An approval Hermes withdraws (an
  interrupt, a session reap, a timeout) leaves the card saying it was denied, so it never waits for
  an answer that can no longer be given.
- Clarification questions: options, multiple selections, free text and skipping, with number keys
  selecting an option while the answer box is still empty. Choices past the ninth stay clickable;
  only the ones a key can reach are numbered.
- Follow-ups composed while Hermes works are queued and sent when the turn ends, shown above the
  composer as `queued · …` up to eight messages. A full queue leaves the next message in the composer.
- Slash commands: Hermes's own (`/compress`, `/undo`, `/context`, `/usage`, `/plan`, `/goal`, …), your
  skills, and this pane's `/model`, `/btw`, `/bg`, `/name`, `/resume`, `/cost`, `/doctor`, `/clear`, `/stop`,
  `/help` and `/quit`. Hermes runs its commands the way its own terminal does (`slash.exec`, then
  `command.dispatch` for skills and commands that need the live session) and refuses what may not run
  mid-turn itself. A short answer is a note; a long one folds to a few lines that open in place.
  `/doctor` checks launcher integrity, backend startup, the capability exchange and Tern components in the background.
- The model sheet: `/model` (or a click on the model name in the status strip) opens Hermes's whole
  catalog as a native sheet over the composer — every model of every provider, with its price and
  the credential a provider without one needs, under a heading per provider. A search field at the
  top shows what you type: typing filters, arrows move, `Enter` switches,
  `Escape` to put it away; clicking a row switches straight away. `/model <name>` switches without
  the sheet. A model Hermes calls expensive asks first; a pick made mid-turn is held for the next
  turn start and says so, and the status strip follows what Hermes reports back.
- A watched subagent: click a child's row to open its live transcript (`subagent.tail`, refreshed),
  with interrupt and a line to steer it (`subagent.steer`; a child past its last tool batch misses it,
  which the dispatch says). Steer is typed while watching, `Enter` sends, `Escape` stops watching.
- Masked answers to the one-string asks Hermes makes mid-turn — a sudo password, a named secret, a
  vault master password, a one-time code — typed in the composer as `••••` and sent with `Enter`; an
  empty answer is Hermes's own word for "skipped", so `Escape` declines. The answer is never drawn,
  never logged and never kept once sent.
- `/btw <question>` asks Hermes beside the work it is doing, over a snapshot of the conversation; the
  answer arrives as a dispatch. `/bg <task>` hands it a task on a fresh agent, answered when done.
- The session's name: `/name <title>` names it through Hermes (which owns titles and echoes them
  back, including when its auto-titler renames it). It sits on the startup screen's context line and,
  quietly, on the status strip.
- Follow-ups held while Hermes works can be taken back: each `queued · …` row carries its own `✕`,
  so a thought typed by mistake never has to be sent.
- A live command list in the composer: typing `/` opens a list right above it, one line per command
  with what it does, Hermes's commands and your skills included (`complete.slash`), and each letter
  narrows it; once a command takes an argument it offers Hermes's choices for that (`/personality `).
  The rest of the highlighted command shows faintly after the caret. ↑/↓ move, Tab completes, Enter
  runs it (`/btw`, `/bg` and `/name` wait for their argument instead), and Escape puts it away. Commands
  that would swap the session out from under the pane, or that drive Hermes's own terminal display,
  are left out; a slash mid-draft stays a slash.
- A thinking-effort ring beside the model name: seven levels from `off` to `max`, stepped by pressing
  the ring, with the new level floating above the composer under a lamp. The ring sends
  `config.set` with `key: "reasoning"` and follows the level Hermes echoes back in `session.info`.
  The ring's `off` travels as Hermes's `none`, because Hermes reads `off` as a display word that
  hides reasoning without stopping thinking. Hermes also has an `ultra` above `max` that Tern's
  ring cannot draw, so it is unreachable from here. Hermes offers no way to ask which levels a
  model honours; it clamps silently to the nearest weaker level and reports the result as
  `reasoning_effort_wire`, which the lamp shows when it differs from what was asked for.
- Tool output longer than 48,000 characters says how much was left out rather than truncating silently.
- Stop and a new prompt after interruption.
- Backend error reporting and orderly terminal restoration.
- Existing model/provider/profile configuration; chat flags `--model`, `--provider`, `--profile` and `--tui`.
- Picking a session back up, as in Hermes's own terminal: `hermes --resume <id or title>`,
  `hermes --resume latest`, `hermes -c` (the most recent session) and `hermes -c "<title>"` open the
  native view with the conversation redrawn (your messages, Hermes's replies and the tools it ran),
  ready for the next message. A session that cannot be found says so and starts a new one. On the way
  out, the pane prints how to come back: `hermes --resume <id>`, and `hermes -c "<title>"` once
  Hermes has named the session.
- Switching sessions inside the pane: `/resume` (or `/sessions`) opens a sheet of Hermes's 50 most
  recent sessions, newest first, each with its title (or how it began), size and age; type to filter,
  arrows to move, `Enter` to pick one up. Its conversation replaces the transcript and this live
  session is closed in Hermes; nothing is lost, since Hermes keeps every session. `/resume <id or
  title>` skips the sheet, and switching asks you to finish or stop a running turn first. A new session's welcome
  offers `Continue “…”` for the last conversation. The sheet lists sessions from every project,
  as Hermes's own picker does: Hermes's listing does not say which folder a session ran in.
- Ordinary Hermes subcommands, including `hermes model`, `hermes doctor` and `hermes --version`,
  pass directly to the original command.

The adapter respects Hermes's approval policy. It does not enable approvals when your policy is off,
and it never automatically approves a request.

## Reliability and recovery

- Queue capacity is eight. Overflow stays in the composer and is explained.
- Stop, interruption and failed turns pause the queue. Continue or discard it explicitly.
- Messages submitted during a session switch wait for the confirmed destination. A failed switch
  returns those messages to the draft; none are sent to the previous session.
- A backend disconnect retains the transcript and ordinary drafts, clears stale questions and
  secret inputs, and marks unfinished work interrupted. Reconnect resumes Hermes's stored session
  without resending earlier work. If that fails, starting a new session is an explicit choice.
- Ordinary drafts and queues are saved privately for their project, profile and stored session.
  Restoring saved text resumes that session and leaves its queue paused. Another live pane's
  recovery record is not offered. Secrets and clarification answers are never saved.
- `/find <text>` searches the full loaded conversation. Next/Previous reveal matching turns,
  including those outside the newest 60. `/find-close` returns to the newest turns.
- `/export [path]` writes Markdown with all loaded turns and available tool output, even output
  beyond the pane's visual limit. Existing files are never overwritten. Connection diagnostics,
  approval answers and secret answers are excluded. Text in ordinary messages or tool output is
  exported as conversation content; export is not a general-purpose secret scanner.
