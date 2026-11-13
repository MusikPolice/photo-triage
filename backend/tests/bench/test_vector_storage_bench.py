"""SQLite blobs + numpy vs. sqlite-vec for the CLIP embeddings (#55, plan §12).

Not in the default suite: `just bench` runs it. It needs sqlite-vec, which isn't
a dependency, so run it as

    cd backend && uv run --no-sync --with sqlite-vec pytest -m bench -s \\
        tests/bench/test_vector_storage_bench.py

and it skips without. It writes `VECTORS` random 512-d unit vectors, with
`DUPLICATES` planted near-duplicate pairs, to a scratch database: once as blobs
in an `embeddings` table shaped like the real one, and once in a sqlite-vec
`vec0` table. Then, for each option in a fresh process, with the database file
dropped from the OS page cache first:

- **cold load:** blobs: read every row into one numpy matrix. sqlite-vec: open the
  database and run the first query, since it has nothing to load.
- **API memory:** the process's resident memory after loading, less before.
- **top-k query:** the median time for the `TOP_K` nearest of a query vector, as
  a text search does.
- **all-pairs:** every pair at cosine ≥ `THRESHOLD`, as duplicate grouping does
  (plan §6.7): blockwise matrix products for numpy; a k-nearest query per vector
  for sqlite-vec, timed on `VEC_PAIR_SAMPLE` vectors and scaled up.

Random vectors are further apart than real CLIP embeddings, which doesn't change
brute-force timings: every pair is compared either way.
"""

import json
import os
import sqlite3
import statistics
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.bench

VECTORS = 100_000
DIM = 512
DUPLICATES = 1_000
THRESHOLD = 0.95
TOP_K = 100
QUERIES = 20
BLOCK = 4096
VEC_PAIR_SAMPLE = 500
VEC_PAIR_K = 10
SEED = 55


def _rss_bytes() -> int:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) * 1024
    raise RuntimeError("no VmRSS")


def _drop_from_page_cache(path: Path) -> None:
    for name in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if name.exists():
            fd = os.open(name, os.O_RDONLY)
            try:
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            finally:
                os.close(fd)


def _vectors(rng: np.random.Generator) -> np.ndarray:
    m = rng.standard_normal((VECTORS, DIM), dtype=np.float32)
    # The last DUPLICATES rows are near-copies of the first DUPLICATES.
    noise = rng.standard_normal((DUPLICATES, DIM), dtype=np.float32) * 0.01
    m[-DUPLICATES:] = m[:DUPLICATES] + noise * np.linalg.norm(m[:DUPLICATES], axis=1)[:, None]
    m /= np.linalg.norm(m, axis=1, keepdims=True)
    return m


def _connect(path: Path, *, vec: bool) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    if vec:
        import sqlite_vec  # pyright: ignore[reportMissingImports]

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)  # pyright: ignore[reportUnknownMemberType]
        conn.enable_load_extension(False)
    return conn


def build(path: Path) -> None:
    m = _vectors(np.random.default_rng(SEED))
    conn = _connect(path, vec=True)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(
        "CREATE TABLE embeddings (item_id INTEGER PRIMARY KEY, model TEXT NOT NULL, "
        "vector BLOB NOT NULL)"
    )
    conn.execute(
        f"CREATE VIRTUAL TABLE vec USING vec0(embedding float[{DIM}] distance_metric=cosine)"
    )
    rows = [(i + 1, "ViT-B-32", m[i].astype("<f4").tobytes()) for i in range(VECTORS)]
    conn.executemany("INSERT INTO embeddings VALUES (?, ?, ?)", rows)
    conn.executemany(
        "INSERT INTO vec(rowid, embedding) VALUES (?, ?)", [(r[0], r[2]) for r in rows]
    )
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    np.save(path.with_suffix(".queries.npy"), _vectors(np.random.default_rng(SEED + 1))[:QUERIES])


def _table_bytes(conn: sqlite3.Connection, prefix: str) -> int:
    """Pages used by tables and indexes whose names start with `prefix`."""
    try:
        row = conn.execute(
            "SELECT sum(pgsize) FROM dbstat WHERE name LIKE ?", (f"{prefix}%",)
        ).fetchone()
        return int(row[0] or 0)
    except sqlite3.OperationalError:
        return -1  # dbstat not compiled in


