"""Paged file access with a rollback journal.

The pager owns the database file.  Callers work with page numbers; page 0 is
the file header and is never handed out for data.

Durability model (the same shape SQLite's rollback journal uses):

* Modified pages accumulate in memory for the life of a transaction.
* ``commit()`` writes the *original* contents of every touched page into a
  side journal, fsyncs it, then writes the new pages, fsyncs the database and
  finally deletes the journal.
* If the process dies between those steps the journal is left behind; the next
  ``Pager`` to open the file replays it backwards, restoring the pre-commit
  image.  A journal without its end marker never made it to disk intact, so it
  is discarded instead of replayed.
* ``rollback()`` simply drops the in-memory pages: nothing was written yet.
"""

import os
import struct
from collections import OrderedDict

from .errors import StorageError, TransactionError
from .page import PAGE_SIZE, PAGE_FREE, PAGE_HEADER, SlottedPage, init_page

MAGIC = b"QUARRYDB\x00\x00\x00\x00v001"
JOURNAL_MAGIC = b"QRYJRNL1"
JOURNAL_END = b"QRYJEND1"

_FILE_HDR = struct.Struct("<16sIIIII")
_HDR_FIELDS = 6
DEFAULT_CACHE_PAGES = 2048


class Pager(object):
    def __init__(self, path, cache_pages=DEFAULT_CACHE_PAGES):
        self.path = path
        self.journal_path = path + "-journal"
        self.cache_pages = cache_pages
        self._cache = OrderedDict()
        self._dirty = {}
        self._in_txn = False
        self._txn_start_pages = 0
        self._closed = False

        created = not os.path.exists(path)
        if created:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
            os.close(fd)
        self.f = open(path, "r+b")
        size = os.path.getsize(path)
        if size == 0:
            self.page_count = 1
            self.freelist_head = 0
            self.catalog_root = 0
            self.schema_version = 0
            buf = bytearray(PAGE_SIZE)
            init_page(buf, PAGE_HEADER)
            self._cache[0] = buf
            self._dirty[0] = True
            self._write_header()
            self._flush_all()
        else:
            if size % PAGE_SIZE:
                raise StorageError("database file size is not a multiple of the page size")
            self.page_count = size // PAGE_SIZE
            self._recover()
            self._read_header()

    # -- header -----------------------------------------------------------
    def _read_header(self):
        buf = self._load(0)
        magic, page_size, page_count, freelist, catalog, schema_version = _FILE_HDR.unpack_from(buf, 0)
        if magic != MAGIC:
            raise StorageError("not a Quarry database file")
        if page_size != PAGE_SIZE:
            raise StorageError("database uses page size %d, this build expects %d" % (page_size, PAGE_SIZE))
        self.page_count = max(self.page_count, page_count)
        self.freelist_head = freelist
        self.catalog_root = catalog
        self.schema_version = schema_version

    def _write_header(self):
        buf = self.get_page(0)
        _FILE_HDR.pack_into(
            buf, 0, MAGIC, PAGE_SIZE, self.page_count,
            self.freelist_head, self.catalog_root, self.schema_version)
        self.mark_dirty(0)

    def set_catalog_root(self, page_id):
        self.catalog_root = page_id
        self.schema_version += 1
        self._write_header()

    # -- cache ------------------------------------------------------------
    def _touch(self, page_id):
        # OrderedDict keeps LRU order in O(1); a list would make every page
        # touch a linear scan of the cache.
        try:
            self._cache.move_to_end(page_id)
        except KeyError:
            pass

    def _evict(self):
        # Inside a transaction callers may hold a page buffer across other page
        # loads; evicting one out from under them would silently drop writes.
        # Nothing is mutated outside a transaction, so eviction is safe there.
        if self._in_txn:
            return
        while len(self._cache) > self.cache_pages:
            for pid in list(self._cache):
                if pid not in self._dirty and pid != 0:
                    del self._cache[pid]
                    break
            else:
                return

    def _load(self, page_id):
        buf = self._cache.get(page_id)
        if buf is not None:
            self._touch(page_id)
            return buf
        if page_id >= self.page_count:
            raise StorageError("page %d is past end of file (%d pages)" % (page_id, self.page_count))
        self.f.seek(page_id * PAGE_SIZE)
        data = self.f.read(PAGE_SIZE)
        if len(data) != PAGE_SIZE:
            raise StorageError("short read on page %d" % page_id)
        buf = bytearray(data)
        self._cache[page_id] = buf
        self._touch(page_id)
        self._evict()
        return buf

    def get_page(self, page_id):
        """Return the mutable buffer for ``page_id``."""
        return self._load(page_id)

    def slotted(self, page_id):
        return SlottedPage(self._load(page_id))

    def mark_dirty(self, page_id):
        if not self._in_txn and page_id != 0:
            raise TransactionError("cannot modify page %d outside a transaction" % page_id)
        self._dirty[page_id] = True
        self._touch(page_id)

    # -- allocation -------------------------------------------------------
    def allocate_page(self, page_type=PAGE_FREE, link=0, extra=0):
        if self.freelist_head:
            page_id = self.freelist_head
            buf = self._load(page_id)
            self.freelist_head = SlottedPage(buf).link
            init_page(buf, page_type, link, extra)
            self.mark_dirty(page_id)
            self._write_header()
            return page_id
        page_id = self.page_count
        self.page_count += 1
        buf = bytearray(PAGE_SIZE)
        init_page(buf, page_type, link, extra)
        self._cache[page_id] = buf
        self._touch(page_id)
        self.mark_dirty(page_id)
        self._write_header()
        return page_id

    def free_page(self, page_id):
        if page_id == 0:
            raise StorageError("cannot free the header page")
        buf = self._load(page_id)
        init_page(buf, PAGE_FREE, self.freelist_head)
        self.mark_dirty(page_id)
        self.freelist_head = page_id
        self._write_header()

    # -- transactions -----------------------------------------------------
    @property
    def in_transaction(self):
        return self._in_txn

    def begin(self):
        if self._in_txn:
            raise TransactionError("a transaction is already open")
        self._in_txn = True
        self._txn_start_pages = self.page_count
        self._txn_header = (self.freelist_head, self.catalog_root, self.schema_version)

    def commit(self):
        if not self._in_txn:
            raise TransactionError("no transaction to commit")
        dirty = sorted(self._dirty)
        if dirty:
            self._write_journal(dirty)
            self._flush_all()
            self._remove_journal()
        self._dirty.clear()
        self._in_txn = False

    def rollback(self):
        if not self._in_txn:
            raise TransactionError("no transaction to roll back")
        for page_id in list(self._dirty):
            self._cache.pop(page_id, None)
        self._dirty.clear()
        self.page_count = self._txn_start_pages
        self.freelist_head, self.catalog_root, self.schema_version = self._txn_header
        self._in_txn = False
        # Page 0 was dropped along with the rest; rebuild it from disk state.
        self._load(0)
        _FILE_HDR.pack_into(
            self._cache[0], 0, MAGIC, PAGE_SIZE, self.page_count,
            self.freelist_head, self.catalog_root, self.schema_version)

    def _write_journal(self, dirty):
        records = []
        disk_pages = os.path.getsize(self.path) // PAGE_SIZE
        for page_id in dirty:
            if page_id >= disk_pages:
                continue  # page did not exist before this transaction
            self.f.seek(page_id * PAGE_SIZE)
            data = self.f.read(PAGE_SIZE)
            if len(data) == PAGE_SIZE:
                records.append((page_id, data))
        with open(self.journal_path, "wb") as jf:
            jf.write(JOURNAL_MAGIC)
            jf.write(struct.pack("<II", disk_pages, len(records)))
            for page_id, data in records:
                jf.write(struct.pack("<I", page_id))
                jf.write(data)
            jf.write(JOURNAL_END)
            jf.flush()
            os.fsync(jf.fileno())

    def _remove_journal(self):
        try:
            os.remove(self.journal_path)
        except OSError:
            pass

    def _flush_all(self):
        for page_id in sorted(self._dirty):
            buf = self._cache[page_id]
            self.f.seek(page_id * PAGE_SIZE)
            self.f.write(bytes(buf))
        self.f.flush()
        os.fsync(self.f.fileno())

    def _recover(self):
        if not os.path.exists(self.journal_path):
            return
        with open(self.journal_path, "rb") as jf:
            blob = jf.read()
        if len(blob) < 16 + len(JOURNAL_END) or blob[:8] != JOURNAL_MAGIC or blob[-8:] != JOURNAL_END:
            self._remove_journal()
            return
        orig_pages, count = struct.unpack_from("<II", blob, 8)
        pos = 16
        for _ in range(count):
            page_id = struct.unpack_from("<I", blob, pos)[0]
            pos += 4
            data = blob[pos:pos + PAGE_SIZE]
            pos += PAGE_SIZE
            self.f.seek(page_id * PAGE_SIZE)
            self.f.write(data)
        self.f.flush()
        self.f.truncate(orig_pages * PAGE_SIZE)
        os.fsync(self.f.fileno())
        self.page_count = orig_pages
        self._cache.clear()
        self._remove_journal()

    # -- lifecycle --------------------------------------------------------
    def close(self):
        if self._closed:
            return
        if self._in_txn:
            self.rollback()
        self.f.close()
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False
