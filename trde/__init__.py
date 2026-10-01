"""TRDE -- a small relational database engine written from scratch.

    >>> from trde import connect
    >>> db = connect(":memory:")
    >>> db.execute("CREATE TABLE t (a INTEGER, b TEXT)")
    >>> db.execute("INSERT INTO t VALUES (1, 'one'), (2, 'two')")
    >>> db.execute("SELECT b FROM t WHERE a = ?", (2,)).rows
    [('two',)]
"""

from .database import Database, connect
from .errors import (IntegrityError, ParseError, TRDEError, SchemaError,
                     StorageError, TransactionError, TypeMismatchError)
from .executor import Result

__version__ = "1.0.0"

__all__ = [
    "Database", "connect", "Result", "TRDEError", "ParseError", "SchemaError",
    "IntegrityError", "StorageError", "TransactionError", "TypeMismatchError",
    "__version__",
]
