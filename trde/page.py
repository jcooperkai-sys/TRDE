"""Slotted-page primitives shared by the heap store and the B+tree.

Every page in a TRDE database file is exactly ``PAGE_SIZE`` bytes and starts
with the same 16 byte header::

    offset  size  meaning
    0       1     page type (see PAGE_* constants)
    1       1     flags (unused, reserved)
    2       2     number of slots
    4       2     free_start  - first free byte of the slot array region
    6       2     free_end    - first byte of the cell region (cells grow down)
    8       4     link        - next heap page / next leaf / rightmost child
    12      4     extra       - per page-type scratch space

Slots live immediately after the header, four bytes each: a 2 byte offset and
a 2 byte length.  A slot whose offset is zero is a tombstone (only heap pages
create those).  Cell payloads are packed against the end of the page.
"""

import struct

from .errors import StorageError

PAGE_SIZE = 4096

PAGE_FREE = 0
PAGE_HEADER = 1
PAGE_HEAP = 2
PAGE_BTREE_INTERNAL = 3
PAGE_BTREE_LEAF = 4
PAGE_OVERFLOW = 5

HEADER_SIZE = 16
SLOT_SIZE = 4

_HDR = struct.Struct("<BBHHHII")
_SLOT = struct.Struct("<HH")
_U16_FIELD = struct.Struct("<H")
_U32_FIELD = struct.Struct("<I")


def init_page(buf, page_type, link=0, extra=0):
    """Format ``buf`` (a bytearray of PAGE_SIZE bytes) as an empty page."""
    if len(buf) != PAGE_SIZE:
        raise StorageError("page buffer must be %d bytes" % PAGE_SIZE)
    for i in range(PAGE_SIZE):
        buf[i] = 0
    _HDR.pack_into(buf, 0, page_type, 0, 0, HEADER_SIZE, PAGE_SIZE, link, extra)


