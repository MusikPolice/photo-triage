"""The numbers for `just dry-run-full report` (dev-environment §5).

Runs inside the dry run's worker container, on its database, with the standard
library alone: `scripts/dry_run_full.sh report` pipes it to `python -`. Prints
the items found, the latest walk's time, each stage's throughput (per busy
minute, from `job_stats`, and per wall-clock minute since its earliest job that's
still recorded as done), what's left and how long that would take at that rate,
and the container's peak memory.
"""

import datetime as dt
import sqlite3
from pathlib import Path

DB = Path("/data/photo-triage.db")
CGROUP = Path("/sys/fs/cgroup")
ITEM_STAGES = ("scan", "metadata_read", "frames", "thumbnail", "clip")


def _at(text: str | None) -> dt.datetime | None:
    if text is None:
        return None
    at = dt.datetime.fromisoformat(text)
    return at if at.tzinfo else at.replace(tzinfo=dt.UTC)


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} days"


def _memory() -> str:
    def read(name: str) -> int | None:
        path = CGROUP / name
        return int(path.read_text().split()[0]) if path.exists() else None

    current, peak = read("memory.current"), read("memory.peak")
    if current is None:
        return "unknown (no cgroup v2 memory files)"
    text = f"{current / 2**20:.0f} MiB now"
    if peak is not None:
        text += f", {peak / 2**20:.0f} MiB peak"
    limit = (CGROUP / "memory.max").read_text().strip()
    if limit != "max":
        text += f", of {int(limit) / 2**20:.0f} MiB"
    return text


def main() -> None:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    items = conn.execute("SELECT media_type, count(*) FROM items GROUP BY media_type").fetchall()
    total = sum(n for _, n in items)
    print(f"Items: {total:,} ({', '.join(f'{n:,} {kind}' for kind, n in items)})")

    walk = conn.execute(
        "SELECT started_at, finished_at FROM jobs WHERE stage = 'walk' AND status = 'done' "
        "ORDER BY finished_at DESC LIMIT 1"
    ).fetchone()
    started_at, finished_at = (_at(walk[0]), _at(walk[1])) if walk else (None, None)
    if started_at and finished_at:
        took = (finished_at - started_at).total_seconds()
        print(f"Latest walk: {_duration(took)}, finished {finished_at:%Y-%m-%d %H:%M} UTC")
    else:
        print("Latest walk: none finished yet")

    now = dt.datetime.now(dt.UTC)
    print()
    print(f"{'stage':<14}{'done':>9}{'left':>9}{'/busy min':>11}{'/wall min':>11}  time left")
    for stage in ITEM_STAGES:
        done, left = conn.execute(
            "SELECT sum(status = 'done'), sum(status IN ('pending', 'error', 'running')) "
            "FROM jobs WHERE stage = ?",
            (stage,),
        ).fetchone()
        processed, busy_s = conn.execute(
            "SELECT sum(processed), sum(busy_seconds) FROM job_stats WHERE stage = ?",
            (stage,),
        ).fetchone()
        (first,) = conn.execute(
            "SELECT min(started_at) FROM jobs WHERE stage = ? AND status = 'done'", (stage,)
        ).fetchone()
        done, left, processed = done or 0, left or 0, processed or 0
        busy_rate = processed / (busy_s / 60) if busy_s else 0.0
        first_at = _at(first)
        wall_rate = (
            processed / ((now - first_at).total_seconds() / 60)
            if first_at and now > first_at
            else 0.0
        )
        eta = _duration(left / wall_rate * 60) if wall_rate and left else "-"
        print(f"{stage:<14}{done:>9,}{left:>9,}{busy_rate:>11.1f}{wall_rate:>11.1f}  {eta}")

    print()
    print(f"Worker container memory: {_memory()}")
    print(
        "Wall-clock rates count from each stage's earliest finished job, and stages "
        "share the worker, so the slowest stage's time left is the whole run's."
    )


main()
