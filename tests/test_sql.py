"""End-to-end SQL tests: parsing, DDL, DML, queries, transactions."""

import os
import random
import shutil
import tempfile
import unittest

from trde import connect
from trde.errors import (IntegrityError, ParseError, TRDEError, SchemaError,
                           TransactionError, TypeMismatchError)
from trde.parser import parse, parse_one
from trde import sqlast as ast


class SQLCase(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")

    def tearDown(self):
        self.db.close()

    def rows(self, sql, params=()):
        return self.db.execute(sql, params).rows

    def scalar(self, sql, params=()):
        return self.db.execute(sql, params).scalar()

    def sample(self):
        self.db.execute("""
            CREATE TABLE emp (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                dept TEXT,
                salary REAL,
                manager INTEGER
            )""")
        self.db.execute("""
            INSERT INTO emp (id, name, dept, salary, manager) VALUES
              (1, 'ada',    'eng',   120.0, NULL),
              (2, 'grace',  'eng',   110.0, 1),
              (3, 'linus',  'eng',    90.0, 1),
              (4, 'barbara','sales',  80.0, 1),
              (5, 'katherine','sales',85.5, 4),
              (6, 'margaret', NULL,   70.0, NULL)
            """)


class ParserTest(unittest.TestCase):
    def test_select_shapes(self):
        statement = parse_one("SELECT a, b AS beta, t.c FROM tbl t WHERE a > 1 "
                              "GROUP BY a HAVING COUNT(*) > 2 ORDER BY b DESC LIMIT 10 OFFSET 5")
        self.assertIsInstance(statement, ast.Select)
        self.assertEqual(len(statement.items), 3)
        self.assertEqual(statement.items[1].alias, "beta")
        self.assertEqual(statement.source.alias, "t")
        self.assertTrue(statement.order_by[0].descending)

    def test_precedence(self):
        statement = parse_one("SELECT 1 WHERE 1 + 2 * 3 = 7 AND NOT 0 OR 1")
        where = statement.where
        self.assertEqual(where.op, "OR")
        self.assertEqual(where.left.op, "AND")

    def test_string_escapes_and_comments(self):
        statement = parse_one("SELECT 'it''s' -- trailing comment\n")
        self.assertEqual(statement.items[0].expr.value, "it's")
        statement = parse_one("/* leading */ SELECT 1 /* inner */ + 1")
        self.assertEqual(statement.items[0].expr.op, "+")

    def test_blob_and_number_literals(self):
        statement = parse_one("SELECT x'48656c6c6f', 1.5e2, 42, .5")
        self.assertEqual(statement.items[0].expr.value, b"Hello")
        self.assertEqual(statement.items[1].expr.value, 150.0)
        self.assertEqual(statement.items[2].expr.value, 42)
        self.assertEqual(statement.items[3].expr.value, 0.5)

    def test_multiple_statements(self):
        statements = parse("CREATE TABLE a (x INTEGER); INSERT INTO a VALUES (1); SELECT * FROM a;")
        self.assertEqual(len(statements), 3)

    def test_quoted_identifiers(self):
        statement = parse_one('SELECT "select", [from], `where` FROM "table"')
        self.assertEqual(statement.items[0].expr.name, "select")
        self.assertEqual(statement.source.name, "table")

    def test_join_syntax(self):
        statement = parse_one("SELECT * FROM a LEFT OUTER JOIN b ON a.id = b.id "
                              "INNER JOIN c ON c.id = a.id CROSS JOIN d")
        self.assertEqual([j.kind for j in statement.joins], ["LEFT", "INNER", "CROSS"])

    def test_case_expression(self):
        statement = parse_one("SELECT CASE WHEN a > 1 THEN 'big' ELSE 'small' END FROM t")
        self.assertIsInstance(statement.items[0].expr, ast.Case)

    def test_errors(self):
        for bad in ["SELECT", "SELECT * FROM", "INSERT INTO t", "CREATE TABLE t ()",
                    "SELECT 'unterminated", "SELECT 1 +", "DELETE t", "SELECT * FROM t WHERE"]:
            with self.assertRaises(ParseError):
                parse(bad)

    def test_parameters_are_numbered(self):
        statement = parse_one("SELECT ?, ?, ?")
        self.assertEqual([item.expr.index for item in statement.items], [0, 1, 2])


class DDLTest(SQLCase):
    def test_create_and_list_tables(self):
        self.db.execute("CREATE TABLE a (x INTEGER)")
        self.db.execute("CREATE TABLE b (y TEXT)")
        self.assertEqual(self.db.table_names, ["a", "b"])

    def test_duplicate_table(self):
        self.db.execute("CREATE TABLE a (x INTEGER)")
        with self.assertRaises(SchemaError):
            self.db.execute("CREATE TABLE a (x INTEGER)")
        self.db.execute("CREATE TABLE IF NOT EXISTS a (x INTEGER)")

    def test_duplicate_column(self):
        with self.assertRaises(SchemaError):
            self.db.execute("CREATE TABLE a (x INTEGER, X TEXT)")

    def test_rowid_is_reserved(self):
        with self.assertRaises(SchemaError):
            self.db.execute("CREATE TABLE a (rowid INTEGER)")

    def test_drop_table(self):
        self.db.execute("CREATE TABLE a (x INTEGER)")
        self.db.execute("INSERT INTO a VALUES (1)")
        self.db.execute("DROP TABLE a")
        self.assertEqual(self.db.table_names, [])
        with self.assertRaises(SchemaError):
            self.db.execute("SELECT * FROM a")
        with self.assertRaises(SchemaError):
            self.db.execute("DROP TABLE a")
        self.db.execute("DROP TABLE IF EXISTS a")

    def test_primary_key_creates_index(self):
        self.db.execute("CREATE TABLE a (x INTEGER PRIMARY KEY, y TEXT)")
        schema = self.db.schema()[0]
        self.assertEqual(len(schema["indexes"]), 1)
        self.assertTrue(schema["indexes"][0]["unique"])

    def test_composite_primary_key(self):
        self.db.execute("CREATE TABLE a (x INTEGER, y INTEGER, PRIMARY KEY (x, y))")
        self.db.execute("INSERT INTO a VALUES (1, 1), (1, 2), (2, 1)")
        with self.assertRaises(IntegrityError):
            self.db.execute("INSERT INTO a VALUES (1, 1)")

    def test_table_level_unique(self):
        self.db.execute("CREATE TABLE a (x INTEGER, y INTEGER, UNIQUE (x, y))")
        self.db.execute("INSERT INTO a VALUES (1, 2)")
        with self.assertRaises(IntegrityError):
            self.db.execute("INSERT INTO a VALUES (1, 2)")

    def test_create_and_drop_index(self):
        self.db.execute("CREATE TABLE a (x INTEGER, y TEXT)")
        self.db.execute("CREATE INDEX ix ON a (y)")
        self.assertEqual(self.db.schema()[0]["indexes"][0]["name"], "ix")
        self.db.execute("DROP INDEX ix")
        self.assertEqual(self.db.schema()[0]["indexes"], [])
        with self.assertRaises(SchemaError):
            self.db.execute("DROP INDEX ix")
        self.db.execute("DROP INDEX IF EXISTS ix")

    def test_constraint_index_cannot_be_dropped(self):
        self.db.execute("CREATE TABLE a (x INTEGER PRIMARY KEY)")
        name = self.db.schema()[0]["indexes"][0]["name"]
        with self.assertRaises(SchemaError):
            self.db.execute("DROP INDEX %s" % name)

    def test_index_on_missing_column(self):
        self.db.execute("CREATE TABLE a (x INTEGER)")
        with self.assertRaises(SchemaError):
            self.db.execute("CREATE INDEX ix ON a (nope)")

    def test_index_built_over_existing_rows(self):
        self.db.execute("CREATE TABLE a (x INTEGER, y TEXT)")
        for i in range(50):
            self.db.execute("INSERT INTO a VALUES (?, ?)", (i, "n%d" % i))
        self.db.execute("CREATE INDEX ix ON a (x)")
        self.assertEqual(self.rows("SELECT y FROM a WHERE x = 33"), [("n33",)])


class InsertTest(SQLCase):
    def test_defaults_and_column_lists(self):
        self.db.execute("CREATE TABLE a (x INTEGER, y TEXT DEFAULT 'dflt', z INTEGER DEFAULT 7)")
        self.db.execute("INSERT INTO a (x) VALUES (1)")
        self.assertEqual(self.rows("SELECT * FROM a"), [(1, "dflt", 7)])

    def test_multi_row_insert(self):
        self.db.execute("CREATE TABLE a (x INTEGER)")
        result = self.db.execute("INSERT INTO a VALUES (1),(2),(3)")
        self.assertEqual(result.rowcount, 3)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM a"), 3)

    def test_insert_select(self):
        self.db.execute("CREATE TABLE a (x INTEGER, y TEXT)")
        self.db.execute("CREATE TABLE b (x INTEGER, y TEXT)")
        self.db.execute("INSERT INTO a VALUES (1,'one'),(2,'two')")
        self.db.execute("INSERT INTO b SELECT x, y FROM a WHERE x > 1")
        self.assertEqual(self.rows("SELECT * FROM b"), [(2, "two")])

    def test_not_null_enforced(self):
        self.db.execute("CREATE TABLE a (x INTEGER NOT NULL)")
        with self.assertRaises(IntegrityError):
            self.db.execute("INSERT INTO a VALUES (NULL)")

    def test_type_coercion_and_failure(self):
        self.db.execute("CREATE TABLE a (i INTEGER, r REAL, t TEXT, b BLOB)")
        self.db.execute("INSERT INTO a VALUES ('42', 7, 19, 'bytes')")
        self.assertEqual(self.rows("SELECT * FROM a"), [(42, 7.0, "19", b"bytes")])
        with self.assertRaises(TypeMismatchError):
            self.db.execute("INSERT INTO a (i) VALUES ('not a number')")

    def test_wrong_arity(self):
        self.db.execute("CREATE TABLE a (x INTEGER, y INTEGER)")
        with self.assertRaises(TRDEError):
            self.db.execute("INSERT INTO a VALUES (1)")
        with self.assertRaises(SchemaError):
            self.db.execute("INSERT INTO a (nope) VALUES (1)")

    def test_failed_insert_leaves_no_trace(self):
        self.db.execute("CREATE TABLE a (x INTEGER PRIMARY KEY, y TEXT NOT NULL)")
        self.db.execute("INSERT INTO a VALUES (1, 'ok')")
        with self.assertRaises(IntegrityError):
            self.db.execute("INSERT INTO a VALUES (2, 'fine'), (1, 'dup')")
        self.assertEqual(self.rows("SELECT * FROM a"), [(1, "ok")])

    def test_parameters(self):
        self.db.execute("CREATE TABLE a (x INTEGER, y TEXT)")
        self.db.execute("INSERT INTO a VALUES (?, ?)", (5, "five"))
        self.assertEqual(self.rows("SELECT * FROM a WHERE x = ?", (5,)), [(5, "five")])

    def test_executemany(self):
        self.db.execute("CREATE TABLE a (x INTEGER)")
        self.db.executemany("INSERT INTO a VALUES (?)", [(i,) for i in range(20)])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM a"), 20)


class SelectTest(SQLCase):
    def test_select_without_table(self):
        self.assertEqual(self.rows("SELECT 1 + 1, 'x' || 'y', NULL"), [(2, "xy", None)])

    def test_projection_and_aliases(self):
        self.sample()
        result = self.db.execute("SELECT name AS who, salary * 2 AS doubled FROM emp WHERE id = 1")
        self.assertEqual(result.columns, ["who", "doubled"])
        self.assertEqual(result.rows, [("ada", 240.0)])

    def test_star_and_qualified_star(self):
        self.sample()
        self.assertEqual(self.db.execute("SELECT * FROM emp").columns,
                         ["id", "name", "dept", "salary", "manager"])
        self.assertEqual(self.db.execute("SELECT e.* FROM emp e").columns,
                         ["id", "name", "dept", "salary", "manager"])

    def test_where_operators(self):
        self.sample()
        self.assertEqual(len(self.rows("SELECT * FROM emp WHERE salary >= 90")), 3)
        self.assertEqual(len(self.rows("SELECT * FROM emp WHERE dept IS NULL")), 1)
        self.assertEqual(len(self.rows("SELECT * FROM emp WHERE dept IS NOT NULL")), 5)
        self.assertEqual(len(self.rows("SELECT * FROM emp WHERE name IN ('ada','linus')")), 2)
        self.assertEqual(len(self.rows("SELECT * FROM emp WHERE name NOT IN ('ada','linus')")), 4)
        self.assertEqual(len(self.rows("SELECT * FROM emp WHERE salary BETWEEN 80 AND 110")), 4)
        self.assertEqual(len(self.rows("SELECT * FROM emp WHERE name LIKE '%a%'")), 5)
        self.assertEqual(len(self.rows("SELECT * FROM emp WHERE name LIKE 'A%'")), 1)
        self.assertEqual(len(self.rows("SELECT * FROM emp WHERE name NOT LIKE '%a%'")), 1)

    def test_null_logic(self):
        self.sample()
        self.assertEqual(self.rows("SELECT * FROM emp WHERE dept = NULL"), [])
        self.assertEqual(self.scalar("SELECT 1 WHERE NULL OR 1"), 1)
        self.assertEqual(self.rows("SELECT 1 WHERE NULL AND 1"), [])
        self.assertIsNone(self.scalar("SELECT NULL + 1"))
        self.assertIsNone(self.scalar("SELECT NULL || 'x'"))

    def test_order_by(self):
        self.sample()
        names = [r[0] for r in self.rows("SELECT name FROM emp ORDER BY salary")]
        self.assertEqual(names[0], "margaret")
        self.assertEqual(names[-1], "ada")
        names = [r[0] for r in self.rows("SELECT name FROM emp ORDER BY salary DESC")]
        self.assertEqual(names[0], "ada")
        rows = self.rows("SELECT dept, name FROM emp ORDER BY dept, name DESC")
        self.assertEqual(rows[0], (None, "margaret"))

    def test_order_by_alias_and_ordinal(self):
        self.sample()
        rows = self.rows("SELECT name, salary * 2 AS pay FROM emp ORDER BY pay DESC LIMIT 1")
        self.assertEqual(rows, [("ada", 240.0)])
        rows = self.rows("SELECT name, salary FROM emp ORDER BY 2 LIMIT 1")
        self.assertEqual(rows, [("margaret", 70.0)])

    def test_limit_offset(self):
        self.sample()
        self.assertEqual(len(self.rows("SELECT * FROM emp LIMIT 2")), 2)
        self.assertEqual(len(self.rows("SELECT * FROM emp LIMIT 2 OFFSET 5")), 1)
        self.assertEqual(len(self.rows("SELECT * FROM emp LIMIT 3, 2")), 2)

    def test_distinct(self):
        self.sample()
        rows = self.rows("SELECT DISTINCT dept FROM emp ORDER BY dept")
        self.assertEqual(rows, [(None,), ("eng",), ("sales",)])

    def test_aggregates(self):
        self.sample()
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM emp"), 6)
        self.assertEqual(self.scalar("SELECT COUNT(dept) FROM emp"), 5)
        self.assertEqual(self.scalar("SELECT COUNT(DISTINCT dept) FROM emp"), 2)
        self.assertAlmostEqual(self.scalar("SELECT SUM(salary) FROM emp"), 555.5)
        self.assertAlmostEqual(self.scalar("SELECT AVG(salary) FROM emp"), 555.5 / 6)
        self.assertEqual(self.scalar("SELECT MIN(name) FROM emp"), "ada")
        self.assertEqual(self.scalar("SELECT MAX(salary) FROM emp"), 120.0)
        self.assertEqual(self.scalar("SELECT GROUP_CONCAT(dept) FROM emp WHERE dept='eng'"),
                         "eng,eng,eng")

    def test_aggregate_on_empty_table(self):
        self.db.execute("CREATE TABLE a (x INTEGER)")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM a"), 0)
        self.assertIsNone(self.scalar("SELECT SUM(x) FROM a"))
        self.assertIsNone(self.scalar("SELECT AVG(x) FROM a"))
        self.assertIsNone(self.scalar("SELECT MAX(x) FROM a"))

    def test_group_by_and_having(self):
        self.sample()
        rows = self.rows("SELECT dept, COUNT(*) AS n FROM emp GROUP BY dept ORDER BY dept")
        self.assertEqual(rows, [(None, 1), ("eng", 3), ("sales", 2)])
        rows = self.rows("SELECT dept, COUNT(*) FROM emp GROUP BY dept HAVING COUNT(*) > 1 "
                         "ORDER BY dept")
        self.assertEqual(rows, [("eng", 3), ("sales", 2)])

    def test_group_by_expression(self):
        self.sample()
        rows = self.rows("SELECT salary > 90, COUNT(*) FROM emp GROUP BY salary > 90 "
                         "ORDER BY 1")
        self.assertEqual(sorted(rows), [(False, 4), (True, 2)])

    def test_scalar_functions(self):
        self.assertEqual(self.scalar("SELECT UPPER('abc')"), "ABC")
        self.assertEqual(self.scalar("SELECT LOWER('ABC')"), "abc")
        self.assertEqual(self.scalar("SELECT LENGTH('hello')"), 5)
        self.assertEqual(self.scalar("SELECT ABS(-4)"), 4)
        self.assertEqual(self.scalar("SELECT SUBSTR('abcdef', 2, 3)"), "bcd")
        self.assertEqual(self.scalar("SELECT SUBSTR('abcdef', -2)"), "ef")
        self.assertEqual(self.scalar("SELECT COALESCE(NULL, NULL, 3)"), 3)
        self.assertEqual(self.scalar("SELECT IFNULL(NULL, 'x')"), "x")
        self.assertIsNone(self.scalar("SELECT NULLIF(2, 2)"))
        self.assertEqual(self.scalar("SELECT ROUND(2.567, 2)"), 2.57)
        self.assertEqual(self.scalar("SELECT ROUND(2.5)"), 3.0)
        self.assertEqual(self.scalar("SELECT TRIM('  pad  ')"), "pad")
        self.assertEqual(self.scalar("SELECT REPLACE('a-b-c', '-', '+')"), "a+b+c")
        self.assertEqual(self.scalar("SELECT INSTR('hello', 'll')"), 3)
        self.assertEqual(self.scalar("SELECT TYPEOF(1.5)"), "real")
        self.assertEqual(self.scalar("SELECT HEX('AB')"), "4142")
        self.assertEqual(self.scalar("SELECT MIN(3, 1, 2)"), 1)
        self.assertEqual(self.scalar("SELECT MAX(3, 1, 2)"), 3)

    def test_function_errors(self):
        with self.assertRaises(TRDEError):
            self.db.execute("SELECT NOSUCHFUNC(1)")
        with self.assertRaises(TRDEError):
            self.db.execute("SELECT ABS(1, 2)")

    def test_case_expression(self):
        self.sample()
        rows = self.rows("SELECT name, CASE WHEN salary > 100 THEN 'high' "
                         "WHEN salary > 80 THEN 'mid' ELSE 'low' END FROM emp ORDER BY id")
        self.assertEqual(rows[0], ("ada", "high"))
        self.assertEqual(rows[2], ("linus", "mid"))
        self.assertEqual(rows[5], ("margaret", "low"))
        self.assertEqual(self.scalar("SELECT CASE 2 WHEN 1 THEN 'a' WHEN 2 THEN 'b' END"), "b")

    def test_integer_division_and_modulo(self):
        self.assertEqual(self.scalar("SELECT 7 / 2"), 3)
        self.assertEqual(self.scalar("SELECT -7 / 2"), -3)
        self.assertEqual(self.scalar("SELECT 7.0 / 2"), 3.5)
        self.assertEqual(self.scalar("SELECT 7 % 3"), 1)
        self.assertIsNone(self.scalar("SELECT 1 / 0"))

    def test_rowid_pseudocolumn(self):
        self.db.execute("CREATE TABLE a (x INTEGER)")
        self.db.execute("INSERT INTO a VALUES (10),(20)")
        rows = self.rows("SELECT rowid, x FROM a ORDER BY rowid")
        self.assertEqual([r[1] for r in rows], [10, 20])
        target = rows[1][0]
        self.assertEqual(self.rows("SELECT x FROM a WHERE rowid = ?", (target,)), [(20,)])

    def test_unknown_column(self):
        self.sample()
        with self.assertRaises(TRDEError):
            self.db.execute("SELECT nope FROM emp")

    def test_ambiguous_column(self):
        self.db.execute("CREATE TABLE a (x INTEGER)")
        self.db.execute("CREATE TABLE b (x INTEGER)")
        self.db.execute("INSERT INTO a VALUES (1)")
        self.db.execute("INSERT INTO b VALUES (1)")
        with self.assertRaises(TRDEError):
            self.db.execute("SELECT x FROM a JOIN b ON a.x = b.x")
        self.assertEqual(self.rows("SELECT a.x FROM a JOIN b ON a.x = b.x"), [(1,)])


class JoinTest(SQLCase):
    def setUp(self):
        SQLCase.setUp(self)
        self.db.execute("CREATE TABLE u (id INTEGER PRIMARY KEY, name TEXT)")
        self.db.execute("CREATE TABLE o (id INTEGER PRIMARY KEY, uid INTEGER, amount REAL)")
        self.db.execute("INSERT INTO u VALUES (1,'ada'),(2,'grace'),(3,'linus')")
        self.db.execute("INSERT INTO o VALUES (1,1,10.0),(2,1,5.0),(3,2,7.5)")

    def test_inner_join(self):
        rows = self.rows("SELECT u.name, o.amount FROM u JOIN o ON o.uid = u.id "
                         "ORDER BY u.name, o.amount")
        self.assertEqual(rows, [("ada", 5.0), ("ada", 10.0), ("grace", 7.5)])

    def test_left_join_keeps_unmatched(self):
        rows = self.rows("SELECT u.name, o.amount FROM u LEFT JOIN o ON o.uid = u.id "
                         "ORDER BY u.name, o.amount")
        self.assertEqual(rows[-1], ("linus", None))
        self.assertEqual(len(rows), 4)

    def test_left_join_with_aggregate(self):
        rows = self.rows("SELECT u.name, COUNT(o.id), SUM(o.amount) FROM u "
                         "LEFT JOIN o ON o.uid = u.id GROUP BY u.name ORDER BY u.name")
        self.assertEqual(rows, [("ada", 2, 15.0), ("grace", 1, 7.5), ("linus", 0, None)])

    def test_cross_join(self):
        rows = self.rows("SELECT COUNT(*) FROM u CROSS JOIN o")
        self.assertEqual(rows, [(9,)])

    def test_three_way_join(self):
        self.db.execute("CREATE TABLE tag (oid INTEGER, label TEXT)")
        self.db.execute("INSERT INTO tag VALUES (1,'rush'),(3,'gift')")
        rows = self.rows("SELECT u.name, t.label FROM u JOIN o ON o.uid = u.id "
                         "JOIN tag t ON t.oid = o.id ORDER BY u.name")
        self.assertEqual(rows, [("ada", "rush"), ("grace", "gift")])

    def test_self_join(self):
        self.db.execute("CREATE TABLE emp (id INTEGER, name TEXT, boss INTEGER)")
        self.db.execute("INSERT INTO emp VALUES (1,'ada',NULL),(2,'grace',1),(3,'linus',1)")
        rows = self.rows("SELECT e.name, b.name FROM emp e LEFT JOIN emp b ON e.boss = b.id "
                         "ORDER BY e.name")
        self.assertEqual(rows, [("ada", None), ("grace", "ada"), ("linus", "ada")])

    def test_where_filters_after_left_join(self):
        rows = self.rows("SELECT u.name FROM u LEFT JOIN o ON o.uid = u.id "
                         "WHERE o.amount IS NULL")
        self.assertEqual(rows, [("linus",)])

    def test_join_uses_index_plan(self):
        plan = self.db.execute("EXPLAIN SELECT u.name FROM u JOIN o ON o.id = u.id").rows
        self.assertTrue(any("USING INDEX" in line[0] or "INTEGER PRIMARY KEY" in line[0]
                            for line in plan))


class UpdateDeleteTest(SQLCase):
    def setUp(self):
        SQLCase.setUp(self)
        self.db.execute("CREATE TABLE a (id INTEGER PRIMARY KEY, name TEXT, n INTEGER)")
        self.db.execute("INSERT INTO a VALUES (1,'x',10),(2,'y',20),(3,'z',30)")

    def test_update_all(self):
        result = self.db.execute("UPDATE a SET n = n + 1")
        self.assertEqual(result.rowcount, 3)
        self.assertEqual([r[0] for r in self.rows("SELECT n FROM a ORDER BY id")], [11, 21, 31])

    def test_update_where(self):
        self.db.execute("UPDATE a SET name = 'updated' WHERE id = 2")
        self.assertEqual(self.rows("SELECT name FROM a WHERE id = 2"), [("updated",)])

    def test_update_maintains_index(self):
        self.db.execute("CREATE INDEX ix ON a (name)")
        self.db.execute("UPDATE a SET name = 'w' WHERE name = 'y'")
        self.assertEqual(self.rows("SELECT id FROM a WHERE name = 'w'"), [(2,)])
        self.assertEqual(self.rows("SELECT id FROM a WHERE name = 'y'"), [])

    def test_update_unique_violation_rolls_back_row(self):
        with self.assertRaises(IntegrityError):
            self.db.execute("UPDATE a SET id = 1 WHERE id = 2")
        self.assertEqual(sorted(r[0] for r in self.rows("SELECT id FROM a")), [1, 2, 3])
        self.assertEqual(self.rows("SELECT name FROM a WHERE id = 2"), [("y",)])

    def test_update_type_error(self):
        with self.assertRaises(TypeMismatchError):
            self.db.execute("UPDATE a SET n = 'abc'")

    def test_delete_where_and_all(self):
        self.assertEqual(self.db.execute("DELETE FROM a WHERE n > 15").rowcount, 2)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM a"), 1)
        self.db.execute("DELETE FROM a")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM a"), 0)

    def test_delete_maintains_index(self):
        self.db.execute("CREATE INDEX ix ON a (n)")
        self.db.execute("DELETE FROM a WHERE n = 20")
        self.assertEqual(self.rows("SELECT id FROM a WHERE n = 20"), [])
        self.assertEqual(len(self.rows("SELECT id FROM a WHERE n > 5")), 2)

    def test_reinsert_after_delete(self):
        self.db.execute("DELETE FROM a WHERE id = 2")
        self.db.execute("INSERT INTO a VALUES (2, 'again', 22)")
        self.assertEqual(self.rows("SELECT name FROM a WHERE id = 2"), [("again",)])


class EdgeCaseTest(SQLCase):
    def test_update_does_not_revisit_rows_it_moves(self):
        """The Halloween problem: an UPDATE must not chase its own writes."""
        self.db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, k INTEGER)")
        self.db.execute("CREATE INDEX ix ON t (k)")
        self.db.execute("INSERT INTO t VALUES (1,1),(2,2),(3,3),(4,9)")
        self.db.execute("UPDATE t SET k = k + 1 WHERE k < 5")
        self.assertEqual(self.rows("SELECT id, k FROM t ORDER BY id"),
                         [(1, 2), (2, 3), (3, 4), (4, 9)])

    def test_delete_through_an_index_removes_everything(self):
        self.db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, k INTEGER)")
        self.db.execute("CREATE INDEX ix ON t (k)")
        self.db.execute("INSERT INTO t VALUES (1,2),(2,3),(3,4)")
        self.db.execute("DELETE FROM t WHERE k > 1")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM t"), 0)

    def test_unique_index_allows_repeated_nulls(self):
        self.db.execute("CREATE TABLE u (a INTEGER UNIQUE)")
        self.db.execute("INSERT INTO u VALUES (NULL),(NULL),(1)")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM u"), 3)
        with self.assertRaises(IntegrityError):
            self.db.execute("INSERT INTO u VALUES (1)")

    def test_integer_and_real_compare_equal_through_an_index(self):
        self.db.execute("CREATE TABLE n (v REAL)")
        self.db.execute("CREATE INDEX ixn ON n (v)")
        self.db.execute("INSERT INTO n VALUES (5), (5.0), (5.5)")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM n WHERE v = 5"), 2)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM n WHERE v > 5"), 1)

    def test_oversized_index_key_is_rejected_cleanly(self):
        self.db.execute("CREATE TABLE big (s TEXT)")
        self.db.execute("CREATE INDEX ixb ON big (s)")
        with self.assertRaises(TRDEError):
            self.db.execute("INSERT INTO big VALUES (?)", ("x" * 2000,))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM big"), 0)
        self.db.execute("INSERT INTO big VALUES ('short')")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM big"), 1)

    def test_untyped_column_orders_by_storage_class(self):
        self.db.execute("CREATE TABLE m (v)")
        self.db.execute("INSERT INTO m VALUES (2),('b'),(NULL),(1.5),('a')")
        self.assertEqual([r[0] for r in self.rows("SELECT v FROM m ORDER BY v")],
                         [None, 1.5, 2, "a", "b"])

    def test_empty_string_and_zero_are_not_null(self):
        self.db.execute("CREATE TABLE z (t TEXT, n INTEGER)")
        self.db.execute("INSERT INTO z VALUES ('', 0)")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM z WHERE t IS NULL"), 0)
        self.assertEqual(self.rows("SELECT t, n FROM z"), [("", 0)])

    def test_statement_with_trailing_semicolons_and_whitespace(self):
        self.db.execute("  CREATE TABLE t (a INTEGER);;  ")
        self.db.execute("INSERT INTO t VALUES (1);")
        self.assertEqual(self.scalar("SELECT a FROM t;"), 1)


class TransactionTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="trde-txn-")
        self.path = os.path.join(self.dir, "t.trde")
        self.db = connect(self.path)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def reopen(self):
        self.db.close()
        self.db = connect(self.path)
        return self.db

    def test_commit_persists(self):
        self.db.execute("CREATE TABLE a (x INTEGER)")
        self.db.execute("BEGIN")
        self.db.execute("INSERT INTO a VALUES (1)")
        self.db.execute("COMMIT")
        self.reopen()
        self.assertEqual(self.db.execute("SELECT * FROM a").rows, [(1,)])

    def test_rollback_undoes_dml(self):
        self.db.execute("CREATE TABLE a (x INTEGER)")
        self.db.execute("INSERT INTO a VALUES (1)")
        self.db.execute("BEGIN")
        self.db.execute("INSERT INTO a VALUES (2),(3)")
        self.db.execute("DELETE FROM a WHERE x = 1")
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM a"), 2)
        self.db.execute("ROLLBACK")
        self.assertEqual(self.db.execute("SELECT * FROM a").rows, [(1,)])

    def test_rollback_undoes_ddl(self):
        self.db.execute("CREATE TABLE keep (x INTEGER)")
        self.db.execute("BEGIN")
        self.db.execute("CREATE TABLE gone (x INTEGER)")
        self.assertIn("gone", self.db.table_names)
        self.db.execute("ROLLBACK")
        self.assertEqual(self.db.table_names, ["keep"])

    def test_rollback_undoes_drop(self):
        self.db.execute("CREATE TABLE a (x INTEGER)")
        self.db.execute("INSERT INTO a VALUES (7)")
        self.db.execute("BEGIN")
        self.db.execute("DROP TABLE a")
        self.db.execute("ROLLBACK")
        self.assertEqual(self.db.execute("SELECT * FROM a").rows, [(7,)])

    def test_autocommit_rolls_back_failed_statement(self):
        self.db.execute("CREATE TABLE a (x INTEGER PRIMARY KEY)")
        self.db.execute("INSERT INTO a VALUES (1)")
        with self.assertRaises(IntegrityError):
            self.db.execute("INSERT INTO a VALUES (2),(3),(1)")
        self.assertEqual(self.db.execute("SELECT x FROM a").rows, [(1,)])
        self.reopen()
        self.assertEqual(self.db.execute("SELECT x FROM a").rows, [(1,)])

    def test_transaction_context_manager(self):
        self.db.execute("CREATE TABLE a (x INTEGER)")
        with self.db.transaction():
            self.db.execute("INSERT INTO a VALUES (1)")
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM a"), 1)
        try:
            with self.db.transaction():
                self.db.execute("INSERT INTO a VALUES (2)")
                raise ValueError("boom")
        except ValueError:
            pass
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM a"), 1)

    def test_double_begin_and_stray_commit(self):
        self.db.execute("BEGIN")
        with self.assertRaises(TransactionError):
            self.db.execute("BEGIN")
        self.db.execute("COMMIT")
        with self.assertRaises(TransactionError):
            self.db.execute("COMMIT")
        with self.assertRaises(TransactionError):
            self.db.execute("ROLLBACK")

    def test_large_transaction_persists(self):
        self.db.execute("CREATE TABLE a (x INTEGER, y TEXT)")
        self.db.execute("BEGIN")
        for i in range(2000):
            self.db.execute("INSERT INTO a VALUES (?, ?)", (i, "value-%d" % i))
        self.db.execute("COMMIT")
        self.reopen()
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM a"), 2000)
        self.assertEqual(self.db.execute("SELECT y FROM a WHERE x = 1999").rows, [("value-1999",)])


class PersistenceTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="trde-persist-")
        self.path = os.path.join(self.dir, "p.trde")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_schema_and_data_survive_reopen(self):
        db = connect(self.path)
        db.execute("CREATE TABLE a (id INTEGER PRIMARY KEY, name TEXT UNIQUE, val REAL)")
        db.execute("CREATE INDEX ix ON a (val)")
        for i in range(300):
            db.execute("INSERT INTO a VALUES (?, ?, ?)", (i, "n%d" % i, i * 1.5))
        db.close()

        db = connect(self.path)
        self.assertEqual(db.table_names, ["a"])
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM a"), 300)
        self.assertEqual(db.execute("SELECT name FROM a WHERE id = 42").rows, [("n42",)])
        self.assertEqual(db.execute("SELECT id FROM a WHERE val = 63.0").rows, [(42,)])
        indexes = {i["name"] for i in db.schema()[0]["indexes"]}
        self.assertIn("ix", indexes)
        with self.assertRaises(IntegrityError):
            db.execute("INSERT INTO a VALUES (999, 'n42', 0.0)")
        db.close()

    def test_blob_and_unicode_round_trip(self):
        db = connect(self.path)
        db.execute("CREATE TABLE a (b BLOB, t TEXT)")
        payload = bytes(bytearray(range(256)))
        db.execute("INSERT INTO a VALUES (?, ?)", (payload, "héllo wörld 🎉"))
        db.close()
        db = connect(self.path)
        row = db.execute("SELECT b, t FROM a").rows[0]
        self.assertEqual(row[0], payload)
        self.assertEqual(row[1], "héllo wörld 🎉")
        db.close()

    def test_large_values_use_overflow(self):
        db = connect(self.path)
        db.execute("CREATE TABLE a (id INTEGER, body TEXT)")
        big = "x" * 200000
        db.execute("INSERT INTO a VALUES (1, ?)", (big,))
        db.close()
        db = connect(self.path)
        self.assertEqual(db.scalar("SELECT LENGTH(body) FROM a"), 200000)
        db.execute("UPDATE a SET body = 'small'")
        self.assertEqual(db.scalar("SELECT body FROM a"), "small")
        db.close()

    def test_free_pages_are_recycled(self):
        db = connect(self.path)
        db.execute("CREATE TABLE a (x INTEGER, y TEXT)")
        db.execute("BEGIN")
        for i in range(1000):
            db.execute("INSERT INTO a VALUES (?, ?)", (i, "y" * 100))
        db.execute("COMMIT")
        pages = db.pager.page_count
        db.execute("DROP TABLE a")
        db.execute("CREATE TABLE b (x INTEGER, y TEXT)")
        db.execute("BEGIN")
        for i in range(1000):
            db.execute("INSERT INTO b VALUES (?, ?)", (i, "y" * 100))
        db.execute("COMMIT")
        self.assertLessEqual(db.pager.page_count, pages + 10)
        db.close()


