"""An interactive shell for Quarry databases.

    python3 -m quarry mydb.qdb                 # REPL
    python3 -m quarry mydb.qdb "SELECT 1+1"    # run one statement and exit
    cat script.sql | python3 -m quarry mydb.qdb
"""

import os
import sys
import time

from .database import Database
from .errors import QuarryError
from .values import format_number

BANNER = """Quarry %s -- a database engine built from scratch.
Enter SQL terminated by ';'.  ".help" lists shell commands, ".quit" exits."""

HELP = """Shell commands:
  .tables                 list tables
  .schema [TABLE]         show CREATE-style schema
  .indexes [TABLE]        list indexes
  .stats                  file and page statistics
  .mode table|csv|list    output format (default: table)
  .headers on|off         show column headers (default: on)
  .timer on|off           report statement timing
  .read FILE              execute SQL from a file
  .dump                   write the whole database out as SQL
  .help                   this message
  .quit / .exit           leave the shell
Anything else is executed as SQL.  Statements are terminated with ';'."""


def render_value(value, null="NULL"):
    if value is None:
        return null
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return format_number(value)
    if isinstance(value, bytes):
        return "x'%s'" % "".join("%02x" % b for b in bytearray(value))
    return value


def quote_sql(value):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return format_number(value)
    if isinstance(value, bytes):
        return "x'%s'" % "".join("%02x" % b for b in bytearray(value))
    return "'%s'" % value.replace("'", "''")