class SlottedPage(object):
    """A mutable view over a page buffer."""

    __slots__ = ("buf",)

    def __init__(self, buf):
        self.buf = buf

    # -- header accessors -------------------------------------------------
    @property
    def page_type(self):
        return self.buf[0]

    @page_type.setter
    def page_type(self, value):
        self.buf[0] = value

    # Header fields are read on nearly every page touch, so each one is packed
    # and unpacked at its own fixed offset rather than through the whole
    # 16-byte header struct.
    @property
    def num_slots(self):
        return _U16_FIELD.unpack_from(self.buf, 2)[0]

    @num_slots.setter
    def num_slots(self, value):
        _U16_FIELD.pack_into(self.buf, 2, value)

    @property
    def free_start(self):
        return _U16_FIELD.unpack_from(self.buf, 4)[0]

    @free_start.setter
    def free_start(self, value):
        _U16_FIELD.pack_into(self.buf, 4, value)

    @property
    def free_end(self):
        return _U16_FIELD.unpack_from(self.buf, 6)[0]

    @free_end.setter
    def free_end(self, value):
        _U16_FIELD.pack_into(self.buf, 6, value)

    @property
    def link(self):
        return _U32_FIELD.unpack_from(self.buf, 8)[0]

    @link.setter
    def link(self, value):
        _U32_FIELD.pack_into(self.buf, 8, value)

    @property
    def extra(self):
        return _U32_FIELD.unpack_from(self.buf, 12)[0]

    @extra.setter
    def extra(self, value):
        _U32_FIELD.pack_into(self.buf, 12, value)

    # -- space ------------------------------------------------------------
    @property
    def free_space(self):
        """Contiguous bytes available for a new cell *and* its slot."""
        return self.free_end - self.free_start

    def fragmented_space(self):
        """Bytes reclaimable by compaction (dead cells)."""
        live = 0
        for i in range(self.num_slots):
            off, length = self.slot(i)
            if off:
                live += length
        return (PAGE_SIZE - self.free_end) - live

    def can_fit(self, size, new_slot=True):
        need = size + (SLOT_SIZE if new_slot else 0)
        return self.free_space >= need

    # -- slots ------------------------------------------------------------
    def slot(self, index):
        if index < 0 or index >= self.num_slots:
            raise IndexError("slot %d out of range (%d slots)" % (index, self.num_slots))
        return _SLOT.unpack_from(self.buf, HEADER_SIZE + index * SLOT_SIZE)

    def set_slot(self, index, offset, length):
        _SLOT.pack_into(self.buf, HEADER_SIZE + index * SLOT_SIZE, offset, length)

    def cell(self, index):
        off, length = _SLOT.unpack_from(self.buf, HEADER_SIZE + index * SLOT_SIZE)
        if off == 0:
            return None
        return bytes(self.buf[off:off + length])

    def is_live(self, index):
        return self.slot(index)[0] != 0

    def live_slots(self):
        return [i for i in range(self.num_slots) if self.is_live(i)]

    # -- mutation ---------------------------------------------------------
    def _alloc(self, size):
        if self.free_end - self.free_start < size:
            raise StorageError("no room in page for %d bytes" % size)
        self.free_end -= size
        return self.free_end

    def append_cell(self, data):
        """Append ``data`` with a fresh slot; returns the slot index."""
        if not self.can_fit(len(data)):
            raise StorageError("page full")
        self.free_start += SLOT_SIZE
        index = self.num_slots
        self.num_slots = index + 1
        off = self._alloc(len(data))
        self.buf[off:off + len(data)] = data
        self.set_slot(index, off, len(data))
        return index

    def insert_cell(self, index, data):
        """Insert ``data`` so it becomes slot ``index``, shifting later slots."""
        if not self.can_fit(len(data)):
            raise StorageError("page full")
        n = self.num_slots
        if index < 0 or index > n:
            raise IndexError("insert index out of range")
        base = HEADER_SIZE
        src = base + index * SLOT_SIZE
        dst = src + SLOT_SIZE
        moved = (n - index) * SLOT_SIZE
        self.buf[dst:dst + moved] = bytes(self.buf[src:src + moved])
        self.free_start += SLOT_SIZE
        self.num_slots = n + 1
        off = self._alloc(len(data))
        self.buf[off:off + len(data)] = data
        self.set_slot(index, off, len(data))
        return index

    def remove_slot(self, index):
        """Remove slot ``index`` entirely, shifting later slots down."""
        n = self.num_slots
        if index < 0 or index >= n:
            raise IndexError("remove index out of range")
        base = HEADER_SIZE
        dst = base + index * SLOT_SIZE
        src = dst + SLOT_SIZE
        moved = (n - index - 1) * SLOT_SIZE
        self.buf[dst:dst + moved] = bytes(self.buf[src:src + moved])
        self.num_slots = n - 1
        self.free_start -= SLOT_SIZE

    def kill_slot(self, index):
        """Turn slot ``index`` into a tombstone (keeps positions stable)."""
        self.set_slot(index, 0, 0)

    def replace_cell(self, index, data):
        """Rewrite the payload of ``index``; may need compaction first."""
        off, length = self.slot(index)
        if off and len(data) <= length:
            self.buf[off:off + len(data)] = data
            self.set_slot(index, off, len(data))
            return True
        if self.free_space < len(data):
            return False
        new_off = self._alloc(len(data))
        self.buf[new_off:new_off + len(data)] = data
        self.set_slot(index, new_off, len(data))
        return True

    def compact(self):
        """Repack live cells against the end of the page."""
        cells = []
        for i in range(self.num_slots):
            off, length = self.slot(i)
            cells.append(None if off == 0 else bytes(self.buf[off:off + length]))
        self.free_end = PAGE_SIZE
        for i, data in enumerate(cells):
            if data is None:
                self.set_slot(i, 0, 0)
                continue
            off = self._alloc(len(data))
            self.buf[off:off + len(data)] = data
            self.set_slot(i, off, len(data))

    def cells(self):
        return [self.cell(i) for i in range(self.num_slots)]

    def load_cells(self, cells, link=None, extra=None, page_type=None):
        """Reset the page and refill it with ``cells`` (a list of bytes)."""
        ptype = self.page_type if page_type is None else page_type
        lnk = self.link if link is None else link
        ext = self.extra if extra is None else extra
        init_page(self.buf, ptype, lnk, ext)
        for data in cells:
            self.append_cell(data)


def max_payload():
    """Largest cell that can live in an otherwise empty page."""
    return PAGE_SIZE - HEADER_SIZE - SLOT_SIZE
