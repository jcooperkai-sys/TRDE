# Quarry design notes

The file format and the algorithms, in enough detail to reimplement or debug
them. Everything here is 4 KiB pages in a single file; there is no second file
except a transient journal.

## 1. File layout

```
page 0      header (magic, page size, page count, freelist head, catalog root)
page 1..n   heap pages, B+tree pages, overflow pages, catalog pages, free pages
```

Header (little endian, at offset 0 of page 0):

| offset | size | field |
| --- | --- | --- |
| 0 | 16 | magic `QUARRYDB\0\0\0\0v001` |
| 16 | 4 | page size (4096) |
| 20 | 4 | page count |
| 24 | 4 | freelist head page (0 = none) |
| 28 | 4 | catalog root page (0 = empty schema) |
| 32 | 4 | schema version, bumped on every DDL |

Freed pages form a singly linked list through their `link` field, so space is
recycled without ever shrinking the file.

## 2. The slotted page

Every non-header page starts with the same 16-byte header:

```
0  page type      1  flags       2  slot count
4  free_start     6  free_end    8  link        12  extra
```

Slots (4 bytes: offset, length) grow upward from byte 16; cell payloads grow
downward from the end. Free space is `free_end - free_start`. Deleting a heap
row zeroes its slot offset — a tombstone that keeps later slot numbers, and
therefore rowids, stable. `compact()` repacks live cells and reclaims the
holes; the B+tree and the heap both call it before concluding a page is full.

## 3. Rows and rowids

A row is a tagged list of values (`values.serialize_record`): a varint field
count, then per value a type byte plus payload — zigzag varint for integers,
8 bytes for reals, length-prefixed bytes for text and blobs.

A heap cell is `\x00` + the record, or, when the record exceeds 1000 bytes,
`\x01` + total length + first overflow page. Overflow pages hold 4080 bytes
each and chain through `link`.

```
rowid = (page number << 16) | slot index
```

Updates rewrite in place when the new record fits; otherwise the row is
relocated and its rowid changes, which is why every index entry is rewritten on
update rather than patched.

## 4. Index keys

`values.encode_key` maps a value to bytes whose lexicographic order is SQL
order:

| value | encoding |
| --- | --- |
| NULL | `00` |
| number | `10` + order-preserving double + subtype byte + exact 8 bytes |
| text | `20` + UTF-8 with `00` escaped as `00 ff`, terminated `00 00` |
| blob | `30` + same escaping |

The order-preserving double is the IEEE-754 bit pattern with the sign bit
flipped for positives and all bits inverted for negatives — the standard trick
that turns float bits into unsigned integers that sort correctly.

Numbers lead with their magnitude as a double so an INTEGER `5` and a REAL
`5.0` share a prefix; the exact value follows so `5` and `5.0000001` never
collide. Equality probes are therefore *prefix scans*, not point lookups.

Escaping text this way keeps the encoding self-delimiting, so a multi-column
key is just concatenation. Every index key ends with the 8-byte rowid, making
all keys unique — the B+tree never has to handle duplicates, and deleting one
row out of many sharing a value is a point delete.

## 5. The B+tree

Leaf cell: `u16 key length, key, value`. `link` points at the next leaf.
Internal cell: `u16 key length, key, u32 child`. `link` is the rightmost child.

Cell *j* of an internal node means "keys strictly less than key *j* live in
child *j*"; keys at or above the last separator live in `link`. Descent is
`upper_bound(key)` at each level. Searches decode only the O(log n) cells the
binary search probes — decoding whole pages was, measurably, the single
biggest cost in the engine before that changed.

**Insert.** Descend to a leaf, insert in place if it fits (compacting first if
fragmentation is the only problem). Otherwise rebuild the cell list, split it
at the point that balances *bytes* rather than cell count, and return the
separator to the parent, which repeats the process. A root split copies the old
root into a freshly allocated page and reinitializes the root page as an
internal node — so the root page number, which the catalog stores, never
changes.

**Delete.** Remove from the leaf, then rebalance on the way back up. A node
underflows when its live payload drops below a third of the usable page. Try to
borrow from the left sibling, then the right; if neither can spare anything,
merge the two siblings and drop the separator from the parent. If a merge would
overflow the page, the two nodes redistribute evenly instead. When the root
ends up with no separators, its only child is copied back into the root page
and freed.

