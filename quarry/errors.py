"""Exception hierarchy for Quarry."""


class QuarryError(Exception):
    """Base class for every error raised by Quarry."""


class StorageError(QuarryError):
    """Raised when the on-disk representation is corrupt or unusable."""


class ParseError(QuarryError):
    """Raised when SQL text cannot be tokenized or parsed."""

    def __init__(self, message, position=None):
        if position is not None:
            message = "%s (at character %d)" % (message, position)
        super(ParseError, self).__init__(message)
        self.position = position


class SchemaError(QuarryError):
    """Raised for DDL problems: unknown table, duplicate column, ..."""


class IntegrityError(QuarryError):
    """Raised when a constraint (NOT NULL, UNIQUE, PRIMARY KEY) is violated."""


class TypeMismatchError(QuarryError):
    """Raised when a value cannot be coerced into a column's declared type."""


class TransactionError(QuarryError):
    """Raised for illegal transaction control (COMMIT with no transaction...)."""
