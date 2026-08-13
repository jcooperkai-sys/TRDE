# Quarry

A relational database engine written from scratch in pure Python — no
dependencies, no `sqlite3`, no ORM, nothing but the standard library and a file
on disk.

It has the parts a real database has: a pager over fixed-size pages, slotted
heap storage with overflow chains, B+tree indexes, a rollback journal for
crash-safe transactions, a SQL parser, a query planner that picks index access
paths, and a shell.

```
$ python3 -m quarry shop.qdb
Quarry 1.0.0 -- a database engine built from scratch.
quarry> CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, city TEXT);
CREATE TABLE users
quarry> INSERT INTO users VALUES (1,'ada','london'),(2,'grace','nyc'),(3,'alan','london');
3 rows inserted
quarry> SELECT city, COUNT(*) AS n FROM users GROUP BY city ORDER BY n DESC;
city   | n
-------+--
london | 2
nyc    | 1
(2 rows)
quarry> EXPLAIN SELECT * FROM users WHERE id = 2;
detail
------------------------------------------
SCAN users USING INDEX users_pkey (id=?)
(1 rows)
```

From Python:

```python
from quarry import connect

db = connect("shop.qdb")            # or ":memory:"
db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
db.execute("INSERT INTO t VALUES (?, ?)", (1, "ada"))

with db.transaction():              # commits on exit, rolls back on exception
    db.execute("UPDATE t SET name = 'Ada' WHERE id = 1")

db.execute("SELECT name FROM t WHERE id = ?", (1,)).rows   # [('Ada',)]
db.execute("SELECT * FROM t").dicts()                      # [{'id': 1, 'name': 'Ada'}]
db.close()
```

## How it is built

| Layer | File | What it does |
| --- | --- | --- |
| Pages | `quarry/page.py` | 4 KiB slotted pages: 16-byte header, slot array growing up, cells growing down, compaction |
| Pager | `quarry/pager.py` | File I/O, LRU page cache, free-page list, rollback journal, crash recovery |
| Values | `quarry/values.py` | Type coercion, record serialization, **order-preserving** key encoding |
| Heap | `quarry/heap.py` | Row storage as a page chain; rows over 1 KB spill into overflow chains |
| B+tree | `quarry/btree.py` | Split, borrow, merge, root collapse, leaf-chained range scans, `check()` invariant validator |
| Catalog | `quarry/catalog.py` | The schema, stored as JSON in a page chain the file header points at |
| Tables | `quarry/table.py` | Heap + indexes together; NOT NULL / UNIQUE / PRIMARY KEY enforcement |
| SQL front end | `tokenizer.py`, `sqlast.py`, `parser.py` | Tokenizer and recursive-descent parser with real operator precedence |
| Expressions | `quarry/expr.py` | Three-valued logic, 19 scalar functions, 7 aggregates |
| Executor | `quarry/executor.py` | Access-path selection, nested-loop joins, grouping, sorting, DDL/DML |
| API + shell | `database.py`, `cli.py` | `connect()`, transactions, and the REPL |

Three design decisions worth calling out:

**Order-preserving keys.** Index keys are byte strings encoded so that
byte-order equals SQL value order — NULLs first, then numbers, then text, then
blobs. Integers and reals of the same magnitude share a leading prefix, so an
equality probe finds `5` and `5.0` alike. Every index key ends with the row's
`rowid`, which makes duplicate values distinct and lets the B+tree assume
unique keys.

