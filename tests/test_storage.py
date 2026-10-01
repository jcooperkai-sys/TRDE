"""Tests for the pager, heap store and B+tree."""

import os
import random
import shutil
import struct
import tempfile
import unittest

from trde.btree import BTree
from trde.errors import StorageError
from trde.heap import HeapFile, make_rowid, split_rowid
from trde.page import PAGE_SIZE, SlottedPage, init_page, PAGE_HEAP
from trde.pager import Pager
from trde import values


class TempDBCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="trde-test-")
        self.path = os.path.join(self.dir, "test.trde")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def pager(self):
        return Pager(self.path)


class SlottedPageTest(unittest.TestCase):
    def page(self):
        buf = bytearray(PAGE_SIZE)
        init_page(buf, PAGE_HEAP)
        return SlottedPage(buf)

    def test_append_and_read(self):
        page = self.page()
        self.assertEqual(page.append_cell(b"hello"), 0)
        self.assertEqual(page.append_cell(b"world"), 1)
        self.assertEqual(page.cell(0), b"hello")
        self.assertEqual(page.cell(1), b"world")
        self.assertEqual(page.num_slots, 2)

    def test_insert_shifts_slots(self):
        page = self.page()
        page.append_cell(b"a")
        page.append_cell(b"c")
        page.insert_cell(1, b"b")
        self.assertEqual(page.cells(), [b"a", b"b", b"c"])

    def test_remove_and_kill(self):
        page = self.page()
        for token in (b"a", b"b", b"c"):
            page.append_cell(token)
        page.remove_slot(1)
        self.assertEqual(page.cells(), [b"a", b"c"])
        page.kill_slot(0)
        self.assertIsNone(page.cell(0))
        self.assertEqual(page.live_slots(), [1])

    def test_compaction_reclaims_dead_cells(self):
        page = self.page()
        payload = b"x" * 200
        for _ in range(10):
            page.append_cell(payload)
        before = page.free_space
        for i in range(0, 10, 2):
            page.kill_slot(i)
        self.assertEqual(page.free_space, before)
        self.assertEqual(page.fragmented_space(), 5 * 200)
        page.compact()
        self.assertEqual(page.free_space, before + 5 * 200)
        self.assertEqual([c for c in page.cells() if c], [payload] * 5)

    def test_page_full_raises(self):
        page = self.page()
        with self.assertRaises(StorageError):
            page.append_cell(b"x" * PAGE_SIZE)

    def test_replace_cell_in_place_and_relocated(self):
        page = self.page()
        page.append_cell(b"abcdef")
        page.append_cell(b"tail")
        self.assertTrue(page.replace_cell(0, b"xy"))
        self.assertEqual(page.cell(0), b"xy")
        self.assertTrue(page.replace_cell(0, b"z" * 50))
        self.assertEqual(page.cell(0), b"z" * 50)
        self.assertEqual(page.cell(1), b"tail")


