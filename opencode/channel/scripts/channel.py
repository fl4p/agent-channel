#!/usr/bin/env python3
import argparse
import ctypes
import ctypes.util
import hashlib
import json
import os
import platform
import random
import re
import select
import signal
import subprocess
import string
import sys
import tempfile
import time
from pathlib import Path


def _default_root() -> Path:
    """Channel directory, shared by every agent on the machine.

    POSIX (macOS/Linux) keeps the historical `/tmp/claude-channels` so agents
    across harnesses interoperate. Windows has no `/tmp`, so default to the
    system temp dir. Set CHANNEL_DIR to force a specific path (required if two
    agents would otherwise compute different defaults, e.g. cross-OS)."""
    override = os.environ.get("CHANNEL_DIR")
    if override:
        return Path(override)
    if os.name == "nt":
        return Path(tempfile.gettempdir()) / "claude-channels"
    return Path("/tmp/claude-channels")


ROOT = _default_root()


def safe_name(value: str) -> str:
    allowed = string.ascii_letters + string.digits + "._-"
    out = "".join(c if c in allowed else "_" for c in value)
    return out or "agent"


def paths(channel: str, agent: str):
    ch = safe_name(channel)
    me = safe_name(agent)
    return ROOT / f"{ch}.ndjson", ROOT / f"{ch}.{me}.cursor"


def watch_paths(channel: str, agent: str):
    ch = safe_name(channel)
    me = safe_name(agent)
    return (
        ROOT / f"{ch}.ndjson",
        ROOT / f"{ch}.{me}.watch.cursor",
        ROOT / f"{ch}.{me}.watch.pid",
        ROOT / f"{ch}.{me}.watch.log",
        ROOT / f"{ch}.{me}.watch.out",
        ROOT / f"{ch}.{me}.watch.err",
    )


def ensure(file: Path):
    ROOT.mkdir(parents=True, exist_ok=True)
    file.touch(exist_ok=True)


def line_count(file: Path) -> int:
    if not file.exists():
        return 0
    with file.open("rb") as fh:
        return sum(1 for _ in fh)


def read_cursor(cursor: Path) -> int:
    try:
        return int(cursor.read_text().strip())
    except Exception:
        return 0


def write_cursor(cursor: Path, value: int):
    cursor.write_text(f"{value}\n")


def instance_id() -> str:
    """A per-instance identity so a session that shares an agent NAME with another
    live instance is still distinguishable.

    Priority:
      1. CLAUDE_CHANNEL_IID — explicit, set this when nothing else disambiguates
         (e.g. two sibling Task subagents, or any harness with no session id).
      2. harness session id (Claude Code / Codex / OpenCode), FOLDED WITH the
         CLAUDE_CODE_CHILD_SESSION marker — a Claude Code subagent/fork inherits the
         PARENT's session id and is set apart only by that child marker, so a bare
         session id would make a child look identical to its parent (verified).

    Returns "" when nothing is available; callers then fall back to name-only
    behaviour and the skill relies on unique names instead. NOTE: two instances that
    share BOTH the session id AND the child marker (e.g. two sibling subagents) still
    collide unless one sets CLAUDE_CHANNEL_IID — that is the documented escape hatch."""
    explicit = os.environ.get("CLAUDE_CHANNEL_IID")
    if explicit:
        return safe_name(explicit)
    base = ""
    for key in ("CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID", "OPENCODE_SESSION_ID"):
        val = os.environ.get(key)
        if val:
            base = val
            break
    if not base:
        return ""
    child = os.environ.get("CLAUDE_CODE_CHILD_SESSION")
    if child:                       # subagent/fork shares parent session id -> fold in
        base = f"{base}.c{child}"
    return safe_name(base)


def _id_suffix(iid: str) -> str:
    """A stable 6-hex suffix derived from an instance-id — 16M buckets, so two
    distinct ids collide far less often than a raw last-4-chars slice would."""
    return hashlib.sha1(iid.encode("utf-8", "replace")).hexdigest()[:6]


