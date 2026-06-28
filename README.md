# agent-channel

Let two or more AI coding agents — **Claude Code**, **Codex**, or **OpenCode** — talk to
each other across separate sessions over a shared, file-based named channel.

No server, no daemon, no API keys. Agents append JSON lines to
`/tmp/claude-channels/<channel>.ndjson` and each keeps its own durable cursor.
On macOS/BSD the receive path blocks on `kqueue` filesystem events, so an agent
can *wait* for a peer message with **zero CPU and zero model inference** until
something actually arrives — then wake exactly once.

```
agent A  ──send──▶  /tmp/claude-channels/demo.ndjson  ◀──wait/listen──  agent B
   ▲                                                                       │
   └──────────────────────────── replies ─────────────────────────────────┘
```

## What's in the box

A single Python helper (`scripts/channel.py`, identical across all three) plus a
harness-specific `SKILL.md` that teaches the agent how to drive it:

| Harness      | Skill source                 |
|--------------|------------------------------|
| Claude Code  | `plugins/channel/skills/channel` (also installable as a plugin) |
| Codex        | `codex/channel`              |
| OpenCode     | `opencode/channel`           |

Two agents on different harnesses interoperate as long as they share the same
`/tmp/claude-channels/<channel>.ndjson` path.

## Install

### Claude Code (plugin — recommended)

```
/plugin marketplace add fl4p/agent-channel
/plugin install channel@agent-channel
```

Then just ask: *"go on channel demo as alice and watch it"*.

### Claude Code (plain skill, no plugin)

```bash
git clone https://github.com/fl4p/agent-channel ~/agent-channel
ln -s ~/agent-channel/plugins/channel/skills/channel ~/.claude/skills/channel
```

### Codex

```bash
git clone https://github.com/fl4p/agent-channel ~/agent-channel   # if not already
ln -s ~/agent-channel/codex/channel ~/.codex/skills/channel
```

### OpenCode

```bash
git clone https://github.com/fl4p/agent-channel ~/agent-channel   # if not already
ln -s ~/agent-channel/opencode/channel ~/.config/opencode/skills/channel
```

### Pi

Pi behaves like the OpenCode tier — it watches the channel through a background
**`bash_background`** / **`monitor`** tool and is woken on a peer message exactly
like Claude Code's background `wait`. Pi ships no background-bash support of its
own, so install the
[`fl4p/pi-bash-background`](https://github.com/fl4p/pi-bash-background) extension
first; the OpenCode `SKILL.md` then works as-is.

```bash
# 1. background-wake extension (registers bash_background + monitor)
git clone https://github.com/fl4p/pi-bash-background ~/pi-bash-background
ln -s ~/pi-bash-background/src/index.ts ~/.pi/agent/extensions/bash-background.ts

# 2. the channel skill (reuse the OpenCode variant)
git clone https://github.com/fl4p/agent-channel ~/agent-channel   # if not already
ln -s ~/agent-channel/opencode/channel ~/.pi/agent/skills/channel
```

Then just ask: *"go on channel demo as alice and watch it"* — the agent arms
`bash_background`/`monitor` running `channel.py wait`/`stream` and is woken once
per message with zero idle CPU. Without the extension, messaging still works; fall
back to foreground `listen`.

(Adjust the destination to your harness's skill directory if it differs.)

## Usage

Ask the agent in natural language — it invokes the skill itself:

- *"open channel `demo` as `alice` and tell me when the other agent says something"*
- *"send 'build is green' on channel demo"*
- *"watch channel demo in the background"*
- *"leave the channel"*

You never run `channel.py` by hand; the skill drives it for the agent.

## Receive primitives

- **`wait` (preferred, 0-token).** Launched as a *background* command in harnesses
  that re-invoke the agent on background-command exit (Claude Code). It blocks on
  real filesystem events until a peer message lands, prints it, exits — waking the
  agent exactly once with **zero idle CPU**: `kqueue` on macOS/BSD, `inotify` on
  Linux (glibc *and* musl/Alpine). Windows, and any host where neither watcher can
  be set up, fall back to a short bounded sleep poll — same behavior, just a little
  idle CPU instead of true event blocking.
- **`listen --timeout 30` (portable).** Foreground bounded listen for harnesses
  without background wake-up. Re-run while actively waiting.
- **`watch-start` (legacy).** A detached watcher that only logs and posts desktop
  notifications; it never wakes the agent on its own.

> **Harness support for background `wait`.** The zero-token background `wait`
> needs the harness to re-invoke the agent when a background command exits
> ("background injection"). **Claude Code** supports this today. **Codex** does
> not yet ([openai/codex#22003](https://github.com/openai/codex/issues/22003)),
> and **upstream OpenCode** does not yet — open PR
> [anomalyco/opencode#33806](https://github.com/anomalyco/opencode/pull/33806)
> adds it (a Monitor background-watcher tool). On those, use foreground `listen`
> until support lands. (Messaging itself works everywhere regardless.)

## Protocol

Shared transcript — append-only NDJSON, one JSON object per line:

```
/tmp/claude-channels/<channel>.ndjson
{"from":"alice","ts":1234567890,"text":"hello"}
```

Each agent tracks its position in a sibling cursor file
(`/tmp/claude-channels/<channel>.<agent>.cursor`) so nothing is seen twice and
agents never re-read their own messages.

## Platform support

Pure Python 3 standard library, no third-party deps.

| OS                    | Messaging (`send`/`listen`/`wait`/`leave`) | Wake mechanism           | `watch-*` daemon | Desktop notifications |
|-----------------------|--------------------------------------------|--------------------------|------------------|-----------------------|
| **macOS**             | ✅                                          | `kqueue` events (0 CPU)  | ✅               | ✅ (`osascript`)       |
| **Linux** (glibc)     | ✅                                          | `inotify` events (0 CPU) | ✅               | — (no-op)             |
| **Linux** (musl/Alpine) | ✅                                        | `inotify` events (0 CPU) | ✅               | — (no-op)             |
| **Windows**           | ✅                                          | bounded sleep poll       | ✅               | — (no-op)             |

`wait`/`listen` block on native filesystem events with zero idle CPU on macOS
(`kqueue`) and Linux (`inotify`). Anywhere a watcher can't be set up — Windows,
or an exotic host — they degrade to a short bounded sleep poll: identical
behavior, just a little idle CPU.

Notes:

- The channel directory is `/tmp/claude-channels` on macOS/Linux and
  `%TEMP%\claude-channels` on Windows. Set the **`CHANNEL_DIR`** environment
  variable to override it — required only if two agents would otherwise compute
  different paths (e.g. a macOS and a Windows agent on the same host).
- Desktop notifications (`--desktop`) are macOS-only; elsewhere they silently
  no-op and the channel still works.
- An earlier MCP-broker implementation of this idea is deprecated in favor of the
  file-based approach here — no extra process, no polling, instant wake.

**Tested on:** macOS (kqueue), Linux glibc (`python:3-slim`) and musl
(`python:3-alpine`) — both confirmed holding an `inotify` fd at zero idle CPU and
waking on a peer send — and Windows Python (via Wine: `%TEMP%` path, `ctypes`
`pid_alive`, and the detached `watch-*` daemon all verified).

## License

MIT — see [LICENSE](LICENSE).
