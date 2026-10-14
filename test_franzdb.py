"""
FranzDB test suite.

  1. SQL behaviour: hand-written checks with known answers (NULL logic, joins, constraints, ...)
  2. Transactions: COMMIT, ROLLBACK, and statement atomicity (a failing statement changes nothing)
  3. Storage: 20,000 rows survive closing and reopening; B+trees stay valid; dropped pages get reused
  4. Crash recovery: the power is "cut" in the middle of a commit, and the database must come back intact
  5. Differential testing: thousands of random queries and updates run on FranzDB AND on SQLite
     (the database built into Python), and every single result must be identical

Run:  python test_franzdb.py
"""
import math
import os
import random
import sqlite3
import sys
import tempfile
import time

from franzdb import Database, DBError, SimulatedCrash

TMP = tempfile.mkdtemp(prefix="franzdb_test_")
PASSED, FAILED = [0], [0]


def check(label, cond, detail=""):
    if cond:
        PASSED[0] += 1
        print(f"  PASS  {label}")
    else:
        FAILED[0] += 1
        print(f"  FAIL  {label}  {detail}")


def expect_error(db, sql, needle):
    try:
        db.execute(sql)
        return False, "no error raised"
    except DBError as e:
        return needle in str(e), str(e)


def path(name):
    p = os.path.join(TMP, name)
    for f in (p, p + "-journal"):
        if os.path.exists(f):
            os.remove(f)
    return p


