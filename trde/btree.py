"""A B+tree over byte-string keys.

Keys are unique, opaque byte strings compared lexicographically (see
``trde.values.encode_index_key`` for how SQL tuples become keys).  Values are
arbitrary byte strings that live only in leaves; leaves are chained left to
right so range scans never revisit an internal node.

Node layouts, on top of :class:`trde.page.SlottedPage`:

leaf (``PAGE_BTREE_LEAF``)
    cell = u16 key length, key, value.  ``link`` is the next leaf.

internal (``PAGE_BTREE_INTERNAL``)
    cell = u16 key length, key, u32 child.  ``link`` is the rightmost child.
    Cell *j* means "keys strictly below ``key_j`` live in ``child_j``"; keys at
    or above the last separator live in ``link``.

The root page number never changes, so a catalog entry can point at it
forever: a root split copies the old root into a fresh page, and a root
collapse copies its only child back in.
"""

import struct
from bisect import bisect_left, bisect_right

from .errors import StorageError
from .page import (HEADER_SIZE, PAGE_BTREE_INTERNAL, PAGE_BTREE_LEAF,
                   PAGE_SIZE, SLOT_SIZE, SlottedPage, init_page)
from .page import _SLOT

USABLE = PAGE_SIZE - HEADER_SIZE
MAX_KEY = 900
MAX_VALUE = 900
_MIN_FILL = USABLE // 3

_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")


def _leaf_cell(key, value):
    return _U16.pack(len(key)) + key + value


def _split_leaf_cell(cell):
    (klen,) = _U16.unpack_from(cell, 0)
    return bytes(cell[2:2 + klen]), bytes(cell[2 + klen:])


def _internal_cell(key, child):
    return _U16.pack(len(key)) + key + _U32.pack(child)


def _split_internal_cell(cell):
    (klen,) = _U16.unpack_from(cell, 0)
    key = bytes(cell[2:2 + klen])
    (child,) = _U32.unpack_from(cell, 2 + klen)
    return key, child


class _Node(object):
    """Convenience wrapper decoding a page into keys/children or keys/values."""

    def __init__(self, tree, page_id):
        self.tree = tree
        self.page_id = page_id
        self.page = tree.pager.slotted(page_id)

    @property
    def is_leaf(self):
        return self.page.page_type == PAGE_BTREE_LEAF

    def keys(self):
        if self.is_leaf:
            return [_split_leaf_cell(c)[0] for c in self.page.cells()]
        return [_split_internal_cell(c)[0] for c in self.page.cells()]

    def key_at(self, index):
        """Decode just the key of one cell.

        Node searches run through this rather than :meth:`keys` so a lookup
        touches O(log n) cells per page instead of decoding the whole page.
        """
        buf = self.page.buf
        off, _ = _SLOT.unpack_from(buf, HEADER_SIZE + index * SLOT_SIZE)
        (klen,) = _U16.unpack_from(buf, off)
        return bytes(buf[off + 2:off + 2 + klen])

    def lower_bound(self, key):
        """First slot whose key is >= ``key``."""
        low, high = 0, self.page.num_slots
        while low < high:
            middle = (low + high) // 2
            if self.key_at(middle) < key:
                low = middle + 1
            else:
                high = middle
        return low

    def upper_bound(self, key):
        """First slot whose key is > ``key``."""
        low, high = 0, self.page.num_slots
        while low < high:
            middle = (low + high) // 2
            if self.key_at(middle) <= key:
                low = middle + 1
            else:
                high = middle
        return low

    def entries(self):
        if self.is_leaf:
            return [_split_leaf_cell(c) for c in self.page.cells()]
        return [_split_internal_cell(c) for c in self.page.cells()]

    def child_at(self, index):
        n = self.page.num_slots
        if index == n:
            return self.page.link
        return _split_internal_cell(self.page.cell(index))[1]

    def used_bytes(self):
        """Live payload plus slot overhead.

        Deleted cells leave holes behind until the page is compacted, so this
        deliberately ignores ``free_end`` — an occupancy measure that counted
        dead bytes would never report an underflow.
        """
        total = self.page.num_slots * SLOT_SIZE
        for i in range(self.page.num_slots):
            total += self.page.slot(i)[1]
        return total

    def dirty(self):
        self.tree.pager.mark_dirty(self.page_id)


