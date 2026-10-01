"""Runtime table objects: a heap plus its indexes, with constraints enforced."""

import struct

from .btree import BTree
from .catalog import IndexMeta
from .errors import IntegrityError
from .heap import HeapFile
from .values import (coerce, deserialize_record, encode_index_key,
                     encode_prefix, serialize_record)

_LOW = b"\x00" * 8
_HIGH = b"\xff" * 8


class Index(object):
    def __init__(self, pager, meta, table_meta):
        self.meta = meta
        self.tree = BTree(pager, meta.root)
        self.positions = [table_meta.column_index(c) for c in meta.columns]

    @property
    def name(self):
        return self.meta.name

    @property
    def unique(self):
        return self.meta.unique

    def key_values(self, row):
        return [row[p] for p in self.positions]

    def add(self, row, rowid):
        keys = self.key_values(row)
        if self.meta.unique and all(v is not None for v in keys):
            prefix = encode_prefix(keys)
            for _ in self.tree.items(prefix + _LOW, prefix + _HIGH):
                raise IntegrityError(
                    "UNIQUE constraint failed: %s (%s)"
                    % (self.meta.table, ", ".join(self.meta.columns)))
        self.tree.insert(encode_index_key(keys, rowid), b"")

    def remove(self, row, rowid):
        self.tree.delete(encode_index_key(self.key_values(row), rowid))

    def lookup(self, keys):
        """Rowids whose leading index columns equal ``keys``."""
        prefix = encode_prefix(keys)
        return [struct.unpack(">Q", key[-8:])[0]
                for key, _ in self.tree.items(prefix + _LOW, prefix + _HIGH)]

    def range(self, low=None, high=None, include_low=True, include_high=True, descending=False):
        """Rowids over a range on the leading index column."""
        start = None if low is None else encode_prefix([low]) + (_LOW if include_low else _HIGH)
        end = None if high is None else encode_prefix([high]) + (_HIGH if include_high else _LOW)
        items = self.tree.reversed_items(start, end) if descending else self.tree.items(start, end)
        for key, _ in items:
            yield struct.unpack(">Q", key[-8:])[0]

    def scan_rowids(self, descending=False):
        items = self.tree.reversed_items() if descending else self.tree.items()
        for key, _ in items:
            yield struct.unpack(">Q", key[-8:])[0]


class Table(object):
    def __init__(self, pager, meta):
        self.pager = pager
        self.meta = meta
        self.heap = HeapFile(pager, meta.root)
        self.indexes = [Index(pager, index, meta) for index in meta.indexes]

    @property
    def name(self):
        return self.meta.name

    @property
    def columns(self):
        return self.meta.columns

    # -- row validation ---------------------------------------------------
    def normalize(self, row):
        if len(row) != len(self.meta.columns):
            raise IntegrityError("table %s has %d columns but %d values were supplied"
                                 % (self.meta.name, len(self.meta.columns), len(row)))
        out = []
        for column, value in zip(self.meta.columns, row):
            value = coerce(value, column.type)
            if value is None and (column.not_null or column.primary_key):
                raise IntegrityError("NOT NULL constraint failed: %s.%s"
                                     % (self.meta.name, column.name))
            out.append(value)
        return out

    # -- mutations --------------------------------------------------------
    def insert(self, row):
        row = self.normalize(row)
        rowid = self.heap.insert(serialize_record(row))
        added = []
        try:
            for index in self.indexes:
                index.add(row, rowid)
                added.append(index)
        except Exception:
            for index in added:
                index.remove(row, rowid)
            self.heap.delete(rowid)
            raise
        return rowid, row

    def update(self, rowid, old_row, new_row):
        new_row = self.normalize(new_row)
        for index in self.indexes:
            index.remove(old_row, rowid)
        new_rowid = self.heap.update(rowid, serialize_record(new_row))
        added = []
        try:
            for index in self.indexes:
                index.add(new_row, new_rowid)
                added.append(index)
        except Exception:
            for index in added:
                index.remove(new_row, new_rowid)
            # Put the row back exactly as it was before the failed update.
            restored = self.heap.update(new_rowid, serialize_record(old_row))
            for index in self.indexes:
                index.add(old_row, restored)
            raise
        return new_rowid, new_row

    def delete(self, rowid, row):
        for index in self.indexes:
            index.remove(row, rowid)
        self.heap.delete(rowid)

    # -- reads ------------------------------------------------------------
    def row(self, rowid):
        data = self.heap.get(rowid)
        if data is None:
            return None
        return deserialize_record(data)

    def scan(self):
        for rowid, data in self.heap.scan():
            yield rowid, deserialize_record(data)

    def count(self):
        return self.heap.count()

    # -- index maintenance ------------------------------------------------
    def create_index(self, name, columns, unique, origin="CREATE INDEX"):
        tree = BTree.create(self.pager)
        meta = IndexMeta(name, self.meta.name, columns, unique, tree.root, origin)
        index = Index(self.pager, meta, self.meta)
        for rowid, row in self.scan():
            index.add(row, rowid)
        self.meta.indexes.append(meta)
        self.indexes.append(index)
        return meta

    def drop_index(self, name):
        for i, index in enumerate(self.indexes):
            if index.name.lower() == name.lower():
                index.tree.drop()
                del self.indexes[i]
                self.meta.indexes.remove(index.meta)
                return True
        return False

    def find_index(self, name):
        for index in self.indexes:
            if index.name.lower() == name.lower():
                return index
        return None

    def drop(self):
        for index in self.indexes:
            index.tree.drop()
        self.heap.drop()
