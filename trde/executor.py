"""Statement execution: planning, joins, aggregation and DDL/DML.

Rows flow through the pipeline as flat tuples.  For a query over N tables a row
holds every column of table 0, then table 0's rowid, then every column of
table 1, then its rowid, and so on; :class:`SourceSet` maps names to offsets so
expressions can be evaluated without copying rows into dicts.

Access-path selection is cost-free but effective: WHERE and ON are split on
AND, and any conjunct comparing an indexed column against a value already
bound (a constant, a parameter, or a column of an outer table) can drive an
index seek instead of a scan.  Filters are always re-applied to the assembled
row, so an index is only ever an accelerator -- never the arbiter of
correctness.
"""

import functools
import struct

from . import sqlast as ast
from .btree import BTree
from .catalog import ColumnMeta, TableMeta
from .errors import (IntegrityError, TRDEError, SchemaError,
                     TransactionError)
from .expr import (AGGREGATE_NAMES, Aggregator, Frame, evaluate,
                   find_aggregates, truth)
from .heap import HeapFile
from .table import Table
from .values import compare, normalize_type, type_of

_EQ_OPS = ("=", "==")
_RANGE_OPS = ("<", "<=", ">", ">=")


class Result(object):
    """The outcome of one statement."""

    __slots__ = ("columns", "rows", "rowcount", "message", "plan")

    def __init__(self, columns=None, rows=None, rowcount=0, message=None, plan=None):
        self.columns = columns or []
        self.rows = rows if rows is not None else []
        self.rowcount = rowcount
        self.message = message
        self.plan = plan or []

    def __iter__(self):
        return iter(self.rows)

    def __len__(self):
        return len(self.rows)

    def __repr__(self):
        return "Result(columns=%r, rows=%d)" % (self.columns, len(self.rows))

    def dicts(self):
        return [dict(zip(self.columns, row)) for row in self.rows]

    def scalar(self):
        if not self.rows or not self.rows[0]:
            return None
        return self.rows[0][0]


def describe_expr(node):
    """Human-readable label for an output column."""
    if isinstance(node, ast.ColumnRef):
        return node.name if node.table is None else "%s.%s" % (node.table, node.name)
    if isinstance(node, ast.Literal):
        if isinstance(node.value, str):
            return "'%s'" % node.value
        return "NULL" if node.value is None else str(node.value)
    if isinstance(node, ast.Param):
        return "?"
    if isinstance(node, ast.FuncCall):
        if node.star:
            return "%s(*)" % node.name
        inner = ", ".join(describe_expr(a) for a in node.args)
        return "%s(%s%s)" % (node.name, "DISTINCT " if node.distinct else "", inner)
    if isinstance(node, ast.Binary):
        return "%s %s %s" % (describe_expr(node.left), node.op, describe_expr(node.right))
    if isinstance(node, ast.Unary):
        return "%s%s" % (node.op if node.op == "-" else node.op + " ", describe_expr(node.operand))
    if isinstance(node, ast.IsNull):
        return "%s IS %sNULL" % (describe_expr(node.operand), "NOT " if node.negated else "")
    if isinstance(node, ast.InList):
        return "%s %sIN (...)" % (describe_expr(node.operand), "NOT " if node.negated else "")
    if isinstance(node, ast.Between):
        return "%s %sBETWEEN %s AND %s" % (describe_expr(node.operand),
                                           "NOT " if node.negated else "",
                                           describe_expr(node.low), describe_expr(node.high))
    if isinstance(node, ast.Like):
        return "%s %sLIKE %s" % (describe_expr(node.operand), "NOT " if node.negated else "",
                                 describe_expr(node.pattern))
    if isinstance(node, ast.Case):
        return "CASE"
    return "expr"