Occupancy is measured as *live* bytes (`_Node.used_bytes`), not as
`PAGE_SIZE - free_end`. That distinction is not cosmetic: counting the holes
left by deleted cells as occupied means a node can be logically empty and still
report itself as full, so it never merges. That bug existed, and
`test_delete_everything` is what caught it.

`BTree.check()` walks the whole tree verifying key order, subtree bounds, and
that the leaf chain agrees with the traversal. The fuzz test calls it every few
hundred operations.

## 6. Transactions

The pager holds modified pages in memory for the life of a transaction and
never writes a partial transaction to disk. On commit:

1. Read the *current* on-disk image of every dirty page that already existed.
2. Write them to `<db>-journal` with a header and an end marker; fsync.
3. Write the new page images to the database file; fsync.
4. Delete the journal.

A crash before step 2 completes leaves a journal without its end marker, which
is discarded — nothing was modified yet. A crash during step 3 leaves a
complete journal, which the next open replays, restoring every page and
truncating the file back to its pre-transaction page count. Replay is
idempotent, so crashing during recovery is safe too.

`ROLLBACK` needs no journal at all: dropping the dirty pages from the cache and
restoring the in-memory header (page count, freelist head, catalog root) undoes
everything, because nothing reached the file.

One consequence worth knowing: page eviction is disabled inside a transaction.
Callers hold page buffers across other page loads, and evicting a buffer out
from under a caller would silently discard writes. Outside a transaction
nothing is mutated, so the LRU runs normally.

## 7. Catalog

The schema is one JSON document — tables, columns, types, constraints, index
names and index root pages — stored in a chain of pages. The header points at
the head. DDL rewrites the whole document, allocates a new chain, swaps the
header pointer, and frees the old chain, all inside the statement's
transaction. That is why `ROLLBACK` undoes a `CREATE TABLE` as cleanly as it
undoes an `INSERT`.

## 8. Query execution

A row in flight is a flat tuple: all of table 0's columns, then table 0's
rowid, then table 1's columns, then its rowid, and so on. `SourceSet` maps
`(alias, column)` and unqualified names to offsets, marking a name ambiguous
when two tables share it.

**Access paths.** `WHERE` and each `ON` are split on `AND`. For the table at
each join level, a conjunct is usable if it references that table and otherwise
only tables already bound by outer loops. From those:

* `rowid = expr` → direct heap fetch.
* equality on a leading prefix of an index's columns → index seek, preferring
  the longest prefix and then unique indexes.
* a range (`<`, `<=`, `>`, `>=`, `BETWEEN`) on an index's first column → index
  range scan.
* otherwise a full scan.

Because a usable conjunct may reference an outer table's column, this yields
index nested-loop joins for free: `JOIN o ON o.user_id = u.id` seeks
`ix_orders_user` once per user row rather than scanning orders.

The full `WHERE` is then evaluated against every assembled row anyway. The
index narrows the candidate set; it never determines the answer.

**Joins** are nested loops. A `LEFT JOIN` tracks whether the inner loop
produced any surviving row and, if not, emits one row with the inner table's
columns set to NULL.

**Grouping** hashes each row on the group key (typed, so `1` and `'1'` are
different groups), stepping one `Aggregator` per aggregate per group. Bare
`SELECT` expressions are then evaluated against the group's first row with a
map from aggregate node identity to its computed value, which is how
`HAVING` and `ORDER BY` can reference aggregates.

**ORDER BY** resolves output aliases and 1-based ordinals before falling back
to evaluating the expression, sorts with the SQL comparison rules (NULLs
first), and only then applies `OFFSET` and `LIMIT`.

## 9. Deliberate omissions

No subqueries, no `UNION`, no `RIGHT`/`FULL` joins, no `ALTER TABLE`, no
foreign keys, no views, no triggers, no `CAST`. No locking or concurrency: one
process, one connection. Sorting and grouping are in-memory. Index keys are
capped at 900 bytes so that at least four fit in a page and splits always make
progress.