def is_own(obj: dict, me: str, my_iid: str) -> bool:
    """Whether `obj` was sent by THIS instance (filtered out on receive).

    Own = same sender name AND (no instance-id on the record, or no id for us, or the
    ids match). A same-name message carrying a DIFFERENT instance-id — i.e. a forked
    session posting under the inherited name — is NOT own, so it is delivered instead
    of being silently swallowed by the `from == me` echo filter (the collision bug)."""
    if obj.get("from") != me:
        return False
    riid = obj.get("iid")
    return (not riid) or (not my_iid) or (riid == my_iid)


def owner_file(channel: str, agent: str) -> Path:
    return ROOT / f"{safe_name(channel)}.{safe_name(agent)}.owner"


def stream_stop_file(channel: str, agent: str) -> Path:
    """Cooperative stop marker shared by every stream for this channel/name.

    `leave` writes the marker before appending its departure message. The channel
    append wakes blocked streams, which observe the marker before reading again
    and exit without replaying or advancing a removed/reset cursor.
    """
    return ROOT / f"{safe_name(channel)}.{safe_name(agent)}.stream.stop"


def last_activity(file: Path, name: str):
    """(newest_ts_from_name, left_flag). left_flag True if its last line was a leave."""
    last_ts, left = None, False
    for _lineno, line in iter_lines_after(file, 0):
        obj = parse_line(line)
        if not obj or obj.get("from") != name:
            continue
        last_ts = obj.get("ts", last_ts)
        left = str(obj.get("text", "")).strip() == "left the channel"
    return last_ts, left


def name_recently_active(file: Path, name: str, window: int = 7200) -> bool:
    """Has `name` posted (without a trailing leave) within `window` seconds?"""
    last_ts, left = last_activity(file, name)
    if last_ts is None or left:
        return False
    try:
        return (int(time.time()) - int(last_ts)) <= window
    except Exception:
        return True


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # On Windows os.kill(pid, 0) would TerminateProcess, so query a handle
        # instead of signalling.
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def read_pid(pid_file: Path):
    try:
        return int(pid_file.read_text().strip())
    except Exception:
        return None


def iter_lines_after(file: Path, last: int):
    with file.open("r", encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, start=1):
            if lineno > last:
                yield lineno, line.rstrip("\n")


def parse_line(line: str):
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def fmt(obj) -> str:
    sender = str(obj.get("from", ""))
    text = str(obj.get("text", ""))
    return f"[{sender}] {text}"