def format_table(columns, rows, headers=True):
    if not columns:
        return ""
    text_rows = [[render_value(v) for v in row] for row in rows]
    widths = [len(c) if headers else 0 for c in columns]
    for row in text_rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(cell))
    lines = []
    if headers:
        lines.append(" | ".join(c.ljust(widths[i]) for i, c in enumerate(columns)))
        lines.append("-+-".join("-" * w for w in widths))
    for row in text_rows:
        lines.append(" | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return "\n".join(lines)


def format_csv(columns, rows, headers=True):
    def escape(text):
        if any(ch in text for ch in ',"\n'):
            return '"%s"' % text.replace('"', '""')
        return text

    lines = []
    if headers:
        lines.append(",".join(escape(c) for c in columns))
    for row in rows:
        lines.append(",".join(escape(render_value(v, "")) for v in row))
    return "\n".join(lines)


def format_list(columns, rows, headers=True):
    lines = []
    if headers:
        lines.append("|".join(columns))
    for row in rows:
        lines.append("|".join(render_value(v, "") for v in row))
    return "\n".join(lines)


FORMATTERS = {"table": format_table, "csv": format_csv, "list": format_list}


class Shell(object):
    def __init__(self, db, out=None):
        self.db = db
        self.out = out or sys.stdout
        self.mode = "table"
        self.headers = True
        self.timer = False
        self.buffer = ""

    def write(self, text=""):
        self.out.write(text + "\n")

    # -- output -----------------------------------------------------------
    def show(self, result, elapsed=None):
        if result is None:
            return
        if result.columns:
            body = FORMATTERS[self.mode](result.columns, result.rows, self.headers)
            if body:
                self.write(body)
            if self.mode == "table":
                self.write("(%d row%s)" % (len(result.rows), "" if len(result.rows) == 1 else "s"))
        elif result.message:
            self.write(result.message)
        if self.timer and elapsed is not None:
            self.write("Run time: %.3f ms" % (elapsed * 1000.0))

    # -- input ------------------------------------------------------------
    def feed(self, line):
        """Feed one line of input; returns False when the shell should exit."""
        stripped = line.strip()
        if not self.buffer and stripped.startswith("."):
            return self.command(stripped)
        if not stripped and not self.buffer:
            return True
        self.buffer += line + "\n"
        if ";" not in self.buffer:
            return True
        statement, self.buffer = self.buffer, ""
        self.run(statement)
        return True

    def flush(self):
        if self.buffer.strip():
            self.run(self.buffer)
        self.buffer = ""

    def run(self, sql):
        start = time.time()
        try:
            result = self.db.execute(sql)
        except QuarryError as exc:
            self.write("Error: %s" % exc)
            return
        except Exception as exc:  # pragma: no cover - defensive
            self.write("Internal error: %s" % exc)
            return
        self.show(result, time.time() - start)

    # -- dot commands -----------------------------------------------------
    def command(self, line):
        parts = line.split()
        name = parts[0].lower()
        args = parts[1:]
        if name in (".quit", ".exit"):
            return False
        if name == ".help":
            self.write(HELP)
        elif name == ".tables":
            names = self.db.table_names
            self.write("\n".join(names) if names else "(no tables)")
        elif name == ".schema":
            self.write(self.schema_text(args[0] if args else None))
        elif name == ".indexes":
            self.write(self.indexes_text(args[0] if args else None))
        elif name == ".stats":
            self.write(self.stats_text())
        elif name == ".mode":
            if not args or args[0] not in FORMATTERS:
                self.write("usage: .mode table|csv|list")
            else:
                self.mode = args[0]
        elif name == ".headers":
            self.headers = bool(args) and args[0].lower() in ("on", "true", "1", "yes")
        elif name == ".timer":
            self.timer = bool(args) and args[0].lower() in ("on", "true", "1", "yes")
        elif name == ".read":
            if not args:
                self.write("usage: .read FILE")
            elif not os.path.exists(args[0]):
                self.write("no such file: %s" % args[0])
            else:
                with open(args[0]) as handle:
                    self.run(handle.read())
        elif name == ".dump":
            self.write(self.dump_text())
        else:
            self.write("unknown command %r -- try .help" % name)
        return True

    def schema_text(self, table_name=None):
        lines = []
        for entry in self.db.schema():
            if table_name and entry["table"].lower() != table_name.lower():
                continue
            parts = []
            for column in entry["columns"]:
                piece = "  %s %s" % (column["name"], column["type"])
                if column["primary_key"]:
                    piece += " PRIMARY KEY"
                elif column["not_null"]:
                    piece += " NOT NULL"
                if column["default"] is not None:
                    piece += " DEFAULT %s" % quote_sql(column["default"])
                parts.append(piece)
            lines.append("CREATE TABLE %s (\n%s\n);" % (entry["table"], ",\n".join(parts)))
            for index in entry["indexes"]:
                if index["origin"] == "CREATE INDEX":
                    lines.append("CREATE %sINDEX %s ON %s (%s);" % (
                        "UNIQUE " if index["unique"] else "", index["name"],
                        entry["table"], ", ".join(index["columns"])))
        return "\n".join(lines) if lines else "(no tables)"

    def indexes_text(self, table_name=None):
        lines = []
        for entry in self.db.schema():
            if table_name and entry["table"].lower() != table_name.lower():
                continue
            for index in entry["indexes"]:
                lines.append("%-28s %-12s %s%s" % (
                    index["name"], entry["table"],
                    ", ".join(index["columns"]),
                    "  [unique, %s]" % index["origin"] if index["unique"] else ""))
        return "\n".join(lines) if lines else "(no indexes)"

    def stats_text(self):
        pager = self.db.pager
        free = 0
        page_id = pager.freelist_head
        seen = set()
        while page_id and page_id not in seen:
            seen.add(page_id)
            free += 1
            page_id = pager.slotted(page_id).link
        size = pager.page_count * 4096
        rows = []
        for name in self.db.table_names:
            rows.append("  %-20s %8d rows" % (name, self.db.table(name).count()))
        return "\n".join([
            "file:        %s" % self.db.path,
            "pages:       %d (%d bytes, %.1f KiB)" % (pager.page_count, size, size / 1024.0),
            "free pages:  %d" % free,
            "cached:      %d" % len(pager._cache),
            "tables:",
        ] + (rows or ["  (none)"]))

    def dump_text(self):
        lines = ["BEGIN;"]
        for entry in self.db.schema():
            lines.append(self.schema_text(entry["table"]))
            table = self.db.table(entry["table"])
            names = ", ".join(c["name"] for c in entry["columns"])
            for _, row in table.scan():
                lines.append("INSERT INTO %s (%s) VALUES (%s);" % (
                    entry["table"], names, ", ".join(quote_sql(v) for v in row)))
        lines.append("COMMIT;")
        return "\n".join(lines)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    if argv and argv[0] in ("-v", "--version"):
        from . import __version__
        print("quarry %s" % __version__)
        return 0
    path = argv[0] if argv else ":memory:"
    sql = " ".join(argv[1:]) if len(argv) > 1 else None
    db = Database(path)
    shell = Shell(db)
    try:
        if sql:
            shell.run(sql)
            return 0
        if not sys.stdin.isatty():
            for line in sys.stdin:
                if not shell.feed(line.rstrip("\n")):
                    break
            shell.flush()
            return 0
        from . import __version__
        print(BANNER % __version__)
        while True:
            try:
                prompt = "quarry> " if not shell.buffer else "   ...> "
                line = input(prompt)
            except EOFError:
                print()
                break
            except KeyboardInterrupt:
                print("^C")
                shell.buffer = ""
                continue
            if not shell.feed(line):
                break
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