**Indexes accelerate, they never decide.** The planner splits `WHERE` and `ON`
on `AND` and uses any conjunct comparing an indexed column against an
already-bound value (a constant, a parameter, or an outer table's column) to
drive a seek — including for the inner side of a join. The full predicate is
then re-applied to the assembled row regardless, so a planning mistake can cost
time but cannot change an answer. `test_index_and_scan_agree` asserts exactly
that by dropping every index and re-running the same queries.

**Crash safety by journal.** A transaction accumulates modified pages in
memory. On commit the *original* image of every touched page is written to
`<db>-journal` and fsynced, then the new pages are written and fsynced, then
the journal is deleted. A journal found at open time is replayed, restoring the
pre-commit state; a journal missing its end marker never landed intact and is
discarded. `ROLLBACK` just drops the in-memory pages — nothing was written yet.

## SQL supported

```sql
CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT NOT NULL DEFAULT 'x',
                c REAL, d BLOB, UNIQUE (b, c))   -- + IF NOT EXISTS
DROP TABLE [IF EXISTS] t
CREATE [UNIQUE] INDEX ix ON t (a, b)             -- multi-column
DROP INDEX [IF EXISTS] ix

INSERT INTO t (a, b) VALUES (1, 'x'), (2, 'y')
INSERT INTO t SELECT ... FROM other
UPDATE t SET a = a + 1, b = UPPER(b) WHERE ...
DELETE FROM t WHERE ...

SELECT [DISTINCT] cols FROM t [alias]
  [INNER | LEFT | CROSS] JOIN u ON ...           -- any number, self-joins fine
  WHERE ... GROUP BY ... HAVING ...
  ORDER BY expr [ASC|DESC], 2, alias
  LIMIT n [OFFSET m]

BEGIN; COMMIT; ROLLBACK;
EXPLAIN SELECT ...
```

Expressions: `+ - * / %`, `||`, `= == != <> < <= > >=`, `AND OR NOT`,
`IS [NOT] NULL`, `IS`, `[NOT] IN (...)`, `[NOT] BETWEEN`, `[NOT] LIKE`
(`%` / `_`), `CASE [expr] WHEN ... THEN ... ELSE ... END`, `?` parameters,
`x'hex'` blob literals, quoted identifiers (`"a"`, `` `a` ``, `[a]`), and the
`rowid` pseudo-column.

Scalar functions: `ABS LENGTH UPPER LOWER SUBSTR COALESCE IFNULL NULLIF ROUND
TRIM LTRIM RTRIM REPLACE INSTR TYPEOF HEX MIN MAX`.
Aggregates: `COUNT SUM TOTAL AVG MIN MAX GROUP_CONCAT`, each with `DISTINCT`.

NULL behaves the way SQL requires: it propagates through arithmetic, comparisons
against it are unknown, `NULL AND FALSE` is FALSE, `NULL OR TRUE` is TRUE, and
`UNIQUE` indexes permit repeated NULLs. Integer division truncates toward zero
and division by zero yields NULL, matching SQLite.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

156 tests, no dependencies, about 4 seconds:

* **`test_storage.py`** — slotted pages, pager, journal replay of a simulated
  crash, heap overflow rows, page recycling, and a B+tree fuzzed against a
  Python dict over 6000 random insert/delete operations with structural
  invariants (`check()`) verified throughout.
* **`test_sql.py`** — parser, DDL, DML, constraints, joins, aggregates,
  transactions, persistence across reopen, and a 10,000-row scale test.
* **`test_vs_sqlite.py`** — **differential testing**: identical schema, rows and
  36 query templates are run against Quarry and against the standard library's
  `sqlite3`, and the result sets must match — before and after mutations, with
  bound parameters, and with every index dropped.
* **`test_crash.py`** — forks a child, kills it with `os._exit` partway through
  writing its pages, and checks that reopening the file replays the journal back
  to the pre-crash state with every index still consistent.
* **`test_cli.py`** — shell formatting, dot commands, `.dump` round-trip.

## Benchmark

`python3 bench.py 20000` on an Apple M-series laptop, Python 3.9. sqlite3 (a C
engine) is shown for scale, and it wins everything — the point is the order of
magnitude, not the contest.

| Operation | Quarry | sqlite3 |
| --- | --- | --- |
| insert 20,000 rows (one transaction) | 2.52 s | 15.9 ms |
| 200 primary-key lookups | 22.3 ms | 1.3 ms |
| 50 secondary-index lookups | 13.4 ms | 335 µs |
| full scan with `LIKE` | 96.4 ms | 757 µs |
| indexed range scan | 7.3 ms | 27 µs |
| `GROUP BY` over all rows | 117 ms | 4.1 ms |
| `ORDER BY` + `LIMIT 20` | 246 ms | 666 µs |
| file size | 2.9 MB | 808 KB |

Roughly 8,000 inserts/second through `execute()` and 12,000/second through
`executemany()` (which parses the statement once). Parsing accounts for about
0.7 s of that 2.5 s. The file is larger than SQLite's mostly because every
index entry carries a full order-preserving key.

## Limits

Honest list of what it does not do: no subqueries or `UNION`, no `RIGHT`/`FULL`
join, no `ALTER TABLE`, no foreign keys, no views or triggers, no `CAST`, no
concurrency (one process, one connection — there is no locking), and index keys
are capped at 900 bytes. Page size is fixed at 4 KiB. Aggregation and sorting
are done in memory, so a `GROUP BY` over a table larger than RAM will not
finish.

## Layout

```
quarry/       the engine (17 modules, ~4,800 lines)
tests/        156 tests
bench.py      benchmark against sqlite3
docs/DESIGN.md  file format and algorithms in detail
```

MIT licensed.