def notify_desktop(title: str, message: str):
    if platform.system() != "Darwin":
        return
    msg = message.replace("\n", " ")[:180]
    script = f"display notification {json.dumps(msg)} with title {json.dumps(title)} sound name \"Glass\""
    try:
        subprocess.run(["/usr/bin/osascript", "-e", script], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
    except Exception:
        pass


def cmd_setup(args) -> int:
    ch = safe_name(args.channel)
    name = safe_name(args.agent)
    file, cursor = paths(args.channel, name)
    ensure(file)
    iid = instance_id()
    of = owner_file(args.channel, name)
    owner = of.read_text().strip() if of.exists() else ""

    # Auto-rename only when we can PROVE a different live instance holds this name:
    # a recorded owner-id that differs from ours AND recent traffic under the name.
    # Without an iid we cannot tell "me re-joining" from "someone else", so we do NOT
    # guess — the skill instructions rely on unique names (and CLAUDE_CHANNEL_IID) for
    # that case instead of a heuristic that both mis-fires and misses.
    collision = bool(iid and owner and owner != iid and name_recently_active(file, name))
    if collision:
        requested = name
        tok = _id_suffix(iid)
        # strip any prior `-<6hex>` suffix so repeated renames (e.g. a restarting session)
        # don't compound into name-a1b2c3-d4e5f6-… ; always base off the original stem.
        base = re.sub(r"-[0-9a-f]{6}$", "", args.agent)
        name = safe_name(f"{base}-{tok}")
        file, cursor = paths(args.channel, name)
        of = owner_file(args.channel, name)
        print(f"WARNING: agent name '{requested}' is already ACTIVE on channel '{ch}' "
              f"from another session (e.g. a forked session sharing the inherited "
              f"name). Two agents under one name go SILENTLY BLIND to each other's "
              f"messages, so adopting a unique name instead.")
        print(f"IMPORTANT: use agent name '{name}' for ALL further channel commands "
              f"(send/history/wait/stream/leave), NOT '{requested}'.")
    if iid:
        of.write_text(iid + "\n")
    write_cursor(cursor, line_count(file))
    print(f"channel={ch} agent={name} file={file} cursor={cursor}")
    return 0


def cmd_send(args) -> int:
    file, cursor = paths(args.channel, args.agent)
    ensure(file)
    me = safe_name(args.agent)
    my_iid = instance_id()
    # Show unread peer messages BEFORE sending so the caller sees anything that
    # arrived while it was working — prevents message-crossing where both sides
    # talk past each other. Display-only: this does NOT advance the shared
    # cursor, so a concurrently-armed poll/listen/wait still delivers every line
    # to its own stdout. Our freshly appended line needs no cursor bump either —
    # the next read filters it out via is_own.
    missed = []
    for _lineno, line in iter_lines_after(file, read_cursor(cursor)):
        obj = parse_line(line)
        if obj and not is_own(obj, me, my_iid):
            missed.append(obj)
    if missed:
        print(f"[drain: {len(missed)} unread message(s)]")
        for obj in missed:
            print(fmt(obj))
        print("[end drain]")
    # Read the body from stdin so shell metacharacters (backticks, parens, globs, `$`)
    # in the message never reach the CALLER's shell as command arguments — the caller
    # pipes/heredocs the text instead of interpolating it into an arg. Triggered by the
    # `--stdin` flag OR a `-`/`--stdin` token used as the SOLE message token (`text` is
    # argparse.REMAINDER, which greedily swallows `--stdin` after the positionals, so the
    # flag also has to be recognised there). Anything ambiguous is REFUSED loudly rather
    # than silently corrupting the message or dropping the piped body.
    SENTINELS = ("-", "--stdin")
    body = list(args.text or [])
    # stdin mode when: the --stdin flag was parsed (only possible before the REMAINDER),
    # OR the SOLE message token is a `-`/`--stdin` sentinel (the flag-after-positionals
    # form, since REMAINDER swallows the flag into `text`). A `-`/`--stdin` appearing AMONG
    # other words is ordinary literal text (a dash, a range "3 - 5", a bullet) and is left
    # alone — NOT treated as a sentinel and NOT an error.
    stdin_mode = bool(getattr(args, "stdin", False)) or (len(body) == 1 and body[0] in SENTINELS)
    if not stdin_mode and "--stdin" in body:
        # `--stdin` among other words is almost never literal text (unlike a bare `-`) —
        # it's a caller that typed the flag after the positionals but forgot to pipe a
        # body. Refuse loudly instead of sending a message with a stray "--stdin" glued in.
        print("error: '--stdin' among positional text is ambiguous (did you mean to pipe "
              "the body?). Use --stdin with piped/heredoc input, or remove the token.",
              file=sys.stderr)
        return 2
    if stdin_mode:
        leftover = [t for t in body if t not in SENTINELS]
        if leftover:                    # explicit --stdin flag AND positional text
            print("error: --stdin was given together with positional text; pass the body "
                  "via stdin only.", file=sys.stderr)
            return 2
        if sys.stdin.isatty():          # interactive terminal -> nothing piped; fail fast
            print("error: --stdin but stdin is a TTY (nothing piped). Pipe or heredoc the "
                  "body, e.g.  printf '%s' \"$msg\" | ... send ch agent --stdin",
                  file=sys.stderr)
            return 2
        try:                            # backstop: a held-open but silent stdin (not a tty)
            ready, _, _ = select.select([sys.stdin], [], [], 10.0)  # would hang read()
            if not ready:
                print("error: --stdin but no input arrived within 10s (nothing piped, or "
                      "stdin held open with no data). Aborting instead of hanging.",
                      file=sys.stderr)
                return 2
        except (OSError, ValueError):   # select unsupported for this fd/platform (e.g.
            pass                        # Windows pipes); isatty already handled the tty case
        # Collapse newlines (like the positional path) so ONE message stays ONE physical
        # line: stream/watch-run and per-line monitors rely on one event per message, and
        # a JSON-embedded newline would fan a message into multiple unattributed lines.
        text = sys.stdin.read().replace("\r", " ").replace("\n", " ").strip()
    else:
        text = " ".join(body).replace("\r", " ").replace("\n", " ").strip()
    if not text:                        # no empty messages from EITHER path (closed/EOF
        print("error: empty message (nothing to send).", file=sys.stderr)  # stdin, or a
        return 2                        # bare `send ch agent` with no text tokens)
    record = {"from": me, "ts": int(time.time()), "text": text}
    if my_iid:
        record["iid"] = my_iid
    # If this name is owned by a different live instance, warn on stderr — a forked
    # session that never re-ran setup lands here, and this tells it to rename.
    of = owner_file(args.channel, args.agent)
    owner = of.read_text().strip() if of.exists() else ""
    if my_iid and owner and owner != my_iid and name_recently_active(file, me):
        print(f"WARNING: '{me}' is also active from another session (instance {owner[:8]} "
              f"vs yours {my_iid[:8]}). Re-run `setup` to adopt a unique name, or your "
              f"messages will be indistinguishable on the channel.", file=sys.stderr)
    with file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"sent: {text}")
    return 0