class SourceSet(object):
    """Name resolution over the tables participating in a query."""

    def __init__(self):
        self.aliases = []
        self.tables = []
        self.offsets = []
        self.rowid_positions = []
        self.index = {}
        self.width = 0
        self._counts = {}

    def add(self, alias, table):
        alias_lower = alias.lower()
        if alias_lower in self.aliases:
            raise SchemaError("duplicate table alias %r" % alias)
        offset = self.width
        self.aliases.append(alias_lower)
        self.tables.append(table)
        self.offsets.append(offset)
        for i, column in enumerate(table.columns):
            name = column.name.lower()
            self.index[(alias_lower, name)] = offset + i
            self._counts[name] = self._counts.get(name, 0) + 1
            self.index[(None, name)] = offset + i if self._counts[name] == 1 else None
        rowid_position = offset + len(table.columns)
        self.rowid_positions.append(rowid_position)
        self.index[(alias_lower, "rowid")] = rowid_position
        if "rowid" not in self._counts:
            self.index.setdefault((None, "rowid"), rowid_position)
        elif (None, "rowid") in self.index and self._counts.get("rowid", 0) == 0:
            self.index[(None, "rowid")] = None
        self.width = rowid_position + 1
        return len(self.tables) - 1

    def level_of(self, alias):
        try:
            return self.aliases.index(alias.lower())
        except ValueError:
            return -1

    def owner_of(self, column_ref):
        """Alias index owning ``column_ref``, or -1 when unresolvable."""
        if column_ref.table is not None:
            return self.level_of(column_ref.table)
        name = column_ref.name.lower()
        position = self.index.get((None, name))
        if position is None:
            return -1
        for level in range(len(self.tables)):
            start = self.offsets[level]
            end = self.rowid_positions[level]
            if start <= position <= end:
                return level
        return -1

    def column_position(self, level, name):
        return self.index.get((self.aliases[level], name.lower()))

    def star_positions(self, alias=None):
        out = []
        for level, table in enumerate(self.tables):
            if alias is not None and self.aliases[level] != alias.lower():
                continue
            base = self.offsets[level]
            for i, column in enumerate(table.columns):
                out.append((base + i, column.name))
        if alias is not None and not out:
            raise SchemaError("no such table: %s" % alias)
        return out


def split_conjuncts(node, out=None):
    """Flatten an AND tree into a list of predicates."""
    if out is None:
        out = []
    if node is None:
        return out
    if isinstance(node, ast.Binary) and node.op == "AND":
        split_conjuncts(node.left, out)
        split_conjuncts(node.right, out)
    else:
        out.append(node)
    return out


def referenced_levels(node, sources, out=None):
    if out is None:
        out = set()
    if isinstance(node, ast.ColumnRef):
        level = sources.owner_of(node)
        if level < 0:
            raise TRDEError("no such column: %s" % (
                node.name if node.table is None else "%s.%s" % (node.table, node.name)))
        out.add(level)
    elif isinstance(node, ast.Node):
        for field in node._fields:
            referenced_levels(getattr(node, field), sources, out)
    elif isinstance(node, (list, tuple)):
        for item in node:
            referenced_levels(item, sources, out)
    return out


class AccessPath(object):
    """How one table's rows are produced at its level of the join."""

    def __init__(self, kind, index=None, op=None, keys=None, low=None, high=None,
                 include_low=True, include_high=True, description=""):
        self.kind = kind          # "scan" | "index" | "rowid" | "index-range"
        self.index = index
        self.op = op
        self.keys = keys or []
        self.low = low
        self.high = high
        self.include_low = include_low
        self.include_high = include_high
        self.description = description


