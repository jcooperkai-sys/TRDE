"""Expression evaluation.

NULL follows SQL's three-valued logic: it propagates through arithmetic and
comparisons, ``NULL AND FALSE`` is FALSE, ``NULL OR TRUE`` is TRUE, and
everything else involving NULL is unknown (Python ``None``).

Values compare using the storage-class ordering from :mod:`quarry.values`
(NULL < numbers < text < blobs), the same rule SQLite uses.
"""

import decimal
import re

from . import sqlast as ast
from .errors import QuarryError
from .values import compare, format_number, type_of

_LIKE_CACHE = {}


class Frame(object):
    """Row-level evaluation context handed to :func:`evaluate`."""

    __slots__ = ("columns", "index", "values", "params", "aggregates")

    def __init__(self, columns=(), index=None, values=(), params=(), aggregates=None):
        self.columns = columns          # list of (alias, column_name) lowercased
        self.index = index or {}        # lookup dict built by the executor
        self.values = values
        self.params = params
        self.aggregates = aggregates or {}

    def clone(self, values):
        return Frame(self.columns, self.index, values, self.params, self.aggregates)

    def lookup(self, table, name):
        key = (table.lower() if table else None, name.lower())
        try:
            slot = self.index[key]
        except KeyError:
            if key[0] is None:
                raise QuarryError("no such column: %s" % name)
            raise QuarryError("no such column: %s.%s" % (table, name))
        if slot is None:
            raise QuarryError("ambiguous column name: %s" % name)
        return self.values[slot]


def truth(value):
    """SQL truthiness: None stays unknown, numbers are true when non-zero."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return _text_as_number(value) != 0
    if isinstance(value, bytes):
        return len(value) > 0
    return bool(value)


def _text_as_number(text):
    stripped = text.strip()
    for cast in (int, float):
        try:
            return cast(stripped)
        except ValueError:
            continue
    match = re.match(r"^[+-]?\d+", stripped)
    if match:
        return int(match.group(0))
    return 0


def _numeric(value):
    """Best-effort numeric coercion used by arithmetic operators."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return _text_as_number(value)
    return 0


def like_to_regex(pattern, escape=None):
    key = (pattern, escape)
    cached = _LIKE_CACHE.get(key)
    if cached is not None:
        return cached
    out = ["(?is)^"]
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if escape and ch == escape and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        if ch == "%":
            out.append(".*")
        elif ch == "_":
            out.append(".")
        else:
            out.append(re.escape(ch))
        i += 1
    out.append("$")
    compiled = re.compile("".join(out), re.DOTALL)
    if len(_LIKE_CACHE) < 512:
        _LIKE_CACHE[key] = compiled
    return compiled


def evaluate(node, frame):
    kind = type(node)
    handler = _HANDLERS.get(kind)
    if handler is None:
        raise QuarryError("cannot evaluate %r" % (node,))
    return handler(node, frame)


# -- individual node handlers --------------------------------------------
def _eval_literal(node, frame):
    return node.value


def _eval_param(node, frame):
    try:
        return frame.params[node.index]
    except (IndexError, TypeError):
        raise QuarryError("missing binding for parameter %d" % (node.index + 1))


def _eval_column(node, frame):
    return frame.lookup(node.table, node.name)


def _eval_unary(node, frame):
    value = evaluate(node.operand, frame)
    if node.op == "-":
        number = _numeric(value)
        return None if number is None else -number
    if node.op == "NOT":
        result = truth(value)
        return None if result is None else (not result)
    raise QuarryError("unknown unary operator %r" % node.op)


def _arith(op, left, right):
    if left is None or right is None:
        return None
    a, b = _numeric(left), _numeric(right)
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if op == "/":
        if b == 0:
            return None
        if isinstance(a, int) and isinstance(b, int):
            quotient = abs(a) // abs(b)
            return -quotient if (a < 0) != (b < 0) else quotient
        return a / b
    if op == "%":
        if b == 0:
            return None
        remainder = abs(a) % abs(b)
        return -remainder if a < 0 else remainder
    raise QuarryError("unknown arithmetic operator %r" % op)


