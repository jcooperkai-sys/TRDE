"""Differential testing against sqlite3.

The same schema, the same rows and the same queries go to Quarry and to the
sqlite3 module in the standard library; the result sets must match.  Query
templates deliberately avoid constructs where the two engines are entitled to
disagree (bare columns in a GROUP BY, unordered output, type-affinity corner
cases), so any difference here is a real bug in Quarry.
"""

import random
import sqlite3
import unittest

from quarry import connect

SCHEMA = [
    """CREATE TABLE users (
           id INTEGER PRIMARY KEY,
           name TEXT NOT NULL,
           city TEXT,
           age INTEGER,
           score REAL
       )""",
    """CREATE TABLE orders (
           id INTEGER PRIMARY KEY,
           user_id INTEGER,
           item TEXT,
           qty INTEGER,
           price REAL
       )""",
    "CREATE INDEX ix_users_city ON users (city)",
    "CREATE INDEX ix_orders_user ON orders (user_id)",
    "CREATE INDEX ix_orders_item_qty ON orders (item, qty)",
]

CITIES = ["london", "nyc", "tokyo", "berlin", None]
ITEMS = ["bolt", "nut", "washer", "gear", "spring"]
NAMES = ["ada", "grace", "linus", "barbara", "katherine", "margaret", "alan", "edsger"]

QUERIES = [
    "SELECT * FROM users ORDER BY id",
    "SELECT id, name FROM users WHERE age > 30 ORDER BY id",
    "SELECT id FROM users WHERE city = 'london' ORDER BY id",
    "SELECT id FROM users WHERE city IS NULL ORDER BY id",
    "SELECT id FROM users WHERE city IS NOT NULL AND age <= 40 ORDER BY id",
    "SELECT id, age FROM users WHERE age BETWEEN 25 AND 45 ORDER BY age, id",
    "SELECT id FROM users WHERE name IN ('ada', 'linus', 'nobody') ORDER BY id",
    "SELECT id FROM users WHERE name NOT IN ('ada') ORDER BY id",
    "SELECT id, name FROM users WHERE name LIKE 'a%' ORDER BY id",
    "SELECT id FROM users WHERE name LIKE '%r%' AND city LIKE '_o%' ORDER BY id",
    "SELECT COUNT(*) FROM users",
    "SELECT COUNT(city), COUNT(DISTINCT city) FROM users",
    "SELECT city, COUNT(*) FROM users GROUP BY city ORDER BY city",
    "SELECT city, COUNT(*) FROM users GROUP BY city HAVING COUNT(*) > 1 ORDER BY city",
    "SELECT city, MIN(age), MAX(age), SUM(age) FROM users GROUP BY city ORDER BY city",
    "SELECT DISTINCT city FROM users ORDER BY city",
    "SELECT id, age + 1, age * 2, age / 3, age % 7 FROM users WHERE age IS NOT NULL ORDER BY id",
    "SELECT id, name || '@' || COALESCE(city, 'nowhere') FROM users ORDER BY id",
    "SELECT id, UPPER(name), LENGTH(name), SUBSTR(name, 2, 3) FROM users ORDER BY id",
    "SELECT id, ABS(age - 40), ROUND(score, 2) FROM users ORDER BY id",
    "SELECT id FROM users ORDER BY age DESC, id LIMIT 5",
    "SELECT id FROM users ORDER BY score LIMIT 4 OFFSET 3",
    "SELECT id, CASE WHEN age > 40 THEN 'senior' WHEN age > 25 THEN 'mid' ELSE 'junior' END "
    "FROM users ORDER BY id",
    "SELECT COUNT(*) FROM orders",
    "SELECT item, SUM(qty), COUNT(*) FROM orders GROUP BY item ORDER BY item",
    "SELECT id FROM orders WHERE item = 'bolt' AND qty = 3 ORDER BY id",
    "SELECT id FROM orders WHERE qty >= 4 ORDER BY id",
    "SELECT u.id, o.id FROM users u JOIN orders o ON o.user_id = u.id ORDER BY u.id, o.id",
    "SELECT u.name, o.item FROM users u JOIN orders o ON o.user_id = u.id "
    "WHERE o.qty > 2 ORDER BY u.name, o.item, o.id",
    "SELECT u.id, COUNT(o.id) FROM users u LEFT JOIN orders o ON o.user_id = u.id "
    "GROUP BY u.id ORDER BY u.id",
    "SELECT u.id, SUM(o.qty * o.price) FROM users u LEFT JOIN orders o ON o.user_id = u.id "
    "GROUP BY u.id ORDER BY u.id",
    "SELECT u.city, COUNT(*) FROM users u JOIN orders o ON o.user_id = u.id "
    "GROUP BY u.city ORDER BY u.city",
    "SELECT u.id FROM users u LEFT JOIN orders o ON o.user_id = u.id WHERE o.id IS NULL "
    "ORDER BY u.id",
    "SELECT a.id, b.id FROM users a JOIN users b ON a.city = b.city AND a.id < b.id "
    "ORDER BY a.id, b.id",
    "SELECT item, qty, price FROM orders ORDER BY item, qty DESC, id LIMIT 12",
    "SELECT COUNT(*) FROM users WHERE age > (30) AND (city = 'nyc' OR city = 'tokyo')",
]

