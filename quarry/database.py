"""The public database API."""

import contextlib

from .catalog import Catalog
from .errors import QuarryError, TransactionError
from .executor import Executor, Result
from .pager import Pager
from .parser import parse
from .table import Table


class Database(object):
    """A Quarry database.

    ``execute`` runs one or more statements and returns the last result.
    Statements outside an explicit ``BEGIN`` run in their own transaction and
    commit on success, roll back on failure.
    """

    def __init__(self, path=":memory:", cache_pages=None):
        self.path = path
        if path == ":memory:":
            import tempfile
            self._tempdir = tempfile.mkdtemp(prefix="quarry-mem-")
            path = "%s/memory.qdb" % self._tempdir
        else:
            self._tempdir = None
        kwargs = {} if cache_pages is None else {"cache_pages": cache_pages}
        self.pager = Pager(path, **kwargs)
        self.catalog = Catalog(self.pager)
        self.executor = Executor(self)
        self._tables = {}
        self._depth = 0
        self._closed = False

    # -- schema access ----------------------------------------------------
    def table(self, name):
        key = name.lower()
        cached = self._tables.get(key)
        if cached is None:
            cached = Table(self.pager, self.catalog.get_table(name))
            self._tables[key] = cached
        return cached

    def invalidate(self):
        self._tables = {}

    @property
    def table_names(self):
        return sorted(meta.name for meta in self.catalog.tables.values())

    def schema(self):
        """A description of every table and index, for tools and the shell."""
        out = []
        for name in self.table_names:
            meta = self.catalog.get_table(name)
            out.append({
                "table": meta.name,
                "columns": [{"name": c.name, "type": c.type, "not_null": c.not_null,
                             "primary_key": c.primary_key, "default": c.default}
                            for c in meta.columns],
                "indexes": [{"name": i.name, "columns": list(i.columns),
                             "unique": i.unique, "origin": i.origin}
                            for i in meta.indexes],
            })
        return out

    # -- transactions -----------------------------------------------------
    @property
    def in_transaction(self):
        return self.pager.in_transaction

    def begin(self):
        if self.pager.in_transaction:
            raise TransactionError("a transaction is already open")
        self.pager.begin()
        self._depth = 1

    def commit(self):
        if not self.pager.in_transaction:
            raise TransactionError("no transaction to commit")
        self.pager.commit()
        self._depth = 0

    def rollback(self):
        if not self.pager.in_transaction:
            raise TransactionError("no transaction to roll back")
        self.pager.rollback()
        self._depth = 0
        self.catalog.load()
        self.invalidate()

    @contextlib.contextmanager
    def auto_transaction(self):
        """Wrap a statement in a transaction unless the caller opened one."""
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self.pager.begin()
        self._depth = 1
        try:
            yield
        except Exception:
            self._depth = 0
            if self.pager.in_transaction:
                self.pager.rollback()
            self.catalog.load()
            self.invalidate()
            raise
        else:
            self._depth = 0
            self.pager.commit()

    @contextlib.contextmanager
    def transaction(self):
        """Explicit transaction block: commits on exit, rolls back on error."""
        self.begin()
        try:
            yield self
        except Exception:
            if self.pager.in_transaction:
                self.rollback()
            raise
        else:
            if self.pager.in_transaction:
                self.commit()

    # -- statement execution ----------------------------------------------
    def execute(self, sql, params=()):
        if self._closed:
            raise QuarryError("database is closed")
        statements = parse(sql)
        if not statements:
            return Result(message="no statement")
        result = None
        for statement in statements:
            result = self.executor.execute(statement, params)
        return result

    def executemany(self, sql, sequence_of_params):
        statements = parse(sql)
        if len(statements) != 1:
            raise QuarryError("executemany() needs exactly one statement")
        count = 0
        with self.transaction() if not self.in_transaction else _null_context():
            for params in sequence_of_params:
                result = self.executor.execute(statements[0], params)
                count += result.rowcount
        return Result(rowcount=count, message="%d rows affected" % count)

    def query(self, sql, params=()):
        """Run a statement and return its rows."""
        return self.execute(sql, params).rows

    def scalar(self, sql, params=()):
        return self.execute(sql, params).scalar()

    # -- lifecycle --------------------------------------------------------
    def close(self):
        if self._closed:
            return
        self.pager.close()
        self._closed = True
        if self._tempdir:
            import shutil
            shutil.rmtree(self._tempdir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def __repr__(self):
        return "<Database %s: %d tables>" % (self.path, len(self.catalog.tables))


@contextlib.contextmanager
def _null_context():
    yield


def connect(path=":memory:"):
    """Open (or create) a database at ``path``."""
    return Database(path)