class BTree(object):
    def __init__(self, pager, root):
        self.pager = pager
        self.root = root

    @classmethod
    def create(cls, pager):
        root = pager.allocate_page(PAGE_BTREE_LEAF)
        return cls(pager, root)

    # -- lookup -----------------------------------------------------------
    def _descend(self, key):
        """Return (leaf_page_id, path) where path is [(page_id, child_index)]."""
        page_id = self.root
        path = []
        while True:
            node = _Node(self, page_id)
            if node.is_leaf:
                return page_id, path
            index = node.upper_bound(key)
            path.append((page_id, index))
            page_id = node.child_at(index)
            if page_id == 0:
                raise StorageError("B+tree internal node has a null child pointer")

    def get(self, key):
        page_id, _ = self._descend(key)
        node = _Node(self, page_id)
        index = node.lower_bound(key)
        if index < node.page.num_slots and node.key_at(index) == key:
            return _split_leaf_cell(node.page.cell(index))[1]
        return None

    def __contains__(self, key):
        return self.get(key) is not None

    # -- insertion --------------------------------------------------------
    def insert(self, key, value, replace=True):
        """Insert or replace ``key``.  Returns True if a new key was added."""
        if len(key) > MAX_KEY:
            raise StorageError("index key of %d bytes exceeds the %d byte limit" % (len(key), MAX_KEY))
        if len(value) > MAX_VALUE:
            raise StorageError("index value of %d bytes exceeds the %d byte limit" % (len(value), MAX_VALUE))
        self._last_insert_added = False
        split = self._insert(self.root, key, value, replace)
        if split is not None:
            sep, right_id = split
            self._grow_root(sep, right_id)
        return self._last_insert_added

    def _grow_root(self, sep, right_id):
        """Move the old root aside and install a new internal root in its place."""
        root_buf = self.pager.get_page(self.root)
        left_id = self.pager.allocate_page(PAGE_BTREE_LEAF)
        left_buf = self.pager.get_page(left_id)
        left_buf[:] = root_buf
        self.pager.mark_dirty(left_id)
        init_page(root_buf, PAGE_BTREE_INTERNAL, right_id)
        SlottedPage(root_buf).append_cell(_internal_cell(sep, left_id))
        self.pager.mark_dirty(self.root)

    def _insert(self, page_id, key, value, replace):
        node = _Node(self, page_id)
        if node.is_leaf:
            return self._insert_leaf(node, key, value, replace)
        index = node.upper_bound(key)
        child_id = node.child_at(index)
        split = self._insert(child_id, key, value, replace)
        if split is None:
            return None
        sep, new_id = split
        return self._insert_separator(node, index, sep, new_id)

    def _insert_leaf(self, node, key, value, replace):
        index = node.lower_bound(key)
        cell = _leaf_cell(key, value)
        if index < node.page.num_slots and node.key_at(index) == key:
            if not replace:
                self._last_insert_added = False
                return None
            if node.page.replace_cell(index, cell):
                node.dirty()
                self._last_insert_added = False
                return None
            entries = node.entries()
            entries[index] = (key, value)
            self._last_insert_added = False
            return self._rewrite_leaf(node, entries)
        self._last_insert_added = True
        if node.page.can_fit(len(cell)):
            node.page.insert_cell(index, cell)
            node.dirty()
            return None
        if node.page.fragmented_space() > 0:
            node.page.compact()
            if node.page.can_fit(len(cell)):
                node.page.insert_cell(index, cell)
                node.dirty()
                return None
        entries = node.entries()
        entries.insert(index, (key, value))
        return self._rewrite_leaf(node, entries)

    def _rewrite_leaf(self, node, entries):
        """Repack a leaf, splitting into two pages when the payload is too big."""
        cells = [_leaf_cell(k, v) for k, v in entries]
        if self._fits(cells):
            node.page.load_cells(cells)
            node.dirty()
            return None
        pivot = self._split_point(cells)
        right_id = self.pager.allocate_page(PAGE_BTREE_LEAF)
        right = _Node(self, right_id)
        right.page.load_cells(cells[pivot:], link=node.page.link, page_type=PAGE_BTREE_LEAF)
        right.dirty()
        node.page.load_cells(cells[:pivot], link=right_id, page_type=PAGE_BTREE_LEAF)
        node.dirty()
        return entries[pivot][0], right_id

    def _insert_separator(self, node, index, sep, child_id):
        cell = _internal_cell(sep, child_id)
        # The new child holds keys >= sep; the existing pointer at `index`
        # keeps the lower half, so the separator slots in at `index`.
        old_child = node.child_at(index)
        entries = node.entries()
        link = node.page.link
        if node.page.can_fit(len(cell)) or (node.page.fragmented_space() and self._compact_fits(node, cell)):
            node.page.insert_cell(index, _internal_cell(sep, old_child))
            if index + 1 < node.page.num_slots:
                key_next, _ = _split_internal_cell(node.page.cell(index + 1))
                node.page.replace_cell(index + 1, _internal_cell(key_next, child_id))
            else:
                node.page.link = child_id
            node.dirty()
            return None
        entries.insert(index, (sep, old_child))
        if index + 1 < len(entries):
            entries[index + 1] = (entries[index + 1][0], child_id)
        else:
            link = child_id
        return self._rewrite_internal(node, entries, link)

    def _compact_fits(self, node, cell):
        node.page.compact()
        return node.page.can_fit(len(cell))

    def _rewrite_internal(self, node, entries, link):
        cells = [_internal_cell(k, c) for k, c in entries]
        if self._fits(cells):
            node.page.load_cells(cells, link=link, page_type=PAGE_BTREE_INTERNAL)
            node.dirty()
            return None
        pivot = self._split_point(cells)
        sep, sep_child = entries[pivot]
        right_id = self.pager.allocate_page(PAGE_BTREE_INTERNAL)
        right = _Node(self, right_id)
        right.page.load_cells([_internal_cell(k, c) for k, c in entries[pivot + 1:]],
                              link=link, page_type=PAGE_BTREE_INTERNAL)
        right.dirty()
        node.page.load_cells(cells[:pivot], link=sep_child, page_type=PAGE_BTREE_INTERNAL)
        node.dirty()
        return sep, right_id

    @staticmethod
    def _fits(cells):
        return sum(len(c) + SLOT_SIZE for c in cells) <= USABLE

    @staticmethod
    def _split_point(cells):
        total = sum(len(c) + SLOT_SIZE for c in cells)
        running = 0
        for i, cell in enumerate(cells):
            running += len(cell) + SLOT_SIZE
            if running * 2 >= total and i > 0:
                return i
        return max(1, len(cells) // 2)

    # -- deletion ---------------------------------------------------------
    def delete(self, key):
        removed = self._delete(self.root, key)
        if removed:
            root = _Node(self, self.root)
            if not root.is_leaf and root.page.num_slots == 0:
                self._collapse_root(root.page.link)
        return removed

    def _collapse_root(self, child_id):
        child_buf = self.pager.get_page(child_id)
        root_buf = self.pager.get_page(self.root)
        root_buf[:] = child_buf
        self.pager.mark_dirty(self.root)
        self.pager.free_page(child_id)

    def _delete(self, page_id, key):
        node = _Node(self, page_id)
        if node.is_leaf:
            index = node.lower_bound(key)
            if index >= node.page.num_slots or node.key_at(index) != key:
                return False
            node.page.remove_slot(index)
            node.dirty()
            return True
        index = node.upper_bound(key)
        child_id = node.child_at(index)
        if not self._delete(child_id, key):
            return False
        self._rebalance(node, index)
        return True

    def _rebalance(self, parent, index):
        child = _Node(self, parent.child_at(index))
        if child.used_bytes() >= _MIN_FILL or parent.page.num_slots == 0:
            return
        if index > 0 and self._borrow_left(parent, index, child):
            return
        if index < parent.page.num_slots and self._borrow_right(parent, index, child):
            return
        if index > 0:
            self._merge(parent, index - 1)
        else:
            self._merge(parent, 0)

    def _sep_key(self, parent, index):
        return _split_internal_cell(parent.page.cell(index))[0]

    def _set_sep(self, parent, index, key):
        _, child = _split_internal_cell(parent.page.cell(index))
        cell = _internal_cell(key, child)
        if not parent.page.replace_cell(index, cell):
            entries = parent.entries()
            entries[index] = (key, child)
            parent.page.load_cells([_internal_cell(k, c) for k, c in entries],
                                   link=parent.page.link, page_type=PAGE_BTREE_INTERNAL)
        parent.dirty()

    def _borrow_left(self, parent, index, child):
        left = _Node(self, parent.child_at(index - 1))
        if left.page.num_slots <= 1 or left.used_bytes() < _MIN_FILL:
            return False
        if child.is_leaf:
            entries = left.entries()
            moved_key, moved_value = entries[-1]
            cell = _leaf_cell(moved_key, moved_value)
            if not child.page.can_fit(len(cell)):
                child.page.compact()
                if not child.page.can_fit(len(cell)):
                    return False
            left.page.remove_slot(left.page.num_slots - 1)
            left.dirty()
            child.page.insert_cell(0, cell)
            child.dirty()
            self._set_sep(parent, index - 1, moved_key)
            return True
        sep = self._sep_key(parent, index - 1)
        left_entries = left.entries()
        moved_key, moved_child = left_entries[-1]
        cell = _internal_cell(sep, left.page.link)
        if not child.page.can_fit(len(cell)):
            child.page.compact()
            if not child.page.can_fit(len(cell)):
                return False
        child.page.insert_cell(0, cell)
        child.dirty()
        left.page.remove_slot(left.page.num_slots - 1)
        left.page.link = moved_child
        left.dirty()
        self._set_sep(parent, index - 1, moved_key)
        return True

    def _borrow_right(self, parent, index, child):
        right = _Node(self, parent.child_at(index + 1))
        if right.page.num_slots <= 1 or right.used_bytes() < _MIN_FILL:
            return False
        if child.is_leaf:
            moved_key, moved_value = right.entries()[0]
            cell = _leaf_cell(moved_key, moved_value)
            if not child.page.can_fit(len(cell)):
                child.page.compact()
                if not child.page.can_fit(len(cell)):
                    return False
            right.page.remove_slot(0)
            right.dirty()
            child.page.append_cell(cell)
            child.dirty()
            new_sep = _split_leaf_cell(right.page.cell(0))[0]
            self._set_sep(parent, index, new_sep)
            return True
        sep = self._sep_key(parent, index)
        first_key, first_child = right.entries()[0]
        cell = _internal_cell(sep, child.page.link)
        if not child.page.can_fit(len(cell)):
            child.page.compact()
            if not child.page.can_fit(len(cell)):
                return False
        child.page.append_cell(cell)
        child.page.link = first_child
        child.dirty()
        right.page.remove_slot(0)
        right.dirty()
        self._set_sep(parent, index, first_key)
        return True

    def _merge(self, parent, index):
        """Merge child ``index+1`` into child ``index``, dropping separator ``index``."""
        left = _Node(self, parent.child_at(index))
        right = _Node(self, parent.child_at(index + 1))
        sep = self._sep_key(parent, index)
        if left.is_leaf:
            entries = left.entries() + right.entries()
            cells = [_leaf_cell(k, v) for k, v in entries]
            if not self._fits(cells):
                # Too big to merge: redistribute evenly instead.
                pivot = self._split_point(cells)
                right_next = right.page.link
                left.page.load_cells(cells[:pivot], link=right.page_id, page_type=PAGE_BTREE_LEAF)
                right.page.load_cells(cells[pivot:], link=right_next, page_type=PAGE_BTREE_LEAF)
                left.dirty()
                right.dirty()
                self._set_sep(parent, index, entries[pivot][0])
                return
            left.page.load_cells(cells, link=right.page.link, page_type=PAGE_BTREE_LEAF)
            left.dirty()
        else:
            entries = left.entries() + [(sep, left.page.link)] + right.entries()
            cells = [_internal_cell(k, c) for k, c in entries]
            if not self._fits(cells):
                pivot = self._split_point(cells)
                new_sep, new_sep_child = entries[pivot]
                left.page.load_cells(cells[:pivot], link=new_sep_child, page_type=PAGE_BTREE_INTERNAL)
                right.page.load_cells([_internal_cell(k, c) for k, c in entries[pivot + 1:]],
                                      link=right.page.link, page_type=PAGE_BTREE_INTERNAL)
                left.dirty()
                right.dirty()
                self._set_sep(parent, index, new_sep)
                return
            left.page.load_cells(cells, link=right.page.link, page_type=PAGE_BTREE_INTERNAL)
            left.dirty()
        self.pager.free_page(right.page_id)
        self._drop_separator(parent, index)

    def _drop_separator(self, parent, index):
        """Remove separator ``index`` and the child pointer that followed it."""
        entries = parent.entries()
        link = parent.page.link
        left_child = entries[index][1]
        if index + 1 < len(entries):
            entries[index + 1] = (entries[index + 1][0], left_child)
        else:
            link = left_child
        del entries[index]
        parent.page.load_cells([_internal_cell(k, c) for k, c in entries],
                               link=link, page_type=PAGE_BTREE_INTERNAL)
        parent.dirty()

    # -- iteration --------------------------------------------------------
    def _first_leaf(self):
        page_id = self.root
        while True:
            node = _Node(self, page_id)
            if node.is_leaf:
                return page_id
            page_id = node.child_at(0)

    def items(self, start=None, end=None, include_start=True, include_end=True):
        """Yield (key, value) pairs in ascending key order within [start, end]."""
        if start is None:
            page_id = self._first_leaf()
            index = 0
        else:
            page_id, _ = self._descend(start)
            node = _Node(self, page_id)
            index = node.upper_bound(start) if not include_start else node.lower_bound(start)
        while page_id:
            node = _Node(self, page_id)
            count = node.page.num_slots
            while index < count:
                key, value = _split_leaf_cell(node.page.cell(index))
                if end is not None:
                    if key > end or (key == end and not include_end):
                        return
                yield key, value
                index += 1
            page_id = node.page.link
            index = 0

    def reversed_items(self, start=None, end=None, include_start=True, include_end=True):
        """Same range as :meth:`items` but yielded in descending key order."""
        collected = list(self.items(start, end, include_start, include_end))
        for item in reversed(collected):
            yield item

    def keys_list(self):
        return [k for k, _ in self.items()]

    def __len__(self):
        return sum(1 for _ in self.items())

    # -- maintenance ------------------------------------------------------
    def drop(self):
        for page_id in self._all_pages():
            self.pager.free_page(page_id)

    def _all_pages(self):
        stack = [self.root]
        out = []
        while stack:
            page_id = stack.pop()
            out.append(page_id)
            node = _Node(self, page_id)
            if not node.is_leaf:
                for i in range(node.page.num_slots + 1):
                    stack.append(node.child_at(i))
        return out

    def check(self):
        """Validate structural invariants; raises StorageError on violation."""
        keys = self._check(self.root, None, None, is_root=True)
        flat = list(self.keys_list())
        if flat != sorted(flat):
            raise StorageError("leaf chain is not sorted")
        if len(set(flat)) != len(flat):
            raise StorageError("duplicate keys in leaf chain")
        if flat != keys:
            raise StorageError("leaf chain disagrees with tree traversal")
        return len(flat)

    def _check(self, page_id, low, high, is_root=False, depth=0):
        node = _Node(self, page_id)
        keys = node.keys()
        if keys != sorted(keys):
            raise StorageError("keys out of order in page %d" % page_id)
        for key in keys:
            if low is not None and key < low:
                raise StorageError("key below subtree bound in page %d" % page_id)
            if high is not None and key >= high:
                raise StorageError("key above subtree bound in page %d" % page_id)
        if node.is_leaf:
            if not is_root and node.page.num_slots == 0:
                raise StorageError("empty non-root leaf %d" % page_id)
            return keys
        if not keys:
            raise StorageError("internal page %d has no separators" % page_id)
        out = []
        bounds = [low] + keys + [high]
        for i in range(len(keys) + 1):
            out.extend(self._check(node.child_at(i), bounds[i], bounds[i + 1], depth=depth + 1))
        return out
