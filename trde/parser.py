"""Recursive-descent SQL parser producing :mod:`trde.sqlast` nodes.

Expression precedence, loosest to tightest::

    OR  <  AND  <  NOT  <  comparison (= <> < <= > >= IS IN LIKE BETWEEN)
        <  ||  <  + -  <  * / %  <  unary + -  <  primary
"""

from . import sqlast as ast
from .errors import ParseError
from .tokenizer import (TK_BLOB, TK_EOF, TK_IDENT, TK_KEYWORD, TK_NUMBER,
                        TK_OP, TK_PARAM, TK_STRING, tokenize)

COMPARISONS = {"=", "==", "!=", "<>", "<", "<=", ">", ">="}
AGGREGATES = {"COUNT", "SUM", "AVG", "MIN", "MAX", "TOTAL", "GROUP_CONCAT"}


class Parser(object):
    def __init__(self, text):
        self.text = text
        self.tokens = tokenize(text)
        self.pos = 0

    # -- token helpers ----------------------------------------------------
    @property
    def token(self):
        return self.tokens[self.pos]

    def next_token(self):
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def at_keyword(self, *words):
        token = self.token
        return token.kind == TK_KEYWORD and token.value in words

    def at_op(self, *ops):
        token = self.token
        return token.kind == TK_OP and token.value in ops

    def at_end(self):
        return self.token.kind == TK_EOF

    def accept_keyword(self, *words):
        if self.at_keyword(*words):
            return self.next_token().value
        return None

    def accept_op(self, *ops):
        if self.at_op(*ops):
            return self.next_token().value
        return None

    def expect_keyword(self, *words):
        if not self.at_keyword(*words):
            raise ParseError("expected %s, found %r" % (" or ".join(words), self._describe()), self.token.pos)
        return self.next_token().value

    def expect_op(self, op):
        if not self.at_op(op):
            raise ParseError("expected %r, found %r" % (op, self._describe()), self.token.pos)
        return self.next_token().value

    def expect_name(self):
        token = self.token
        if token.kind == TK_IDENT:
            self.pos += 1
            return token.value
        if token.kind == TK_KEYWORD and token.value in ("KEY", "COUNT"):
            self.pos += 1
            return token.value
        raise ParseError("expected a name, found %r" % self._describe(), token.pos)

    def _describe(self):
        token = self.token
        if token.kind == TK_EOF:
            return "end of statement"
        return token.value

    # -- entry points -----------------------------------------------------
    def parse_script(self):
        statements = []
        while True:
            while self.accept_op(";"):
                pass
            if self.at_end():
                return statements
            statements.append(self.parse_statement())
            if not self.at_end() and not self.at_op(";"):
                raise ParseError("unexpected %r after statement" % self._describe(), self.token.pos)

    def parse_statement(self):
        if self.at_keyword("EXPLAIN"):
            self.next_token()
            return ast.Explain(self.parse_statement())
        if self.at_keyword("SELECT"):
            return self.parse_select()
        if self.at_keyword("INSERT"):
            return self.parse_insert()
        if self.at_keyword("UPDATE"):
            return self.parse_update()
        if self.at_keyword("DELETE"):
            return self.parse_delete()
        if self.at_keyword("CREATE"):
            return self.parse_create()
        if self.at_keyword("DROP"):
            return self.parse_drop()
        if self.at_keyword("BEGIN"):
            self.next_token()
            self.accept_keyword("TRANSACTION")
            return ast.Begin()
        if self.at_keyword("COMMIT"):
            self.next_token()
            self.accept_keyword("TRANSACTION")
            return ast.Commit()
        if self.at_keyword("ROLLBACK"):
            self.next_token()
            self.accept_keyword("TRANSACTION")
            return ast.Rollback()
        raise ParseError("unsupported statement starting at %r" % self._describe(), self.token.pos)

    # -- DDL --------------------------------------------------------------
    def parse_create(self):
        self.expect_keyword("CREATE")
        unique = self.accept_keyword("UNIQUE") is not None
        if self.at_keyword("INDEX"):
            self.next_token()
            if_not_exists = self._if_not_exists()
            name = self.expect_name()
            self.expect_keyword("ON")
            table = self.expect_name()
            self.expect_op("(")
            columns = [self.expect_name()]
            while self.accept_op(","):
                columns.append(self.expect_name())
            self.expect_op(")")
            return ast.CreateIndex(name, table, columns, unique, if_not_exists)
        if unique:
            raise ParseError("UNIQUE is only valid before INDEX here", self.token.pos)
        self.expect_keyword("TABLE")
        if_not_exists = self._if_not_exists()
        name = self.expect_name()
        self.expect_op("(")
        columns = []
        constraints = []
        while True:
            if self.at_keyword("PRIMARY", "UNIQUE"):
                constraints.append(self._parse_table_constraint())
            else:
                columns.append(self._parse_column_def())
            if self.accept_op(","):
                continue
            break
        self.expect_op(")")
        if not columns:
            raise ParseError("a table needs at least one column", self.token.pos)
        return ast.CreateTable(name, columns, constraints, if_not_exists)

    def _if_not_exists(self):
        if self.at_keyword("IF"):
            self.next_token()
            self.expect_keyword("NOT")
            self.expect_keyword("EXISTS")
            return True
        return False

    def _if_exists(self):
        if self.at_keyword("IF"):
            self.next_token()
            self.expect_keyword("EXISTS")
            return True
        return False

    def _parse_table_constraint(self):
        if self.accept_keyword("PRIMARY"):
            self.expect_keyword("KEY")
            kind = "PRIMARY KEY"
        else:
            self.expect_keyword("UNIQUE")
            kind = "UNIQUE"
        self.expect_op("(")
        columns = [self.expect_name()]
        while self.accept_op(","):
            columns.append(self.expect_name())
        self.expect_op(")")
        return ast.TableConstraint(kind, columns)

    def _parse_column_def(self):
        name = self.expect_name()
        type_name = ""
        if self.token.kind == TK_IDENT:
            type_name = self.next_token().value
            if self.accept_op("("):
                while not self.accept_op(")"):
                    self.next_token()
        not_null = False
        primary_key = False
        unique = False
        default = None
        while True:
            if self.at_keyword("NOT"):
                self.next_token()
                self.expect_keyword("NULL")
                not_null = True
            elif self.at_keyword("PRIMARY"):
                self.next_token()
                self.expect_keyword("KEY")
                primary_key = True
            elif self.at_keyword("UNIQUE"):
                self.next_token()
                unique = True
            elif self.at_keyword("DEFAULT"):
                self.next_token()
                default = self.parse_expr()
            elif self.at_keyword("NULL"):
                self.next_token()
            else:
                break
        return ast.ColumnDef(name, type_name, not_null, primary_key, unique, default)

    def parse_drop(self):
        self.expect_keyword("DROP")
        if self.accept_keyword("INDEX"):
            if_exists = self._if_exists()
            return ast.DropIndex(self.expect_name(), if_exists)
        self.expect_keyword("TABLE")
        if_exists = self._if_exists()
        return ast.DropTable(self.expect_name(), if_exists)

    # -- DML --------------------------------------------------------------
    def parse_insert(self):
        self.expect_keyword("INSERT")
        self.expect_keyword("INTO")
        table = self.expect_name()
        columns = None
        if self.at_op("(") and not self._peek_is_select_after_paren():
            self.next_token()
            columns = [self.expect_name()]
            while self.accept_op(","):
                columns.append(self.expect_name())
            self.expect_op(")")
        if self.at_keyword("SELECT"):
            return ast.Insert(table, columns, None, self.parse_select())
        self.expect_keyword("VALUES")
        rows = [self._parse_value_row()]
        while self.accept_op(","):
            rows.append(self._parse_value_row())
        return ast.Insert(table, columns, rows, None)

    def _peek_is_select_after_paren(self):
        token = self.tokens[self.pos + 1]
        return token.kind == TK_KEYWORD and token.value == "SELECT"

    def _parse_value_row(self):
        self.expect_op("(")
        row = [self.parse_expr()]
        while self.accept_op(","):
            row.append(self.parse_expr())
        self.expect_op(")")
        return row

    def parse_update(self):
        self.expect_keyword("UPDATE")
        table = self.expect_name()
        self.expect_keyword("SET")
        assignments = []
        while True:
            column = self.expect_name()
            self.expect_op("=")
            assignments.append((column, self.parse_expr()))
            if not self.accept_op(","):
                break
        where = None
        if self.accept_keyword("WHERE"):
            where = self.parse_expr()
        return ast.Update(table, assignments, where)

    def parse_delete(self):
        self.expect_keyword("DELETE")
        self.expect_keyword("FROM")
        table = self.expect_name()
        where = None
        if self.accept_keyword("WHERE"):
            where = self.parse_expr()
        return ast.Delete(table, where)

    # -- SELECT -----------------------------------------------------------
    def parse_select(self):
        self.expect_keyword("SELECT")
        distinct = False
        if self.accept_keyword("DISTINCT"):
            distinct = True
        else:
            self.accept_keyword("ALL")
        items = [self._parse_select_item()]
        while self.accept_op(","):
            items.append(self._parse_select_item())
        source = None
        joins = []
        if self.accept_keyword("FROM"):
            source = self._parse_table_ref()
            while True:
                join = self._parse_join()
                if join is None:
                    break
                joins.append(join)
        where = self.parse_expr() if self.accept_keyword("WHERE") else None
        group_by = None
        having = None
        if self.accept_keyword("GROUP"):
            self.expect_keyword("BY")
            group_by = [self.parse_expr()]
            while self.accept_op(","):
                group_by.append(self.parse_expr())
            if self.accept_keyword("HAVING"):
                having = self.parse_expr()
        order_by = None
        if self.accept_keyword("ORDER"):
            self.expect_keyword("BY")
            order_by = [self._parse_order_item()]
            while self.accept_op(","):
                order_by.append(self._parse_order_item())
        limit = None
        offset = None
        if self.accept_keyword("LIMIT"):
            limit = self.parse_expr()
            if self.accept_keyword("OFFSET"):
                offset = self.parse_expr()
            elif self.accept_op(","):
                offset, limit = limit, self.parse_expr()
        return ast.Select(items, source, joins, where, group_by, having,
                          order_by, limit, offset, distinct)

    def _parse_select_item(self):
        if self.at_op("*"):
            self.next_token()
            return ast.SelectItem(ast.Star(None), None)
        if self.token.kind == TK_IDENT and self.tokens[self.pos + 1].kind == TK_OP \
                and self.tokens[self.pos + 1].value == "." \
                and self.tokens[self.pos + 2].kind == TK_OP and self.tokens[self.pos + 2].value == "*":
            table = self.next_token().value
            self.next_token()
            self.next_token()
            return ast.SelectItem(ast.Star(table), None)
        expr = self.parse_expr()
        alias = None
        if self.accept_keyword("AS"):
            alias = self.expect_name()
        elif self.token.kind == TK_IDENT:
            alias = self.next_token().value
        return ast.SelectItem(expr, alias)

    def _parse_table_ref(self):
        name = self.expect_name()
        alias = None
        if self.accept_keyword("AS"):
            alias = self.expect_name()
        elif self.token.kind == TK_IDENT:
            alias = self.next_token().value
        return ast.TableRef(name, alias)

    def _parse_join(self):
        kind = None
        start = self.pos
        if self.at_keyword("CROSS"):
            self.next_token()
            self.expect_keyword("JOIN")
            kind = "CROSS"
        elif self.at_keyword("LEFT"):
            self.next_token()
            self.accept_keyword("OUTER")
            self.expect_keyword("JOIN")
            kind = "LEFT"
        elif self.at_keyword("INNER"):
            self.next_token()
            self.expect_keyword("JOIN")
            kind = "INNER"
        elif self.at_keyword("JOIN"):
            self.next_token()
            kind = "INNER"
        else:
            self.pos = start
            return None
        table = self._parse_table_ref()
        on = None
        if self.accept_keyword("ON"):
            on = self.parse_expr()
        return ast.Join(kind, table, on)

    def _parse_order_item(self):
        expr = self.parse_expr()
        descending = False
        if self.accept_keyword("DESC"):
            descending = True
        else:
            self.accept_keyword("ASC")
        return ast.OrderItem(expr, descending)

    # -- expressions ------------------------------------------------------
    def parse_expr(self):
        return self._parse_or()

    def _parse_or(self):
        left = self._parse_and()
        while self.accept_keyword("OR"):
            left = ast.Binary("OR", left, self._parse_and())
        return left

    def _parse_and(self):
        left = self._parse_not()
        while self.accept_keyword("AND"):
            left = ast.Binary("AND", left, self._parse_not())
        return left

    def _parse_not(self):
        if self.accept_keyword("NOT"):
            return ast.Unary("NOT", self._parse_not())
        return self._parse_comparison()

    def _parse_comparison(self):
        left = self._parse_concat()
        while True:
            if self.token.kind == TK_OP and self.token.value in COMPARISONS:
                op = self.next_token().value
                left = ast.Binary(op, left, self._parse_concat())
                continue
            if self.at_keyword("IS"):
                self.next_token()
                negated = self.accept_keyword("NOT") is not None
                if self.accept_keyword("NULL"):
                    left = ast.IsNull(left, negated)
                else:
                    right = self._parse_concat()
                    left = ast.Binary("IS NOT" if negated else "IS", left, right)
                continue
            negated = False
            if self.at_keyword("NOT") and self.tokens[self.pos + 1].kind == TK_KEYWORD \
                    and self.tokens[self.pos + 1].value in ("IN", "LIKE", "BETWEEN"):
                self.next_token()
                negated = True
            if self.at_keyword("IN"):
                self.next_token()
                self.expect_op("(")
                items = []
                if not self.at_op(")"):
                    items.append(self.parse_expr())
                    while self.accept_op(","):
                        items.append(self.parse_expr())
                self.expect_op(")")
                left = ast.InList(left, items, negated)
                continue
            if self.at_keyword("LIKE"):
                self.next_token()
                left = ast.Like(left, self._parse_concat(), negated)
                continue
            if self.at_keyword("BETWEEN"):
                self.next_token()
                low = self._parse_concat()
                self.expect_keyword("AND")
                high = self._parse_concat()
                left = ast.Between(left, low, high, negated)
                continue
            if negated:
                raise ParseError("NOT must be followed by IN, LIKE or BETWEEN here", self.token.pos)
            return left

    def _parse_concat(self):
        left = self._parse_additive()
        while self.at_op("||"):
            self.next_token()
            left = ast.Binary("||", left, self._parse_additive())
        return left

    def _parse_additive(self):
        left = self._parse_multiplicative()
        while self.token.kind == TK_OP and self.token.value in ("+", "-"):
            op = self.next_token().value
            left = ast.Binary(op, left, self._parse_multiplicative())
        return left

    def _parse_multiplicative(self):
        left = self._parse_unary()
        while self.token.kind == TK_OP and self.token.value in ("*", "/", "%"):
            op = self.next_token().value
            left = ast.Binary(op, left, self._parse_unary())
        return left

    def _parse_unary(self):
        if self.at_op("-"):
            self.next_token()
            return ast.Unary("-", self._parse_unary())
        if self.at_op("+"):
            self.next_token()
            return self._parse_unary()
        return self._parse_primary()

    def _parse_primary(self):
        token = self.token
        if token.kind == TK_NUMBER:
            self.next_token()
            return ast.Literal(token.value)
        if token.kind == TK_STRING:
            self.next_token()
            return ast.Literal(token.value)
        if token.kind == TK_BLOB:
            self.next_token()
            return ast.Literal(token.value)
        if token.kind == TK_PARAM:
            self.next_token()
            return ast.Param(token.value)
        if self.at_keyword("NULL"):
            self.next_token()
            return ast.Literal(None)
        if self.at_keyword("CASE"):
            return self._parse_case()
        if self.at_op("("):
            self.next_token()
            expr = self.parse_expr()
            self.expect_op(")")
            return expr
        if token.kind in (TK_IDENT, TK_KEYWORD) and self._is_callable(token):
            name = self.next_token().value.upper()
            self.expect_op("(")
            distinct = False
            star = False
            args = []
            if self.at_op("*"):
                self.next_token()
                star = True
            elif not self.at_op(")"):
                distinct = self.accept_keyword("DISTINCT") is not None
                args.append(self.parse_expr())
                while self.accept_op(","):
                    args.append(self.parse_expr())
            self.expect_op(")")
            return ast.FuncCall(name, args, distinct, star)
        if token.kind == TK_IDENT:
            name = self.next_token().value
            if self.at_op("."):
                self.next_token()
                return ast.ColumnRef(name, self.expect_name())
            return ast.ColumnRef(None, name)
        raise ParseError("unexpected %r in expression" % self._describe(), token.pos)

    def _is_callable(self, token):
        following = self.tokens[self.pos + 1]
        if not (following.kind == TK_OP and following.value == "("):
            return False
        if token.kind == TK_KEYWORD:
            return token.value in AGGREGATES
        return True

    def _parse_case(self):
        self.expect_keyword("CASE")
        operand = None
        if not self.at_keyword("WHEN"):
            operand = self.parse_expr()
        whens = []
        while self.accept_keyword("WHEN"):
            condition = self.parse_expr()
            self.expect_keyword("THEN")
            whens.append((condition, self.parse_expr()))
        orelse = None
        if self.accept_keyword("ELSE"):
            orelse = self.parse_expr()
        self.expect_keyword("END")
        if not whens:
            raise ParseError("CASE needs at least one WHEN branch", self.token.pos)
        return ast.Case(operand, whens, orelse)


def parse(text):
    """Parse ``text`` into a list of statements."""
    return Parser(text).parse_script()


def parse_one(text):
    statements = parse(text)
    if len(statements) != 1:
        raise ParseError("expected exactly one statement, found %d" % len(statements))
    return statements[0]
