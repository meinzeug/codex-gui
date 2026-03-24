#!/usr/bin/env python3

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
DEFAULT_APP = APP_DIR / "codex_terminal_gui.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Supervisor that keeps codex_terminal_gui.py running."
    )
    parser.add_argument(
        "--app",
        default=str(DEFAULT_APP),
        help="Path to the GUI app to supervise.",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python interpreter used to launch the GUI app.",
    )
    parser.add_argument(
        "--restart-delay",
        type=float,
        default=1.0,
        help="Seconds to wait before restarting the GUI after it exits.",
    )
    parser.add_argument(
        "--max-restarts",
        type=int,
        default=None,
        help="Optional limit for restart attempts. Omit for endless supervision.",
    )
    parser.add_argument(
        "app_args",
        nargs=argparse.REMAINDER,
        help="Arguments passed through to codex_terminal_gui.py. Prefix with -- to separate them.",
    )
    args = parser.parse_args()
    if args.app_args and args.app_args[0] == "--":
        args.app_args = args.app_args[1:]
    return args


def main() -> int:
    args = parse_args()
    app_path = Path(args.app).expanduser().resolve()
    if not app_path.exists():
        print(f"GUI app not found: {app_path}", file=sys.stderr)
        return 1

    child: subprocess.Popen | None = None
    stop_requested = False
    restart_count = 0

    def handle_signal(signum, _frame) -> None:
        nonlocal stop_requested, child
        stop_requested = True
        if child is not None and child.poll() is None:
            child.send_signal(signum)

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, handle_signal)

    while True:
        env = os.environ.copy()
        env["CODEX_GUI_SUPERMODE"] = "1"
        env["CODEX_GUI_SUPERVISOR_PID"] = str(os.getpid())
        command = [args.python, str(app_path), *args.app_args]

        print(f"[supermode] starting {' '.join(command)}", file=sys.stderr, flush=True)
        started_at = time.monotonic()
        child = subprocess.Popen(command, cwd=str(app_path.parent), env=env)
        exit_code = child.wait()
        child = None
        runtime = time.monotonic() - started_at

        if stop_requested:
            return exit_code

        restart_count += 1
        if args.max_restarts is not None and restart_count > args.max_restarts:
            print(
                f"[supermode] max restarts reached after exit code {exit_code}",
                file=sys.stderr,
                flush=True,
            )
            return exit_code

        delay = args.restart_delay
        if runtime < 2:
            delay = max(delay, 2.0)

        print(
            f"[supermode] child exited with {exit_code}, restarting in {delay:.1f}s",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(delay)


if __name__ == "__main__":
    raise SystemExit(main())