# ============================================================================ 1. SQL behaviour
def test_sql():
    print("SQL behaviour:")
    db = Database()
    q = db.query
    db.execute("""
        CREATE TABLE emp (id INTEGER PRIMARY KEY, name TEXT NOT NULL, dept TEXT, salary INTEGER, boss INTEGER);
        INSERT INTO emp VALUES (1, 'Ana', 'eng', 120, NULL), (2, 'Ben', 'eng', 100, 1), (3, 'Cara', 'ops', 90, 1),
                               (4, 'Dan', 'ops', NULL, 3), (5, 'Eve', NULL, 70, 3);
        CREATE TABLE dept (code TEXT PRIMARY KEY, title TEXT);
        INSERT INTO dept VALUES ('eng', 'Engineering'), ('ops', 'Operations'), ('hr', 'People');
    """)
    check("three-valued logic: NULL = NULL is NULL", q("SELECT NULL = NULL, NULL IS NULL, 1 IN (2, NULL), 1 IN (1, NULL)") == [(None, 1, None, 1)])
    check("NULL AND false is false, NULL OR true is true", q("SELECT NULL AND 0, NULL OR 1, NOT NULL") == [(0, 1, None)])
    check("WHERE skips rows where the condition is NULL", q("SELECT name FROM emp WHERE salary > 80 ORDER BY id") == [("Ana",), ("Ben",), ("Cara",)])
    check("integer division truncates, real division doesn't", q("SELECT 7 / 2, -7 / 2, 7 % 3, -7 % 3, 7 / 2.0, 1 / 0") == [(3, -3, 1, -1, 3.5, None)])
    check("aggregates ignore NULL", q("SELECT COUNT(*), COUNT(salary), SUM(salary), AVG(salary), MIN(salary), MAX(salary) FROM emp") == [(5, 4, 380, 95.0, 70, 120)])
    check("aggregates over zero rows", q("SELECT COUNT(*), SUM(salary), MAX(name), TOTAL(salary) FROM emp WHERE 0") == [(0, None, None, 0.0)])
    check("GROUP BY puts NULLs in one group, HAVING filters groups",
          q("SELECT dept, COUNT(*) FROM emp GROUP BY dept HAVING COUNT(*) >= 1 ORDER BY dept") == [(None, 1), ("eng", 2), ("ops", 2)])
    check("ORDER BY: NULLs first ascending, last descending",
          q("SELECT salary FROM emp ORDER BY salary") == [(None,), (70,), (90,), (100,), (120,)] and
          q("SELECT salary FROM emp ORDER BY salary DESC") == [(120,), (100,), (90,), (70,), (None,)])
    check("INNER JOIN", q("SELECT e.name, d.title FROM emp e JOIN dept d ON e.dept = d.code ORDER BY e.id") ==
          [("Ana", "Engineering"), ("Ben", "Engineering"), ("Cara", "Operations"), ("Dan", "Operations")])
    check("LEFT JOIN keeps unmatched rows", q("SELECT d.code, COUNT(e.id) FROM dept d LEFT JOIN emp e ON e.dept = d.code GROUP BY d.code ORDER BY d.code") ==
          [("eng", 2), ("hr", 0), ("ops", 2)])
    check("self join (employee -> boss)", q("SELECT e.name, b.name FROM emp e JOIN emp b ON e.boss = b.id ORDER BY e.id") ==
          [("Ben", "Ana"), ("Cara", "Ana"), ("Dan", "Cara"), ("Eve", "Cara")])
    check("DISTINCT, LIMIT, OFFSET", q("SELECT DISTINCT dept FROM emp ORDER BY dept LIMIT 2 OFFSET 1") == [("eng",), ("ops",)])
    check("CASE, LIKE, BETWEEN, string functions",
          q("SELECT name, CASE WHEN salary BETWEEN 90 AND 110 THEN 'mid' ELSE 'other' END, upper(substr(name, 1, 2)), length(name) "
            "FROM emp WHERE name LIKE '%a%' ORDER BY name") == [("Ana", "other", "AN", 3), ("Cara", "mid", "CA", 4), ("Dan", "other", "DA", 3)])
    check("|| concatenation and coalesce", q("SELECT name || ':' || coalesce(dept, '-') FROM emp WHERE id = 5") == [("Eve:-",)])
    check("INSERT ... SELECT", db.execute("CREATE TABLE rich (name TEXT); INSERT INTO rich SELECT name FROM emp WHERE salary >= 100").rowcount == 2
          and q("SELECT COUNT(*) FROM rich") == [(2,)])
    db.execute("CREATE TABLE typed (i INTEGER, r REAL, t TEXT)")
    db.execute("INSERT INTO typed VALUES ('42', 3, 99), (7.0, '2.5', 1.5)")
    check("column type affinity converts values", q("SELECT i, r, t, typeof(i), typeof(r), typeof(t) FROM typed") ==
          [(42, 3.0, "99", "integer", "real", "text"), (7, 2.5, "1.5", "integer", "real", "text")])
    check("DEFAULT values", db.execute("CREATE TABLE d (a INTEGER, b TEXT DEFAULT 'x', c INTEGER DEFAULT -1); INSERT INTO d (a) VALUES (1)")
          and q("SELECT * FROM d") == [(1, "x", -1)])

    for label, sql, needle in [
        ("NOT NULL constraint", "INSERT INTO emp (id, name) VALUES (9, NULL)", "NOT NULL constraint failed: emp.name"),
        ("PRIMARY KEY constraint", "INSERT INTO emp VALUES (1, 'X', 'eng', 1, NULL)", "UNIQUE constraint failed: emp.id"),
        ("UNIQUE text primary key", "INSERT INTO dept VALUES ('eng', 'Again')", "UNIQUE constraint failed: dept.code"),
        ("unknown table", "SELECT * FROM nope", "no such table: nope"),
        ("unknown column", "SELECT wage FROM emp", "no such column: wage"),
        ("ambiguous column", "SELECT name FROM emp a JOIN emp b ON a.id = b.boss", "ambiguous column name: name"),
        ("aggregate in WHERE", "SELECT * FROM emp WHERE COUNT(*) > 1", "misuse of aggregate"),
        ("syntax error", "SELEC * FROM emp", 'near "SELEC": syntax error'),
        ("wrong number of values", "INSERT INTO emp VALUES (1, 2)", "5 columns but 2 values"),
        ("INTEGER PRIMARY KEY must be an integer", "INSERT INTO emp VALUES ('abc', 'X', NULL, NULL, NULL)", "datatype mismatch"),
    ]:
        ok, msg = expect_error(db, sql, needle)
        check(f"error: {label}", ok, msg)
    db.close()