def run_numpy(path: Path) -> dict[str, float]:
    queries = np.load(path.with_suffix(".queries.npy"))
    before = _rss_bytes()
    t0 = time.perf_counter()
    conn = _connect(path, vec=False)
    blobs = [b for (b,) in conn.execute("SELECT vector FROM embeddings ORDER BY item_id")]
    m = np.frombuffer(b"".join(blobs), dtype="<f4").reshape(len(blobs), DIM).copy()
    del blobs
    load_s = time.perf_counter() - t0
    memory = _rss_bytes() - before

    times = []
    for q in queries:
        t = time.perf_counter()
        scores = m @ q
        top = np.argpartition(-scores, TOP_K)[:TOP_K]
        top = top[np.argsort(-scores[top])]
        times.append(time.perf_counter() - t)

    t = time.perf_counter()
    pairs = 0
    for start in range(0, len(m), BLOCK):
        block = m[start : start + BLOCK] @ m[start:].T
        i, j = np.nonzero(block >= THRESHOLD)
        pairs += int(np.count_nonzero(j > i))
    pairs_s = time.perf_counter() - t
    return {
        "load_s": load_s,
        "memory_mb": memory / 1e6,
        "query_ms": statistics.median(times) * 1000,
        "all_pairs_s": pairs_s,
        "pairs": pairs,
        "disk_mb": _table_bytes(conn, "embeddings") / 1e6,
    }


def run_vec(path: Path) -> dict[str, float]:
    queries = np.load(path.with_suffix(".queries.npy"))
    knn = "SELECT rowid, distance FROM vec WHERE embedding MATCH ? AND k = ?"
    before = _rss_bytes()
    t0 = time.perf_counter()
    conn = _connect(path, vec=True)
    conn.execute(knn, (queries[0].tobytes(), TOP_K)).fetchall()
    load_s = time.perf_counter() - t0
    memory = _rss_bytes() - before

    times = []
    for q in queries:
        t = time.perf_counter()
        conn.execute(knn, (q.tobytes(), TOP_K)).fetchall()
        times.append(time.perf_counter() - t)

    # Every vector's nearest neighbours, sampled: the planted duplicates' first
    # halves (rowids 1..) and the rest alike.
    sample = np.random.default_rng(SEED + 2).choice(VECTORS, VEC_PAIR_SAMPLE, replace=False) + 1
    t = time.perf_counter()
    found = 0
    for rowid in sample:
        (blob,) = conn.execute(
            "SELECT embedding FROM vec WHERE rowid = ?", (int(rowid),)
        ).fetchone()
        for other, distance in conn.execute(knn, (blob, VEC_PAIR_K)):
            if other != rowid and 1 - distance >= THRESHOLD:
                found += 1
    sample_s = time.perf_counter() - t
    memory = max(memory, _rss_bytes() - before)
    return {
        "load_s": load_s,
        "memory_mb": memory / 1e6,
        "query_ms": statistics.median(times) * 1000,
        "all_pairs_s": sample_s * VECTORS / VEC_PAIR_SAMPLE,
        "pairs_in_sample": found,
        "disk_mb": _table_bytes(conn, "vec") / 1e6,
    }


def _in_fresh_process(mode: str, path: Path) -> dict[str, float]:
    _drop_from_page_cache(path)
    out = subprocess.run(
        [sys.executable, __file__, mode, str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


def test_vector_storage(tmp_path: Path) -> None:
    pytest.importorskip("sqlite_vec")
    path = tmp_path / "vectors.db"
    t = time.perf_counter()
    build(path)
    print(f"\nBuilt {VECTORS:,} vectors in {time.perf_counter() - t:.1f} s")
    results = {mode: _in_fresh_process(mode, path) for mode in ("numpy", "vec")}
    print(json.dumps(results, indent=2))
    # The planted pairs are all found by the exhaustive pass.
    assert results["numpy"]["pairs"] >= DUPLICATES


if __name__ == "__main__":
    mode, db = sys.argv[1], Path(sys.argv[2])
    print(json.dumps((run_numpy if mode == "numpy" else run_vec)(db)))
