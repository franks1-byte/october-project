# FranzDB: a SQL database engine from scratch

A working relational database written in pure Python with **no dependencies**. It stores data on disk in **B+trees**, has **indexes**, a **query planner**, **transactions**, and a **rollback journal** that survives power cuts. It is tested query-for-query against **SQLite**: 19,800 random statements, and every result was identical.

```
franzdb> SELECT s.name, round(AVG(e.grade), 2) AS gwa, COUNT(*) AS subjects
     ...>   FROM students s JOIN enrollments e ON e.student_id = s.id
     ...>   GROUP BY s.id ORDER BY gwa LIMIT 3;
+-------+------+----------+
| name  | gwa  | subjects |
+-------+------+----------+
| Carlo | 1.08 |        3 |
| Faith | 1.38 |        2 |
| Gab   | 1.42 |        3 |
+-------+------+----------+
3 rows

franzdb> EXPLAIN SELECT s.name, e.grade FROM students s JOIN enrollments e ON e.student_id = s.id WHERE s.id = 3;
| SEARCH students AS s USING INTEGER PRIMARY KEY (rowid=?)            |
|   SEARCH enrollments AS e USING INDEX enroll_student (student_id=?) |
```

## Run it
```bash
python franzdb.py school.db          # opens (or creates) a database file
franzdb> .read examples/school.sql   # load the sample data
franzdb> .help
```
Use it from Python:
```python
from franzdb import Database
db = Database("shop.db")
db.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT UNIQUE, price REAL)")
db.execute("INSERT INTO items (name, price) VALUES ('pen', 12.5), ('notebook', 45)")
print(db.query("SELECT name FROM items WHERE price < 20"))   # [('pen',)]
```
Run the tests (about 30 seconds): `python test_franzdb.py`

## SQL support
| Area | Supported |
|---|---|
| Tables | `CREATE TABLE [IF NOT EXISTS]`, `DROP TABLE`, column types with SQLite-style affinity |
| Constraints | `PRIMARY KEY` (`INTEGER PRIMARY KEY` = the rowid), `UNIQUE`, `NOT NULL`, `DEFAULT` |
| Indexes | `CREATE [UNIQUE] INDEX`, `DROP INDEX`, automatic indexes for UNIQUE columns |
| Queries | `SELECT [DISTINCT]`, `WHERE`, `JOIN`, `LEFT JOIN`, self joins, `GROUP BY`, `HAVING`, `ORDER BY` (by expression, alias, or column number), `LIMIT`/`OFFSET` |
| Expressions | `+ - * / %`, `\|\|`, comparisons, `AND OR NOT` with three-valued NULL logic, `IS [NOT] NULL`, `[NOT] IN`, `[NOT] LIKE`, `[NOT] BETWEEN`, `CASE` |
| Aggregates | `COUNT(*)`, `COUNT([DISTINCT] x)`, `SUM`, `AVG`, `MIN`, `MAX`, `TOTAL`, `GROUP_CONCAT` |
| Functions | `abs length upper lower substr round coalesce ifnull nullif typeof min max` |
| Writes | `INSERT … VALUES` (many rows), `INSERT … SELECT`, `UPDATE`, `DELETE` |
| Transactions | `BEGIN`, `COMMIT`, `ROLLBACK`; every statement is atomic on its own |
| Tools | `EXPLAIN`, plus the shell commands `.tables .schema .indexes .check .read .timer` |

## How it works
```
SQL text → tokenizer → recursive-descent parser → AST → planner → compiled closures → executor
                                                                                       │
                     B+tree (tables, indexes) → page cache → pager + rollback journal → disk
```

### Storage: pages and B+trees
- The file is a sequence of **4 KB pages**. Page 0 holds the schema; every other page is a B+tree node or a free page.
- Each **table** is a B+tree keyed by `rowid`. Each **index** is a B+tree keyed by `(value, rowid)`, so index entries for the same value stay together.
- Leaves are **linked left to right**, so range scans (`age BETWEEN 20 AND 30`) walk sideways instead of climbing back up the tree.
- Nodes **split by bytes, not by count**, so both halves always fit, even with very different row sizes.
- Dropped tables put their pages on a **free list**, and new data reuses them instead of growing the file.

### The query planner
For each table in the `FROM` clause, the planner looks at the `WHERE` and `ON` conditions and picks the cheapest access path:

| Access path | When it is used |
|---|---|
| Rowid lookup | `id = 5`, `id IN (1, 2, 3)` |
| Rowid range | `id BETWEEN 100 AND 200` |
| Index lookup | `email = 'a@b.c'` (UNIQUE indexes are preferred) |
| Index range | `age > 30`, `price BETWEEN 10 AND 20` |
| Full scan | nothing better is available |

In joins, the inner table is looked up **using values from the outer row**. That's how `JOIN enrollments e ON e.student_id = s.id` uses the index once per student. In a benchmark, an index lookup took **0.14 ms**, against **54 ms** for scanning 20,000 rows.

### Transactions and crash safety
- Changes stay in the page cache until `COMMIT`.
- Every write statement records an undo copy of each page **before** changing it. If the statement fails halfway (for example, the 3rd row of an `INSERT` breaks a `UNIQUE` rule), it is undone completely.
- **Commit protocol (rollback journal):** first the *original* pages are copied to `db-journal` and flushed to disk, then the new pages are written, then the journal is deleted. If the power fails mid-commit, the next open finds the journal and puts the old pages back, so the database is never half-updated.

### NULLs and types, the SQLite way
- `NULL = NULL` is `NULL`, `NULL AND 0` is `0`, `1 IN (2, NULL)` is `NULL`.
- `7 / 2 = 3` (integer division truncates toward zero) and `-7 % 3 = -1`.
- Sort order is `NULL < numbers < text`.
- Column **affinity**: `'42'` inserted into an `INTEGER` column becomes the number `42`.
- `SUM` and `AVG` use **compensated (Neumaier) summation**, so adding many decimals doesn't drift.

## Testing
`test_franzdb.py` runs 50 checks:

| Suite | What it proves |
|---|---|
| SQL behaviour | NULL logic, integer math, joins, grouping, ordering, affinity, defaults, and 10 error messages |
| Transactions | `ROLLBACK` works, a failing statement changes nothing, committed data survives reopening |
| Storage | 20,000 rows inserted in random order (tree depth 3, ~940 pages) survive reopening; bulk update and delete keep indexes in sync; dropped pages are reused |
| Crash recovery | The power is cut after 0, 1, 3, 10, and 40 page writes of a big commit; every time, the database reopens **exactly** as it was before |
| **Differential vs SQLite** | Random queries (filters, joins, `LEFT JOIN`, `GROUP BY`, `HAVING`, `DISTINCT`, functions, `LIMIT`) and random `INSERT`/`UPDATE`/`DELETE` run on both databases, and **every result must match** |

An **integrity checker** (`.check` in the shell) verifies every B+tree invariant, confirms that each index matches its table exactly, and checks that every page in the file belongs to exactly one tree or to the free list.

## Limits (by design, to keep it readable)
- Rows are limited to about 1 KB.
- Deleted rows free space inside pages but don't merge half-empty pages.
- One writer at a time, with no concurrent connections.
- No subqueries, views, or `ALTER TABLE`.
- Rowids for new rows continue from the highest rowid ever used.