def collect_new(file: Path, cursor: Path, me: str):
    total = line_count(file)
    last = read_cursor(cursor)
    if total < last:
        last = 0
    my_iid = instance_id()
    messages = []
    for _lineno, line in iter_lines_after(file, last):
        obj = parse_line(line)
        if obj and not is_own(obj, me, my_iid):
            messages.append(obj)
    write_cursor(cursor, total)
    return messages, total


# --- Linux inotify receive path (stdlib ctypes; no third-party deps) --------
# Mirrors the kqueue WRITE|EXTEND|DELETE|RENAME watch set below, so Linux gets
# the same zero-CPU, event-driven wait that macOS/BSD already have instead of
# degrading to the sleep-poll fallback.
_IN_MODIFY = 0x00000002
_IN_CLOSE_WRITE = 0x00000008
_IN_DELETE_SELF = 0x00000400
_IN_MOVE_SELF = 0x00000800
_INOTIFY_MASK = _IN_MODIFY | _IN_CLOSE_WRITE | _IN_DELETE_SELF | _IN_MOVE_SELF
_inotify_lib = None
_inotify_tried = False


def _inotify_libc():
    """Bind libc inotify calls once via ctypes. None if unavailable.

    Tries the resolved libc name, then the glibc soname, then ``None`` (the
    running program's own symbols) so musl (Alpine) — where neither
    ``find_library`` nor ``libc.so.6`` resolves — still binds via the
    already-loaded C library instead of silently dropping to the sleep poll."""
    global _inotify_lib, _inotify_tried
    if not _inotify_tried:
        _inotify_tried = True  # bind at most once; failure stays cached as None
        for libname in (ctypes.util.find_library("c"), "libc.so.6", None):
            try:
                lib = ctypes.CDLL(libname, use_errno=True)
                lib.inotify_init1.argtypes = [ctypes.c_int]
                lib.inotify_init1.restype = ctypes.c_int
                lib.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
                lib.inotify_add_watch.restype = ctypes.c_int
                _inotify_lib = lib
                break
            except (OSError, AttributeError):
                continue
    return _inotify_lib


def _wait_inotify(file: Path, observed_total: int, timeout):
    """Block on Linux inotify until ``file`` changes or ``timeout`` elapses.

    Returns True if it changed (or may have), False on timeout, and None if
    inotify could not be set up so the caller falls back to the bounded sleep.
    A fresh inotify fd per call keeps the watch armed *before* the race recheck
    below, closing the lost-wakeup window between the last read and arming the
    watch -- the same ordering the kqueue path uses.
    """
    libc = _inotify_libc()
    if libc is None:
        return None
    fd = libc.inotify_init1(os.O_NONBLOCK)
    if fd < 0:
        return None
    try:
        if libc.inotify_add_watch(fd, os.fsencode(str(file)), _INOTIFY_MASK) < 0:
            return None
        if line_count(file) != observed_total:
            return True
        rlist, _, _ = select.select([fd], [], [], timeout)
        return bool(rlist)  # event fired (True) vs timed out (False)
    finally:
        os.close(fd)


