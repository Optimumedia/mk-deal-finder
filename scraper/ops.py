"""Keeping the twice-a-day run healthy: one run at a time, catch-up after a
missed slot, and a per-run health summary that flags a source that suddenly
returns far fewer ads than usual (a site redesign usually looks like that:
no error, just nothing found)."""
from __future__ import annotations

import logging
import os
import statistics
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

# The scheduled times on the PC (local time). setup_schedule.ps1 registers the same.
SLOTS = ("12:15", "16:45")
SOURCE_LABELS = {"reklama5": "Reklama5", "kirsm": "bailiff register (KIRSM)", "novel": "novelestate.com",
                 "nedviznosti": "nedviznosti.com.mk"}
# Sources whose item count legitimately swings to ~0 (sitemap: only changed pages are read).
# For these only "could not load a single page" counts as broken.
VOLATILE = {"nedviznosti"}


# ----------------------------------------------------------------- one run at a time
class RunLock:
    """Exclusive lock on a file, held by the operating system for as long as the
    process lives. A crashed or killed run can never leave a stale lock behind."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._f = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        f.seek(0)
        f.truncate()
        f.write(f"pid {os.getpid()} since {datetime.now().isoformat(timespec='seconds')}\n")
        f.flush()
        self._f = f
        return True

    def release(self) -> None:
        if not self._f:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._f.seek(0)
                msvcrt.locking(self._f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._f.close()
        self._f = None

    def held_elsewhere(self) -> bool:
        if self.acquire():
            self.release()
            return False
        return True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()


# ----------------------------------------------------------------- catch-up
def _parse(ts: str) -> datetime:
    d = datetime.fromisoformat(ts)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def missed_slot(now_local: datetime, runs: list[tuple[str, str]], slots=SLOTS,
                grace_minutes: int = 20) -> datetime | None:
    """The most recent scheduled slot that passed without a finished run, or None.

    `runs` are (started_at UTC ISO, status) pairs. A slot counts as done when a run
    started after it finished (ok, blocked or error). A run that died part-way
    (interrupted / still 'running' with nobody holding the lock) does not, so the
    next check retries, but only once: two attempts after one slot is enough.
    """
    tz = now_local.tzinfo
    past = []
    for d in (now_local.date() - timedelta(days=1), now_local.date()):
        for s in slots:
            h, m = (int(x) for x in s.split(":"))
            at = datetime.combine(d, dtime(h, m), tzinfo=tz)
            if at + timedelta(minutes=grace_minutes) <= now_local:
                past.append(at)
    if not past:
        return None
    slot = max(past)
    since = [st for ts, st in runs if ts and _parse(ts) >= slot - timedelta(minutes=10)]
    if any(st in ("ok", "blocked", "error") for st in since) or len(since) >= 2:
        return None
    return slot


# ----------------------------------------------------------------- watching the log
class Counter(logging.Handler):
    """Counts warnings and errors per phase of the run, and remembers the size of
    the detail queue the detail step reports."""

    def __init__(self):
        super().__init__(logging.NOTSET)       # must see INFO to catch the detail-queue line
        self.phase = "start"
        self.warnings: dict[str, int] = {}
        self.detail_queue: int | None = None
        self.first: dict[str, str] = {}

    def handle(self, record: logging.LogRecord) -> bool:      # sees INFO too, counts WARNING+
        if isinstance(record.msg, str) and record.msg.startswith("detail queue:") and record.args:
            self.detail_queue = record.args[0]
        if record.levelno >= logging.WARNING:
            self.warnings[self.phase] = self.warnings.get(self.phase, 0) + 1
            self.first.setdefault(self.phase, record.getMessage()[:160])
        return True

    def emit(self, record):                                     # handle() does the work
        pass

    def total(self) -> int:
        return sum(self.warnings.values())


# ----------------------------------------------------------------- health
def broken_sources(current: dict, history: list[dict], min_ratio: float = 0.4, min_usual: int = 10,
                   min_history: int = 3) -> list[dict]:
    """Sources that look broken in this run.

    current: {source: {"pages": n, "seen": n, ...}} for this run.
    history: the same dicts from recent successful runs of the same kind (full / quick).
    A source is broken when it loaded no page at all, or when it saw fewer than
    `min_ratio` x its usual number of ads (median of the recent runs).
    """
    out = []
    for src, c in current.items():
        if c.get("pages", 0) == 0:
            out.append({"source": src, "seen": c.get("seen", 0), "usual": None, "why": "no page could be loaded"})
            continue
        if src in VOLATILE:
            continue
        past = [h[src]["seen"] for h in history
                if src in h and h[src].get("seen") is not None and not h[src].get("broken")]
        if len(past) < min_history:
            continue
        usual = statistics.median(past)
        if usual >= min_usual and c.get("seen", 0) < min_ratio * usual:
            out.append({"source": src, "seen": c.get("seen", 0), "usual": round(usual),
                        "why": f"{c.get('seen', 0):,} ads instead of the usual ~{round(usual):,}"})
    return out


def details_look_broken(read: int, queue: int | None, budget: int) -> bool:
    """Most of the detail pages it tried could not be read (parser or site change)."""
    if not queue:
        return False
    tried = min(queue, budget)
    return tried >= 10 and read < 0.4 * tried


def plain_summary(broken: list[dict]) -> str:
    return "; ".join(f"{SOURCE_LABELS.get(b['source'], b['source'])}: {b['why']}" for b in broken)


def file_mb(path: str | Path) -> float:
    try:
        return round(Path(path).stat().st_size / 1_048_576, 1)
    except OSError:
        return 0.0
