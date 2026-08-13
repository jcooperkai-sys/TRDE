"""Benchmark Quarry, with sqlite3 alongside for scale.

    python3 bench.py [row_count]

sqlite3 is a C engine and will win every line; it is here so the numbers have
a reference point rather than floating free.
"""

import os
import random
import shutil
import sqlite3
import sys
import tempfile
import time

from quarry import connect

SCHEMA = """CREATE TABLE t (
    id INTEGER PRIMARY KEY,
    bucket INTEGER,
    name TEXT,
    score REAL
)"""


def timed(fn):
    start = time.time()
    result = fn()
    return time.time() - start, result


def human(seconds):
    if seconds < 1e-3:
        return "%6.1f us" % (seconds * 1e6)
    if seconds < 1:
        return "%6.1f ms" % (seconds * 1e3)
    return "%6.2f s " % seconds


def run(engine, rows, path, execute, commit, close, many=None):
    results = {}
    execute(SCHEMA)
    execute("CREATE INDEX ix_bucket ON t (bucket)")
    rng = random.Random(42)
    data = [(i, i % 1000, "name-%d" % i, rng.uniform(0, 100)) for i in range(rows)]

    def bulk():
        if many:
            many("INSERT INTO t VALUES (?, ?, ?, ?)", data)
        else:
            execute("BEGIN")
            for row in data:
                execute("INSERT INTO t VALUES (?, ?, ?, ?)", row)
            execute("COMMIT")
    results["insert %d rows (1 txn)" % rows] = timed(bulk)[0]

    probes = [rng.randrange(rows) for _ in range(200)]

    def point_lookups():
        for key in probes:
            execute("SELECT name FROM t WHERE id = ?", (key,))
    results["200 primary-key lookups"] = timed(point_lookups)[0]

    def index_lookups():
        for key in probes[:50]:
            execute("SELECT COUNT(*) FROM t WHERE bucket = ?", (key % 1000,))
    results["50 secondary-index lookups"] = timed(index_lookups)[0]

    def full_scan():
        execute("SELECT COUNT(*) FROM t WHERE name LIKE '%999%'")
    results["full scan with LIKE"] = timed(full_scan)[0]

    def range_scan():
        execute("SELECT COUNT(*) FROM t WHERE id BETWEEN 1000 AND 2000")
    results["indexed range scan"] = timed(range_scan)[0]

    def group_by():
        execute("SELECT bucket, COUNT(*), AVG(score) FROM t GROUP BY bucket")
    results["GROUP BY over all rows"] = timed(group_by)[0]

    def sort():
        execute("SELECT id FROM t ORDER BY score DESC LIMIT 20")
    results["ORDER BY + LIMIT 20"] = timed(sort)[0]

    def updates():
        execute("UPDATE t SET score = score + 1 WHERE bucket = 7")
    results["UPDATE one bucket"] = timed(updates)[0]

    def deletes():
        execute("DELETE FROM t WHERE bucket = 11")
    results["DELETE one bucket"] = timed(deletes)[0]

    commit()
    close()
    results["_bytes"] = os.path.getsize(path)
    return results


def bench_quarry(rows, directory):
    path = os.path.join(directory, "bench.qdb")
    db = connect(path)
    return run("quarry", rows, path, db.execute, lambda: None, db.close)


def bench_sqlite(rows, directory):
    path = os.path.join(directory, "bench.sqlite")
    conn = sqlite3.connect(path)

    def execute(sql, params=()):
        return conn.execute(sql, params).fetchall()

    return run("sqlite3", rows, path, execute, conn.commit, conn.close,
               many=lambda sql, data: (conn.executemany(sql, data), conn.commit()))


def main():
    rows = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
    directory = tempfile.mkdtemp(prefix="quarry-bench-")
    try:
        quarry_results = bench_quarry(rows, directory)
        sqlite_results = bench_sqlite(rows, directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)

    print("Quarry benchmark -- %d rows, page size 4096" % rows)
    print("%-32s %10s %10s   %s" % ("operation", "quarry", "sqlite3", "ratio"))
    print("-" * 68)
    for key in quarry_results:
        if key.startswith("_"):
            continue
        mine, theirs = quarry_results[key], sqlite_results[key]
        ratio = ("%.0fx" % (mine / theirs)) if theirs > 0 else "-"
        print("%-32s %10s %10s   %s" % (key, human(mine), human(theirs), ratio))
    print("-" * 68)
    print("%-32s %9.1f K %9.1f K" % ("file size",
                                     quarry_results["_bytes"] / 1024.0,
                                     sqlite_results["_bytes"] / 1024.0))


if __name__ == "__main__":
    main()