_COMPARATORS = {
    "=": lambda c: c == 0,
    "==": lambda c: c == 0,
    "!=": lambda c: c != 0,
    "<>": lambda c: c != 0,
    "<": lambda c: c < 0,
    "<=": lambda c: c <= 0,
    ">": lambda c: c > 0,
    ">=": lambda c: c >= 0,
}


def _eval_binary(node, frame):
    op = node.op
    if op == "AND":
        left = truth(evaluate(node.left, frame))
        if left is False:
            return False
        right = truth(evaluate(node.right, frame))
        if right is False:
            return False
        if left is None or right is None:
            return None
        return True
    if op == "OR":
        left = truth(evaluate(node.left, frame))
        if left is True:
            return True
        right = truth(evaluate(node.right, frame))
        if right is True:
            return True
        if left is None or right is None:
            return None
        return False
    left = evaluate(node.left, frame)
    right = evaluate(node.right, frame)
    if op in ("IS", "IS NOT"):
        same = compare(left, right) == 0 and type_of(left) == type_of(right)
        return same if op == "IS" else (not same)
    if op == "||":
        if left is None or right is None:
            return None
        return _as_text(left) + _as_text(right)
    if op in _COMPARATORS:
        if left is None or right is None:
            return None
        return _COMPARATORS[op](compare(left, right))
    return _arith(op, left, right)


def _as_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return format_number(value)


def _eval_is_null(node, frame):
    value = evaluate(node.operand, frame)
    return (value is not None) if node.negated else (value is None)


def _eval_in(node, frame):
    value = evaluate(node.operand, frame)
    if value is None:
        return None
    saw_null = False
    for item in node.items:
        candidate = evaluate(item, frame)
        if candidate is None:
            saw_null = True
            continue
        if compare(value, candidate) == 0:
            return not node.negated
    if saw_null:
        return None
    return node.negated


def _eval_between(node, frame):
    value = evaluate(node.operand, frame)
    low = evaluate(node.low, frame)
    high = evaluate(node.high, frame)
    if value is None or low is None or high is None:
        return None
    inside = compare(value, low) >= 0 and compare(value, high) <= 0
    return (not inside) if node.negated else inside


def _eval_like(node, frame):
    value = evaluate(node.operand, frame)
    pattern = evaluate(node.pattern, frame)
    if value is None or pattern is None:
        return None
    matched = bool(like_to_regex(_as_text(pattern)).match(_as_text(value)))
    return (not matched) if node.negated else matched


def _eval_case(node, frame):
    if node.operand is not None:
        subject = evaluate(node.operand, frame)
        for condition, result in node.whens:
            if compare(subject, evaluate(condition, frame)) == 0:
                return evaluate(result, frame)
    else:
        for condition, result in node.whens:
            if truth(evaluate(condition, frame)) is True:
                return evaluate(result, frame)
    return evaluate(node.orelse, frame) if node.orelse is not None else None


def _eval_func(node, frame):
    key = id(node)
    if key in frame.aggregates:
        return frame.aggregates[key]
    if node.name in AGGREGATE_NAMES and not (node.name in ("MIN", "MAX") and len(node.args) > 1):
        raise QuarryError("misuse of aggregate function %s()" % node.name)
    handler = SCALAR_FUNCTIONS.get(node.name)
    if handler is None:
        raise QuarryError("no such function: %s" % node.name)
    args = [evaluate(arg, frame) for arg in node.args]
    return handler(args)


# -- scalar functions ----------------------------------------------------
def _fn_abs(args):
    _arity("ABS", args, 1)
    value = _numeric(args[0])
    return None if value is None else abs(value)


def _fn_length(args):
    _arity("LENGTH", args, 1)
    value = args[0]
    if value is None:
        return None
    if isinstance(value, bytes):
        return len(value)
    return len(_as_text(value))


def _fn_upper(args):
    _arity("UPPER", args, 1)
    return None if args[0] is None else _as_text(args[0]).upper()


def _fn_lower(args):
    _arity("LOWER", args, 1)
    return None if args[0] is None else _as_text(args[0]).lower()