class PagerTest(TempDBCase):
    def test_create_and_reopen(self):
        pager = self.pager()
        pager.begin()
        page_id = pager.allocate_page()
        pager.get_page(page_id)[100] = 42
        pager.mark_dirty(page_id)
        pager.commit()
        pager.close()

        pager = self.pager()
        self.assertEqual(pager.get_page(page_id)[100], 42)
        pager.close()

    def test_rollback_discards_changes(self):
        pager = self.pager()
        pager.begin()
        page_id = pager.allocate_page()
        pager.get_page(page_id)[0] = 9
        pager.mark_dirty(page_id)
        pager.commit()

        pager.begin()
        pager.get_page(page_id)[0] = 77
        pager.mark_dirty(page_id)
        pager.rollback()
        self.assertEqual(pager.get_page(page_id)[0], 9)
        pager.close()

    def test_rollback_releases_new_pages(self):
        pager = self.pager()
        before = pager.page_count
        pager.begin()
        pager.allocate_page()
        pager.allocate_page()
        pager.rollback()
        self.assertEqual(pager.page_count, before)
        pager.close()

    def test_freelist_reuses_pages(self):
        pager = self.pager()
        pager.begin()
        a = pager.allocate_page()
        b = pager.allocate_page()
        pager.free_page(a)
        pager.free_page(b)
        self.assertEqual(pager.allocate_page(), b)
        self.assertEqual(pager.allocate_page(), a)
        pager.commit()
        pager.close()

    def test_write_outside_transaction_rejected(self):
        pager = self.pager()
        with self.assertRaises(Exception):
            pager.allocate_page()
        pager.close()

    def test_journal_replay_restores_previous_image(self):
        pager = self.pager()
        pager.begin()
        page_id = pager.allocate_page()
        pager.get_page(page_id)[0:4] = b"good"
        pager.mark_dirty(page_id)
        pager.commit()
        pager.close()

        # Simulate a crash: journal the old image, scribble over the page, and
        # leave the journal behind as if the process died mid-commit.
        pager = self.pager()
        pager.begin()
        pager.get_page(page_id)[0:4] = b"BAD!"
        pager.mark_dirty(page_id)
        pager._write_journal(sorted(pager._dirty))
        pager._flush_all()
        pager.f.close()
        pager._closed = True

        recovered = self.pager()
        self.assertEqual(bytes(recovered.get_page(page_id)[0:4]), b"good")
        self.assertFalse(os.path.exists(recovered.journal_path))
        recovered.close()

    def test_truncated_journal_is_ignored(self):
        pager = self.pager()
        pager.begin()
        page_id = pager.allocate_page()
        pager.get_page(page_id)[0:4] = b"keep"
        pager.mark_dirty(page_id)
        pager.commit()
        pager.close()
        with open(self.path + "-journal", "wb") as jf:
            jf.write(b"QRYJRNL1" + struct.pack("<II", 1, 1) + b"\x00" * 10)
        pager = self.pager()
        self.assertEqual(bytes(pager.get_page(page_id)[0:4]), b"keep")
        pager.close()


class HeapTest(TempDBCase):
    def setUp(self):
        TempDBCase.setUp(self)
        self.p = self.pager()
        self.p.begin()
        self.heap = HeapFile.create(self.p)

    def tearDown(self):
        try:
            self.p.close()
        finally:
            TempDBCase.tearDown(self)

    def test_rowid_roundtrip(self):
        self.assertEqual(split_rowid(make_rowid(12345, 42)), (12345, 42))

    def test_insert_get_delete(self):
        rid = self.heap.insert(b"row one")
        self.assertEqual(self.heap.get(rid), b"row one")
        self.assertTrue(self.heap.delete(rid))
        self.assertIsNone(self.heap.get(rid))
        self.assertFalse(self.heap.delete(rid))

    def test_many_rows_span_pages(self):
        payloads = [("row-%04d" % i).encode() * 10 for i in range(500)]
        rowids = [self.heap.insert(p) for p in payloads]
        self.assertEqual(self.heap.count(), 500)
        for rid, payload in zip(rowids, payloads):
            self.assertEqual(self.heap.get(rid), payload)
        scanned = dict(self.heap.scan())
        self.assertEqual(len(scanned), 500)
        self.assertEqual(sorted(scanned.values()), sorted(payloads))

    def test_overflow_rows(self):
        big = os.urandom(50000)
        rid = self.heap.insert(big)
        self.assertEqual(self.heap.get(rid), big)
        bigger = os.urandom(120000)
        rid2 = self.heap.update(rid, bigger)
        self.assertEqual(self.heap.get(rid2), bigger)
        self.heap.delete(rid2)
        self.assertIsNone(self.heap.get(rid2))

    def test_update_in_place_and_relocating(self):
        rid = self.heap.insert(b"small")
        self.assertEqual(self.heap.update(rid, b"tiny"), rid)
        self.assertEqual(self.heap.get(rid), b"tiny")
        rid2 = self.heap.update(rid, b"x" * 900)
        self.assertEqual(self.heap.get(rid2), b"x" * 900)

    def test_space_is_reused_after_deletes(self):
        rowids = [self.heap.insert(b"y" * 300) for _ in range(200)]
        pages_before = len(list(self.heap._pages()))
        for rid in rowids:
            self.heap.delete(rid)
        for _ in range(200):
            self.heap.insert(b"z" * 300)
        pages_after = len(list(self.heap._pages()))
        self.assertLessEqual(pages_after, pages_before * 2)
        self.assertEqual(self.heap.count(), 200)

    def test_drop_frees_pages(self):
        for _ in range(100):
            self.heap.insert(b"q" * 500)
        self.heap.insert(os.urandom(20000))
        before = self.p.page_count
        self.heap.drop()
        new_heap = HeapFile.create(self.p)
        self.assertLessEqual(self.p.page_count, before + 1)
        self.assertEqual(new_heap.count(), 0)


