---
name: channel
description: Connect this agent to another Codex, Claude Code, or OpenCode agent through the shared /tmp/claude-channels named-channel file protocol. Use when the user asks to open, join, watch, poll, send on, leave, or coordinate with another agent over a channel.
---

# Channel

Use a shared append-only NDJSON file to talk with another agent in a separate session. This skill is compatible across Codex, Claude Code, and OpenCode because all participants share `/tmp/claude-channels/<channel>.ndjson`.

## User Interface

The user should invoke this as a skill, for example `$channel vrp gpt`, or ask in
natural language to join, watch, send on, poll, or leave a channel. Do not ask
the user to run the Python helper. The helper commands below are internal
implementation details for the agent to execute.

## Model

Most agent harnesses are turn-based: they only see output returned by the current tool call. There are two receive primitives:

1. **Background wake-up (preferred — zero token burn).** If your harness re-invokes you when a *background* command exits (Claude Code does: `Bash` with `run_in_background: true`), launch `wait` in the background. On macOS it blocks on filesystem events with `kqueue`, with NO model inference until a peer message lands, then prints it and exits — which wakes you exactly when there's something to read. You handle the message, then re-launch `wait` to keep watching. While it blocks you are not invoked at all, so idle watching costs nothing. This is the right way to "watch a channel in the background."

2. **Foreground bounded listen (portable fallback).** If your harness does NOT re-invoke on background exit, use `listen --timeout 30`: read what returns, act on it, then listen again. On macOS this is filesystem-event backed, not fixed-interval polling. Each listen still occupies the current model turn, so only use it when actively waiting with the user present, or when background wake-up isn't available.

While joined to a live channel, treat listening as active work. With foreground `listen`, do not stop after one empty poll if the user is waiting for the peer; run another `listen --timeout 30` unless the user asks you to stop, the peer leaves, or the channel task is clearly complete. With background `wait`, just end the turn — the harness will bring you back on the next message.

Shell variables often do not persist between tool calls. Resolve the channel name and your agent name once, tell the user which name you adopted, then pass those literal values to every command.

## Arguments

Infer these from the user's request:

- `channel`: required. If missing, ask the user for the channel name and stop.
- `agent name`: optional. If missing, choose a readable unique name.

Recommended fallback names:

- Codex: `codex-<cwd-basename>-<short-random>` (use the helper: `python3 <HELPER> name`)
- Claude Code: session name if available, else `<cwd-basename>-<CLAUDE_CODE_SESSION_ID[0:4]>`
- OpenCode: **WARNING** — opencode often sets the session title to the user's prompt text, so two agents receiving the same prompt (e.g. "watch channel tst") will both get the title "Watching tst channel". This causes **name collision** and silent message loss. Do NOT use the raw session title as the agent name. Use the helper (`channel.py name`) which always generates a unique random suffix, or fall back to the **session slug** (guaranteed unique per session).

Both agents must use different names. If a generated name might collide, ask the user for an explicit name.

## Agent-Internal Helper

`<HELPER>` is the bundled `scripts/channel.py` shipped alongside this `SKILL.md`.
Resolve its **absolute** path once and pass that literal path to every command
below (shell variables do not persist between tool calls, so do not rely on an
exported `$HELPER`). As a Claude Code plugin the script is at
`${CLAUDE_PLUGIN_ROOT}/skills/channel/scripts/channel.py`; as a plain skill it is
the `scripts/channel.py` next to this file.

Use the bundled helper internally when available:

```bash
python3 <HELPER> <command> <channel> <agent> [args...]
```

Commands:

```bash
python3 <HELPER> name
python3 <HELPER> setup <channel> <agent>
python3 <HELPER> send <channel> <agent> "hello"
python3 <HELPER> history <channel> <agent>
python3 <HELPER> poll <channel> <agent> --timeout 30
python3 <HELPER> listen <channel> <agent> --timeout 30
python3 <HELPER> wait <channel> <agent>   # run in BACKGROUND
python3 <HELPER> watch-start <channel> <agent>
python3 <HELPER> watch-status <channel> <agent>
python3 <HELPER> watch-log <channel> <agent> --lines 20
python3 <HELPER> watch-stop <channel> <agent>
python3 <HELPER> leave <channel> <agent>
```

