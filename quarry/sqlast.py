"""AST node definitions.

Plain classes with ``__slots__`` and a generated ``__repr__`` -- enough for
pattern-free dispatch in the executor and readable failures in tests.
"""


class Node(object):
    _fields = ()

    def __repr__(self):
        inner = ", ".join("%s=%r" % (f, getattr(self, f)) for f in self._fields)
        return "%s(%s)" % (type(self).__name__, inner)

    def __eq__(self, other):
        return type(self) is type(other) and all(
            getattr(self, f) == getattr(other, f) for f in self._fields)

    def __ne__(self, other):
        return not self.__eq__(other)


def _node(name, fields, defaults=()):
    """Build a simple AST node class."""
    slots = tuple(fields)
    pad = (None,) * (len(slots) - len(defaults))
    all_defaults = pad + tuple(defaults)

    def __init__(self, *args, **kwargs):
        if len(args) > len(slots):
            raise TypeError("%s takes at most %d arguments" % (name, len(slots)))
        for i, field in enumerate(slots):
            if i < len(args):
                setattr(self, field, args[i])
            elif field in kwargs:
                setattr(self, field, kwargs.pop(field))
            else:
                setattr(self, field, all_defaults[i])
        if kwargs:
            raise TypeError("unexpected keyword %r for %s" % (list(kwargs)[0], name))

    return type(name, (Node,), {"__slots__": slots, "_fields": slots, "__init__": __init__})


# -- expressions ---------------------------------------------------------
Literal = _node("Literal", ["value"])
Param = _node("Param", ["index"])
ColumnRef = _node("ColumnRef", ["table", "name"])
Star = _node("Star", ["table"])
Unary = _node("Unary", ["op", "operand"])
Binary = _node("Binary", ["op", "left", "right"])
FuncCall = _node("FuncCall", ["name", "args", "distinct", "star"], (False, False))
IsNull = _node("IsNull", ["operand", "negated"], (False,))
InList = _node("InList", ["operand", "items", "negated"], (False,))
Between = _node("Between", ["operand", "low", "high", "negated"], (False,))
Like = _node("Like", ["operand", "pattern", "negated"], (False,))
Case = _node("Case", ["operand", "whens", "orelse"])

# -- statements ----------------------------------------------------------
ColumnDef = _node("ColumnDef", ["name", "type", "not_null", "primary_key", "unique", "default"],
                  (False, False, False, None))
TableConstraint = _node("TableConstraint", ["kind", "columns"])
CreateTable = _node("CreateTable", ["name", "columns", "constraints", "if_not_exists"], (False,))
DropTable = _node("DropTable", ["name", "if_exists"], (False,))
CreateIndex = _node("CreateIndex", ["name", "table", "columns", "unique", "if_not_exists"], (False, False))
DropIndex = _node("DropIndex", ["name", "if_exists"], (False,))

TableRef = _node("TableRef", ["name", "alias"])
Join = _node("Join", ["kind", "table", "on"])
SelectItem = _node("SelectItem", ["expr", "alias"])
OrderItem = _node("OrderItem", ["expr", "descending"], (False,))
Select = _node("Select", ["items", "source", "joins", "where", "group_by", "having",
                          "order_by", "limit", "offset", "distinct"], (False,))
Insert = _node("Insert", ["table", "columns", "rows", "select"])
Update = _node("Update", ["table", "assignments", "where"])
Delete = _node("Delete", ["table", "where"])
Begin = _node("Begin", [])
Commit = _node("Commit", [])
Rollback = _node("Rollback", [])
Explain = _node("Explain", ["statement"])
