#!/usr/bin/env python3
import argparse
import ctypes
import json
import os
import platform
import random
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
    file, cursor = paths(args.channel, args.agent)
    ensure(file)
    write_cursor(cursor, line_count(file))
    print(f"channel={safe_name(args.channel)} agent={safe_name(args.agent)} file={file} cursor={cursor}")
    return 0


def cmd_send(args) -> int:
    file, _cursor = paths(args.channel, args.agent)
    ensure(file)
    text = " ".join(args.text).replace("\r", " ").replace("\n", " ")
    record = {"from": safe_name(args.agent), "ts": int(time.time()), "text": text}
    with file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"sent: {text}")
    return 0


def collect_new(file: Path, cursor: Path, me: str):
    total = line_count(file)
    last = read_cursor(cursor)
    if total < last:
        last = 0
    messages = []
    for _lineno, line in iter_lines_after(file, last):
        obj = parse_line(line)
        if obj and obj.get("from") != me:
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


def _inotify_libc():
    """Bind libc inotify calls once via ctypes. None if unavailable."""
    global _inotify_lib
    if _inotify_lib is None:
        try:
            lib = ctypes.CDLL("libc.so.6", use_errno=True)
            lib.inotify_init1.argtypes = [ctypes.c_int]
            lib.inotify_init1.restype = ctypes.c_int
            lib.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
            lib.inotify_add_watch.restype = ctypes.c_int
            _inotify_lib = lib
        except (OSError, AttributeError):
            _inotify_lib = False  # cache the failure; don't retry libc each loop
    return _inotify_lib or None


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
            # A peer 'left the channel' is terminal — surface it so the agent
            # stops re-arming the watcher.
            if any(str(m.get("text", "")).strip() == "left the channel" for m in messages):
                print("[wait: a peer left the channel]")
            return 0
        if deadline is not None and time.time() >= deadline:
            print(f"[wait: idle {int(args.timeout)}s, no new messages — re-arm to keep watching; cursor={total}]")
            return 0
        timeout = None if deadline is None else max(0.0, deadline - time.time())
        wait_for_file_change(file, total, timeout, args.interval)


def cmd_history(args) -> int:
    file, _cursor = paths(args.channel, args.agent)
    ensure(file)
    me = safe_name(args.agent)
    any_msg = False
    for _lineno, line in iter_lines_after(file, 0):
        obj = parse_line(line)
        if obj and obj.get("from") != me:
            print(fmt(obj))
            any_msg = True
    if not any_msg:
        print("[history empty]")
    return 0


def cmd_leave(args) -> int:
    send_args = argparse.Namespace(channel=args.channel, agent=args.agent, text=["left the channel"])
    rc = cmd_send(send_args)
    _file, cursor = paths(args.channel, args.agent)
    try:
        cursor.unlink()
    except FileNotFoundError:
        pass
    print(f"left channel={safe_name(args.channel)} agent={safe_name(args.agent)}")
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
    try:
        while True:
            total = line_count(file)
            last = read_cursor(cursor)
            if total < last:
                last = 0
            lines = []
            for _lineno, line in iter_lines_after(file, last):
                obj = parse_line(line)
                if obj and obj.get("from") != me:
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
    wait.set_defaults(func=cmd_wait)

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
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