def wait_for_file_change(file: Path, observed_total: int, timeout, fallback_interval: float) -> bool:
    """Wait until the channel file changes.

    On macOS/BSD this uses kqueue vnode events, so listen/wait are not polling.
    On platforms without kqueue, fall back to a bounded sleep so the helper still
    works everywhere.
    """
    if timeout is not None and timeout <= 0:
        return False

    if hasattr(select, "kqueue") and hasattr(select, "kevent"):
        fd = os.open(file, os.O_RDONLY)
        try:
            # Open kqueue inside the try so fd is closed even if kqueue() raises
            # (e.g. EMFILE under fd pressure); otherwise the stream loop leaks fds.
            kq = select.kqueue()
            try:
                flags = select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR
                fflags = (
                    select.KQ_NOTE_WRITE
                    | select.KQ_NOTE_EXTEND
                    | select.KQ_NOTE_DELETE
                    | select.KQ_NOTE_RENAME
                )
                event = select.kevent(fd, filter=select.KQ_FILTER_VNODE, flags=flags, fflags=fflags)
                kq.control([event], 0, 0)
                if line_count(file) != observed_total:
                    return True
                events = kq.control(None, 1, timeout)
                return bool(events)
            finally:
                kq.close()
        finally:
            os.close(fd)

    if sys.platform.startswith("linux"):
        result = _wait_inotify(file, observed_total, timeout)
        if result is not None:
            return result

    sleep_for = fallback_interval
    if timeout is not None:
        sleep_for = min(sleep_for, timeout)
    time.sleep(max(0.0, sleep_for))
    return True


def cmd_poll(args) -> int:
    file, cursor = paths(args.channel, args.agent)
    ensure(file)
    me = safe_name(args.agent)
    deadline = time.time() + args.timeout
    messages = []
    total = line_count(file)
    while True:
        messages, total = collect_new(file, cursor, me)
        if messages or time.time() >= deadline:
            break
        wait_for_file_change(file, total, max(0.0, deadline - time.time()), args.interval)
    for obj in messages:
        print(fmt(obj))
    print(f"[poll done; cursor={total}]")
    return 0


def cmd_listen(args) -> int:
    return cmd_poll(args)


def cmd_wait(args) -> int:
    """Block with ZERO model inference until a peer message arrives past the
    cursor, then print it and EXIT — the background watch primitive.

    Launch this in the BACKGROUND: a harness that re-invokes the agent when a
    background command exits will wake the agent exactly when there's something
    new to read — no foreground poll loop, no per-tick token burn while idle.
    The agent handles the message(s) and re-launches `wait` to keep watching.
    Shares the same durable per-agent cursor as poll/listen, so don't run a
    foreground poll AND a background wait at once (they'd steal lines from each
    other); because `wait` exits as soon as it delivers, there's no lingering
    process to collide with once you're woken.

    A positive --timeout (default 1800s) exits 0 with a re-arm marker after that
    many idle seconds: a heartbeat that bounds the background slot's lifetime
    (the agent just relaunches on the next wake). --timeout 0 blocks
    indefinitely — the truest 0-token watch, but with no heartbeat safety net."""
    file, cursor = paths(args.channel, args.agent)
    ensure(file)
    me = safe_name(args.agent)
    deadline = None if args.timeout <= 0 else time.time() + args.timeout
    while True:
        messages, total = collect_new(file, cursor, me)
        if messages:
            for obj in messages:
                print(fmt(obj))
            print(f"[wait: {len(messages)} new message(s); cursor={total}]")
            if args.desktop:
                notify_desktop(f"{safe_name(args.channel)} channel", fmt(messages[-1]))
            # A peer 'left the channel' is normally terminal — surface it so the agent
            # stops re-arming. With --stay, keep watching through OTHER peers leaving
            # (busy multi-agent channels where peers come and go): deliver the leave
            # message but suppress the terminal marker, so the agent re-arms as usual.
            if (not getattr(args, "stay", False)
                    and any(str(m.get("text", "")).strip() == "left the channel"
                            for m in messages)):
                print("[wait: a peer left the channel]")
            return 0
        if deadline is not None and time.time() >= deadline:
            print(f"[wait: idle {int(args.timeout)}s, no new messages — re-arm to keep watching; cursor={total}]")
            return 0
        timeout = None if deadline is None else max(0.0, deadline - time.time())
        wait_for_file_change(file, total, timeout, args.interval)