class ScaleTest(SQLCase):
    def test_ten_thousand_rows_with_index(self):
        self.db.execute("CREATE TABLE big (id INTEGER PRIMARY KEY, bucket INTEGER, name TEXT)")
        self.db.execute("CREATE INDEX ix_bucket ON big (bucket)")
        self.db.execute("BEGIN")
        for i in range(10000):
            self.db.execute("INSERT INTO big VALUES (?, ?, ?)", (i, i % 97, "row-%d" % i))
        self.db.execute("COMMIT")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM big"), 10000)
        self.assertEqual(self.rows("SELECT name FROM big WHERE id = 7777"), [("row-7777",)])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM big WHERE bucket = 5"),
                         len([i for i in range(10000) if i % 97 == 5]))
        rows = self.rows("SELECT COUNT(*) FROM big WHERE id BETWEEN 100 AND 199")
        self.assertEqual(rows, [(100,)])
        self.db.execute("DELETE FROM big WHERE bucket = 5")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM big WHERE bucket = 5"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM big"),
                         10000 - len([i for i in range(10000) if i % 97 == 5]))

    def test_index_matches_scan_under_random_churn(self):
        rng = random.Random(4242)
        self.db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, k INTEGER, v TEXT)")
        self.db.execute("CREATE INDEX ix ON t (k)")
        model = {}
        self.db.execute("BEGIN")
        for i in range(1500):
            k = rng.randrange(50)
            self.db.execute("INSERT INTO t VALUES (?, ?, ?)", (i, k, "v%d" % i))
            model[i] = k
        self.db.execute("COMMIT")
        for _ in range(300):
            victim = rng.choice(list(model))
            if rng.random() < 0.5:
                self.db.execute("DELETE FROM t WHERE id = ?", (victim,))
                del model[victim]
            else:
                k = rng.randrange(50)
                self.db.execute("UPDATE t SET k = ? WHERE id = ?", (k, victim))
                model[victim] = k
        for k in range(50):
            expected = sorted(i for i, kk in model.items() if kk == k)
            got = sorted(r[0] for r in self.rows("SELECT id FROM t WHERE k = ?", (k,)))
            self.assertEqual(got, expected)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM t"), len(model))


if __name__ == "__main__":
    unittest.main()