# ============================================================================ 2. transactions
def test_transactions():
    print("\nTransactions:")
    db = Database(path("tx.db"))
    db.execute("CREATE TABLE acct (id INTEGER PRIMARY KEY, owner TEXT UNIQUE, balance INTEGER)")
    db.execute("INSERT INTO acct VALUES (1, 'ana', 100), (2, 'ben', 50)")
    db.execute("BEGIN; UPDATE acct SET balance = balance - 30 WHERE id = 1; UPDATE acct SET balance = balance + 30 WHERE id = 2")
    check("changes are visible inside the transaction", db.query("SELECT balance FROM acct ORDER BY id") == [(70,), (80,)])
    db.execute("ROLLBACK")
    check("ROLLBACK undoes everything", db.query("SELECT balance FROM acct ORDER BY id") == [(100,), (50,)])

    ok, msg = expect_error(db, "INSERT INTO acct VALUES (3, 'cara', 10), (4, 'dan', 10), (5, 'ana', 10)", "UNIQUE constraint failed: acct.owner")
    check("a failing multi-row INSERT is rejected", ok, msg)
    check("...and inserts NONE of its rows (statement atomicity)", db.query("SELECT COUNT(*) FROM acct") == [(2,)])

    db.execute("BEGIN")
    db.execute("INSERT INTO acct VALUES (3, 'cara', 10)")
    ok, _ = expect_error(db, "UPDATE acct SET owner = 'ana' WHERE id = 3", "UNIQUE")
    db.execute("COMMIT")
    check("inside a transaction, only the failing statement is undone", ok and
          db.query("SELECT id, owner FROM acct ORDER BY id") == [(1, "ana"), (2, "ben"), (3, "cara")])
    db.close()
    db = Database(path("tx.db") if False else os.path.join(TMP, "tx.db"))
    check("committed data survives reopening", db.query("SELECT SUM(balance) FROM acct") == [(160,)])
    check("integrity check passes", db.integrity_check() == ["ok"], db.integrity_check())
    db.close()


# ============================================================================ 3. storage
def test_storage():
    print("\nStorage (B+trees on disk):")
    p = path("big.db")
    rng = random.Random(7)
    db = Database(p)
    db.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, sku TEXT UNIQUE, price REAL, qty INTEGER, note TEXT)")
    db.execute("CREATE INDEX items_qty ON items(qty)")
    ids = list(range(1, 20001))
    rng.shuffle(ids)                                  # insert in random order to stress page splits
    t0 = time.time()
    db.execute("BEGIN")
    for i in ids:
        db.execute(f"INSERT INTO items VALUES ({i}, 'SKU-{i:06d}', {i % 997 / 4}, {i % 50}, '{'x' * (i % 40)}')")
    db.execute("COMMIT")
    insert_time = time.time() - t0
    count, depth = db._tree(db._table("items")).check()
    pages = db.pager.cat["num_pages"]
    db.close()
    size_kb = os.path.getsize(p) // 1024
    check(f"20,000 rows inserted in random order ({insert_time:.1f}s, {pages} pages, {size_kb} KB, tree depth {depth})",
          count == 20000 and depth >= 3)

    db = Database(p)
    check("all rows are back after reopening", db.query("SELECT COUNT(*), SUM(id), SUM(qty) FROM items") ==
          [(20000, sum(range(1, 20001)), sum(i % 50 for i in range(1, 20001)))])
    check("integrity check passes (trees, indexes, every page accounted for)", db.integrity_check() == ["ok"], db.integrity_check())
    plan = db.query("EXPLAIN SELECT * FROM items WHERE sku = 'SKU-012345'")
    check("planner uses the UNIQUE index for sku = ?", "USING INDEX autoindex_items_sku" in plan[0][0], plan)
    check("planner uses the rowid for id BETWEEN", "INTEGER PRIMARY KEY" in db.query("EXPLAIN SELECT * FROM items WHERE id BETWEEN 10 AND 20")[0][0])

    t0 = time.perf_counter()
    for k in range(200):
        db.query(f"SELECT price FROM items WHERE sku = 'SKU-{k * 97 + 1:06d}'")
    indexed = (time.perf_counter() - t0) / 200
    t0 = time.perf_counter()
    db.query("SELECT price FROM items WHERE note = 'nothing-matches'")
    scan = time.perf_counter() - t0
    check(f"index lookup is fast: {indexed * 1000:.2f} ms vs full scan {scan * 1000:.0f} ms ({scan / indexed:.0f}x faster)",
          db.query("SELECT price FROM items WHERE sku = 'SKU-000100'") == [(100 % 997 / 4,)] and scan / indexed > 20)

    db.execute("DELETE FROM items WHERE qty < 25")
    db.execute("UPDATE items SET qty = qty + 100, note = 'updated' WHERE id % 3 = 0")
    check("bulk DELETE and UPDATE keep tables and indexes in sync",
          db.query("SELECT COUNT(*) FROM items") == [(10000,)] and db.integrity_check() == ["ok"])

    before = db.pager.cat["num_pages"]
    db.execute("DROP TABLE items")
    db.execute("CREATE TABLE items2 (id INTEGER PRIMARY KEY, v TEXT)")
    db.execute("BEGIN")
    for i in range(5000):
        db.execute(f"INSERT INTO items2 VALUES ({i}, '{'y' * 30}')")
    db.execute("COMMIT")
    check(f"dropped pages are reused (file stayed at {db.pager.cat['num_pages']} pages)",
          db.pager.cat["num_pages"] == before and db.integrity_check() == ["ok"])
    db.close()