def cmd_stream(args) -> int:
    """Stream peer messages to stdout, one line each, FOREVER — the per-line
    monitor primitive.

    Unlike `wait` (which exits after the first delivery so a wake-on-exit host
    re-invokes the agent), this keeps running. Arm it under a host that has a
    persistent per-line monitor tool — e.g. OpenCode's experimental `monitor`,
    which wakes the agent on each new stdout line — and every peer message is
    delivered inline as it arrives, with no re-arm between messages. Blocks on
    filesystem events while idle (zero CPU on macOS/Linux; bounded sleep poll
    elsewhere). Each line is flushed immediately so the monitor sees it in real
    time through the pipe. Peer leaves are delivered as messages and surfaced
    with a marker, but the stream stays armed because idle channels are cheap.
    Pass --exit-on-leave to restore the old two-party teardown behavior."""
    file, cursor = paths(args.channel, args.agent)
    ensure(file)
    me = safe_name(args.agent)
    stop_file = stream_stop_file(args.channel, args.agent)
    try:
        stop_file.unlink()
    except FileNotFoundError:
        pass
    while True:
        if stop_file.exists():
            return 0
        messages, total = collect_new(file, cursor, me)
        for obj in messages:
            print(fmt(obj), flush=True)
        # Peer leaves are ordinary channel events for persistent monitors. Surface the
        # marker, but keep watching unless the caller explicitly asks for two-party
        # teardown behavior.
        if any(str(m.get("text", "")).strip() == "left the channel" for m in messages):
            print("[stream: a peer left the channel]", flush=True)
            if getattr(args, "exit_on_leave", False):
                return 0
        wait_for_file_change(file, total, None, args.interval)


def cmd_history(args) -> int:
    file, _cursor = paths(args.channel, args.agent)
    ensure(file)
    me = safe_name(args.agent)
    my_iid = instance_id()
    any_msg = False
    for _lineno, line in iter_lines_after(file, 0):
        obj = parse_line(line)
        if obj and not is_own(obj, me, my_iid):
            print(fmt(obj))
            any_msg = True
    if not any_msg:
        print("[history empty]")
    return 0


def cmd_leave(args) -> int:
    file, cursor = paths(args.channel, args.agent)
    my_iid = instance_id()
    of = owner_file(args.channel, args.agent)
    owner = of.read_text().strip() if of.exists() else ""
    # Refuse to leave under a name a DIFFERENT live instance owns. Otherwise a stale
    # pre-rename name would stop the rightful owner's streams, remove its owner file,
    # and inject a spurious departure event. Destructive path -> hard refuse.
    if my_iid and owner and owner != my_iid and name_recently_active(file, safe_name(args.agent)):
        print(f"error: '{safe_name(args.agent)}' is owned by another live instance "
              f"({owner[:8]}); refusing to leave under it. Use your own (renamed) name.",
              file=sys.stderr)
        return 2
    # Ask every persistent stream under this channel/name to exit BEFORE the
    # departure append wakes it. Preserve the cursor: deleting it while a stream
    # is alive makes read_cursor() fall back to zero and replays the transcript.
    # A later setup() deliberately resets the cursor to the then-current end.
    stream_stop_file(args.channel, args.agent).write_text(
        f"leave {int(time.time())}\n", encoding="utf-8"
    )
    send_args = argparse.Namespace(channel=args.channel, agent=args.agent, text=["left the channel"])
    rc = cmd_send(send_args)
    try:
        of.unlink()
    except FileNotFoundError:
        pass
    print(f"left channel={safe_name(args.channel)} agent={safe_name(args.agent)}")
    print(f"cursor preserved={cursor}; next setup resets it to the channel end")
    return rc


