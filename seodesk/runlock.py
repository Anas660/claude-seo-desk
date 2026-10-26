"""One run at a time: a scheduled run and "Run now" on the dashboard share a lock file in data/."""
from __future__ import annotations

import time
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator, Optional

from .settings import data_dir, log_dir

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
    import msvcrt


class AlreadyRunning(Exception):
    pass


def lock_file() -> Path:
    return data_dir() / "run.lock"


def run_log() -> Path:
    return log_dir() / "latest-run.log"


def _lock(handle: IO[str]) -> None:
    """Raises OSError when another process holds the lock."""
    if fcntl is not None:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)


def _unlock(handle: IO[str]) -> None:
    if fcntl is not None:
        fcntl.flock(handle, fcntl.LOCK_UN)
    else:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def run_lock(path: Optional[Path] = None, wait_seconds: float = 0) -> Iterator[None]:
    """Takes the lock, waiting up to `wait_seconds` for a running job to finish."""
    path = path or lock_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w")
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            _lock(handle)
            break
        except OSError:
            if time.monotonic() >= deadline:
                handle.close()
                raise AlreadyRunning from None
            time.sleep(5)
    try:
        yield
    finally:
        _unlock(handle)
        handle.close()


def is_running(path: Optional[Path] = None) -> bool:
    path = path or lock_file()
    if not path.exists():
        return False
    try:
        with run_lock(path):
            return False
    except AlreadyRunning:
        return True