def _fn_substr(args):
    if len(args) not in (2, 3):
        raise QuarryError("SUBSTR() takes 2 or 3 arguments")
    if args[0] is None or args[1] is None:
        return None
    text = _as_text(args[0])
    start = int(_numeric(args[1]))
    if start > 0:
        begin = start - 1
    elif start == 0:
        begin = 0
    else:
        begin = max(len(text) + start, 0)
    if len(args) == 3:
        if args[2] is None:
            return None
        count = int(_numeric(args[2]))
        if count < 0:
            end = begin
            begin = max(begin + count, 0)
            return text[begin:end]
        return text[begin:begin + count]
    return text[begin:]


def _fn_coalesce(args):
    if not args:
        raise QuarryError("COALESCE() needs at least one argument")
    for value in args:
        if value is not None:
            return value
    return None


def _fn_ifnull(args):
    _arity("IFNULL", args, 2)
    return args[0] if args[0] is not None else args[1]


def _fn_nullif(args):
    _arity("NULLIF", args, 2)
    return None if compare(args[0], args[1]) == 0 else args[0]


def _fn_round(args):
    if len(args) not in (1, 2):
        raise QuarryError("ROUND() takes 1 or 2 arguments")
    if args[0] is None:
        return None
    digits = 0 if len(args) == 1 else int(_numeric(args[1]) or 0)
    digits = max(digits, 0)
    value = float(_numeric(args[0]))
    if value != value or value in (float("inf"), float("-inf")):
        return value
    # Round the *exact* binary value, half away from zero.  Scaling by a power
    # of ten first would round 65.205 (stored as 65.20499...) up to 65.21.
    quantum = decimal.Decimal(1).scaleb(-digits)
    exact = decimal.Decimal(value).quantize(quantum, rounding=decimal.ROUND_HALF_UP)
    return float(exact)


def _fn_trim(args, mode="both"):
    if len(args) not in (1, 2):
        raise QuarryError("TRIM() takes 1 or 2 arguments")
    if args[0] is None:
        return None
    text = _as_text(args[0])
    chars = _as_text(args[1]) if len(args) == 2 else " "
    if mode == "both":
        return text.strip(chars)
    if mode == "left":
        return text.lstrip(chars)
    return text.rstrip(chars)


def _fn_replace(args):
    _arity("REPLACE", args, 3)
    if any(a is None for a in args):
        return None
    return _as_text(args[0]).replace(_as_text(args[1]), _as_text(args[2]))


def _fn_instr(args):
    _arity("INSTR", args, 2)
    if args[0] is None or args[1] is None:
        return None
    return _as_text(args[0]).find(_as_text(args[1])) + 1


def _fn_typeof(args):
    _arity("TYPEOF", args, 1)
    return type_of(args[0]).lower() if args[0] is not None else "null"


def _fn_hex(args):
    _arity("HEX", args, 1)
    value = args[0]
    if value is None:
        return None
    data = value if isinstance(value, bytes) else _as_text(value).encode("utf-8")
    return "".join("%02X" % b for b in bytearray(data))


def _fn_min(args):
    if len(args) < 2:
        raise QuarryError("MIN() as a scalar function needs at least 2 arguments")
    if any(a is None for a in args):
        return None
    best = args[0]
    for value in args[1:]:
        if compare(value, best) < 0:
            best = value
    return best


def _fn_max(args):
    if len(args) < 2:
        raise QuarryError("MAX() as a scalar function needs at least 2 arguments")
    if any(a is None for a in args):
        return None
    best = args[0]
    for value in args[1:]:
        if compare(value, best) > 0:
            best = value
    return best


def _arity(name, args, count):
    if len(args) != count:
        raise QuarryError("%s() takes exactly %d argument%s" % (name, count, "" if count == 1 else "s"))