# ============================================================================ 4. crash recovery
def test_crash_recovery():
    print("\nCrash recovery (rollback journal):")
    for crash_point in (0, 1, 3, 10, 40):
        p = path(f"crash{crash_point}.db")
        db = Database(p)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT, n INTEGER)")
        db.execute("CREATE INDEX t_n ON t(n)")
        db.execute("BEGIN")
        for i in range(3000):
            db.execute(f"INSERT INTO t VALUES ({i}, 'value {i}', {i % 17})")
        db.execute("COMMIT")
        good = db.query("SELECT COUNT(*), SUM(n), SUM(length(v)) FROM t")

        db.execute("BEGIN")
        db.execute("UPDATE t SET n = n + 1000, v = v || ' changed'")
        db.execute("DELETE FROM t WHERE id % 2 = 0")
        for i in range(3000, 4000):
            db.execute(f"INSERT INTO t VALUES ({i}, 'new', 1)")
        db.pager.crash_after = crash_point
        try:
            db.execute("COMMIT")
            crashed = False
        except SimulatedCrash:
            crashed = True
        db.pager.f.close()                 # the "power cut": no cleanup, half-written pages left on disk
        journal_left = os.path.exists(p + "-journal")

        db = Database(p)
        restored = db.query("SELECT COUNT(*), SUM(n), SUM(length(v)) FROM t")
        check(f"power cut after {crash_point:>2} page writes -> journal replayed, database exactly as before",
              crashed and journal_left and db.pager.recovered and restored == good and db.integrity_check() == ["ok"],
              f"{restored} vs {good}, {db.integrity_check()}")
        db.close()

    p = path("halfjournal.db")
    db = Database(p)
    db.execute("CREATE TABLE t (x INTEGER); INSERT INTO t VALUES (1), (2), (3)")
    db.close()
    with open(p + "-journal", "wb") as j:
        j.write(b"FZJOURN1" + b"\x00" * 40)    # crash while the journal itself was being written
    db = Database(p)
    check("a half-written journal is ignored (the database file was never touched)",
          db.query("SELECT SUM(x) FROM t") == [(6,)] and not os.path.exists(p + "-journal"))
    db.close()


# ============================================================================ 5. differential testing vs SQLite
CITIES = ["Cebu", "Davao", "Manila", "Iloilo", "Baguio", "Tacloban", "Bohol", None]
NAMES = ["ana", "Ben", "cara", "Dan", "eve", "Fritz", "gino", "Hana", "ivy", "Jo", "kai", "Lea"]


def random_value(rng, kind):
    if rng.random() < 0.12:
        return None
    if kind == "age":
        return rng.randint(0, 90)
    if kind == "score":
        return round(rng.uniform(-50, 150), 2)
    if kind == "city":
        return rng.choice(CITIES)
    if kind == "team":
        return rng.randint(1, 12)
    return rng.choice(NAMES) + str(rng.randint(0, 99))


