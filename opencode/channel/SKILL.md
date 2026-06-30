---
name: channel
description: Connect this agent to another Codex, Claude Code, or OpenCode agent through the shared /tmp/claude-channels named-channel file protocol. Use when the user asks to open, join, watch, poll, send on, leave, or coordinate with another agent over a channel.
---

# Channel

Use a shared append-only NDJSON file to talk with another agent in a separate session. This is compatible with Codex, Claude Code, and OpenCode channel skills: all agents share `/tmp/claude-channels/<channel>.ndjson`, and each participant has its own cursor.

## User Interface

The user should invoke this as a skill, for example `$channel vrp gpt`, or ask in
natural language to join, watch, send on, poll, or leave a channel. Do not ask
the user to run the Python helper. The helper commands below are internal
implementation details for the agent to execute.

## Important Model

Agents are turn-based. Do not start a background watcher and expect its output to enter context unless the host explicitly has a persistent monitor tool that feeds output back into the conversation. The receive primitive is `listen` or `wait`: on macOS these block on filesystem events with `kqueue`, so they wake when the channel file changes instead of polling on a fixed sleep. On platforms without a stdlib filesystem watcher, the helper falls back to a short bounded sleep interval.

> **OpenCode: watch with a background tool if you have one, else foreground `listen`.**
> Background watching needs the harness to feed watcher output back into context;
> OpenCode does this via experimental tools (PR
> <https://github.com/anomalyco/opencode/pull/33806>). In preference order:
>
> - **`monitor`** — arm `python3 <HELPER> stream <channel> <agent>`. `stream` prints
>   one line per message and never exits, so `monitor` wakes you inline on each
>   message, no re-arm. `background_stop` to stop (it also self-exits on peer leave).
> - **`bash_background`** — wake-on-exit, like Claude's `run_in_background`. Arm
>   `python3 <HELPER> wait <channel> <agent> --timeout 0`; it wakes you when `wait`
>   exits on the next message (read its logfile), then re-arm. `background_stop` to stop.
> - **neither** (stock OpenCode) — foreground `listen --timeout 30`, re-run while
>   waiting. `wait` runs but OpenCode won't wake you on its exit.

While joined to a live channel, treat listening as active work. Use an explicit bounded timeout such as `--timeout 30` so the command returns cleanly in turn-based harnesses. Do not stop after one empty poll if the user is waiting for the peer; run another `listen --timeout 30` call unless the user asks you to stop, the peer leaves, or the channel task is clearly complete.

Shell variables do not persist between tool calls in many agent harnesses. Resolve the channel name and your agent name once, tell the user which name you adopted, then pass those literal values to every helper command.

## Arguments

Infer these from the user's request:

- `channel`: required. If missing, ask the user for the channel name and stop.
- `agent name`: optional. If missing, choose a readable unique name such as `<agent>-<cwd-basename>-<short-random>`.

**OpenCode name-collision warning:** OpenCode often sets the session title to the
user's prompt text, so two agents handed the same prompt (e.g. "watch channel
tst") would adopt the same name and silently lose each other's messages. Do NOT
use the raw session title as the agent name — use the helper's `name` command
(always a unique random suffix) or the per-session slug.

The two agents on a channel must use different agent names. If a generated name might collide, ask the user for an explicit one.

## Agent-Internal Helper

`<HELPER>` is the bundled `scripts/channel.py` shipped alongside this `SKILL.md`.
Resolve its **absolute** path once and pass that literal path to every command
below (shell variables do not persist between tool calls in many harnesses, so
do not rely on an exported `$HELPER`).

Use the bundled helper internally:

```bash
python3 <HELPER> <command> <channel> <agent> [args...]
```

Commands:

```bash
# Create the channel file and set this agent's cursor to the current end.
python3 <HELPER> setup <channel> <agent>

# Append a JSON message. The helper handles JSON escaping and newlines.
python3 <HELPER> send <channel> <agent> "hello"

# Read existing transcript without moving the cursor.
python3 <HELPER> history <channel> <agent>

# Wait briefly for new peer messages, then print them and advance the cursor.
python3 <HELPER> poll <channel> <agent> --timeout 30

# Listen for live peer messages. Use this after joining, after sending, or whenever
# the user expects a response. On macOS this is filesystem-event backed.
python3 <HELPER> listen <channel> <agent> --timeout 30

# Block until a peer message arrives, then exit. Use in the background only when
# the harness re-invokes the agent after background command completion.
python3 <HELPER> wait <channel> <agent>

# Stream peer messages to stdout FOREVER, one line each (never exits until a peer
# leaves). Arm this under a per-line monitor tool (OpenCode's `monitor`) so each
# message wakes you inline with no re-arm. See the OpenCode note above.
python3 <HELPER> stream <channel> <agent>

# Start a zero-inference watcher in the background. It writes a watch log and,
# on macOS, posts desktop notifications for peer messages without advancing the
# normal poll/listen cursor.
python3 <HELPER> watch-start <channel> <agent>

# Inspect or stop the background watcher.
python3 <HELPER> watch-status <channel> <agent>
python3 <HELPER> watch-log <channel> <agent> --lines 20
python3 <HELPER> watch-stop <channel> <agent>

# Announce departure and remove this agent's cursor.
python3 <HELPER> leave <channel> <agent>
```

The helper prints peer messages as `[from] text`. It skips messages from the current agent and advances the cursor past all seen lines, including self messages. If the channel file is reset, the cursor recovers from the beginning. `listen`, `wait`, `stream`, and `watch-start` use filesystem events on macOS and only use the `--interval` value as a fallback when filesystem events are unavailable.

`watch-start` is a separate log/desktop-notification daemon. It runs outside
inference, skips this agent's own messages, writes
`/tmp/claude-channels/<channel>.<agent>.watch.log`, and uses a separate
`watch.cursor` so later foreground `poll`/`listen` calls still see unread
messages. It does not wake OpenCode or enter model context; use it only when the
user explicitly asks for external log/desktop monitoring.

## Workflow

When joining a channel:

1. Confirm the channel name. Resolve the agent name and report it.
   If no explicit name was given, generate a unique one using the helper:
   ```bash
   ME=$(python3 <HELPER> name 2>/dev/null | tail -n 1)
   echo "me=$ME"
   ```
2. Run `setup`.
3. Send `hello`.
4. Run `history` once and summarize any existing peer messages.
5. Start watching (see the OpenCode note above): `monitor`+`stream` (end the turn;
   wakes inline; `background_stop` when done), else `bash_background`+`wait --timeout 0`
   (end the turn; re-arm after each wake), else one foreground `listen`, turn by turn.
6. Continue turn by turn:
   - When the user gives a message, send it and listen again.
   - When `listen` returns peer messages, show them to the user and respond as requested.
   - When `listen` times out and the user is waiting for the peer, run `listen` again.
   - If a peer message is `left the channel`, report that the peer left and stop polling.
   - Before answering "no response" or ending the turn, check the channel one more time.

If the user wants to wait without spending inference tokens, use OpenCode's
background wake tool: `monitor` with `stream`, or `bash_background` with
`wait --timeout 0`. If neither exists and the user explicitly wants external
notifications, start `watch-start`, tell them it only notifies/logs externally,
and end the turn. On a later turn, run `watch-log` or foreground `listen` before
answering.

## Leaving

Treat these user messages as leave commands: `leave`, `leave the channel`, `exit`, `quit`, `stop watching`, `/leave`, `/exit`, `/quit`, `disconnect`, `close the channel`, `done`, `bye`, `goodbye`.

On a leave command, run `leave`, report that you left, and stop polling.

## Protocol Details

The shared transcript lives at:

```text
/tmp/claude-channels/<channel>.ndjson
```

Each line is:

```json
{"from":"agent-name","ts":1234567890,"text":"message text"}
```

Each agent's cursor lives at:

```text
/tmp/claude-channels/<channel>.<agent>.cursor
```

Keep messages concise and single-purpose. For long code, summaries, or diffs, send a short description and let the user decide whether to relay details.
