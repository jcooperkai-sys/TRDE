"""Tests for the shell: output formats, dot commands, error handling."""

import os
import shutil
import sys
import tempfile
import unittest

try:
    from io import StringIO
except ImportError:  # pragma: no cover
    from StringIO import StringIO

from quarry import connect
from quarry.cli import Shell, format_csv, format_list, format_table, main, quote_sql


class FormatterTest(unittest.TestCase):
    columns = ["id", "name"]
    rows = [(1, "ada"), (2, None), (3, "a,b")]

    def test_table_alignment(self):
        text = format_table(self.columns, self.rows)
        lines = text.split("\n")
        self.assertEqual(lines[0], "id | name")
        self.assertTrue(set(lines[1]) <= set("-+"))
        self.assertIn("2  | NULL", text)

    def test_table_without_headers(self):
        text = format_table(self.columns, self.rows, headers=False)
        self.assertNotIn("name", text.split("\n")[0])

    def test_csv_quoting(self):
        text = format_csv(self.columns, self.rows)
        self.assertEqual(text.split("\n")[0], "id,name")
        self.assertIn('"a,b"', text)
        self.assertIn("2,", text)

    def test_list_format(self):
        text = format_list(self.columns, self.rows)
        self.assertEqual(text.split("\n")[1], "1|ada")

    def test_quote_sql(self):
        self.assertEqual(quote_sql(None), "NULL")
        self.assertEqual(quote_sql(3), "3")
        self.assertEqual(quote_sql("it's"), "'it''s'")
        self.assertEqual(quote_sql(b"\x00\xff"), "x'00ff'")


class ShellTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="quarry-cli-")
        self.db = connect(os.path.join(self.dir, "s.qdb"))
        self.out = StringIO()
        self.shell = Shell(self.db, self.out)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def feed(self, *lines):
        for line in lines:
            self.shell.feed(line)
        return self.out.getvalue()

    def test_statement_spanning_lines(self):
        self.feed("CREATE TABLE t (a INTEGER,", "b TEXT);")
        self.assertEqual(self.db.table_names, ["t"])

    def test_select_output(self):
        self.feed("CREATE TABLE t (a INTEGER);", "INSERT INTO t VALUES (1),(2);")
        output = self.feed("SELECT * FROM t;")
        self.assertIn("(2 rows)", output)

    def test_error_is_reported_not_raised(self):
        output = self.feed("SELECT * FROM missing;")
        self.assertIn("Error:", output)
        output = self.feed("SELECT nonsense from;")
        self.assertIn("Error:", output)

    def test_dot_tables_and_schema(self):
        self.feed("CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT DEFAULT 'x');")
        self.feed("CREATE INDEX ix ON t (b);")
        output = self.feed(".tables")
        self.assertIn("t", output)
        output = self.feed(".schema")
        self.assertIn("CREATE TABLE t", output)
        self.assertIn("PRIMARY KEY", output)
        self.assertIn("CREATE INDEX ix", output)
        output = self.feed(".indexes")
        self.assertIn("ix", output)

    def test_dot_stats(self):
        self.feed("CREATE TABLE t (a INTEGER);", "INSERT INTO t VALUES (1);")
        output = self.feed(".stats")
        self.assertIn("pages:", output)
        self.assertIn("1 rows", output)

    def test_dot_mode_and_headers(self):
        self.feed("CREATE TABLE t (a INTEGER);", "INSERT INTO t VALUES (5);")
        self.shell.feed(".mode csv")
        output = self.feed("SELECT * FROM t;")
        self.assertIn("a\n5", output)
        self.shell.feed(".headers off")
        self.out.truncate(0)
        self.out.seek(0)
        output = self.feed("SELECT * FROM t;")
        self.assertEqual(output.strip(), "5")

    def test_dot_quit_and_unknown(self):
        self.assertFalse(self.shell.feed(".quit"))
        self.assertTrue(self.shell.feed(".nope"))
        self.assertIn("unknown command", self.out.getvalue())

    def test_dot_read(self):
        path = os.path.join(self.dir, "script.sql")
        with open(path, "w") as handle:
            handle.write("CREATE TABLE r (x INTEGER); INSERT INTO r VALUES (9);")
        self.feed(".read %s" % path)
        self.assertEqual(self.db.scalar("SELECT x FROM r"), 9)
        output = self.feed(".read /nonexistent/file.sql")
        self.assertIn("no such file", output)

    def test_dump_round_trips(self):
        self.feed("CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT);")
        self.feed("INSERT INTO t VALUES (1, 'it''s'), (2, NULL);")
        dump = self.shell.dump_text()
        other = connect(":memory:")
        try:
            other.execute(dump)
            self.assertEqual(other.execute("SELECT * FROM t ORDER BY a").rows,
                             [(1, "it's"), (2, None)])
        finally:
            other.close()

    def test_timer(self):
        self.shell.feed(".timer on")
        output = self.feed("SELECT 1;")
        self.assertIn("Run time:", output)

    def test_flush_runs_unterminated_statement(self):
        self.shell.feed("CREATE TABLE t (a INTEGER)")
        self.shell.flush()
        self.assertEqual(self.db.table_names, ["t"])


class MainTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="quarry-main-")
        self.path = os.path.join(self.dir, "m.qdb")
        self._stdout = sys.stdout
        sys.stdout = StringIO()  # main() prints; keep the test run quiet

    def tearDown(self):
        sys.stdout = self._stdout
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_one_shot_execution(self):
        self.assertEqual(main([self.path, "CREATE TABLE t (a INTEGER)"]), 0)
        self.assertEqual(main([self.path, "INSERT INTO t VALUES (1)"]), 0)
        db = connect(self.path)
        try:
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM t"), 1)
        finally:
            db.close()

    def test_version_and_help(self):
        self.assertEqual(main(["--version"]), 0)
        self.assertEqual(main(["--help"]), 0)


if __name__ == "__main__":
    unittest.main()