class BTreeTest(TempDBCase):
    def setUp(self):
        TempDBCase.setUp(self)
        self.p = self.pager()
        self.p.begin()
        self.tree = BTree.create(self.p)

    def tearDown(self):
        try:
            self.p.close()
        finally:
            TempDBCase.tearDown(self)

    def key(self, n):
        return struct.pack(">Q", n)

    def test_empty_tree(self):
        self.assertIsNone(self.tree.get(b"nothing"))
        self.assertEqual(list(self.tree.items()), [])
        self.assertEqual(self.tree.check(), 0)

    def test_insert_and_get(self):
        self.assertTrue(self.tree.insert(b"b", b"2"))
        self.assertTrue(self.tree.insert(b"a", b"1"))
        self.assertFalse(self.tree.insert(b"a", b"1-new"))
        self.assertEqual(self.tree.get(b"a"), b"1-new")
        self.assertEqual(self.tree.get(b"b"), b"2")
        self.assertIsNone(self.tree.get(b"c"))
        self.assertEqual([k for k, _ in self.tree.items()], [b"a", b"b"])

    def test_no_replace(self):
        self.tree.insert(b"k", b"first")
        self.tree.insert(b"k", b"second", replace=False)
        self.assertEqual(self.tree.get(b"k"), b"first")

    def test_sequential_insert_splits(self):
        n = 3000
        for i in range(n):
            self.tree.insert(self.key(i), b"v%d" % i)
        self.assertEqual(self.tree.check(), n)
        self.assertEqual([k for k, _ in self.tree.items()], [self.key(i) for i in range(n)])
        for i in range(n):
            self.assertEqual(self.tree.get(self.key(i)), b"v%d" % i)

    def test_reverse_insert_splits(self):
        n = 2000
        for i in reversed(range(n)):
            self.tree.insert(self.key(i), b"v")
        self.assertEqual(self.tree.check(), n)

    def test_random_insert_delete_matches_dict(self):
        rng = random.Random(1234)
        model = {}
        for step in range(6000):
            k = self.key(rng.randrange(1500))
            if rng.random() < 0.6:
                v = b"v%d" % step
                self.tree.insert(k, v)
                model[k] = v
            else:
                expect = k in model
                self.assertEqual(self.tree.delete(k), expect)
                model.pop(k, None)
            if step % 500 == 0:
                self.tree.check()
        self.tree.check()
        self.assertEqual(dict(self.tree.items()), model)

    def test_delete_everything(self):
        keys = [self.key(i) for i in range(1200)]
        for k in keys:
            self.tree.insert(k, b"payload-" * 4)
        rng = random.Random(7)
        rng.shuffle(keys)
        for i, k in enumerate(keys):
            self.assertTrue(self.tree.delete(k))
            if i % 200 == 0:
                self.tree.check()
        self.assertEqual(self.tree.check(), 0)
        self.assertEqual(list(self.tree.items()), [])
        self.assertTrue(self.tree.insert(b"after", b"ok"))
        self.assertEqual(self.tree.get(b"after"), b"ok")

    def test_variable_length_keys(self):
        rng = random.Random(99)
        model = {}
        for i in range(1500):
            k = bytes(bytearray(rng.randrange(1, 120)) ) + b"%d" % i
            v = b"x" * rng.randrange(0, 60)
            self.tree.insert(k, v)
            model[k] = v
        self.tree.check()
        self.assertEqual(dict(self.tree.items()), model)

    def test_range_scans(self):
        for i in range(200):
            self.tree.insert(self.key(i), b"v")
        got = [k for k, _ in self.tree.items(self.key(10), self.key(20))]
        self.assertEqual(got, [self.key(i) for i in range(10, 21)])
        got = [k for k, _ in self.tree.items(self.key(10), self.key(20),
                                             include_start=False, include_end=False)]
        self.assertEqual(got, [self.key(i) for i in range(11, 20)])
        got = [k for k, _ in self.tree.items(start=self.key(195))]
        self.assertEqual(got, [self.key(i) for i in range(195, 200)])
        got = [k for k, _ in self.tree.reversed_items(self.key(0), self.key(3))]
        self.assertEqual(got, [self.key(i) for i in reversed(range(4))])

    def test_missing_key_range_start(self):
        for i in range(0, 100, 2):
            self.tree.insert(self.key(i), b"v")
        got = [k for k, _ in self.tree.items(self.key(11), self.key(15))]
        self.assertEqual(got, [self.key(12), self.key(14)])

    def test_oversized_key_rejected(self):
        with self.assertRaises(StorageError):
            self.tree.insert(b"k" * 2000, b"v")

    def test_persistence_across_reopen(self):
        for i in range(800):
            self.tree.insert(self.key(i), b"v%d" % i)
        root = self.tree.root
        self.p.commit()
        self.p.close()
        p2 = Pager(self.path)
        tree = BTree(p2, root)
        self.assertEqual(tree.check(), 800)
        self.assertEqual(tree.get(self.key(500)), b"v500")
        p2.close()

    def test_drop_frees_pages(self):
        for i in range(500):
            self.tree.insert(self.key(i), b"v")
        pages = len(self.tree._all_pages())
        self.assertGreater(pages, 1)
        self.tree.drop()
        fresh = BTree.create(self.p)
        self.assertEqual(fresh.check(), 0)