MUTATIONS = [
    "UPDATE users SET age = age + 1 WHERE city = 'london'",
    "UPDATE users SET city = 'tokyo' WHERE id % 7 = 0",
    "UPDATE orders SET qty = qty * 2 WHERE item = 'nut'",
    "DELETE FROM orders WHERE qty > 8",
    "DELETE FROM users WHERE age IS NULL",
    "UPDATE users SET score = score / 2 WHERE score > 50",
]


def normalize(value):
    if isinstance(value, float):
        return round(value, 9)
    if isinstance(value, bool):
        return int(value)
    return value


def normalize_rows(rows):
    return [tuple(normalize(v) for v in row) for row in rows]


class DifferentialTest(unittest.TestCase):
    def build(self, seed, rows=120, orders=260):
        rng = random.Random(seed)
        quarry_db = connect(":memory:")
        lite = sqlite3.connect(":memory:")
        for statement in SCHEMA:
            quarry_db.execute(statement)
            lite.execute(statement)

        user_rows = []
        for i in range(1, rows + 1):
            user_rows.append((
                i,
                rng.choice(NAMES) + str(rng.randrange(100)),
                rng.choice(CITIES),
                rng.choice([None] + list(range(18, 66))),
                round(rng.uniform(0, 100), 4),
            ))
        order_rows = []
        for i in range(1, orders + 1):
            order_rows.append((
                i,
                rng.randrange(1, rows + 1),
                rng.choice(ITEMS),
                rng.randrange(1, 10),
                round(rng.uniform(1, 50), 4),
            ))

        quarry_db.execute("BEGIN")
        for row in user_rows:
            quarry_db.execute("INSERT INTO users VALUES (?, ?, ?, ?, ?)", row)
        for row in order_rows:
            quarry_db.execute("INSERT INTO orders VALUES (?, ?, ?, ?, ?)", row)
        quarry_db.execute("COMMIT")
        lite.executemany("INSERT INTO users VALUES (?, ?, ?, ?, ?)", user_rows)
        lite.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?)", order_rows)
        lite.commit()
        return quarry_db, lite

    def compare_all(self, quarry_db, lite, label=""):
        for sql in QUERIES:
            expected = normalize_rows(lite.execute(sql).fetchall())
            actual = normalize_rows(quarry_db.execute(sql).rows)
            self.assertEqual(actual, expected,
                             "%s mismatch for: %s\n  quarry:  %r\n  sqlite3: %r"
                             % (label, sql, actual[:5], expected[:5]))

    def test_matches_sqlite_on_fresh_data(self):
        for seed in (1, 2, 3):
            quarry_db, lite = self.build(seed)
            try:
                self.compare_all(quarry_db, lite, "seed %d" % seed)
            finally:
                quarry_db.close()
                lite.close()

    def test_matches_sqlite_after_mutations(self):
        quarry_db, lite = self.build(17)
        try:
            for statement in MUTATIONS:
                quarry_db.execute(statement)
                lite.execute(statement)
                lite.commit()
                self.compare_all(quarry_db, lite, "after %r" % statement)
        finally:
            quarry_db.close()
            lite.close()

    def test_matches_sqlite_with_parameters(self):
        quarry_db, lite = self.build(5)
        try:
            cases = [
                ("SELECT id FROM users WHERE age > ? ORDER BY id", (40,)),
                ("SELECT id FROM users WHERE city = ? ORDER BY id", ("nyc",)),
                ("SELECT id FROM orders WHERE item = ? AND qty > ? ORDER BY id", ("gear", 4)),
                ("SELECT COUNT(*) FROM users WHERE name LIKE ?", ("a%",)),
            ]
            for sql, params in cases:
                expected = normalize_rows(lite.execute(sql, params).fetchall())
                actual = normalize_rows(quarry_db.execute(sql, params).rows)
                self.assertEqual(actual, expected, "mismatch for %s %r" % (sql, params))
        finally:
            quarry_db.close()
            lite.close()

    def test_index_and_scan_agree(self):
        """The same predicate must return the same rows with and without an index."""
        quarry_db, lite = self.build(9)
        try:
            probes = [
                "SELECT id FROM users WHERE city = 'berlin' ORDER BY id",
                "SELECT id FROM orders WHERE user_id = 12 ORDER BY id",
                "SELECT id FROM orders WHERE item = 'gear' AND qty = 5 ORDER BY id",
                "SELECT id FROM users WHERE id BETWEEN 20 AND 60 ORDER BY id",
            ]
            indexed = [quarry_db.execute(sql).rows for sql in probes]
            quarry_db.execute("DROP INDEX ix_users_city")
            quarry_db.execute("DROP INDEX ix_orders_user")
            quarry_db.execute("DROP INDEX ix_orders_item_qty")
            for sql, before in zip(probes, indexed):
                after = quarry_db.execute(sql).rows
                self.assertEqual(after, before, "index/scan disagreement for %s" % sql)
                self.assertEqual(normalize_rows(after),
                                 normalize_rows(lite.execute(sql).fetchall()))
        finally:
            quarry_db.close()
            lite.close()


if __name__ == "__main__":
    unittest.main()