SCALAR_FUNCTIONS = {
    "ABS": _fn_abs,
    "LENGTH": _fn_length,
    "UPPER": _fn_upper,
    "LOWER": _fn_lower,
    "SUBSTR": _fn_substr,
    "SUBSTRING": _fn_substr,
    "COALESCE": _fn_coalesce,
    "IFNULL": _fn_ifnull,
    "NULLIF": _fn_nullif,
    "ROUND": _fn_round,
    "TRIM": _fn_trim,
    "LTRIM": lambda args: _fn_trim(args, "left"),
    "RTRIM": lambda args: _fn_trim(args, "right"),
    "REPLACE": _fn_replace,
    "INSTR": _fn_instr,
    "TYPEOF": _fn_typeof,
    "HEX": _fn_hex,
    "MIN": _fn_min,
    "MAX": _fn_max,
}

AGGREGATE_NAMES = {"COUNT", "SUM", "TOTAL", "AVG", "MIN", "MAX", "GROUP_CONCAT"}


_HANDLERS = {
    ast.Literal: _eval_literal,
    ast.Param: _eval_param,
    ast.ColumnRef: _eval_column,
    ast.Unary: _eval_unary,
    ast.Binary: _eval_binary,
    ast.IsNull: _eval_is_null,
    ast.InList: _eval_in,
    ast.Between: _eval_between,
    ast.Like: _eval_like,
    ast.Case: _eval_case,
    ast.FuncCall: _eval_func,
}


# -- aggregates -----------------------------------------------------------
class Aggregator(object):
    """Accumulates one aggregate function over the rows of a group."""

    def __init__(self, node):
        self.node = node
        self.name = node.name
        self.distinct = node.distinct
        self.seen = set() if node.distinct else None
        self.count = 0
        self.total = 0.0
        self.int_total = 0
        self.all_int = True
        self.best = None
        self.texts = []
        self.saw_value = False

    def step(self, frame):
        if self.node.star:
            self.count += 1
            return
        if not self.node.args:
            raise QuarryError("%s() requires an argument" % self.name)
        value = evaluate(self.node.args[0], frame)
        if value is None:
            return
        if self.distinct:
            marker = (type_of(value), value)
            if marker in self.seen:
                return
            self.seen.add(marker)
        self.count += 1
        self.saw_value = True
        if self.name in ("SUM", "TOTAL", "AVG"):
            number = _numeric(value)
            if isinstance(number, int) and not isinstance(number, bool):
                self.int_total += number
            else:
                self.all_int = False
            self.total += float(number)
        elif self.name == "MIN":
            if self.best is None or compare(value, self.best) < 0:
                self.best = value
        elif self.name == "MAX":
            if self.best is None or compare(value, self.best) > 0:
                self.best = value
        elif self.name == "GROUP_CONCAT":
            separator = ","
            if len(self.node.args) > 1:
                separator = _as_text(evaluate(self.node.args[1], frame))
            self.texts.append((separator, _as_text(value)))

    def result(self):
        name = self.name
        if name == "COUNT":
            return self.count
        if name == "SUM":
            if not self.saw_value:
                return None
            return self.int_total if self.all_int else self.total
        if name == "TOTAL":
            return self.total
        if name == "AVG":
            if not self.count:
                return None
            return self.total / self.count
        if name in ("MIN", "MAX"):
            return self.best
        if name == "GROUP_CONCAT":
            if not self.texts:
                return None
            out = self.texts[0][1]
            for separator, text in self.texts[1:]:
                out += separator + text
            return out
        raise QuarryError("unknown aggregate %s()" % name)


def find_aggregates(node, found=None):
    """Collect aggregate FuncCall nodes reachable from ``node``."""
    if found is None:
        found = []
    if node is None or isinstance(node, (str, int, float, bytes)):
        return found
    if isinstance(node, ast.FuncCall) and node.name in AGGREGATE_NAMES:
        # MIN/MAX are aggregates with one argument and scalar with several.
        scalar_minmax = node.name in ("MIN", "MAX") and len(node.args) > 1
        if not scalar_minmax:
            found.append(node)
            return found
    if isinstance(node, ast.Node):
        for field in node._fields:
            find_aggregates(getattr(node, field), found)
    elif isinstance(node, (list, tuple)):
        for item in node:
            find_aggregates(item, found)
    return found


def contains_aggregate(node):
    return bool(find_aggregates(node))