class ValueEncodingTest(unittest.TestCase):
    def test_record_roundtrip(self):
        row = [None, 42, -7, 3.5, "héllo", b"\x00\x01", True, False, 2 ** 62]
        self.assertEqual(values.deserialize_record(values.serialize_record(row)), row)

    def test_key_ordering_numbers(self):
        samples = [-(2 ** 40), -5, -1.5, 0, 0.5, 1, 2, 1e9, 2 ** 40]
        encoded = [values.encode_key(v) for v in samples]
        self.assertEqual(encoded, sorted(encoded))

    def test_key_ordering_text(self):
        samples = ["", "a", "a\x00", "ab", "b", "z" * 50]
        encoded = [values.encode_key(v) for v in samples]
        self.assertEqual(encoded, sorted(encoded))

    def test_null_sorts_first(self):
        self.assertLess(values.encode_key(None), values.encode_key(-(2 ** 60)))
        self.assertLess(values.encode_key(10 ** 9), values.encode_key("a"))

    def test_int_and_real_share_prefix(self):
        self.assertEqual(values.numeric_prefix(5), values.numeric_prefix(5.0))
        self.assertTrue(values.encode_key(5).startswith(values.numeric_prefix(5)))
        self.assertTrue(values.encode_key(5.0).startswith(values.numeric_prefix(5.0)))

    def test_index_key_roundtrip(self):
        key = values.encode_index_key(["abc", 12], 99)
        prefix, rowid = values.split_index_key(key)
        self.assertEqual(rowid, 99)
        self.assertEqual(prefix, values.encode_prefix(["abc", 12]))

    def test_coercion(self):
        self.assertEqual(values.coerce("12", values.INTEGER), 12)
        self.assertEqual(values.coerce(3, values.REAL), 3.0)
        self.assertEqual(values.coerce(3.0, values.TEXT), "3")
        self.assertEqual(values.coerce("hi", values.BLOB), b"hi")
        self.assertIsNone(values.coerce(None, values.INTEGER))

    def test_coercion_failures(self):
        from trde.errors import TypeMismatchError
        with self.assertRaises(TypeMismatchError):
            values.coerce("abc", values.INTEGER)
        with self.assertRaises(TypeMismatchError):
            values.coerce(1.5, values.INTEGER)
        with self.assertRaises(TypeMismatchError):
            values.coerce(b"x", values.TEXT)

    def test_compare_total_order(self):
        self.assertEqual(values.compare(None, None), 0)
        self.assertEqual(values.compare(None, 1), -1)
        self.assertEqual(values.compare(2, 1.5), 1)
        self.assertEqual(values.compare("a", "b"), -1)


if __name__ == "__main__":
    unittest.main()


class LegacyMagicTest(unittest.TestCase):
    """Files written before the rename (header magic QUARRYDB) still open."""

    def test_quarry_era_file_opens(self):
        from trde import connect
        from trde.pager import LEGACY_MAGIC, MAGIC
        d = tempfile.mkdtemp(prefix="trde-legacy-")
        path = os.path.join(d, "old.trde")
        db = connect(path)
        db.execute("CREATE TABLE t (a INTEGER)")
        db.execute("INSERT INTO t VALUES (7)")
        db.close()
        with open(path, "r+b") as f:
            assert f.read(len(MAGIC)) == MAGIC
            f.seek(0)
            f.write(LEGACY_MAGIC)
        db = connect(path)
        self.assertEqual(db.execute("SELECT a FROM t").rows, [(7,)])
        db.close()