`listen` defaults to 30 seconds. Re-run it after timeout while still waiting for a peer. The helper prints peer messages as `[from] text`, skips the current agent's messages, and advances a durable per-agent cursor.

`wait` is the **background wake-up** path and shares the SAME durable cursor as
`poll`/`listen` (so nothing is seen twice across modes). On macOS it uses
filesystem events rather than fixed-interval polling. Run it as a background
command — in Claude Code, `Bash` with `run_in_background: true`. It blocks with
zero model inference until a peer message arrives, prints it, and exits; the
harness then re-invokes you with that output. Handle the message and launch a
fresh `wait` to keep watching. It exits with a re-arm marker after `--timeout`
idle seconds (default 1800) as a heartbeat; pass `--timeout 0` to block forever.
Add `--desktop` for a macOS notification too. Because `wait` advances the shared
cursor, do NOT run a foreground `poll`/`listen` while a background `wait` is
live — they'd steal lines from each other; `wait` exits the moment it delivers,
so once you're woken there's no lingering process.

`watch-start` is a DIFFERENT, older daemon: it runs forever, only logs to
`/tmp/claude-channels/<channel>.<agent>.watch.log` + posts desktop
notifications, and never wakes the agent (you'd have to `watch-log`/`poll` later,
which costs a turn). Prefer `wait` when your harness supports background-exit
wake-up; `watch-start` is for hosts that don't. It uses a separate
`watch.cursor`, so it won't disturb the `wait`/`listen` cursor.

## Workflow

When joining:

1. Confirm the channel name and choose an agent name.
2. Run `setup`.
3. Send `hello`.
4. Run `history` once and summarize existing peer messages.
5. Start watching. **Default to background `wait`** when your harness re-invokes
   on background-command exit (Claude Code): launch `wait` with
   `run_in_background: true` and end the turn. Otherwise fall back to foreground
   `listen`.
6. Continue turn by turn:
   - When the user gives a message, `send` it, then launch a fresh background
     `wait` (or foreground `listen`).
   - When `wait`/`listen` returns peer messages, show them to the user, respond
     as requested, then re-arm the watch (launch `wait` again).
   - With foreground `listen`, if it times out and the user is still waiting,
     run `listen` again; before answering "no response", check once more.
   - If a peer message says `left the channel` (the `wait` output shows
     `a peer left the channel`), report that the peer left and stop watching —
     do not re-arm.

To watch with zero token burn between turns, this background `wait` IS the
mechanism: launch it and end the turn; a new message wakes you automatically.
(`watch-start` is the legacy log-only alternative for harnesses without
background-exit wake-up — it never wakes you on its own.)

## Claude Code Notes

Claude Code sessions may use a `SessionEnd` hook to announce departure. If that hook exists, setup should persist a sidecar with channel and name:

```bash
printf '%s\n' "{\"file\":\"/tmp/claude-channels/<channel>.ndjson\",\"me\":\"<agent>\"}" > "/tmp/claude-channels/.session-$CLAUDE_CODE_SESSION_ID"
```

If running without the helper, preserve the same wire format and cursor semantics described below.

## Leaving

Treat these user messages as leave commands: `leave`, `leave the channel`, `exit`, `quit`, `stop watching`, `/leave`, `/exit`, `/quit`, `disconnect`, `close the channel`, `done`, `bye`, `goodbye`.

On a leave command, run `leave`, report that you left, and stop polling.

## Protocol

The shared transcript is:

```text
/tmp/claude-channels/<channel>.ndjson
```

Each line is a single JSON object:

```json
{"from":"agent-name","ts":1234567890,"text":"message text"}
```

Each agent cursor is:

```text
/tmp/claude-channels/<channel>.<agent>.cursor
```

The cursor stores the last line number processed by that agent. Advance it past all seen lines, including self messages. If the channel file is reset and total lines are less than the cursor, restart from line 0.

Keep channel messages concise and single-purpose. For long code, summaries, or diffs, send a short description and let the user decide whether to relay details.
