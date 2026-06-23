# agent-channel

Let two AI coding agents — **Claude Code**, **Codex**, or **OpenCode** — talk to
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
  that re-invoke the agent on background-command exit (Claude Code). Blocks on
  filesystem events until a peer message lands, prints it, exits — waking the
  agent exactly once with no idle polling.
- **`listen --timeout 30` (portable).** Foreground bounded listen for harnesses
  without background wake-up. Re-run while actively waiting.
- **`watch-start` (legacy).** A detached watcher that only logs and posts desktop
  notifications; it never wakes the agent on its own.

## Protocol

Shared transcript — append-only NDJSON, one JSON object per line:

```
/tmp/claude-channels/<channel>.ndjson
{"from":"alice","ts":1234567890,"text":"hello"}
```

Each agent tracks its position in a sibling cursor file
(`/tmp/claude-channels/<channel>.<agent>.cursor`) so nothing is seen twice and
agents never re-read their own messages.

> Note: an earlier MCP-broker implementation of this idea is deprecated in favor
> of the file-based approach here — no extra process, no polling, instant wake.

## License

MIT — see [LICENSE](LICENSE).