def sql_lit(v):
    if v is None:
        return "NULL"
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    return repr(v)


def random_pred(rng, depth=0, p="p."):
    r = rng.random()
    if depth < 2 and r < 0.25:
        return f"({random_pred(rng, depth + 1, p)} {rng.choice(['AND', 'OR'])} {random_pred(rng, depth + 1, p)})"
    if depth < 2 and r < 0.3:
        return f"NOT ({random_pred(rng, depth + 1, p)})"
    choice = rng.randint(0, 13)
    op = rng.choice(["=", "!=", "<", "<=", ">", ">="])
    if choice == 0:
        return f"{p}age {op} {rng.randint(0, 90)}"
    if choice == 1:
        return f"{p}score {op} {round(rng.uniform(-50, 150), 1)}"
    if choice == 2:
        return f"{p}city {rng.choice(['=', '!='])} '{rng.choice(CITIES[:-1])}'"
    if choice == 3:
        return f"{p}{rng.choice(['age', 'score', 'city', 'team'])} IS {rng.choice(['', 'NOT '])}NULL"
    if choice == 4:
        lo = rng.randint(0, 80)
        return f"{p}age {rng.choice(['', 'NOT '])}BETWEEN {lo} AND {lo + rng.randint(0, 30)}"
    if choice == 5:
        items = ", ".join(str(rng.randint(1, 14)) for _ in range(rng.randint(1, 4))) + rng.choice(["", ", NULL"])
        return f"{p}team {rng.choice(['', 'NOT '])}IN ({items})"
    if choice == 6:
        return f"{p}name LIKE '{rng.choice(['a', 'B', 'e', 'h', 'J', '%'])}%'"
    if choice == 7:
        return f"{p}id {op} {rng.randint(0, 700)}"
    if choice == 8:
        lo = rng.randint(0, 600)
        return f"{p}id BETWEEN {lo} AND {lo + rng.randint(0, 80)}"
    if choice == 9:
        return f"{p}age + {p}team {op} {rng.randint(10, 90)}"
    if choice == 10:
        return f"{p}age % {rng.randint(2, 9)} = {rng.randint(0, 2)}"
    if choice == 11:
        return f"{p}city IN ('{rng.choice(CITIES[:-1])}', '{rng.choice(CITIES[:-1])}')"
    if choice == 12:
        return f"{p}id IN ({', '.join(str(rng.randint(1, 650)) for _ in range(rng.randint(1, 6)))})"
    return f"{p}score * 2 {op} {p}age"


SCALAR_EXPRS = ["p.age * 2", "p.age / 3", "p.age % 7", "-p.age", "p.score + p.age", "p.score / 4", "upper(p.name)",
                "lower(p.city)", "length(p.name)", "p.name || '-' || p.age", "coalesce(p.city, 'none')",
                "ifnull(p.team, -1)", "abs(p.score)", "CASE WHEN p.age > 50 THEN 'old' WHEN p.age IS NULL THEN '?' ELSE 'young' END",
                "p.age > 40", "p.team IS NULL", "substr(p.name, 2, 3)", "nullif(p.team, 3)", "typeof(p.score)",
                "max(p.age, p.team)", "min(p.age, 30)", "p.age BETWEEN 20 AND 40", "p.city LIKE '%a%'"]
AGG_EXPRS = ["COUNT(*)", "COUNT(p.age)", "COUNT(DISTINCT p.city)", "SUM(p.age)", "AVG(p.score)", "MIN(p.name)",
             "MAX(p.score)", "TOTAL(p.age)", "SUM(p.score)", "MAX(p.age) - MIN(p.age)", "COUNT(DISTINCT p.team)"]
GROUP_KEYS = ["p.city", "p.team", "p.age / 10", "p.age IS NULL", "p.city, p.team"]


