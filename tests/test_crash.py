"""Crash recovery, verified by actually killing a process mid-commit.

A forked child opens the database, does real work, and is terminated with
``os._exit`` partway through writing its pages -- after the journal has been
fsynced but before the data pages are all on disk, which is exactly the window
the journal exists to cover.  The parent then reopens the file and checks that
the database is intact at its pre-crash state.
"""

import os
import shutil
import struct
import tempfile
import unittest

from quarry import connect
from quarry.btree import BTree
from quarry.pager import Pager


def _child_crashes_mid_commit(path, halt_after):
    """Runs in the forked child; never returns."""
    db = connect(path)
    pager = db.pager
    original_flush = pager._flush_all

    def partial_flush():
        """Write only the first few dirty pages, then die like a power cut."""
        for count, page_id in enumerate(sorted(pager._dirty)):
            if count >= halt_after:
                break
            pager.f.seek(page_id * 4096)
            pager.f.write(bytes(pager._cache[page_id]))
        pager.f.flush()
        os.fsync(pager.f.fileno())
        os._exit(9)

    pager._flush_all = partial_flush
    db.execute("BEGIN")
    for i in range(500, 900):
        db.execute("INSERT INTO t VALUES (?, ?)", (i, "new-%d" % i))
    db.execute("DELETE FROM t WHERE id < 100")
    db.execute("COMMIT")
    os._exit(0)  # pragma: no cover - the commit above always exits first


class CrashRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="quarry-crash-")
        self.path = os.path.join(self.dir, "crash.qdb")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def seed(self):
        db = connect(self.path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
        db.execute("CREATE INDEX ix_name ON t (name)")
        db.execute("BEGIN")
        for i in range(500):
            db.execute("INSERT INTO t VALUES (?, ?)", (i, "row-%d" % i))
        db.execute("COMMIT")
        db.close()

    def crash_child(self, halt_after):
        pid = os.fork()
        if pid == 0:
            try:
                _child_crashes_mid_commit(self.path, halt_after)
            except BaseException:
                os._exit(70)
        _, status = os.waitpid(pid, 0)
        return status

    def test_recovers_to_pre_crash_state(self):
        self.seed()
        status = self.crash_child(halt_after=3)
        self.assertNotEqual(status, 0, "child was supposed to die mid-commit")
        self.assertTrue(os.path.exists(self.path + "-journal"),
                        "a crash mid-commit must leave the journal behind")

        db = connect(self.path)
        try:
            self.assertFalse(os.path.exists(self.path + "-journal"),
                             "opening the database must replay and clear the journal")
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM t"), 500)
            self.assertEqual(db.execute("SELECT name FROM t WHERE id = 0").rows, [("row-0",)])
            self.assertEqual(db.execute("SELECT name FROM t WHERE id = 499").rows, [("row-499",)])
            self.assertEqual(db.execute("SELECT id FROM t WHERE name = 'new-600'").rows, [],
                             "uncommitted inserts must not survive")
            # Indexes must agree with the heap after recovery.
            by_index = db.execute("SELECT id FROM t WHERE name = 'row-250'").rows
            self.assertEqual(by_index, [(250,)])
            for meta in db.catalog.get_table("t").indexes:
                BTree(db.pager, meta.root).check()
        finally:
            db.close()

    def test_recovery_is_idempotent(self):
        """Crashing during recovery itself must still leave a consistent file."""
        self.seed()
        self.crash_child(halt_after=2)
        journal = self.path + "-journal"
        with open(journal, "rb") as handle:
            saved = handle.read()
        db = connect(self.path)
        db.close()
        # Replay the very same journal a second time; the result must not change.
        with open(journal, "wb") as handle:
            handle.write(saved)
        db = connect(self.path)
        try:
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM t"), 500)
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM t WHERE name LIKE 'new-%'"), 0)
        finally:
            db.close()

    def test_writes_after_recovery_still_work(self):
        self.seed()
        self.crash_child(halt_after=4)
        db = connect(self.path)
        try:
            db.execute("INSERT INTO t VALUES (10000, 'after-recovery')")
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM t"), 501)
        finally:
            db.close()
        db = connect(self.path)
        try:
            self.assertEqual(db.execute("SELECT name FROM t WHERE id = 10000").rows,
                             [("after-recovery",)])
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
