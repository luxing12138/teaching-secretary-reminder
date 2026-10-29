"""One-instance Markdown-to-DingTalk synchronization service.

It checks the task directory once per second, debounces local edits, and polls
mapped DingTalk todos because DWS does not expose a completion webhook.
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from core import TaskStore
from dws_integration import DwsWorkflow
from reminder_worker import show_due


class ServiceLock:
    def __init__(self, task_dir: Path) -> None:
        self.path = task_dir.parent / ".dingtalk-sync-service.lock"
        self.acquired = False

    def acquire(self) -> bool:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(str(os.getpid()))
            self.acquired = True
            return True
        except FileExistsError:
            try:
                if time.time() - self.path.stat().st_mtime > 120:
                    self.path.unlink()
                    return self.acquire()
            except OSError:
                pass
            return False

    def close(self) -> None:
        if self.acquired:
            try:
                self.path.unlink()
            except OSError:
                pass

    def heartbeat(self) -> None:
        if self.acquired:
            try:
                self.path.touch()
            except OSError:
                pass


def run(once: bool = False, remote_seconds: int = 15, debounce_seconds: int = 1) -> int:
    store = TaskStore()
    lock = ServiceLock(store.task_dir)
    if not lock.acquire():
        return 0
    workflow = DwsWorkflow(store)
    revision = store.revision()
    changed_at: float | None = time.monotonic()
    next_remote = time.monotonic()
    next_reminder = time.monotonic() + 300
    try:
        while True:
            now = time.monotonic()
            lock.heartbeat()
            current = store.revision()
            if current != revision:
                revision, changed_at = current, now
            if now >= next_remote or (changed_at is not None and now - changed_at >= debounce_seconds):
                try:
                    workflow.sync_all()
                except Exception:
                    # A later polling cycle retries; local Markdown is never discarded.
                    pass
                revision = store.revision()
                changed_at = None
                next_remote = time.monotonic() + remote_seconds
                if once:
                    return 0
            if now >= next_reminder:
                try:
                    show_due(store)
                except Exception:
                    pass
                next_reminder = time.monotonic() + 300
            time.sleep(1)
    finally:
        lock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--remote-seconds", type=int, default=15)
    args = parser.parse_args()
    raise SystemExit(run(args.once, max(5, args.remote_seconds)))