def random_query(rng):
    where = f" WHERE {random_pred(rng)}" if rng.random() < 0.85 else ""
    kind = rng.random()
    if kind < 0.35:                                         # plain filtered select
        cols = rng.sample(SCALAR_EXPRS, rng.randint(1, 4))
        lim = f" LIMIT {rng.randint(0, 30)}" + (f" OFFSET {rng.randint(0, 20)}" if rng.random() < 0.4 else "") if rng.random() < 0.3 else ""
        order = rng.choice(["p.age", "p.score DESC", "p.name", "p.city DESC", "p.team", "1"])
        return f"SELECT p.id, {', '.join(cols)} FROM people p{where} ORDER BY {order}, p.id{lim}", True
    if kind < 0.45:
        return f"SELECT * FROM people p{where}", False
    if kind < 0.65:                                         # GROUP BY
        key = rng.choice(GROUP_KEYS)
        aggs = rng.sample(AGG_EXPRS, rng.randint(1, 3))
        having = f" HAVING COUNT(*) > {rng.randint(0, 20)}" if rng.random() < 0.4 else ""
        nkeys = key.count(",") + 1
        return f"SELECT {key}, {', '.join(aggs)} FROM people p{where} GROUP BY {key}{having} ORDER BY {', '.join(str(i + 1) for i in range(nkeys))}", True
    if kind < 0.72:                                         # aggregate over everything
        return f"SELECT {', '.join(rng.sample(AGG_EXPRS, rng.randint(1, 4)))} FROM people p{where}", True
    if kind < 0.78:
        col = rng.choice(["p.city", "p.team", "p.age / 10", "p.city, p.team"])
        return f"SELECT DISTINCT {col} FROM people p{where} ORDER BY {', '.join(str(i + 1) for i in range(col.count(',') + 1))}", True
    if kind < 0.9:                                          # joins
        join = rng.choice(["JOIN", "LEFT JOIN"])
        extra = f" AND t.budget > {rng.randint(0, 900)}" if rng.random() < 0.3 else ""
        return (f"SELECT p.id, p.name, t.id, t.title, t.budget FROM people p {join} teams t ON p.team = t.id{extra}"
                f"{where} ORDER BY p.id, t.id"), True
    join = rng.choice(["JOIN", "LEFT JOIN"])                # join + group by
    return (f"SELECT t.title, COUNT(p.id), SUM(p.age), MAX(p.score) FROM teams t {join} people p ON p.team = t.id"
            f"{where.replace('WHERE', 'AND') if join == 'LEFT JOIN' and where else where} GROUP BY t.title ORDER BY 1"), True


def random_change(rng, next_id):
    r = rng.random()
    if r < 0.35:
        sets = rng.choice(["age = age + 1", "score = score * 1.5", f"city = '{rng.choice(CITIES[:-1])}'", "team = NULL",
                           f"team = {rng.randint(1, 12)}", "name = upper(name)", "age = NULL, score = 0", "age = team * 3"])
        return f"UPDATE people SET {sets} WHERE {random_pred(rng, p='')}", next_id
    if r < 0.55:
        return f"DELETE FROM people WHERE {random_pred(rng, p='')}", next_id
    if r < 0.6:
        new = next_id + rng.randint(1, 5)
        return f"UPDATE people SET id = {new} WHERE id = {rng.randint(1, next_id)}", new + 1
    rows = []
    for _ in range(rng.randint(1, 8)):
        rows.append(f"({next_id}, {sql_lit(random_value(rng, 'name'))}, {sql_lit(random_value(rng, 'age'))}, "
                    f"{sql_lit(random_value(rng, 'score'))}, {sql_lit(random_value(rng, 'city'))}, {sql_lit(random_value(rng, 'team'))})")
        next_id += 1
    return f"INSERT INTO people VALUES {', '.join(rows)}", next_id


def normalize(rows, ordered):
    out = [tuple((type(v).__name__, v) for v in r) for r in rows]
    return out if ordered else sorted(out, key=lambda r: repr([(t, round(v, 6) if t == "float" else v) for t, v in r]))