def cmd_watch_start(args) -> int:
    file, cursor, pid_file, log_file, out_file, err_file = watch_paths(args.channel, args.agent)
    ensure(file)
    pid = read_pid(pid_file)
    if pid and pid_alive(pid):
        if not args.restart:
            print(f"watch already running: channel={safe_name(args.channel)} agent={safe_name(args.agent)} pid={pid}")
            print(f"log={log_file}")
            return 0
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.2)
    if args.from_end:
        write_cursor(cursor, line_count(file))
    elif not cursor.exists():
        write_cursor(cursor, 0)

    cmd = [
        sys.executable, str(Path(__file__).resolve()), "watch-run",
        args.channel, args.agent,
        "--interval", str(args.interval),
    ]
    if args.desktop:
        cmd.append("--desktop")
    # Detach the watcher so it outlives this invocation. start_new_session is
    # POSIX-only (setsid); on Windows use the equivalent creation flags.
    with out_file.open("ab") as out, err_file.open("ab") as err:
        if os.name == "nt":
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            proc = subprocess.Popen(cmd, stdout=out, stderr=err,
                                    creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
        else:
            proc = subprocess.Popen(cmd, stdout=out, stderr=err, start_new_session=True)
    pid_file.write_text(f"{proc.pid}\n")
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(f"[watch-start] channel={safe_name(args.channel)} agent={safe_name(args.agent)} "
                 f"pid={proc.pid} cursor={read_cursor(cursor)} desktop={args.desktop}\n")
    print(f"watch started: channel={safe_name(args.channel)} agent={safe_name(args.agent)} pid={proc.pid}")
    print(f"log={log_file}")
    return 0


def cmd_watch_run(args) -> int:
    file, cursor, pid_file, log_file, _out_file, _err_file = watch_paths(args.channel, args.agent)
    ensure(file)
    pid_file.write_text(f"{os.getpid()}\n")
    me = safe_name(args.agent)
    my_iid = instance_id()
    try:
        while True:
            total = line_count(file)
            last = read_cursor(cursor)
            if total < last:
                last = 0
            lines = []
            for _lineno, line in iter_lines_after(file, last):
                obj = parse_line(line)
                if obj and not is_own(obj, me, my_iid):
                    lines.append(fmt(obj))
            if lines:
                with log_file.open("a", encoding="utf-8") as fh:
                    for line in lines:
                        fh.write(line + "\n")
                if args.desktop:
                    notify_desktop(f"{safe_name(args.channel)} channel", lines[-1])
            write_cursor(cursor, total)
            wait_for_file_change(file, total, None, args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        try:
            if read_pid(pid_file) == os.getpid():
                pid_file.unlink()
        except FileNotFoundError:
            pass


def cmd_watch_stop(args) -> int:
    _file, _cursor, pid_file, log_file, _out_file, _err_file = watch_paths(args.channel, args.agent)
    pid = read_pid(pid_file)
    if not pid:
        print(f"watch not running: channel={safe_name(args.channel)} agent={safe_name(args.agent)}")
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        pid_file.unlink()
    except FileNotFoundError:
        pass
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(f"[watch-stop] channel={safe_name(args.channel)} agent={safe_name(args.agent)} pid={pid}\n")
    print(f"watch stopped: channel={safe_name(args.channel)} agent={safe_name(args.agent)} pid={pid}")
    return 0


def cmd_watch_status(args) -> int:
    _file, cursor, pid_file, log_file, out_file, err_file = watch_paths(args.channel, args.agent)
    pid = read_pid(pid_file)
    running = bool(pid and pid_alive(pid))
    print(f"channel={safe_name(args.channel)} agent={safe_name(args.agent)} running={running} pid={pid or ''}")
    print(f"cursor={read_cursor(cursor)} log={log_file}")
    print(f"stdout={out_file} stderr={err_file}")
    return 0


def cmd_watch_log(args) -> int:
    _file, _cursor, _pid_file, log_file, _out_file, _err_file = watch_paths(args.channel, args.agent)
    if not log_file.exists():
        print("[watch log empty]")
        return 0
    lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-args.lines:]:
        print(line)
    return 0


def cmd_name(args) -> int:
    base = safe_name(Path.cwd().name)
    suffix = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(4))
    print(f"codex-{base}-{suffix}")
    return 0


