"""Schema catalog.

The whole schema is a JSON document stored in a chain of pages whose head page
number lives in the file header.  DDL rewrites the document wholesale: schemas
are tiny compared to data, and one atomic pointer swap inside the transaction
keeps the catalog consistent with the tables it describes.
"""

import json
import struct

from .errors import SchemaError
from .page import HEADER_SIZE, PAGE_OVERFLOW, PAGE_SIZE, SlottedPage, init_page

CHUNK = PAGE_SIZE - HEADER_SIZE


def write_blob(pager, data):
    """Store ``data`` in a fresh page chain; returns the head page number."""
    if not data:
        return 0
    chunks = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)]
    page_ids = [pager.allocate_page(PAGE_OVERFLOW) for _ in chunks]
    for i, (page_id, chunk) in enumerate(zip(page_ids, chunks)):
        buf = pager.get_page(page_id)
        nxt = page_ids[i + 1] if i + 1 < len(page_ids) else 0
        init_page(buf, PAGE_OVERFLOW, nxt, len(chunk))
        buf[HEADER_SIZE:HEADER_SIZE + len(chunk)] = chunk
        pager.mark_dirty(page_id)
    return page_ids[0]


def read_blob(pager, page_id):
    out = bytearray()
    while page_id:
        page = SlottedPage(pager.get_page(page_id))
        out += page.buf[HEADER_SIZE:HEADER_SIZE + page.extra]
        page_id = page.link
    return bytes(out)


def free_blob(pager, page_id):
    while page_id:
        nxt = SlottedPage(pager.get_page(page_id)).link
        pager.free_page(page_id)
        page_id = nxt


class IndexMeta(object):
    __slots__ = ("name", "table", "columns", "unique", "root", "origin")

    def __init__(self, name, table, columns, unique, root, origin="CREATE INDEX"):
        self.name = name
        self.table = table
        self.columns = list(columns)
        self.unique = unique
        self.root = root
        self.origin = origin

    def to_json(self):
        return {"name": self.name, "table": self.table, "columns": self.columns,
                "unique": self.unique, "root": self.root, "origin": self.origin}

    @classmethod
    def from_json(cls, blob):
        return cls(blob["name"], blob["table"], blob["columns"], blob["unique"],
                   blob["root"], blob.get("origin", "CREATE INDEX"))


class ColumnMeta(object):
    __slots__ = ("name", "type", "not_null", "primary_key", "default")

    def __init__(self, name, type_, not_null=False, primary_key=False, default=None):
        self.name = name
        self.type = type_
        self.not_null = not_null
        self.primary_key = primary_key
        self.default = default

    def to_json(self):
        default = self.default
        if isinstance(default, bytes):
            default = {"__blob__": "".join("%02x" % b for b in bytearray(default))}
        return {"name": self.name, "type": self.type, "not_null": self.not_null,
                "primary_key": self.primary_key, "default": default}

    @classmethod
    def from_json(cls, blob):
        default = blob["default"]
        if isinstance(default, dict) and "__blob__" in default:
            default = bytes(bytearray.fromhex(default["__blob__"]))
        return cls(blob["name"], blob["type"], blob["not_null"], blob["primary_key"], default)


class TableMeta(object):
    __slots__ = ("name", "columns", "root", "indexes", "sql")

    def __init__(self, name, columns, root, indexes=None, sql=""):
        self.name = name
        self.columns = columns
        self.root = root
        self.indexes = indexes if indexes is not None else []
        self.sql = sql

    def column_index(self, name):
        lowered = name.lower()
        for i, column in enumerate(self.columns):
            if column.name.lower() == lowered:
                return i
        return -1

    def column(self, name):
        index = self.column_index(name)
        if index < 0:
            raise SchemaError("table %s has no column %r" % (self.name, name))
        return self.columns[index]

    def column_names(self):
        return [c.name for c in self.columns]

    def to_json(self):
        return {"name": self.name, "root": self.root, "sql": self.sql,
                "columns": [c.to_json() for c in self.columns],
                "indexes": [i.to_json() for i in self.indexes]}

    @classmethod
    def from_json(cls, blob):
        return cls(blob["name"],
                   [ColumnMeta.from_json(c) for c in blob["columns"]],
                   blob["root"],
                   [IndexMeta.from_json(i) for i in blob["indexes"]],
                   blob.get("sql", ""))


class Catalog(object):
    def __init__(self, pager):
        self.pager = pager
        self.tables = {}
        self.load()

    def load(self):
        self.tables = {}
        head = self.pager.catalog_root
        if not head:
            return
        raw = read_blob(self.pager, head)
        if not raw:
            return
        document = json.loads(raw.decode("utf-8"))
        for blob in document.get("tables", []):
            meta = TableMeta.from_json(blob)
            self.tables[meta.name.lower()] = meta

    def save(self):
        document = {"version": 1, "tables": [t.to_json() for t in self.tables.values()]}
        raw = json.dumps(document, sort_keys=True).encode("utf-8")
        old = self.pager.catalog_root
        head = write_blob(self.pager, raw)
        self.pager.set_catalog_root(head)
        if old:
            free_blob(self.pager, old)

    # -- accessors --------------------------------------------------------
    def get_table(self, name):
        meta = self.tables.get(name.lower())
        if meta is None:
            raise SchemaError("no such table: %s" % name)
        return meta

    def has_table(self, name):
        return name.lower() in self.tables

    def add_table(self, meta):
        if self.has_table(meta.name):
            raise SchemaError("table %s already exists" % meta.name)
        self.tables[meta.name.lower()] = meta

    def remove_table(self, name):
        self.tables.pop(name.lower(), None)

    def all_indexes(self):
        for table in self.tables.values():
            for index in table.indexes:
                yield table, index

    def find_index(self, name):
        for table, index in self.all_indexes():
            if index.name.lower() == name.lower():
                return table, index
        return None, None