def same_results(a, b):
    """Exact match, except decimals may differ in the last few digits (summation order)."""
    if isinstance(a, str) or len(a) != len(b):
        return False
    for ra, rb in zip(a, b):
        if len(ra) != len(rb):
            return False
        for (ta, va), (tb, vb) in zip(ra, rb):
            if ta != tb:
                return False
            if ta == "float" and not math.isclose(va, vb, rel_tol=1e-9, abs_tol=1e-9):
                return False
            if ta != "float" and va != vb:
                return False
    return True


def test_differential(rounds=12, queries_per_round=250, seed=2026):
    print("\nDifferential testing vs SQLite:")
    rng = random.Random(seed)
    ours = Database(path("diff.db"), sync=False)
    ref = sqlite3.connect(":memory:")
    schema = """
        CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT, age INTEGER, score REAL, city TEXT, team INTEGER);
        CREATE TABLE teams (id INTEGER PRIMARY KEY, title TEXT, budget INTEGER);
        CREATE INDEX people_age ON people(age);
        CREATE INDEX people_city ON people(city);
        CREATE INDEX people_team ON people(team);
        CREATE INDEX teams_budget ON teams(budget);
    """
    ours.execute(schema)
    ref.executescript(schema)
    setup = []
    ids = list(range(1, 601))
    rng.shuffle(ids)
    for i in ids:
        setup.append(f"INSERT INTO people VALUES ({i}, {sql_lit(random_value(rng, 'name'))}, {sql_lit(random_value(rng, 'age'))}, "
                     f"{sql_lit(random_value(rng, 'score'))}, {sql_lit(random_value(rng, 'city'))}, {sql_lit(random_value(rng, 'team'))})")
    for i in range(1, 11):                                   # teams 11 and 12 don't exist: tests LEFT JOIN
        setup.append(f"INSERT INTO teams VALUES ({i}, 'Team {chr(64 + i)}', {rng.randint(100, 1000)})")
    ours.execute("BEGIN; " + "; ".join(setup) + "; COMMIT")
    for s in setup:
        ref.execute(s)

    next_id, total_q, total_w, mismatches = 601, 0, 0, 0
    for rnd in range(rounds):
        for _ in range(queries_per_round):
            sql, ordered = random_query(rng)
            total_q += 1
            try:
                got = normalize(ours.query(sql), ordered)
            except Exception as e:      # noqa: BLE001 - any crash is a bug worth printing
                got = f"ERROR {type(e).__name__}: {e}"
            want = normalize(ref.execute(sql).fetchall(), ordered)
            if not same_results(got, want):
                mismatches += 1
                if mismatches <= 5:
                    print(f"        MISMATCH: {sql}\n          franzdb: {str(got)[:300]}\n          sqlite:  {str(want)[:300]}")
        for _ in range(25):
            sql, next_id = random_change(rng, next_id)
            total_w += 1
            err_ours = err_ref = None
            try:
                ours.execute(sql)
            except DBError as e:
                err_ours = str(e)
            try:
                ref.execute(sql)
            except sqlite3.Error as e:
                err_ref = str(e)
            if (err_ours is None) != (err_ref is None):
                mismatches += 1
                if mismatches <= 5:
                    print(f"        MISMATCH on write: {sql}\n          franzdb: {err_ours}\n          sqlite:  {err_ref}")
        same = same_results(normalize(ours.query("SELECT * FROM people ORDER BY id"), True),
                            normalize(ref.execute("SELECT * FROM people ORDER BY id").fetchall(), True))
        if not same:
            mismatches += 1
            print(f"        TABLE CONTENTS DIFFER after round {rnd}")
    integrity = ours.integrity_check()
    check(f"{total_q} random queries + {total_w} random INSERT/UPDATE/DELETE: {total_q + total_w - mismatches} identical to SQLite",
          mismatches == 0)
    check("database still passes the integrity check after all the random changes", integrity == ["ok"], integrity)
    ours.close()


if __name__ == "__main__":
    t0 = time.time()
    test_sql()
    test_transactions()
    test_storage()
    test_crash_recovery()
    test_differential()
    print(f"\n{PASSED[0]} passed, {FAILED[0]} failed  ({time.time() - t0:.0f}s)")
    print("ALL TESTS PASSED" if FAILED[0] == 0 else "SOME TESTS FAILED")
    sys.exit(1 if FAILED[0] else 0)
