"""Heap storage: a linked list of slotted pages holding row payloads.

A row is addressed by a ``rowid`` packing the page number and the slot index::

    rowid = (page_id << 16) | slot

Slots are never renumbered inside a page, so a rowid stays valid until the row
is deleted.  Rows larger than ``MAX_INLINE`` are written to a chain of
overflow pages and the heap cell keeps only a pointer.
"""

import struct

from .errors import StorageError
from .page import (HEADER_SIZE, PAGE_HEAP, PAGE_OVERFLOW, PAGE_SIZE,
                   SlottedPage, init_page)

MAX_INLINE = 1000
OVERFLOW_CAPACITY = PAGE_SIZE - HEADER_SIZE
SEARCH_LIMIT = 32  # pages probed for free space before extending the chain

_INLINE = 0
_OVERFLOW = 1


def make_rowid(page_id, slot):
    if slot > 0xFFFF:
        raise StorageError("slot index %d does not fit in a rowid" % slot)
    return (page_id << 16) | slot


def split_rowid(rowid):
    return rowid >> 16, rowid & 0xFFFF


class HeapFile(object):
    def __init__(self, pager, first_page):
        self.pager = pager
        self.first_page = first_page

    @classmethod
    def create(cls, pager):
        page_id = pager.allocate_page(PAGE_HEAP)
        page = pager.slotted(page_id)
        page.extra = page_id  # insert cursor hint
        pager.mark_dirty(page_id)
        return cls(pager, page_id)

    # -- overflow ---------------------------------------------------------
    def _write_overflow(self, data):
        chunks = [data[i:i + OVERFLOW_CAPACITY] for i in range(0, len(data), OVERFLOW_CAPACITY)]
        page_ids = [self.pager.allocate_page(PAGE_OVERFLOW) for _ in chunks]
        for i, (page_id, chunk) in enumerate(zip(page_ids, chunks)):
            buf = self.pager.get_page(page_id)
            nxt = page_ids[i + 1] if i + 1 < len(page_ids) else 0
            init_page(buf, PAGE_OVERFLOW, nxt, len(chunk))
            buf[HEADER_SIZE:HEADER_SIZE + len(chunk)] = chunk
            self.pager.mark_dirty(page_id)
        return page_ids[0]

    def _read_overflow(self, page_id, total):
        out = bytearray()
        while page_id and len(out) < total:
            page = self.pager.slotted(page_id)
            if page.page_type != PAGE_OVERFLOW:
                raise StorageError("overflow chain reached a non-overflow page")
            length = page.extra
            out += page.buf[HEADER_SIZE:HEADER_SIZE + length]
            page_id = page.link
        if len(out) != total:
            raise StorageError("overflow chain is %d bytes, expected %d" % (len(out), total))
        return bytes(out)

    def _free_overflow(self, page_id):
        while page_id:
            nxt = self.pager.slotted(page_id).link
            self.pager.free_page(page_id)
            page_id = nxt

    def _encode_cell(self, data):
        if len(data) <= MAX_INLINE:
            return bytes(bytearray([_INLINE])) + data
        head = self._write_overflow(data)
        return struct.pack("<BII", _OVERFLOW, len(data), head)

    def _decode_cell(self, cell):
        if cell[0] == _INLINE:
            return bytes(cell[1:])
        _, total, head = struct.unpack("<BII", cell)
        return self._read_overflow(head, total)

    def _release_cell(self, cell):
        if cell[0] == _OVERFLOW:
            _, _, head = struct.unpack("<BII", cell)
            self._free_overflow(head)

    # -- chain helpers ----------------------------------------------------
    def _pages(self):
        page_id = self.first_page
        while page_id:
            yield page_id
            page_id = self.pager.slotted(page_id).link

    def _cursor(self):
        return self.pager.slotted(self.first_page).extra or self.first_page

    def _set_cursor(self, page_id):
        head = self.pager.slotted(self.first_page)
        head.extra = page_id
        self.pager.mark_dirty(self.first_page)

    def _place(self, cell):
        """Find (or make) a page with room for ``cell`` and store it."""
        start = self._cursor()
        probed = 0
        page_id = start
        last = start
        while page_id and probed < SEARCH_LIMIT:
            page = self.pager.slotted(page_id)
            if page.can_fit(len(cell)):
                slot = page.append_cell(cell)
                self.pager.mark_dirty(page_id)
                self._set_cursor(page_id)
                return make_rowid(page_id, slot)
            if page.fragmented_space() + page.free_space >= len(cell) + 4:
                page.compact()
                if page.can_fit(len(cell)):
                    slot = page.append_cell(cell)
                    self.pager.mark_dirty(page_id)
                    self._set_cursor(page_id)
                    return make_rowid(page_id, slot)
            last = page_id
            page_id = page.link
            probed += 1
        # Always extend at the true tail so the chain stays intact.
        while True:
            link = self.pager.slotted(last).link
            if not link:
                break
            last = link
        new_id = self.pager.allocate_page(PAGE_HEAP)
        tail = self.pager.slotted(last)
        tail.link = new_id
        self.pager.mark_dirty(last)
        page = self.pager.slotted(new_id)
        slot = page.append_cell(cell)
        self.pager.mark_dirty(new_id)
        self._set_cursor(new_id)
        return make_rowid(new_id, slot)

    # -- public API -------------------------------------------------------
    def insert(self, data):
        return self._place(self._encode_cell(data))

    def get(self, rowid):
        page_id, slot = split_rowid(rowid)
        page = self.pager.slotted(page_id)
        if page.page_type != PAGE_HEAP or slot >= page.num_slots:
            return None
        cell = page.cell(slot)
        if cell is None:
            return None
        return self._decode_cell(cell)

    def update(self, rowid, data):
        """Replace a row's payload.  Returns the (possibly new) rowid."""
        page_id, slot = split_rowid(rowid)
        page = self.pager.slotted(page_id)
        cell = page.cell(slot)
        if cell is None:
            raise StorageError("row %d does not exist" % rowid)
        self._release_cell(cell)
        new_cell = self._encode_cell(data)
        if page.replace_cell(slot, new_cell):
            self.pager.mark_dirty(page_id)
            return rowid
        page.kill_slot(slot)
        page.compact()
        if page.can_fit(len(new_cell), new_slot=False) and page.free_space >= len(new_cell):
            off = page.free_end - len(new_cell)
            page.free_end = off
            page.buf[off:off + len(new_cell)] = new_cell
            page.set_slot(slot, off, len(new_cell))
            self.pager.mark_dirty(page_id)
            return rowid
        self.pager.mark_dirty(page_id)
        return self._place(new_cell)

    def delete(self, rowid):
        page_id, slot = split_rowid(rowid)
        page = self.pager.slotted(page_id)
        cell = page.cell(slot)
        if cell is None:
            return False
        self._release_cell(cell)
        page.kill_slot(slot)
        self.pager.mark_dirty(page_id)
        return True

    def scan(self):
        for page_id in self._pages():
            page = self.pager.slotted(page_id)
            for slot in range(page.num_slots):
                cell = page.cell(slot)
                if cell is None:
                    continue
                yield make_rowid(page_id, slot), self._decode_cell(cell)

    def count(self):
        total = 0
        for page_id in self._pages():
            page = self.pager.slotted(page_id)
            total += len(page.live_slots())
        return total

    def drop(self):
        page_ids = list(self._pages())
        for page_id in page_ids:
            page = self.pager.slotted(page_id)
            for slot in range(page.num_slots):
                cell = page.cell(slot)
                if cell is not None:
                    self._release_cell(cell)
        for page_id in page_ids:
            self.pager.free_page(page_id)