def build_parser():
    p = argparse.ArgumentParser(description="Shared file channel helper for Codex/Claude/OpenCode agents.")
    sub = p.add_subparsers(dest="command", required=True)

    name = sub.add_parser("name", help="generate a readable agent name")
    name.set_defaults(func=cmd_name)

    for cmd, fn in (("setup", cmd_setup), ("history", cmd_history), ("leave", cmd_leave)):
        sp = sub.add_parser(cmd)
        sp.add_argument("channel")
        sp.add_argument("agent")
        sp.set_defaults(func=fn)

    send = sub.add_parser("send")
    send.add_argument("channel")
    send.add_argument("agent")
    send.add_argument("--stdin", action="store_true",
                      help="read the message body from stdin instead of args, so shell "
                           "metacharacters (backticks, parens, globs, $) can't be executed "
                           "by the caller's shell — pipe or heredoc the text")
    send.add_argument("text", nargs=argparse.REMAINDER)
    send.set_defaults(func=cmd_send)

    poll = sub.add_parser("poll")
    poll.add_argument("channel")
    poll.add_argument("agent")
    poll.add_argument("--timeout", type=float, default=30.0)
    poll.add_argument("--interval", type=float, default=0.25,
                      help="fallback sleep interval when filesystem events are unavailable")
    poll.set_defaults(func=cmd_poll)

    listen = sub.add_parser("listen")
    listen.add_argument("channel")
    listen.add_argument("agent")
    listen.add_argument("--timeout", type=float, default=30.0)
    listen.add_argument("--interval", type=float, default=0.25,
                        help="fallback sleep interval when filesystem events are unavailable")
    listen.set_defaults(func=cmd_listen)

    wait = sub.add_parser(
        "wait",
        help="block (0 model inference) until a peer message, then exit — run in the "
             "BACKGROUND so the harness wakes the agent on new messages",
    )
    wait.add_argument("channel")
    wait.add_argument("agent")
    wait.add_argument("--timeout", type=float, default=1800.0,
                      help="idle seconds before exiting with a re-arm marker (default 1800)")
    wait.add_argument("--interval", type=float, default=0.25,
                      help="fallback sleep interval when filesystem events are unavailable")
    wait.add_argument("--desktop", action="store_true",
                      help="also fire a macOS desktop notification on new messages")
    wait.add_argument("--stay", action="store_true",
                      help="keep watching when OTHER peers leave (busy multi-peer "
                           "channels); default exits so the agent stops re-arming")
    wait.set_defaults(func=cmd_wait)

    stream = sub.add_parser(
        "stream",
        help="stream peer messages to stdout FOREVER, one line each — arm under a "
             "per-line monitor tool (e.g. OpenCode's `monitor`) for inline, "
             "no-re-arm channel watching",
    )
    stream.add_argument("channel")
    stream.add_argument("agent")
    stream.add_argument("--interval", type=float, default=0.25,
                        help="fallback sleep interval when filesystem events are unavailable")
    stream.add_argument("--stay", action="store_true",
                        help="deprecated compatibility no-op; stream now stays armed "
                             "through peer leaves by default")
    stream.add_argument("--exit-on-leave", action="store_true",
                        help="exit when any peer leaves (old two-party teardown behavior)")
    stream.set_defaults(func=cmd_stream)

    watch_start = sub.add_parser("watch-start", help="start a zero-inference background channel watcher")
    watch_start.add_argument("channel")
    watch_start.add_argument("agent")
    watch_start.add_argument("--interval", type=float, default=0.25,
                             help="fallback sleep interval when filesystem events are unavailable")
    watch_start.add_argument("--from-start", dest="from_end", action="store_false",
                             help="watch from the beginning instead of the current file end")
    watch_start.add_argument("--no-desktop", dest="desktop", action="store_false",
                             help="disable macOS desktop notifications; still writes the watch log")
    watch_start.add_argument("--restart", action="store_true")
    watch_start.set_defaults(func=cmd_watch_start, from_end=True, desktop=True)

    watch_run = sub.add_parser("watch-run", help=argparse.SUPPRESS)
    watch_run.add_argument("channel")
    watch_run.add_argument("agent")
    watch_run.add_argument("--interval", type=float, default=0.25)
    watch_run.add_argument("--desktop", action="store_true")
    watch_run.set_defaults(func=cmd_watch_run)

    for cmd, fn in (("watch-stop", cmd_watch_stop), ("watch-status", cmd_watch_status)):
        sp = sub.add_parser(cmd)
        sp.add_argument("channel")
        sp.add_argument("agent")
        sp.set_defaults(func=fn)

    watch_log = sub.add_parser("watch-log")
    watch_log.add_argument("channel")
    watch_log.add_argument("agent")
    watch_log.add_argument("--lines", type=int, default=20)
    watch_log.set_defaults(func=cmd_watch_log)
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