class Executor(object):
    def __init__(self, database):
        self.db = database

    # -- dispatch ---------------------------------------------------------
    def execute(self, statement, params=()):
        handler = {
            ast.Select: self.exec_select,
            ast.Insert: self.exec_insert,
            ast.Update: self.exec_update,
            ast.Delete: self.exec_delete,
            ast.CreateTable: self.exec_create_table,
            ast.DropTable: self.exec_drop_table,
            ast.CreateIndex: self.exec_create_index,
            ast.DropIndex: self.exec_drop_index,
            ast.Begin: self.exec_begin,
            ast.Commit: self.exec_commit,
            ast.Rollback: self.exec_rollback,
            ast.Explain: self.exec_explain,
        }.get(type(statement))
        if handler is None:
            raise TRDEError("cannot execute %r" % (statement,))
        return handler(statement, params)

    # -- transactions -----------------------------------------------------
    def exec_begin(self, statement, params):
        self.db.begin()
        return Result(message="BEGIN")

    def exec_commit(self, statement, params):
        self.db.commit()
        return Result(message="COMMIT")

    def exec_rollback(self, statement, params):
        self.db.rollback()
        return Result(message="ROLLBACK")

    def exec_explain(self, statement, params):
        inner = statement.statement
        if not isinstance(inner, ast.Select):
            return Result(columns=["detail"], rows=[("%s statement" % type(inner).__name__.upper(),)])
        plan = self.plan_select(inner, params)
        return Result(columns=["detail"], rows=[(line,) for line in plan], plan=plan)

    # -- DDL --------------------------------------------------------------
    def exec_create_table(self, statement, params):
        with self.db.auto_transaction():
            catalog = self.db.catalog
            if catalog.has_table(statement.name):
                if statement.if_not_exists:
                    return Result(message="table %s already exists" % statement.name)
                raise SchemaError("table %s already exists" % statement.name)
            seen = set()
            columns = []
            for definition in statement.columns:
                lowered = definition.name.lower()
                if lowered in seen:
                    raise SchemaError("duplicate column name: %s" % definition.name)
                if lowered == "rowid":
                    raise SchemaError("'rowid' is reserved and cannot be a column name")
                seen.add(lowered)
                default = None
                if definition.default is not None:
                    default = evaluate(definition.default, Frame(params=params))
                columns.append(ColumnMeta(definition.name, normalize_type(definition.type),
                                          definition.not_null, definition.primary_key, default))
            heap = HeapFile.create(self.db.pager)
            meta = TableMeta(statement.name, columns, heap.first_page, [], "")
            catalog.add_table(meta)
            table = Table(self.db.pager, meta)

            primary = [c.name for c in columns if c.primary_key]
            for constraint in statement.constraints:
                for name in constraint.columns:
                    if meta.column_index(name) < 0:
                        raise SchemaError("table %s has no column %r" % (meta.name, name))
                if constraint.kind == "PRIMARY KEY":
                    if primary:
                        raise SchemaError("table %s has more than one primary key" % meta.name)
                    primary = list(constraint.columns)
                    for name in primary:
                        column = meta.column(name)
                        column.primary_key = True
                        column.not_null = True
                else:
                    table.create_index("sqlite_autoindex_%s_%d" % (meta.name, len(meta.indexes) + 1),
                                       constraint.columns, True, "UNIQUE")
            if primary:
                for name in primary:
                    meta.column(name).not_null = True
                table.create_index("%s_pkey" % meta.name, primary, True, "PRIMARY KEY")
            for definition in statement.columns:
                if definition.unique and not definition.primary_key:
                    table.create_index("%s_%s_key" % (meta.name, definition.name),
                                       [definition.name], True, "UNIQUE")
            catalog.save()
            self.db.invalidate()
        return Result(message="CREATE TABLE %s" % statement.name)

    def exec_drop_table(self, statement, params):
        with self.db.auto_transaction():
            catalog = self.db.catalog
            if not catalog.has_table(statement.name):
                if statement.if_exists:
                    return Result(message="no such table: %s" % statement.name)
                raise SchemaError("no such table: %s" % statement.name)
            table = self.db.table(statement.name)
            table.drop()
            catalog.remove_table(statement.name)
            catalog.save()
            self.db.invalidate()
        return Result(message="DROP TABLE %s" % statement.name)

    def exec_create_index(self, statement, params):
        with self.db.auto_transaction():
            catalog = self.db.catalog
            existing_table, existing = catalog.find_index(statement.name)
            if existing is not None:
                if statement.if_not_exists:
                    return Result(message="index %s already exists" % statement.name)
                raise SchemaError("index %s already exists" % statement.name)
            table = self.db.table(statement.table)
            for column in statement.columns:
                if table.meta.column_index(column) < 0:
                    raise SchemaError("table %s has no column %r" % (table.name, column))
            table.create_index(statement.name, statement.columns, statement.unique)
            catalog.save()
            self.db.invalidate()
        return Result(message="CREATE INDEX %s" % statement.name)

    def exec_drop_index(self, statement, params):
        with self.db.auto_transaction():
            catalog = self.db.catalog
            table_meta, index_meta = catalog.find_index(statement.name)
            if index_meta is None:
                if statement.if_exists:
                    return Result(message="no such index: %s" % statement.name)
                raise SchemaError("no such index: %s" % statement.name)
            if index_meta.origin != "CREATE INDEX":
                raise SchemaError("index %s implements a constraint and cannot be dropped"
                                  % statement.name)
            table = self.db.table(table_meta.name)
            table.drop_index(statement.name)
            catalog.save()
            self.db.invalidate()
        return Result(message="DROP INDEX %s" % statement.name)

    # -- INSERT -----------------------------------------------------------
    def exec_insert(self, statement, params):
        with self.db.auto_transaction():
            table = self.db.table(statement.table)
            meta = table.meta
            if statement.columns is None:
                positions = list(range(len(meta.columns)))
            else:
                positions = []
                for name in statement.columns:
                    position = meta.column_index(name)
                    if position < 0:
                        raise SchemaError("table %s has no column %r" % (meta.name, name))
                    if position in positions:
                        raise SchemaError("column %r named twice in INSERT" % name)
                    positions.append(position)
            if statement.select is not None:
                source = self.exec_select(statement.select, params)
                value_rows = source.rows
                for row in value_rows:
                    if len(row) != len(positions):
                        raise TRDEError("INSERT has %d target columns but the SELECT "
                                          "produced %d" % (len(positions), len(row)))
                literal_rows = [list(row) for row in value_rows]
            else:
                literal_rows = []
                frame = Frame(params=params)
                for row in statement.rows:
                    if len(row) != len(positions):
                        raise TRDEError("INSERT has %d target columns but %d values"
                                          % (len(positions), len(row)))
                    literal_rows.append([evaluate(expr, frame) for expr in row])
            count = 0
            for values in literal_rows:
                row = [column.default for column in meta.columns]
                for position, value in zip(positions, values):
                    row[position] = value
                table.insert(row)
                count += 1
        return Result(rowcount=count, message="%d row%s inserted" % (count, "" if count == 1 else "s"))

    # -- UPDATE / DELETE --------------------------------------------------
    def exec_update(self, statement, params):
        with self.db.auto_transaction():
            table = self.db.table(statement.table)
            sources = SourceSet()
            sources.add(table.name, table.meta)
            targets = []
            for name, expr in statement.assignments:
                position = table.meta.column_index(name)
                if position < 0:
                    raise SchemaError("table %s has no column %r" % (table.name, name))
                targets.append((position, expr))
            path = self.choose_path(sources, 0, split_conjuncts(statement.where), set(), params)
            frame = Frame(sources.aliases, sources.index, (), params)
            pending = []
            for rowid, row in self.iterate(table, path, (), sources, 0, params):
                values = list(row) + [rowid]
                frame.values = values
                if statement.where is not None and truth(evaluate(statement.where, frame)) is not True:
                    continue
                new_row = list(row)
                for position, expr in targets:
                    new_row[position] = evaluate(expr, frame)
                pending.append((rowid, list(row), new_row))
            for rowid, old_row, new_row in pending:
                table.update(rowid, old_row, new_row)
            count = len(pending)
        return Result(rowcount=count, message="%d row%s updated" % (count, "" if count == 1 else "s"))

    def exec_delete(self, statement, params):
        with self.db.auto_transaction():
            table = self.db.table(statement.table)
            sources = SourceSet()
            sources.add(table.name, table.meta)
            path = self.choose_path(sources, 0, split_conjuncts(statement.where), set(), params)
            frame = Frame(sources.aliases, sources.index, (), params)
            doomed = []
            for rowid, row in self.iterate(table, path, (), sources, 0, params):
                frame.values = list(row) + [rowid]
                if statement.where is not None and truth(evaluate(statement.where, frame)) is not True:
                    continue
                doomed.append((rowid, list(row)))
            for rowid, row in doomed:
                table.delete(rowid, row)
            count = len(doomed)
        return Result(rowcount=count, message="%d row%s deleted" % (count, "" if count == 1 else "s"))

    # -- SELECT -----------------------------------------------------------
    def _prepare_select(self, statement, params):
        sources = SourceSet()
        tables = []
        if statement.source is not None:
            table = self.db.table(statement.source.name)
            alias = statement.source.alias or statement.source.name
            sources.add(alias, table.meta)
            tables.append(table)
            for join in statement.joins:
                joined = self.db.table(join.table.name)
                sources.add(join.table.alias or join.table.name, joined.meta)
                tables.append(joined)
        return sources, tables

    def plan_select(self, statement, params):
        sources, tables = self._prepare_select(statement, params)
        if not tables:
            return ["SCAN CONSTANT ROW"]
        where_conjuncts = split_conjuncts(statement.where)
        lines = []
        bound = set()
        for level, table in enumerate(tables):
            conjuncts = list(where_conjuncts)
            if level > 0:
                join = statement.joins[level - 1]
                conjuncts = split_conjuncts(join.on) + ([] if join.kind == "LEFT" else conjuncts)
            path = self.choose_path(sources, level, conjuncts, set(bound), params)
            prefix = "SCAN" if level == 0 else "%s JOIN" % (statement.joins[level - 1].kind)
            lines.append("%s %s %s" % (prefix, table.name, path.description))
            bound.add(level)
        if statement.group_by:
            lines.append("GROUP BY (hash)")
        elif find_aggregates(statement.items):
            lines.append("AGGREGATE (single group)")
        if statement.distinct:
            lines.append("DISTINCT (hash)")
        if statement.order_by:
            lines.append("ORDER BY (sort)")
        if statement.limit is not None:
            lines.append("LIMIT")
        return lines

    def choose_path(self, sources, level, conjuncts, bound_levels, params):
        """Pick an access path for ``level`` given predicates and bound tables."""
        table_meta = sources.tables[level]
        alias = sources.aliases[level]
        usable = []
        for conjunct in conjuncts:
            levels = referenced_levels(conjunct, sources)
            if level not in levels:
                continue
            if not levels.issubset(bound_levels | {level}):
                continue
            usable.append(conjunct)

        def column_of(node):
            if isinstance(node, ast.ColumnRef) and sources.owner_of(node) == level:
                return node.name.lower()
            return None

        def outer_only(node):
            return referenced_levels(node, sources).issubset(bound_levels)

        equalities = {}
        ranges = {}
        rowid_key = None
        for conjunct in usable:
            if isinstance(conjunct, ast.Binary) and conjunct.op in _EQ_OPS + _RANGE_OPS:
                left, right, op = conjunct.left, conjunct.right, conjunct.op
                name = column_of(left)
                other = right
                if name is None:
                    name = column_of(right)
                    other = left
                    op = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}.get(op, op)
                if name is None or not outer_only(other):
                    continue
                if op in _EQ_OPS:
                    if name == "rowid":
                        rowid_key = other
                    equalities.setdefault(name, other)
                else:
                    ranges.setdefault(name, []).append((op, other))
            elif isinstance(conjunct, ast.Between):
                name = column_of(conjunct.operand)
                if name is None or conjunct.negated:
                    continue
                if outer_only(conjunct.low) and outer_only(conjunct.high):
                    ranges.setdefault(name, []).append((">=", conjunct.low))
                    ranges.setdefault(name, []).append(("<=", conjunct.high))

        table = self.db.table(table_meta.name)
        if rowid_key is not None:
            return AccessPath("rowid", keys=[rowid_key],
                              description="USING INTEGER PRIMARY KEY (rowid=?)")

        best = None
        for index in table.indexes:
            keys = []
            for column in index.meta.columns:
                key = equalities.get(column.lower())
                if key is None:
                    break
                keys.append(key)
            if keys:
                score = (len(keys), 1 if index.unique and len(keys) == len(index.meta.columns) else 0)
                if best is None or score > best[0]:
                    best = (score, AccessPath(
                        "index", index=index, keys=keys,
                        description="USING INDEX %s (%s)" % (
                            index.name, ", ".join("%s=?" % c for c in index.meta.columns[:len(keys)]))))
        if best is not None:
            return best[1]

        for index in table.indexes:
            first = index.meta.columns[0].lower()
            bounds = ranges.get(first)
            if not bounds:
                continue
            low = high = None
            include_low = include_high = True
            for op, node in bounds:
                if op in (">", ">="):
                    low = node
                    include_low = op == ">="
                else:
                    high = node
                    include_high = op == "<="
            return AccessPath("index-range", index=index, low=low, high=high,
                              include_low=include_low, include_high=include_high,
                              description="USING INDEX %s (%s range)" % (index.name, index.meta.columns[0]))
        return AccessPath("scan", description="(full table scan)")

    def iterate(self, table, path, outer_values, sources, level, params):
        """Yield (rowid, row) for one level following ``path``."""
        frame = Frame(sources.aliases, sources.index, outer_values, params)
        if path.kind == "rowid":
            value = evaluate(path.keys[0], frame)
            if value is None or isinstance(value, (str, bytes)):
                return
            try:
                rowid = int(value)
            except (TypeError, ValueError):
                return
            row = table.row(rowid)
            if row is not None:
                yield rowid, row
            return
        if path.kind == "index":
            keys = [evaluate(key, frame) for key in path.keys]
            columns = path.index.meta.columns[:len(keys)]
            keys = [self._coerce_key(table, column, key) for column, key in zip(columns, keys)]
            if any(key is None for key in keys):
                return
            for rowid in path.index.lookup(keys):
                row = table.row(rowid)
                if row is not None:
                    yield rowid, row
            return
        if path.kind == "index-range":
            column = path.index.meta.columns[0]
            low = self._coerce_key(table, column, evaluate(path.low, frame)) if path.low is not None else None
            high = self._coerce_key(table, column, evaluate(path.high, frame)) if path.high is not None else None
            if (path.low is not None and low is None) or (path.high is not None and high is None):
                return
            for rowid in path.index.range(low, high, path.include_low, path.include_high):
                row = table.row(rowid)
                if row is not None:
                    yield rowid, row
            return
        for rowid, row in table.scan():
            yield rowid, row

    @staticmethod
    def _coerce_key(table, column_name, value):
        """Match the stored representation so index probes compare equal."""
        if value is None:
            return None
        column = table.meta.column(column_name)
        try:
            from .values import coerce
            return coerce(value, column.type)
        except TRDEError:
            return None

    def _join_rows(self, statement, sources, tables, params):
        """Nested-loop join producing fully assembled rows."""
        if not tables:
            yield [None] * sources.width if sources.width else []
            return
        where_conjuncts = split_conjuncts(statement.where)
        paths = []
        bound = set()
        for level in range(len(tables)):
            conjuncts = list(where_conjuncts)
            if level > 0:
                join = statement.joins[level - 1]
                conjuncts = split_conjuncts(join.on) + ([] if join.kind == "LEFT" else conjuncts)
            paths.append(self.choose_path(sources, level, conjuncts, set(bound), params))
            bound.add(level)

        widths = [len(table.columns) for table in tables]
        frame = Frame(sources.aliases, sources.index, (), params)

        def recurse(level, values):
            if level == len(tables):
                yield list(values)
                return
            join = statement.joins[level - 1] if level > 0 else None
            matched = False
            for rowid, row in self.iterate(tables[level], paths[level], values, sources, level, params):
                candidate = list(values) + list(row) + [rowid]
                if join is not None and join.on is not None:
                    frame.values = candidate + [None] * (sources.width - len(candidate))
                    if truth(evaluate(join.on, frame)) is not True:
                        continue
                matched = True
                for out in recurse(level + 1, candidate):
                    yield out
            if not matched and join is not None and join.kind == "LEFT":
                filler = list(values) + [None] * (widths[level] + 1)
                for out in recurse(level + 1, filler):
                    yield out

        for row in recurse(0, []):
            yield row

    def exec_select(self, statement, params):
        sources, tables = self._prepare_select(statement, params)
        frame = Frame(sources.aliases, sources.index, (), params)

        # Expand * into concrete output expressions.
        outputs = []
        for item in statement.items:
            if isinstance(item.expr, ast.Star):
                if not tables:
                    raise TRDEError("no tables specified for *")
                for position, name in sources.star_positions(item.expr.table):
                    outputs.append((None, name, position))
            else:
                name = item.alias or describe_expr(item.expr)
                outputs.append((item.expr, name, None))

        aggregate_nodes = find_aggregates([item.expr for item in statement.items])
        aggregate_nodes += find_aggregates(statement.having)
        if statement.order_by:
            aggregate_nodes += find_aggregates([o.expr for o in statement.order_by])
        deduped = []
        for node in aggregate_nodes:
            if not any(node is seen for seen in deduped):
                deduped.append(node)
        aggregate_nodes = deduped
        grouped = bool(statement.group_by) or bool(aggregate_nodes)

        rows = self._join_rows(statement, sources, tables, params)
        if statement.where is not None:
            def filtered(source):
                for values in source:
                    frame.values = values
                    if truth(evaluate(statement.where, frame)) is True:
                        yield values
            rows = filtered(rows)

        records = []  # (frame_values, aggregates map)
        if not grouped:
            for values in rows:
                records.append((values, {}))
        else:
            groups = []
            lookup = {}
            for values in rows:
                frame.values = values
                if statement.group_by:
                    key = tuple(self._group_key(evaluate(expr, frame)) for expr in statement.group_by)
                else:
                    key = ()
                entry = lookup.get(key)
                if entry is None:
                    entry = (values, [Aggregator(node) for node in aggregate_nodes])
                    lookup[key] = entry
                    groups.append(entry)
                for aggregator in entry[1]:
                    aggregator.step(frame)
            if not groups and not statement.group_by:
                groups.append(([None] * sources.width, [Aggregator(node) for node in aggregate_nodes]))
            for values, aggregators in groups:
                mapping = {}
                for node, aggregator in zip(aggregate_nodes, aggregators):
                    mapping[id(node)] = aggregator.result()
                records.append((values, mapping))
            if statement.having is not None:
                kept = []
                for values, mapping in records:
                    frame.values = values
                    frame.aggregates = mapping
                    if truth(evaluate(statement.having, frame)) is True:
                        kept.append((values, mapping))
                frame.aggregates = {}
                records = kept

        column_names = [name for _, name, _ in outputs]
        alias_positions = {}
        for position, (_, name, _) in enumerate(outputs):
            alias_positions.setdefault(name.lower(), position)

        produced = []
        for values, mapping in records:
            frame.values = values
            frame.aggregates = mapping
            row = []
            for expr, _, position in outputs:
                row.append(values[position] if expr is None else evaluate(expr, frame))
            sort_key = None
            if statement.order_by:
                sort_key = []
                for item in statement.order_by:
                    sort_key.append(self._order_value(item.expr, frame, row, alias_positions))
            produced.append((tuple(row), sort_key))
        frame.aggregates = {}

        if statement.distinct:
            seen = set()
            unique = []
            for row, sort_key in produced:
                marker = tuple((type_of(v), v) for v in row)
                if marker in seen:
                    continue
                seen.add(marker)
                unique.append((row, sort_key))
            produced = unique

        if statement.order_by:
            directions = [item.descending for item in statement.order_by]

            def cmp_rows(a, b):
                for value_a, value_b, descending in zip(a[1], b[1], directions):
                    result = compare(value_a, value_b)
                    if result:
                        return -result if descending else result
                return 0

            produced.sort(key=functools.cmp_to_key(cmp_rows))

        offset = 0
        if statement.offset is not None:
            offset = int(evaluate(statement.offset, Frame(params=params)) or 0)
        if offset:
            produced = produced[offset:]
        if statement.limit is not None:
            limit = evaluate(statement.limit, Frame(params=params))
            if limit is not None:
                limit = int(limit)
                if limit >= 0:
                    produced = produced[:limit]

        return Result(columns=column_names, rows=[row for row, _ in produced],
                      rowcount=len(produced), plan=self.plan_select(statement, params))

    @staticmethod
    def _group_key(value):
        return (type_of(value), value)

    def _order_value(self, expr, frame, output_row, alias_positions):
        if isinstance(expr, ast.Literal) and isinstance(expr.value, int) \
                and not isinstance(expr.value, bool):
            position = expr.value - 1
            if 0 <= position < len(output_row):
                return output_row[position]
            raise TRDEError("ORDER BY position %d is out of range" % expr.value)
        if isinstance(expr, ast.ColumnRef) and expr.table is None:
            key = expr.name.lower()
            if (None, key) not in frame.index and key in alias_positions:
                return output_row[alias_positions[key]]
            if (None, key) in frame.index and frame.index[(None, key)] is None \
                    and key in alias_positions:
                return output_row[alias_positions[key]]
        return evaluate(expr, frame)
