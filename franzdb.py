#!/usr/bin/env python3
"""
FranzDB: a relational SQL database engine written from scratch in pure Python (no dependencies).

Storage    4 KB pages in a single file. Tables are B+trees keyed by rowid, indexes are B+trees keyed by
           (value, rowid). A page cache, a free-page list (dropped tables give their pages back), and a
           rollback journal that makes every commit atomic: pull the plug mid-commit and the database
           comes back exactly as it was before.
SQL        CREATE/DROP TABLE and INDEX, INSERT (VALUES or SELECT), SELECT with WHERE, JOIN / LEFT JOIN,
           GROUP BY, HAVING, aggregates, DISTINCT, ORDER BY, LIMIT/OFFSET, CASE, LIKE, IN, BETWEEN,
           UPDATE, DELETE, BEGIN/COMMIT/ROLLBACK, EXPLAIN, constraints (PRIMARY KEY, UNIQUE, NOT NULL, DEFAULT).
Planner    Uses rowid lookups and index searches from WHERE / ON clauses instead of scanning whole tables.

Shell      python franzdb.py school.db
Library    db = Database("school.db"); db.execute("SELECT ...").rows
"""
import bisect
import copy
import functools
import json
import math
import os
import re
import struct
import sys
import tempfile
import time

PAGE_SIZE = 4096
MAX_ENTRY = 1000          # bytes per row (or index entry); keeps several entries on every page
MAGIC = b"FRANZDB1"
JOURNAL_MAGIC = b"FZJOURN1"
INT64_MIN, INT64_MAX = -(2 ** 63), 2 ** 63 - 1


class DBError(Exception):
    pass


class SimulatedCrash(Exception):
    """Raised by the crash-test hook to pretend the power went out in the middle of a commit."""


# ============================================================================ values
def encode_values(vals):
    out = [struct.pack(">H", len(vals))]
    for v in vals:
        if v is None:
            out.append(b"N")
        elif isinstance(v, int):
            out.append(b"I" + struct.pack(">q", v))
        elif isinstance(v, float):
            out.append(b"F" + struct.pack(">d", v))
        else:
            b = v.encode("utf-8")
            out.append(b"S" + struct.pack(">I", len(b)) + b)
    return b"".join(out)


def decode_values(buf, pos=0):
    (n,) = struct.unpack_from(">H", buf, pos)
    pos += 2
    vals = []
    for _ in range(n):
        tag = buf[pos]
        pos += 1
        if tag == 78:      # N
            vals.append(None)
        elif tag == 73:    # I
            vals.append(struct.unpack_from(">q", buf, pos)[0])
            pos += 8
        elif tag == 70:    # F
            vals.append(struct.unpack_from(">d", buf, pos)[0])
            pos += 8
        else:              # S
            (ln,) = struct.unpack_from(">I", buf, pos)
            pos += 4
            vals.append(bytes(buf[pos:pos + ln]).decode("utf-8"))
            pos += ln
    return tuple(vals), pos


def vkey(v):
    """The one sort order used everywhere (same as SQLite): NULL < numbers < text."""
    if v is None:
        return (0, 0)
    if isinstance(v, str):
        return (2, v)
    return (1, v)


def tkey(t):
    return tuple(vkey(v) for v in t)


NUM_PREFIX = re.compile(r"\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)")
INT_TEXT = re.compile(r"\s*[-+]?\d+\s*")
NUM_TEXT = re.compile(r"\s*[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?\s*")


def to_num(v):
    """Text used as a number reads its numeric prefix, like SQLite ('12abc' -> 12, 'abc' -> 0)."""
    if v is None or isinstance(v, (int, float)):
        return v
    m = NUM_PREFIX.match(v)
    if not m:
        return 0
    s = m.group(1)
    if re.fullmatch(r"[-+]?\d+", s):
        n = int(s)
        return n if INT64_MIN <= n <= INT64_MAX else float(n)
    return float(s)


def to_bool(v):
    return None if v is None else to_num(v) != 0


def fmt_real(x):
    if math.isinf(x):
        return "Inf" if x > 0 else "-Inf"
    s = "%.15g" % x
    if "e" in s:
        mant, ex = s.split("e")
        if "." not in mant:
            mant += ".0"
        e = int(ex)
        return f"{mant}e{'+' if e >= 0 else '-'}{abs(e):02d}"
    return s if "." in s or "n" in s else s + ".0"


def to_text(v):
    if v is None or isinstance(v, str):
        return v
    return str(v) if isinstance(v, int) else fmt_real(v)


def affinity(ctype, v):
    """Convert a value to fit a column's declared type, following SQLite's affinity rules."""
    if v is None:
        return None
    t = (ctype or "").upper()
    if "INT" in t:
        kind = "integer"
    elif "CHAR" in t or "CLOB" in t or "TEXT" in t:
        kind = "text"
    elif t == "" or "BLOB" in t:
        return v
    elif "REAL" in t or "FLOA" in t or "DOUB" in t:
        kind = "real"
    else:
        kind = "numeric"
    if kind == "text":
        return to_text(v)
    if isinstance(v, str):
        if INT_TEXT.fullmatch(v):
            n = int(v)
            v = n if INT64_MIN <= n <= INT64_MAX else float(n)
        elif NUM_TEXT.fullmatch(v):
            v = float(v)
        else:
            return v      # not a number: stored as text
    if kind == "real":
        return float(v)
    if isinstance(v, float) and v.is_integer() and INT64_MIN <= v <= INT64_MAX:
        return int(v)
    return v


# ============================================================================ pages and nodes
class Node:
    """One page: 'L' = B+tree leaf, 'B' = B+tree branch, 'X' = free page."""
    __slots__ = ("pid", "kind", "keys", "skeys", "vals", "children", "next")

    def __init__(self, pid, kind):
        self.pid, self.kind = pid, kind
        self.reset(kind)

    def reset(self, kind):
        self.kind = kind
        self.keys, self.skeys, self.vals, self.children, self.next = [], [], [], [], 0


def serialize(node):
    if node.kind == "X":
        return b"X" + struct.pack(">I", node.next)
    parts = [node.kind.encode(), struct.pack(">HI", len(node.keys), node.next)]
    if node.kind == "L":
        for k, v in zip(node.keys, node.vals):
            parts.append(encode_values(k))
            parts.append(struct.pack(">H", len(v)) + v)
    else:
        for k in node.keys:
            parts.append(encode_values(k))
        parts.append(struct.pack(f">{len(node.children)}I", *node.children))
    return b"".join(parts)


def deserialize(pid, data):
    kind = chr(data[0])
    if kind not in "LBX":
        raise DBError(f"database disk image is malformed (page {pid})")
    node = Node(pid, kind)
    if kind == "X":
        node.next = struct.unpack_from(">I", data, 1)[0]
        return node
    n, node.next = struct.unpack_from(">HI", data, 1)
    pos = 7
    for _ in range(n):
        k, pos = decode_values(data, pos)
        node.keys.append(k)
        if kind == "L":
            (ln,) = struct.unpack_from(">H", data, pos)
            pos += 2
            node.vals.append(bytes(data[pos:pos + ln]))
            pos += ln
    if kind == "B":
        node.children = list(struct.unpack_from(f">{n + 1}I", data, pos))
    node.skeys = [tkey(k) for k in node.keys]
    return node


class Pager:
    """
    Reads and writes pages, keeps them in a cache, and makes commits atomic with a rollback journal:
      1. copy the ORIGINAL contents of every page about to change into  <db>-journal, and fsync it
      2. write the new pages into the database file, and fsync it
      3. delete the journal (this is the moment the commit happens)
    If we crash during step 2, the next open finds the journal and copies the old pages back.
    """

    def __init__(self, path, sync=True):
        self.path, self.journal, self.sync = path, path + "-journal", sync
        self.crash_after = None   # test hook: number of page writes before a simulated crash
        self.recovered = self.recover()
        fresh = not os.path.exists(path) or os.path.getsize(path) == 0
        self.f = open(path, "w+b" if fresh else "r+b")
        self.cache, self.dirty, self.undo = {}, set(), None
        if fresh:
            self.cat = {"tables": {}, "indexes": {}, "num_pages": 1, "free_head": 0}
            self.commit()
        else:
            self.load_header()

    # ---------------------------------------------------------------- header / catalog
    def header_bytes(self):
        js = json.dumps(self.cat, separators=(",", ":")).encode()
        if len(js) > PAGE_SIZE - 12:
            raise DBError("schema too large")
        return MAGIC + struct.pack(">I", len(js)) + js

    def load_header(self):
        self.f.seek(0)
        data = self.f.read(PAGE_SIZE)
        if data[:8] != MAGIC:
            raise DBError("file is not a FranzDB database")
        (ln,) = struct.unpack_from(">I", data, 8)
        self.cat = json.loads(data[12:12 + ln])

    # ---------------------------------------------------------------- pages
    def get(self, pid):
        node = self.cache.get(pid)
        if node is None:
            if not 0 < pid < self.cat["num_pages"]:
                raise DBError(f"database disk image is malformed (bad page number {pid})")
            self.f.seek(pid * PAGE_SIZE)
            node = deserialize(pid, self.f.read(PAGE_SIZE))
            self.cache[pid] = node
        return node

    def touch(self, node):
        """Call BEFORE changing a page: remembers the old version so a failed statement can be undone."""
        if self.undo is not None and node.pid not in self.undo:
            self.undo[node.pid] = serialize(node)
        self.dirty.add(node.pid)

    def allocate(self, kind):
        head = self.cat["free_head"]
        if head:                                  # reuse a page from the free list
            node = self.get(head)
            self.touch(node)
            self.cat["free_head"] = node.next
            node.reset(kind)
            return node
        pid = self.cat["num_pages"]
        self.cat["num_pages"] += 1
        node = Node(pid, kind)
        self.cache[pid] = node
        if self.undo is not None:
            self.undo.setdefault(pid, None)       # None = "this page did not exist before"
        self.dirty.add(pid)
        return node

    def free(self, pid):
        node = self.get(pid)
        self.touch(node)
        node.reset("X")
        node.next = self.cat["free_head"]
        self.cat["free_head"] = pid

    # ---------------------------------------------------------------- statement atomicity
    def begin_statement(self):
        self.undo = {}
        self.undo_cat = copy.deepcopy(self.cat)

    def end_statement(self):
        self.undo = None

    def undo_statement(self):
        for pid, data in self.undo.items():
            if data is None:
                self.cache.pop(pid, None)
                self.dirty.discard(pid)
            else:
                self.cache[pid] = deserialize(pid, data)
        self.cat = self.undo_cat
        self.undo = None

    # ---------------------------------------------------------------- transactions
    def commit(self):
        pages = {0: self.header_bytes()}
        for pid in sorted(self.dirty):
            data = serialize(self.cache[pid])
            if len(data) > PAGE_SIZE:
                raise DBError(f"internal error: page {pid} overflowed")
            pages[pid] = data
        self.f.seek(0, 2)
        orig_size = self.f.tell()
        saved = [pid for pid in pages if (pid + 1) * PAGE_SIZE <= orig_size]
        with open(self.journal, "wb") as j:
            j.write(JOURNAL_MAGIC + struct.pack(">QI", orig_size, len(saved)))
            for pid in saved:
                self.f.seek(pid * PAGE_SIZE)
                j.write(struct.pack(">I", pid) + self.f.read(PAGE_SIZE).ljust(PAGE_SIZE, b"\0"))
            j.write(b"DONE")
            j.flush()
            if self.sync:
                os.fsync(j.fileno())
        for n, (pid, data) in enumerate(pages.items()):
            if self.crash_after is not None and n >= self.crash_after:
                raise SimulatedCrash(f"simulated power cut after {n} of {len(pages)} page writes")
            self.f.seek(pid * PAGE_SIZE)
            self.f.write(data.ljust(PAGE_SIZE, b"\0"))
        self.f.flush()
        if self.sync:
            os.fsync(self.f.fileno())
        os.remove(self.journal)
        self.dirty.clear()

    def rollback(self):
        self.cache.clear()
        self.dirty.clear()
        self.undo = None
        self.load_header()

    def recover(self):
        """Run on open: if a journal is left over from a crash, put the old pages back."""
        if not os.path.exists(self.journal):
            return False
        with open(self.journal, "rb") as j:
            data = j.read()
        complete = data.startswith(JOURNAL_MAGIC) and data.endswith(b"DONE") and len(data) >= 24
        if complete:
            orig_size, n = struct.unpack_from(">QI", data, 8)
            complete = len(data) == 20 + n * (4 + PAGE_SIZE) + 4
        if not complete:
            # the crash happened while writing the journal, before the database file was touched
            os.remove(self.journal)
            return False
        with open(self.path, "r+b") as f:
            pos = 20
            for _ in range(n):
                (pid,) = struct.unpack_from(">I", data, pos)
                f.seek(pid * PAGE_SIZE)
                f.write(data[pos + 4:pos + 4 + PAGE_SIZE])
                pos += 4 + PAGE_SIZE
            f.truncate(orig_size)
            f.flush()
            os.fsync(f.fileno())
        os.remove(self.journal)
        return True

    def close(self):
        self.f.close()


# ============================================================================ B+tree
class BTree:
    """
    Keys are tuples of values, kept sorted with vkey order. Leaves hold (key, value-bytes) and are linked
    left-to-right for fast range scans. Branch nodes hold separator keys: child i holds keys < keys[i].
    Pages split when they no longer fit in 4 KB. Deletes are lazy (no merging); the freed space in a page
    is reused by later inserts into the same key range.
    """

    def __init__(self, pager, entry):
        self.p, self.entry = pager, entry     # entry is the catalog dict that holds "root"

    def _leaf_for(self, sk):
        node = self.p.get(self.entry["root"])
        while node.kind == "B":
            node = self.p.get(node.children[bisect.bisect_right(node.skeys, sk)])
        return node

    def get(self, key):
        sk = tkey(key)
        leaf = self._leaf_for(sk)
        i = bisect.bisect_left(leaf.skeys, sk)
        if i < len(leaf.skeys) and leaf.skeys[i] == sk:
            return leaf.vals[i]
        return None

    def insert(self, key, val):
        if len(encode_values(key)) + len(val) > MAX_ENTRY:
            raise DBError(f"row too big (the limit is about {MAX_ENTRY} bytes per row)")
        split = self._insert(self.p.get(self.entry["root"]), key, tkey(key), val)
        if split:                                 # the root split: the tree grows one level taller
            sep, sep_sk, right = split
            root = self.p.allocate("B")
            root.keys, root.skeys, root.children = [sep], [sep_sk], [self.entry["root"], right]
            self.entry["root"] = root.pid

    def _insert(self, node, key, sk, val):
        if node.kind == "L":
            i = bisect.bisect_left(node.skeys, sk)
            self.p.touch(node)
            if i < len(node.skeys) and node.skeys[i] == sk:
                node.vals[i] = val
            else:
                node.keys.insert(i, key)
                node.skeys.insert(i, sk)
                node.vals.insert(i, val)
        else:
            i = bisect.bisect_right(node.skeys, sk)
            split = self._insert(self.p.get(node.children[i]), key, sk, val)
            if not split:
                return None
            sep, sep_sk, right = split
            self.p.touch(node)
            node.keys.insert(i, sep)
            node.skeys.insert(i, sep_sk)
            node.children.insert(i + 1, right)
        if len(serialize(node)) <= PAGE_SIZE:
            return None
        return self._split(node)

    def _split(self, node):
        """Split by bytes (not by count) so both halves are guaranteed to fit in a page."""
        right = self.p.allocate(node.kind)
        n = len(node.keys)
        if node.kind == "L":
            sizes = [len(encode_values(k)) + 2 + len(v) for k, v in zip(node.keys, node.vals)]
        else:
            sizes = [len(encode_values(k)) + 4 for k in node.keys]
        half, acc, mid = sum(sizes) / 2, 0, n // 2
        for i, s in enumerate(sizes):
            if acc + s > half:
                mid = i
                break
            acc += s
        if node.kind == "L":
            mid = min(max(mid, 1), n - 1)
            right.keys, right.skeys, right.vals = node.keys[mid:], node.skeys[mid:], node.vals[mid:]
            del node.keys[mid:], node.skeys[mid:], node.vals[mid:]
            right.next, node.next = node.next, right.pid
            return right.keys[0], right.skeys[0], right.pid
        mid = min(max(mid, 1), n - 2)
        sep, sep_sk = node.keys[mid], node.skeys[mid]
        right.keys, right.skeys, right.children = node.keys[mid + 1:], node.skeys[mid + 1:], node.children[mid + 1:]
        del node.keys[mid:], node.skeys[mid:], node.children[mid + 1:]
        return sep, sep_sk, right.pid

    def delete(self, key):
        sk = tkey(key)
        leaf = self._leaf_for(sk)
        i = bisect.bisect_left(leaf.skeys, sk)
        if i < len(leaf.skeys) and leaf.skeys[i] == sk:
            self.p.touch(leaf)
            del leaf.keys[i], leaf.skeys[i], leaf.vals[i]
            return True
        return False

    def scan(self, start=None):
        """Yields (key, value) in key order, starting at the first key >= start."""
        if start is None:
            node = self.p.get(self.entry["root"])
            while node.kind == "B":
                node = self.p.get(node.children[0])
            i = 0
        else:
            sk = tkey(start)
            node = self._leaf_for(sk)
            i = bisect.bisect_left(node.skeys, sk)
        while True:
            while i < len(node.keys):
                yield node.keys[i], node.vals[i]
                i += 1
            if not node.next:
                return
            node = self.p.get(node.next)
            i = 0

    def pages(self):
        out, stack = [], [self.entry["root"]]
        while stack:
            pid = stack.pop()
            out.append(pid)
            node = self.p.get(pid)
            if node.kind == "B":
                stack.extend(node.children)
        return out

    def check(self):
        """Verify every B+tree invariant. Returns (number of keys, depth); raises DBError if broken."""
        leaves = []

        def walk(pid, lo, hi, depth):
            node = self.p.get(pid)
            if node.kind not in "LB":
                raise DBError(f"page {pid} is in a tree but marked free")
            sk = node.skeys
            if any(not a < b for a, b in zip(sk, sk[1:])):
                raise DBError(f"page {pid}: keys out of order")
            if sk and ((lo is not None and sk[0] < lo) or (hi is not None and sk[-1] >= hi)):
                raise DBError(f"page {pid}: key outside its parent's range")
            if len(serialize(node)) > PAGE_SIZE:
                raise DBError(f"page {pid}: overflow")
            if node.kind == "L":
                leaves.append((depth, node))
                return
            if len(node.children) != len(node.keys) + 1:
                raise DBError(f"page {pid}: wrong number of children")
            bounds = [lo] + sk + [hi]
            for i, child in enumerate(node.children):
                walk(child, bounds[i], bounds[i + 1], depth + 1)

        walk(self.entry["root"], None, None, 1)
        depths = {d for d, _ in leaves}
        if len(depths) != 1:
            raise DBError("leaves are at different depths")
        for (_, a), (_, b) in zip(leaves, leaves[1:]):
            if a.next != b.pid:
                raise DBError(f"leaf chain broken at page {a.pid}")
        if leaves[-1][1].next != 0:
            raise DBError("last leaf points somewhere")
        return sum(len(n.keys) for _, n in leaves), depths.pop()


# ============================================================================ SQL tokenizer + parser
TOKEN_RE = re.compile(r"""
    (?P<ws>\s+|--[^\n]*|/\*.*?\*/) |
    (?P<num>(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?) |
    (?P<str>'(?:[^']|'')*') |
    (?P<qid>"(?:[^"]|"")*") |
    (?P<id>[A-Za-z_][A-Za-z_0-9]*) |
    (?P<op><>|!=|==|<=|>=|\|\||[-+*/%=<>(),.;])
""", re.VERBOSE | re.DOTALL)

RESERVED = {
    "SELECT", "FROM", "WHERE", "GROUP", "BY", "HAVING", "ORDER", "LIMIT", "OFFSET", "INSERT", "INTO", "VALUES",
    "UPDATE", "SET", "DELETE", "CREATE", "TABLE", "INDEX", "DROP", "AND", "OR", "NOT", "NULL", "IS", "IN",
    "LIKE", "BETWEEN", "AS", "ON", "JOIN", "LEFT", "INNER", "OUTER", "CROSS", "DISTINCT", "ALL", "ASC", "DESC",
    "CASE", "WHEN", "THEN", "ELSE", "END", "PRIMARY", "KEY", "UNIQUE", "DEFAULT", "BEGIN", "COMMIT",
    "ROLLBACK", "EXPLAIN", "IF", "EXISTS", "TRUE", "FALSE",
}
AGGREGATES = {"count", "sum", "avg", "min", "max", "total", "group_concat"}


def tokenize(sql):
    toks, pos = [], 0
    while pos < len(sql):
        m = TOKEN_RE.match(sql, pos)
        if not m:
            raise DBError(f'unrecognized token: "{sql[pos:pos + 10]}"')
        kind, text = m.lastgroup, m.group()
        if kind == "num":
            if re.fullmatch(r"\d+", text):
                n = int(text)
                value = n if n <= INT64_MAX else float(n)
            else:
                value = float(text)
            toks.append(("num", value, m.start(), m.end()))
        elif kind == "str":
            toks.append(("str", text[1:-1].replace("''", "'"), m.start(), m.end()))
        elif kind == "qid":
            toks.append(("qid", text[1:-1].replace('""', '"'), m.start(), m.end()))
        elif kind in ("id", "op"):
            toks.append((kind, text, m.start(), m.end()))
        pos = m.end()
    toks.append(("eof", None, len(sql), len(sql)))
    return toks


class Parser:
    def __init__(self, sql):
        self.sql, self.t, self.i = sql, tokenize(sql), 0

    # ---------------------------------------------------------------- helpers
    def peek(self, k=0):
        return self.t[min(self.i + k, len(self.t) - 1)]

    def error(self):
        tk = self.peek()
        if tk[0] == "eof":
            return DBError("incomplete input")
        return DBError(f'near "{self.sql[tk[2]:tk[3]]}": syntax error')

    def at_kw(self, *words, k=0):
        tk = self.peek(k)
        return tk[0] == "id" and tk[1].upper() in words

    def kw(self, *words):
        if self.at_kw(*words):
            self.i += 1
            return True
        return False

    def expect_kw(self, word):
        if not self.kw(word):
            raise self.error()

    def at_op(self, op, k=0):
        tk = self.peek(k)
        return tk[0] == "op" and tk[1] == op

    def op(self, op):
        if self.at_op(op):
            self.i += 1
            return True
        return False

    def expect_op(self, op):
        if not self.op(op):
            raise self.error()

    def at_ident(self, k=0):
        tk = self.peek(k)
        return tk[0] == "qid" or (tk[0] == "id" and tk[1].upper() not in RESERVED)

    def ident(self):
        if not self.at_ident():
            raise self.error()
        tk = self.t[self.i]
        self.i += 1
        return tk[1]

    # ---------------------------------------------------------------- statements
    def statements(self):
        out = []
        while True:
            while self.op(";"):
                pass
            if self.peek()[0] == "eof":
                return out
            out.append(self.statement())
            if not self.op(";") and self.peek()[0] != "eof":
                raise self.error()

    def statement(self):
        if self.kw("EXPLAIN"):
            return ("explain", self.select())
        if self.at_kw("SELECT"):
            return self.select()
        if self.kw("INSERT"):
            return self.insert()
        if self.kw("UPDATE"):
            return self.update()
        if self.kw("DELETE"):
            self.expect_kw("FROM")
            table = self.ident()
            where = self.expr() if self.kw("WHERE") else None
            return ("delete", table, where)
        if self.kw("CREATE"):
            return self.create()
        if self.kw("DROP"):
            what = "table" if self.kw("TABLE") else "index" if self.kw("INDEX") else None
            if not what:
                raise self.error()
            if_exists = self.kw("IF") and (self.expect_kw("EXISTS") or True)
            return ("drop", what, self.ident(), if_exists)
        for word, kind in (("BEGIN", "begin"), ("COMMIT", "commit"), ("END", "commit"), ("ROLLBACK", "rollback")):
            if self.kw(word):
                self.kw("TRANSACTION")
                return (kind,)
        raise self.error()

    def select(self):
        self.expect_kw("SELECT")
        q = {"distinct": self.kw("DISTINCT"), "cols": [], "from": [], "where": None, "group": [],
             "having": None, "order": [], "limit": None, "offset": None}
        self.kw("ALL")
        while True:
            if self.op("*"):
                q["cols"].append(("star", None))
            elif self.at_ident() and self.at_op(".", 1) and self.at_op("*", 2):
                table = self.ident()
                self.i += 2
                q["cols"].append(("star", table))
            else:
                start = self.peek()[2]
                e = self.expr()
                text = self.sql[start:self.t[self.i - 1][3]]
                alias = None
                if self.kw("AS"):
                    if self.peek()[0] == "str":
                        alias = self.peek()[1]
                        self.i += 1
                    else:
                        alias = self.ident()
                elif self.at_ident():
                    alias = self.ident()
                q["cols"].append(("expr", e, alias, text))
            if not self.op(","):
                break
        if self.kw("FROM"):
            q["from"].append(self.table_ref("first", None))
            while True:
                if self.op(","):
                    q["from"].append(self.table_ref("inner", None))
                    continue
                if self.kw("LEFT"):
                    self.kw("OUTER")
                    self.expect_kw("JOIN")
                    kind = "left"
                elif self.kw("INNER") or self.kw("CROSS"):
                    self.expect_kw("JOIN")
                    kind = "inner"
                elif self.kw("JOIN"):
                    kind = "inner"
                else:
                    break
                name, alias = self.table_name_alias()
                on = self.expr() if self.kw("ON") else None
                q["from"].append({"table": name, "alias": alias, "kind": kind, "on": on})
        if self.kw("WHERE"):
            q["where"] = self.expr()
        if self.kw("GROUP"):
            self.expect_kw("BY")
            q["group"] = self.expr_list()
        if self.kw("HAVING"):
            q["having"] = self.expr()
        if self.kw("ORDER"):
            self.expect_kw("BY")
            while True:
                e = self.expr()
                desc = self.kw("DESC")
                if not desc:
                    self.kw("ASC")
                q["order"].append((e, desc))
                if not self.op(","):
                    break
        if self.kw("LIMIT"):
            first = self.expr()
            if self.kw("OFFSET"):
                q["limit"], q["offset"] = first, self.expr()
            elif self.op(","):
                q["offset"], q["limit"] = first, self.expr()   # LIMIT offset, count
            else:
                q["limit"] = first
        return ("select", q)

    def table_name_alias(self):
        name = self.ident()
        alias = name
        if self.kw("AS"):
            alias = self.ident()
        elif self.at_ident():
            alias = self.ident()
        return name, alias

    def table_ref(self, kind, on):
        name, alias = self.table_name_alias()
        return {"table": name, "alias": alias, "kind": kind, "on": on}

    def expr_list(self):
        out = [self.expr()]
        while self.op(","):
            out.append(self.expr())
        return out

    def insert(self):
        self.expect_kw("INTO")
        table = self.ident()
        cols = None
        if self.op("("):
            cols = [self.ident()]
            while self.op(","):
                cols.append(self.ident())
            self.expect_op(")")
        if self.kw("VALUES"):
            rows = []
            while True:
                self.expect_op("(")
                rows.append(self.expr_list())
                self.expect_op(")")
                if not self.op(","):
                    break
            return ("insert", table, cols, rows, None)
        if self.at_kw("SELECT"):
            return ("insert", table, cols, None, self.select()[1])
        raise self.error()

    def update(self):
        table = self.ident()
        self.expect_kw("SET")
        sets = []
        while True:
            col = self.ident()
            self.expect_op("=")
            sets.append((col, self.expr()))
            if not self.op(","):
                break
        where = self.expr() if self.kw("WHERE") else None
        return ("update", table, sets, where)

    def create(self):
        unique = self.kw("UNIQUE")
        if self.kw("INDEX"):
            if_not = self.kw("IF") and (self.expect_kw("NOT") or self.expect_kw("EXISTS") or True)
            name = self.ident()
            self.expect_kw("ON")
            table = self.ident()
            self.expect_op("(")
            col = self.ident()
            self.kw("ASC") or self.kw("DESC")
            self.expect_op(")")
            return ("create_index", name, table, col, unique, if_not)
        if unique:
            raise self.error()
        self.expect_kw("TABLE")
        if_not = self.kw("IF") and (self.expect_kw("NOT") or self.expect_kw("EXISTS") or True)
        name = self.ident()
        self.expect_op("(")
        cols = []
        while True:
            col = {"name": self.ident(), "type": "", "pk": False, "notnull": False, "unique": False, "default": None}
            words = []
            while self.at_ident():
                words.append(self.ident())
            if words and self.op("("):          # VARCHAR(20), DECIMAL(10, 2)
                while not self.op(")"):
                    if self.peek()[0] == "eof":
                        raise self.error()
                    self.i += 1
            col["type"] = " ".join(words)
            while True:
                if self.kw("PRIMARY"):
                    self.expect_kw("KEY")
                    self.kw("ASC") or self.kw("DESC")
                    col["pk"] = True
                    if self.at_ident() and self.peek()[1].upper() == "AUTOINCREMENT":
                        self.i += 1
                elif self.kw("NOT"):
                    self.expect_kw("NULL")
                    col["notnull"] = True
                elif self.kw("NULL"):
                    pass
                elif self.kw("UNIQUE"):
                    col["unique"] = True
                elif self.kw("DEFAULT"):
                    neg = self.op("-")
                    tk = self.peek()
                    if tk[0] in ("num", "str"):
                        self.i += 1
                        col["default"] = -tk[1] if neg else tk[1]
                    elif self.kw("NULL"):
                        col["default"] = None
                    else:
                        raise self.error()
                else:
                    break
            cols.append(col)
            if not self.op(","):
                break
        self.expect_op(")")
        return ("create_table", name, cols, if_not)

    # ---------------------------------------------------------------- expressions (lowest precedence first)
    def expr(self):
        left = self.and_expr()
        while self.kw("OR"):
            left = ("or", left, self.and_expr())
        return left

    def and_expr(self):
        left = self.not_expr()
        while self.kw("AND"):
            left = ("and", left, self.not_expr())
        return left

    def not_expr(self):
        if self.kw("NOT"):
            return ("not", self.not_expr())
        return self.equality()

    def equality(self):
        left = self.comparison()
        while True:
            tk = self.peek()
            if tk[0] == "op" and tk[1] in ("=", "==", "!=", "<>"):
                self.i += 1
                op = {"==": "=", "<>": "!="}.get(tk[1], tk[1])
                left = ("bin", op, left, self.comparison())
            elif self.kw("IS"):
                neg = self.kw("NOT")
                self.expect_kw("NULL")
                left = ("isnull", left, neg)
            elif self.at_kw("NOT") and self.at_kw("IN", "LIKE", "BETWEEN", k=1) or self.at_kw("IN", "LIKE", "BETWEEN"):
                neg = self.kw("NOT")
                if self.kw("IN"):
                    self.expect_op("(")
                    items = self.expr_list() if not self.at_op(")") else []
                    self.expect_op(")")
                    left = ("in", left, items, neg)
                elif self.kw("LIKE"):
                    left = ("like", left, self.comparison(), neg)
                else:
                    self.expect_kw("BETWEEN")
                    lo = self.comparison()
                    self.expect_kw("AND")
                    left = ("between", left, lo, self.comparison(), neg)
            else:
                return left

    def comparison(self):
        left = self.additive()
        while self.peek()[0] == "op" and self.peek()[1] in ("<", "<=", ">", ">="):
            op = self.t[self.i][1]
            self.i += 1
            left = ("bin", op, left, self.additive())
        return left

    def additive(self):
        left = self.multiplicative()
        while self.peek()[0] == "op" and self.peek()[1] in ("+", "-"):
            op = self.t[self.i][1]
            self.i += 1
            left = ("bin", op, left, self.multiplicative())
        return left

    def multiplicative(self):
        left = self.concat()
        while self.peek()[0] == "op" and self.peek()[1] in ("*", "/", "%"):
            op = self.t[self.i][1]
            self.i += 1
            left = ("bin", op, left, self.concat())
        return left

    def concat(self):
        left = self.unary()
        while self.op("||"):
            left = ("bin", "||", left, self.unary())
        return left

    def unary(self):
        if self.op("-"):
            e = self.unary()
            return ("lit", -e[1]) if e[0] == "lit" and isinstance(e[1], (int, float)) and e[1] != 0 else ("neg", e)
        if self.op("+"):
            return ("pos", self.unary())
        return self.primary()

    def primary(self):
        tk = self.peek()
        if tk[0] in ("num", "str"):
            self.i += 1
            return ("lit", tk[1])
        if self.kw("NULL"):
            return ("lit", None)
        if self.kw("TRUE"):
            return ("lit", 1)
        if self.kw("FALSE"):
            return ("lit", 0)
        if self.op("("):
            e = self.expr()
            self.expect_op(")")
            return e
        if self.kw("CASE"):
            base = None if self.at_kw("WHEN") else self.expr()
            whens = []
            while self.kw("WHEN"):
                w = self.expr()
                self.expect_kw("THEN")
                whens.append((w, self.expr()))
            if not whens:
                raise self.error()
            other = self.expr() if self.kw("ELSE") else None
            self.expect_kw("END")
            return ("case", base, whens, other)
        if self.at_ident() and self.at_op("(", 1):
            name = self.ident().lower()
            self.i += 1
            if self.op("*"):
                self.expect_op(")")
                return ("func", name, [], False, True)
            distinct = self.kw("DISTINCT")
            args = [] if self.at_op(")") else self.expr_list()
            self.expect_op(")")
            return ("func", name, args, distinct, False)
        if self.at_ident():
            name = self.ident()
            if self.op("."):
                return ("col", name, self.ident())
            return ("col", None, name)
        raise self.error()


def children(e):
    k = e[0]
    if k in ("lit", "col", "colidx"):
        return []
    if k in ("neg", "pos", "not", "isnull"):
        return [e[1]]
    if k == "bin":
        return [e[2], e[3]]
    if k in ("and", "or"):
        return [e[1], e[2]]
    if k == "between":
        return [e[1], e[2], e[3]]
    if k == "in":
        return [e[1]] + list(e[2])
    if k == "like":
        return [e[1], e[2]]
    if k == "case":
        out = [e[1]] if e[1] else []
        for w, t in e[2]:
            out += [w, t]
        return out + ([e[3]] if e[3] else [])
    if k == "func":
        return list(e[2])
    return []


def conjuncts(e):
    if e is None:
        return []
    if e[0] == "and":
        return conjuncts(e[1]) + conjuncts(e[2])
    return [e]


# ============================================================================ expression semantics (3-valued logic)
def arith(op, a, b):
    if a is None or b is None:
        return None
    a, b = to_num(a), to_num(b)
    if isinstance(a, int) and isinstance(b, int):
        if op == "+":
            r = a + b
        elif op == "-":
            r = a - b
        elif op == "*":
            r = a * b
        elif op == "/":
            if b == 0:
                return None
            q = abs(a) // abs(b)
            r = q if (a < 0) == (b < 0) else -q          # integer division truncates toward zero
        else:
            if b == 0:
                return None
            r = abs(a) % abs(b)
            r = -r if a < 0 else r                       # remainder takes the sign of the dividend
        return r if INT64_MIN <= r <= INT64_MAX else float(r)
    if op == "%":
        ia, ib = int(a), int(b)
        if ib == 0:
            return None
        r = abs(ia) % abs(ib)
        return float(-r if ia < 0 else r)
    a, b = float(a), float(b)
    if op == "/":
        return None if b == 0 else a / b
    return a + b if op == "+" else a - b if op == "-" else a * b


def compare(op, a, b):
    if a is None or b is None:
        return None
    ka, kb = vkey(a), vkey(b)
    if op == "=":
        return int(ka == kb)
    if op == "!=":
        return int(ka != kb)
    if op == "<":
        return int(ka < kb)
    if op == "<=":
        return int(ka <= kb)
    if op == ">":
        return int(ka > kb)
    return int(ka >= kb)


def and3(a, b):
    if a is False or b is False:
        return 0
    if a is None or b is None:
        return None
    return 1


def not3(v):
    return None if v is None else int(not v)


@functools.lru_cache(maxsize=256)
def like_regex(pattern):
    rx = "".join(".*" if c == "%" else "." if c == "_" else re.escape(c) for c in pattern)
    return re.compile(rx, re.DOTALL | re.IGNORECASE | re.ASCII)


ASCII_UPPER = str.maketrans("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def fn_round(x, n=0):
    if x is None or n is None:
        return None
    x, n = float(to_num(x)), max(int(to_num(n)), 0)
    if n > 15 or not math.isfinite(x):
        return x
    f = 10 ** n
    return math.copysign(math.floor(abs(x) * f + 0.5) / f, x)


def fn_substr(s, start, length=None):
    if s is None or start is None:
        return None
    s = to_text(s)
    n = len(s)
    p1 = int(to_num(start))
    p2 = n if length is None else int(to_num(length))
    neg = p2 < 0
    if neg:
        p2 = -p2
    if p1 < 0:
        p1 += n
        if p1 < 0:
            p2 += p1
            if p2 < 0:
                p2 = 0
            p1 = 0
    elif p1 > 0:
        p1 -= 1
    elif p2 > 0:
        p2 -= 1
    if neg:
        p1 -= p2
        if p1 < 0:
            p2 += p1
            p1 = 0
    return s[p1:p1 + p2]


def scalar_min_max(name, args):
    if any(a is None for a in args):
        return None
    return (min if name == "min" else max)(args, key=vkey)


SCALARS = {
    "abs": (1, 1, lambda x: None if x is None else abs(to_num(x))),
    "length": (1, 1, lambda x: None if x is None else len(to_text(x))),
    "upper": (1, 1, lambda x: None if x is None else to_text(x).translate(ASCII_UPPER)),
    "lower": (1, 1, lambda x: None if x is None else to_text(x).translate(ASCII_LOWER)),
    "coalesce": (2, 99, lambda *a: next((v for v in a if v is not None), None)),
    "ifnull": (2, 2, lambda a, b: b if a is None else a),
    "nullif": (2, 2, lambda a, b: None if a is not None and b is not None and vkey(a) == vkey(b) else a),
    "round": (1, 2, fn_round),
    "substr": (2, 3, fn_substr),
    "typeof": (1, 1, lambda x: "null" if x is None else "integer" if isinstance(x, int)
               else "real" if isinstance(x, float) else "text"),
}


class Accumulator:
    def __init__(self, name, argf, distinct, star, sepf):
        self.name, self.argf, self.distinct, self.star, self.sepf = name, argf, distinct, star, sepf
        self.seen = set() if distinct else None
        self.count, self.int_sum, self.float_sum, self.comp, self.has_float = 0, 0, 0.0, 0.0, False
        self.best, self.parts, self.sep = None, [], ","

    def step(self, row):
        if self.star:
            self.count += 1
            return
        v = self.argf(row, None)
        if v is None:
            return
        if self.distinct:
            k = vkey(v)
            if k in self.seen:
                return
            self.seen.add(k)
        self.count += 1
        n = self.name
        if n in ("sum", "avg", "total"):
            v = to_num(v)
            if isinstance(v, float):
                # Neumaier compensated summation: tracks the rounding error of every addition
                self.has_float = True
                t = self.float_sum + v
                if abs(self.float_sum) >= abs(v):
                    self.comp += (self.float_sum - t) + v
                else:
                    self.comp += (v - t) + self.float_sum
                self.float_sum = t
            else:
                self.int_sum += v
        elif n in ("min", "max"):
            if self.best is None or (vkey(v) < vkey(self.best) if n == "min" else vkey(v) > vkey(self.best)):
                self.best = v
        elif n == "group_concat":
            if self.sepf and not self.parts:
                self.sep = to_text(self.sepf(row, None))
            self.parts.append(to_text(v))

    def result(self):
        n = self.name
        if n == "count":
            return self.count
        if n in ("min", "max"):
            return self.best
        if n == "group_concat":
            return self.sep.join(self.parts) if self.parts else None
        floats = self.float_sum + self.comp
        if n == "total":
            return float(self.int_sum) + floats
        if self.count == 0:
            return None
        if n == "avg":
            return (self.int_sum + floats) / self.count
        if self.has_float:
            return self.int_sum + floats
        if not INT64_MIN <= self.int_sum <= INT64_MAX:
            raise DBError("integer overflow")
        return self.int_sum


# ============================================================================ the database
class Result:
    def __init__(self, columns=None, rows=None, message=None, rowcount=0):
        self.columns, self.rows, self.message, self.rowcount = columns, rows, message, rowcount

    def __iter__(self):
        return iter(self.rows or [])

    def __repr__(self):
        return format_result(self)


class Source:
    """One table in a FROM clause. In a joined row each source owns a slice: [rowid, col0, col1, ...]."""

    def __init__(self, alias, tname, table, offset, kind, on):
        self.alias, self.tname, self.table, self.offset, self.kind, self.on = alias, tname, table, offset, kind, on
        self.cols = [c["name"] for c in table["columns"]]
        self.colidx = {c.lower(): i for i, c in enumerate(self.cols)}
        self.width = 1 + len(self.cols)


class Database:
    def __init__(self, path=":memory:", sync=True):
        self.temp = None
        if path == ":memory:":
            fd, path = tempfile.mkstemp(suffix=".franzdb")
            os.close(fd)
            os.remove(path)
            self.temp = path
        self.path = path
        self.pager = Pager(path, sync=sync)
        self.in_tx = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self.in_tx:
            self.pager.rollback()
            self.in_tx = False
        self.pager.close()
        if self.temp and os.path.exists(self.temp):
            os.remove(self.temp)

    # ---------------------------------------------------------------- entry point
    def execute(self, sql):
        result = Result(message="ok")
        for st in Parser(sql).statements():
            result = self._run(st)
        return result

    def query(self, sql):
        return self.execute(sql).rows

    def _run(self, st):
        kind = st[0]
        if kind == "select":
            return self.select(st[1])
        if kind == "explain":
            return self.select(st[1][1], explain=True)
        if kind == "begin":
            if self.in_tx:
                raise DBError("cannot start a transaction within a transaction")
            self.in_tx = True
            return Result(message="transaction started")
        if kind == "commit":
            if not self.in_tx:
                raise DBError("cannot commit - no transaction is active")
            self.pager.commit()
            self.in_tx = False
            return Result(message="committed")
        if kind == "rollback":
            if not self.in_tx:
                raise DBError("cannot rollback - no transaction is active")
            self.pager.rollback()
            self.in_tx = False
            return Result(message="rolled back")
        handler = {"insert": self.insert, "update": self.update, "delete": self.delete,
                   "create_table": self.create_table, "create_index": self.create_index, "drop": self.drop}[kind]
        # Every write statement is atomic: if it fails halfway, everything it changed is undone.
        self.pager.begin_statement()
        try:
            result = handler(*st[1:])
        except BaseException:
            self.pager.undo_statement()
            raise
        self.pager.end_statement()
        if not self.in_tx:
            self.pager.commit()
        return result

    # ---------------------------------------------------------------- catalog helpers
    def _table(self, name):
        t = self.pager.cat["tables"].get(name.lower())
        if t is None:
            raise DBError(f"no such table: {name}")
        return t

    def _indexes(self, tname):
        return [(n, ix) for n, ix in self.pager.cat["indexes"].items() if ix["table"] == tname.lower()]

    def _tree(self, entry):
        return BTree(self.pager, entry)

    # ---------------------------------------------------------------- expression compiler
    def resolve(self, scope, table, name):
        lname = name.lower()
        hits = []
        for s in scope:
            if table is not None and s.alias.lower() != table.lower():
                continue
            if lname in s.colidx:
                hits.append(s.offset + 1 + s.colidx[lname])
            elif lname in ("rowid", "oid", "_rowid_"):
                hits.append(s.offset)
        if not hits:
            raise DBError(f"no such column: {table + '.' if table else ''}{name}")
        if len(hits) > 1:
            raise DBError(f"ambiguous column name: {name}")
        return hits[0]

    def compile(self, e, scope, aggs=None):
        """Turn an AST into a Python closure f(row, aggregate_values) -> value."""
        c = lambda x: self.compile(x, scope, aggs)   # noqa: E731
        k = e[0]
        if k == "lit":
            v = e[1]
            return lambda r, a: v
        if k == "col":
            idx = self.resolve(scope, e[1], e[2])
            return lambda r, a: r[idx]
        if k == "colidx":
            idx = e[1]
            return lambda r, a: r[idx]
        if k == "pos":
            return c(e[1])
        if k == "neg":
            f = c(e[1])
            return lambda r, a: arith("-", 0, f(r, a))
        if k == "not":
            f = c(e[1])
            return lambda r, a: not3(to_bool(f(r, a)))
        if k == "bin":
            op, fa, fb = e[1], c(e[2]), c(e[3])
            if op in ("+", "-", "*", "/", "%"):
                return lambda r, a: arith(op, fa(r, a), fb(r, a))
            if op == "||":
                def concat(r, a):
                    x, y = fa(r, a), fb(r, a)
                    return None if x is None or y is None else to_text(x) + to_text(y)
                return concat
            return lambda r, a: compare(op, fa(r, a), fb(r, a))
        if k == "and":
            fa, fb = c(e[1]), c(e[2])

            def f_and(r, a):
                x = to_bool(fa(r, a))
                return 0 if x is False else and3(x, to_bool(fb(r, a)))
            return f_and
        if k == "or":
            fa, fb = c(e[1]), c(e[2])

            def f_or(r, a):
                x = to_bool(fa(r, a))
                if x is True:
                    return 1
                y = to_bool(fb(r, a))
                return 1 if y is True else None if x is None or y is None else 0
            return f_or
        if k == "isnull":
            f, neg = c(e[1]), e[2]
            return lambda r, a: int((f(r, a) is None) != neg)
        if k == "between":
            f, lo, hi, neg = c(e[1]), c(e[2]), c(e[3]), e[4]

            def between(r, a):
                v = f(r, a)
                x = to_bool(compare(">=", v, lo(r, a)))
                res = 0 if x is False else and3(x, to_bool(compare("<=", v, hi(r, a))))
                return not3(res) if neg else res
            return between
        if k == "in":
            f, items, neg = c(e[1]), [c(x) for x in e[2]], e[3]

            def in_list(r, a):
                v = f(r, a)
                if v is None:
                    return None
                has_null, kv = False, vkey(v)
                for item in items:
                    x = item(r, a)
                    if x is None:
                        has_null = True
                    elif vkey(x) == kv:
                        return int(not neg)
                return None if has_null else int(neg)
            return in_list
        if k == "like":
            f, p, neg = c(e[1]), c(e[2]), e[3]

            def like(r, a):
                v, pat = f(r, a), p(r, a)
                if v is None or pat is None:
                    return None
                return int(bool(like_regex(to_text(pat)).fullmatch(to_text(v))) != neg)
            return like
        if k == "case":
            base = c(e[1]) if e[1] else None
            whens = [(c(w), c(t)) for w, t in e[2]]
            other = c(e[3]) if e[3] else (lambda r, a: None)

            def case(r, a):
                if base:
                    bv = base(r, a)
                    for w, t in whens:
                        wv = w(r, a)
                        if bv is not None and wv is not None and vkey(bv) == vkey(wv):
                            return t(r, a)
                else:
                    for w, t in whens:
                        if to_bool(w(r, a)) is True:
                            return t(r, a)
                return other(r, a)
            return case
        if k == "func":
            name, args, distinct, star = e[1], e[2], e[3], e[4]
            is_agg = name in AGGREGATES and not (name in ("min", "max") and len(args) != 1)
            if is_agg:
                if aggs is None:
                    raise DBError(f"misuse of aggregate function {name}()")
                if star and name != "count":
                    raise DBError(f"wrong number of arguments to function {name}()")
                limit = 2 if name == "group_concat" else 1
                if not star and not 1 <= len(args) <= limit:
                    raise DBError(f"wrong number of arguments to function {name}()")
                argf = self.compile(args[0], scope, None) if args else None   # no nested aggregates
                sepf = self.compile(args[1], scope, None) if len(args) > 1 else None
                slot = len(aggs)
                aggs.append((name, argf, distinct, star, sepf))
                return lambda r, a: a[slot]
            if name in ("min", "max"):
                fs = [c(x) for x in args]
                return lambda r, a: scalar_min_max(name, [f(r, a) for f in fs])
            if name not in SCALARS:
                raise DBError(f"no such function: {name}")
            lo, hi, fn = SCALARS[name]
            if not lo <= len(args) <= hi:
                raise DBError(f"wrong number of arguments to function {name}()")
            fs = [c(x) for x in args]
            return lambda r, a: fn(*[f(r, a) for f in fs])
        raise DBError(f"cannot compile {k}")

    def const(self, e):
        return self.compile(e, [], None)([], None)

    def sources_used(self, e, scope):
        """Which FROM-clause sources (by position) does an expression read?"""
        out = set()
        stack = [e]
        while stack:
            x = stack.pop()
            if x[0] == "col":
                pos = self.resolve(scope, x[1], x[2])
                out.add(next(i for i, s in enumerate(scope) if s.offset <= pos < s.offset + s.width))
            stack.extend(children(x))
        return out

    # ---------------------------------------------------------------- query planning
    def plan_access(self, level, scope, conds):
        """Pick the fastest way to fetch rows of scope[level] given conditions that may use earlier sources."""
        s = scope[level]
        best = {"kind": "scan", "score": 0}
        indexed = {ix["column"].lower(): (n, ix) for n, ix in self._indexes(s.tname)}
        rowid_col = s.table["rowid_col"]
        lowers, uppers = {}, {}

        def column_of(x):
            if x[0] != "col":
                return None
            try:
                pos = self.resolve(scope, x[1], x[2])
            except DBError:
                return None
            if not s.offset <= pos < s.offset + s.width:
                return None
            if pos == s.offset or (rowid_col is not None and pos == s.offset + 1 + rowid_col):
                return "rowid"
            name = s.cols[pos - s.offset - 1].lower()
            return name if name in indexed else None

        def usable(x):
            return all(i < level for i in self.sources_used(x, scope))

        def consider(plan):
            nonlocal best
            if plan["score"] > best["score"]:
                best = plan

        for cond in conds:
            if cond[0] == "bin" and cond[1] in ("=", "<", "<=", ">", ">="):
                flip = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "=": "="}
                for col, other, op in ((cond[2], cond[3], cond[1]), (cond[3], cond[2], flip[cond[1]])):
                    target = column_of(col)
                    if target is None or not usable(other):
                        continue
                    f = self.compile(other, scope)
                    if op == "=":
                        if target == "rowid":
                            consider({"kind": "rowid_eq", "vals": [f], "score": 10, "desc": "rowid=?"})
                        else:
                            name, ix = indexed[target]
                            consider({"kind": "index_eq", "index": name, "vals": [f], "score": 9 if ix["unique"] else 8,
                                      "desc": f"{ix['column']}=?"})
                    elif op in (">", ">="):
                        lowers.setdefault(target, (f, op))
                    else:
                        uppers.setdefault(target, (f, op))
                    break
            elif cond[0] == "between" and not cond[4]:
                target = column_of(cond[1])
                if target is not None and usable(cond[2]) and usable(cond[3]):
                    lowers.setdefault(target, (self.compile(cond[2], scope), ">="))
                    uppers.setdefault(target, (self.compile(cond[3], scope), "<="))
            elif cond[0] == "in" and not cond[3]:
                target = column_of(cond[1])
                if target is not None and all(usable(x) for x in cond[2]):
                    fs = [self.compile(x, scope) for x in cond[2]]
                    if target == "rowid":
                        consider({"kind": "rowid_eq", "vals": fs, "score": 7, "desc": "rowid IN (...)"})
                    else:
                        name, ix = indexed[target]
                        consider({"kind": "index_eq", "index": name, "vals": fs, "score": 6,
                                  "desc": f"{ix['column']} IN (...)"})
        for target in set(lowers) | set(uppers):
            lo, hi = lowers.get(target), uppers.get(target)
            col = "rowid" if target == "rowid" else indexed[target][1]["column"]
            desc = " AND ".join(p for p in ((f"{col}{lo[1]}?" if lo else ""), (f"{col}{hi[1]}?" if hi else "")) if p)
            score = (5 if target == "rowid" else 4) - (0 if lo and hi else 2)
            plan = {"kind": "rowid_range" if target == "rowid" else "index_range", "lo": lo, "hi": hi,
                    "score": score, "desc": desc}
            if target != "rowid":
                plan["index"] = indexed[target][0]
            consider(plan)
        return best

    def access(self, s, plan, row):
        """Yields (rowid, record) for one source using the chosen plan."""
        tree = self._tree(s.table)
        kind = plan["kind"]
        if kind == "scan":
            for key, val in tree.scan():
                yield key[0], decode_values(val)[0]
        elif kind == "rowid_eq":
            seen = set()
            for f in plan["vals"]:
                v = f(row, None)
                if isinstance(v, float) and v.is_integer():
                    v = int(v)
                if not isinstance(v, int) or v in seen:
                    continue
                seen.add(v)
                val = tree.get((v,))
                if val is not None:
                    yield v, decode_values(val)[0]
        elif kind == "index_eq":
            ix_tree = self._tree(self.pager.cat["indexes"][plan["index"]])
            seen = set()
            for f in plan["vals"]:
                v = f(row, None)
                if v is None or vkey(v) in seen:
                    continue
                seen.add(vkey(v))
                for key, _ in ix_tree.scan((v,)):
                    if vkey(key[0]) != vkey(v):
                        break
                    yield key[1], decode_values(tree.get((key[1],)))[0]
        else:   # ranges: start at the lower bound, stop after the upper bound (WHERE re-checks the edges)
            lo, hi = plan["lo"], plan["hi"]
            lo_v = lo[0](row, None) if lo else None
            hi_v = hi[0](row, None) if hi else None
            if (lo and lo_v is None) or (hi and hi_v is None):
                return                                   # comparing with NULL is never true
            hi_k = vkey(hi_v) if hi else None
            if kind == "rowid_range":
                if (lo and isinstance(lo_v, str)) or (hi and isinstance(hi_v, str)):
                    yield from self.access(s, {"kind": "scan"}, row)
                    return
                for key, val in tree.scan((lo_v,) if lo else None):
                    if hi and vkey(key[0]) > hi_k:
                        break
                    yield key[0], decode_values(val)[0]
            else:
                ix_tree = self._tree(self.pager.cat["indexes"][plan["index"]])
                for key, _ in ix_tree.scan((lo_v,) if lo else (float("-inf"),)):
                    if hi and vkey(key[0]) > hi_k:
                        break
                    yield key[1], decode_values(tree.get((key[1],)))[0]

    def build_scope(self, q):
        scope, offset = [], 0
        for i, ref in enumerate(q["from"]):
            table = self._table(ref["table"])
            alias = ref["alias"]
            if any(s.alias.lower() == alias.lower() for s in scope):
                raise DBError(f"ambiguous table name: {alias}")
            s = Source(alias, ref["table"].lower(), table, offset, ref["kind"], ref["on"])
            scope.append(s)
            offset += s.width
        return scope, offset

    def joined_rows(self, scope, width, where):
        """Nested-loop join. Each level picks its own access path (index lookups use values from outer rows)."""
        where_conds = conjuncts(where)
        plans, on_fs, cache = [], [], {}
        for level, s in enumerate(scope):
            conds = conjuncts(s.on)
            if level == 0 or s.kind != "left":
                conds = conds + [c for c in where_conds if self._safe_sources(c, scope) and
                                 all(i <= level for i in self.sources_used(c, scope))]
            plans.append(self.plan_access(level, scope, conds))
            on_fs.append(self.compile(s.on, scope) if s.on is not None else None)

        row = [None] * width

        def rows_at(level):
            if level == len(scope):
                yield row[:]
                return
            s, plan = scope[level], plans[level]
            if plan["kind"] == "scan" and level > 0:      # an inner table without a usable index: read it once
                if level not in cache:
                    cache[level] = list(self.access(s, plan, row))
                candidates = cache[level]
            else:
                candidates = self.access(s, plan, row)
            matched = False
            for rid, rec in candidates:
                row[s.offset] = rid
                row[s.offset + 1:s.offset + s.width] = rec
                if on_fs[level] is None or to_bool(on_fs[level](row, None)) is True:
                    matched = True
                    yield from rows_at(level + 1)
            if not matched and s.kind == "left":
                row[s.offset:s.offset + s.width] = [None] * s.width
                yield from rows_at(level + 1)

        if not scope:
            yield []
            return
        yield from rows_at(0)

    def _safe_sources(self, cond, scope):
        try:
            self.sources_used(cond, scope)
            return True
        except DBError:
            return False

    def explain_plans(self, scope, where):
        where_conds = conjuncts(where)
        lines = []
        for level, s in enumerate(scope):
            conds = conjuncts(s.on)
            if level == 0 or s.kind != "left":
                conds = conds + [c for c in where_conds if self._safe_sources(c, scope) and
                                 all(i <= level for i in self.sources_used(c, scope))]
            plan = self.plan_access(level, scope, conds)
            name = s.tname + (f" AS {s.alias}" if s.alias.lower() != s.tname else "")
            prefix = "  " * level + ("LEFT " if s.kind == "left" else "")
            if plan["kind"] == "scan":
                lines.append(f"{prefix}SCAN {name}")
            elif plan["kind"].startswith("rowid"):
                lines.append(f"{prefix}SEARCH {name} USING INTEGER PRIMARY KEY ({plan['desc']})")
            else:
                lines.append(f"{prefix}SEARCH {name} USING INDEX {plan['index']} ({plan['desc']})")
        return lines

    # ---------------------------------------------------------------- SELECT
    def select(self, q, explain=False):
        scope, width = self.build_scope(q)
        where_f = self.compile(q["where"], scope) if q["where"] else None
        for s in scope:
            if s.on is not None:
                self.compile(s.on, scope)          # report bad column names even if no rows
        out_items, names, aliases = [], [], {}
        for item in q["cols"]:
            if item[0] == "star":
                if not scope:
                    raise DBError("no tables specified")
                matched = [s for s in scope if item[1] is None or s.alias.lower() == item[1].lower()]
                if not matched:
                    raise DBError(f"no such table: {item[1]}")
                for s in matched:
                    for j, col in enumerate(s.cols):
                        out_items.append(("colidx", s.offset + 1 + j))
                        names.append(col)
            else:
                if item[2]:
                    aliases.setdefault(item[2].lower(), len(out_items))
                out_items.append(item[1])
                names.append(item[2] or (item[1][2] if item[1][0] == "col" else item[3]))
        aggs = []
        out_fs = [self.compile(e, scope, aggs) for e in out_items]
        group_fs = [self.compile(e, scope) for e in q["group"]]
        having_f = self.compile(q["having"], scope, aggs) if q["having"] else None
        order = []
        for e, desc in q["order"]:
            if e[0] == "lit" and isinstance(e[1], int):
                if not 1 <= e[1] <= len(out_fs):
                    raise DBError(f"ORDER BY term out of range - should be between 1 and {len(out_fs)}")
                order.append((("out", e[1] - 1), desc))
            elif e[0] == "col" and e[1] is None and e[2].lower() in aliases:
                order.append((("out", aliases[e[2].lower()]), desc))
            else:
                order.append((("expr", self.compile(e, scope, aggs)), desc))
        grouped = bool(aggs) or bool(q["group"])
        if having_f and not grouped:
            raise DBError("a GROUP BY clause is required before HAVING")
        limit = self.const(q["limit"]) if q["limit"] is not None else None
        offset = self.const(q["offset"]) if q["offset"] is not None else None

        if explain:
            lines = self.explain_plans(scope, q["where"])
            if grouped and q["group"]:
                lines.append("USE TEMP B-TREE FOR GROUP BY")
            if q["distinct"]:
                lines.append("USE TEMP B-TREE FOR DISTINCT")
            if order:
                lines.append("USE TEMP B-TREE FOR ORDER BY")
            return Result(["plan"], [(line,) for line in lines])

        contexts = []
        rows = (r for r in self.joined_rows(scope, width, q["where"])
                if where_f is None or to_bool(where_f(r, None)) is True)
        if grouped:
            groups = {}
            for r in rows:
                key = tuple(vkey(g(r, None)) for g in group_fs)
                grp = groups.get(key)
                if grp is None:
                    grp = groups[key] = (r, [Accumulator(*spec) for spec in aggs])
                for acc in grp[1]:
                    acc.step(r)
            if not groups and not q["group"]:          # aggregates over zero rows still give one row
                groups[()] = ([None] * width, [Accumulator(*spec) for spec in aggs])
            for rep, accs in groups.values():
                av = [acc.result() for acc in accs]
                if having_f is None or to_bool(having_f(rep, av)) is True:
                    contexts.append((rep, av))
        else:
            contexts = [(r, None) for r in rows]

        results = []
        for r, av in contexts:
            out = tuple(f(r, av) for f in out_fs)
            okey = tuple(out[x] if kind == "out" else x(r, av) for (kind, x), _ in order)
            results.append((out, okey))
        if q["distinct"]:
            seen, uniq = set(), []
            for res in results:
                k = tuple(vkey(v) for v in res[0])
                if k not in seen:
                    seen.add(k)
                    uniq.append(res)
            results = uniq
        if order:
            descs = [d for _, d in order]

            def cmp(x, y):
                for desc, a, b in zip(descs, x[1], y[1]):
                    ka, kb = vkey(a), vkey(b)
                    if ka != kb:
                        c = -1 if ka < kb else 1
                        return -c if desc else c
                return 0
            results.sort(key=functools.cmp_to_key(cmp))
        rows_out = [out for out, _ in results]
        start = max(int(to_num(offset)), 0) if offset is not None else 0
        if limit is not None and int(to_num(limit)) >= 0:
            rows_out = rows_out[start:start + int(to_num(limit))]
        else:
            rows_out = rows_out[start:]
        return Result(names, rows_out)

    # ---------------------------------------------------------------- writes
    def _check_unique(self, tname, ix, v, own_rowid=None):
        if v is None or not ix["unique"]:
            return
        for key, _ in self._tree(ix).scan((v,)):
            if vkey(key[0]) != vkey(v):
                return
            if key[1] != own_rowid:
                raise DBError(f"UNIQUE constraint failed: {tname}.{ix['column']}")

    def _insert_row(self, t, rec):
        cols = t["columns"]
        rec = [affinity(c["type"], v) for c, v in zip(cols, rec)]
        rc = t["rowid_col"]
        if rc is not None and rec[rc] is not None:
            if not isinstance(rec[rc], int):
                raise DBError("datatype mismatch")
            rowid = rec[rc]
        else:
            rowid = t["next_rowid"]
            if rc is not None:
                rec[rc] = rowid
        for c, v in zip(cols, rec):
            if v is None and c["notnull"]:
                raise DBError(f"NOT NULL constraint failed: {t['name']}.{c['name']}")
        tree = self._tree(t)
        if tree.get((rowid,)) is not None:
            col = cols[rc]["name"] if rc is not None else "rowid"
            raise DBError(f"UNIQUE constraint failed: {t['name']}.{col}")
        indexes = self._indexes(t["name"])
        for _, ix in indexes:
            self._check_unique(t["name"], ix, rec[ix["col"]])
        tree.insert((rowid,), encode_values(rec))
        for _, ix in indexes:
            self._tree(ix).insert((rec[ix["col"]], rowid), b"")
        t["next_rowid"] = max(t["next_rowid"], rowid + 1)
        return rowid

    def insert(self, table, cols, value_rows, select_q):
        t = self._table(table)
        names = [c["name"].lower() for c in t["columns"]]
        if cols is None:
            targets = list(range(len(names)))
        else:
            targets = []
            for c in cols:
                if c.lower() not in names:
                    raise DBError(f"table {t['name']} has no column named {c}")
                targets.append(names.index(c.lower()))
        if select_q is not None:
            rows = self.select(select_q).rows
        else:
            rows = [[self.const(e) for e in row] for row in value_rows]
        for row in rows:
            if len(row) != len(targets):
                raise DBError(f"table {t['name']} has {len(names)} columns but {len(row)} values were supplied"
                              if cols is None else f"{len(row)} values for {len(targets)} columns")
            rec = [c["default"] for c in t["columns"]]
            for i, v in zip(targets, row):
                rec[i] = v
            self._insert_row(t, rec)
        return Result(message=f"{len(rows)} row(s) inserted", rowcount=len(rows))

    def _matching(self, t, where):
        scope = [Source(t["name"], t["name"].lower(), t, 0, "first", None)]
        where_f = self.compile(where, scope) if where is not None else None
        plan = self.plan_access(0, scope, conjuncts(where))
        out = []
        for rid, rec in self.access(scope[0], plan, []):
            if where_f is None or to_bool(where_f([rid, *rec], None)) is True:
                out.append((rid, rec))
        return scope, out          # materialised first, so changing rows can't confuse the scan

    def update(self, table, sets, where):
        t = self._table(table)
        scope, targets = self._matching(t, where)
        assigns = []
        for col, e in sets:
            lc = col.lower()
            if lc not in scope[0].colidx:
                raise DBError(f"no such column: {col}")
            assigns.append((scope[0].colidx[lc], self.compile(e, scope)))
        tree, rc = self._tree(t), t["rowid_col"]
        indexes = self._indexes(t["name"])
        for rid, rec in targets:
            row = [rid, *rec]
            new = list(rec)
            for ci, f in assigns:
                new[ci] = affinity(t["columns"][ci]["type"], f(row, None))
            new_rid = rid
            if rc is not None:
                if not isinstance(new[rc], int):
                    raise DBError("datatype mismatch")
                new_rid = new[rc]
            for c, v in zip(t["columns"], new):
                if v is None and c["notnull"]:
                    raise DBError(f"NOT NULL constraint failed: {t['name']}.{c['name']}")
            if new_rid != rid and tree.get((new_rid,)) is not None:
                raise DBError(f"UNIQUE constraint failed: {t['name']}.{t['columns'][rc]['name']}")
            for _, ix in indexes:
                if vkey(new[ix["col"]]) != vkey(rec[ix["col"]]):
                    self._check_unique(t["name"], ix, new[ix["col"]], own_rowid=rid)
            for _, ix in indexes:
                old_v, new_v = rec[ix["col"]], new[ix["col"]]
                if vkey(old_v) != vkey(new_v) or new_rid != rid:
                    ix_tree = self._tree(ix)
                    ix_tree.delete((old_v, rid))
                    ix_tree.insert((new_v, new_rid), b"")
            if new_rid != rid:
                tree.delete((rid,))
                t["next_rowid"] = max(t["next_rowid"], new_rid + 1)
            tree.insert((new_rid,), encode_values(new))
        return Result(message=f"{len(targets)} row(s) updated", rowcount=len(targets))

    def delete(self, table, where):
        t = self._table(table)
        _, targets = self._matching(t, where)
        tree = self._tree(t)
        indexes = self._indexes(t["name"])
        for rid, rec in targets:
            for _, ix in indexes:
                self._tree(ix).delete((rec[ix["col"]], rid))
            tree.delete((rid,))
        return Result(message=f"{len(targets)} row(s) deleted", rowcount=len(targets))

    def create_table(self, name, cols, if_not):
        cat = self.pager.cat
        if name.lower() in cat["tables"]:
            if if_not:
                return Result(message="table already exists")
            raise DBError(f"table {name} already exists")
        seen = set()
        for c in cols:
            if c["name"].lower() in seen:
                raise DBError(f"duplicate column name: {c['name']}")
            seen.add(c["name"].lower())
        pks = [i for i, c in enumerate(cols) if c["pk"]]
        if len(pks) > 1:
            raise DBError(f'table "{name}" has more than one primary key')
        rowid_col = pks[0] if pks and cols[pks[0]]["type"].upper() == "INTEGER" else None
        root = self.pager.allocate("L").pid
        cat["tables"][name.lower()] = {
            "name": name, "root": root, "next_rowid": 1, "rowid_col": rowid_col,
            "columns": [{"name": c["name"], "type": c["type"], "notnull": c["notnull"],
                         "default": c["default"]} for c in cols]}
        for i, c in enumerate(cols):
            if (c["pk"] and i != rowid_col) or c["unique"]:
                self.create_index(f"autoindex_{name}_{c['name']}", name, c["name"], True, False, auto=True)
        return Result(message=f"table {name} created")

    def create_index(self, name, table, col, unique, if_not, auto=False):
        cat = self.pager.cat
        if name.lower() in cat["indexes"]:
            if if_not:
                return Result(message="index already exists")
            raise DBError(f"index {name} already exists")
        t = self._table(table)
        names = [c["name"].lower() for c in t["columns"]]
        if col.lower() not in names:
            raise DBError(f"no such column: {col}")
        ci = names.index(col.lower())
        ix = {"name": name, "table": t["name"].lower(), "column": t["columns"][ci]["name"], "col": ci,
              "root": self.pager.allocate("L").pid, "unique": unique, "auto": auto}
        cat["indexes"][name.lower()] = ix
        tree = self._tree(ix)
        for key, val in list(self._tree(t).scan()):
            v = decode_values(val)[0][ci]
            self._check_unique(t["name"], ix, v)
            tree.insert((v, key[0]), b"")
        return Result(message=f"index {name} created")

    def drop(self, what, name, if_exists):
        cat = self.pager.cat
        if what == "table":
            t = cat["tables"].get(name.lower())
            if t is None:
                if if_exists:
                    return Result(message="no such table")
                raise DBError(f"no such table: {name}")
            for iname, ix in self._indexes(t["name"]):
                for pid in self._tree(ix).pages():
                    self.pager.free(pid)
                del cat["indexes"][iname]
            for pid in self._tree(t).pages():
                self.pager.free(pid)
            del cat["tables"][name.lower()]
            return Result(message=f"table {name} dropped")
        ix = cat["indexes"].get(name.lower())
        if ix is None:
            if if_exists:
                return Result(message="no such index")
            raise DBError(f"no such index: {name}")
        if ix["auto"]:
            raise DBError("index associated with UNIQUE or PRIMARY KEY constraint cannot be dropped")
        for pid in self._tree(ix).pages():
            self.pager.free(pid)
        del cat["indexes"][name.lower()]
        return Result(message=f"index {name} dropped")

    # ---------------------------------------------------------------- integrity check
    def integrity_check(self):
        """Checks every B+tree, that every index matches its table exactly, and that no page is lost."""
        errors, owner = [], {}

        def claim(pids, who):
            for pid in pids:
                if pid in owner:
                    errors.append(f"page {pid} used by both {owner[pid]} and {who}")
                owner[pid] = who

        cat = self.pager.cat
        for tname, t in cat["tables"].items():
            tree = self._tree(t)
            try:
                tree.check()
                claim(tree.pages(), f"table {tname}")
            except DBError as e:
                errors.append(f"table {tname}: {e}")
                continue
            rows = [(k[0], decode_values(v)[0]) for k, v in tree.scan()]
            for iname, ix in self._indexes(tname):
                ix_tree = self._tree(ix)
                try:
                    ix_tree.check()
                    claim(ix_tree.pages(), f"index {iname}")
                except DBError as e:
                    errors.append(f"index {iname}: {e}")
                    continue
                expected = sorted(tkey((rec[ix["col"]], rid)) for rid, rec in rows)
                got = [tkey(k) for k, _ in ix_tree.scan()]
                if got != expected:
                    errors.append(f"index {iname} does not match table {tname}")
        pid, steps = cat["free_head"], 0
        while pid:
            steps += 1
            node = self.pager.get(pid)
            if node.kind != "X" or steps > cat["num_pages"]:
                errors.append("free list is corrupt")
                break
            claim([pid], "free list")
            pid = node.next
        lost = set(range(1, cat["num_pages"])) - set(owner)
        if lost:
            errors.append(f"{len(lost)} page(s) are not used by anything")
        return errors or ["ok"]


# ============================================================================ interactive shell
def fmt_value(v):
    if v is None:
        return "NULL"
    return fmt_real(v) if isinstance(v, float) else str(v)


def format_result(res, max_rows=200):
    if res.columns is None:
        return res.message or ""
    rows = [[fmt_value(v) for v in r] for r in res.rows[:max_rows]]
    widths = [max([len(c)] + [len(r[i]) for r in rows]) for i, c in enumerate(res.columns)]
    line = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    out = [line, "| " + " | ".join(c.ljust(w) for c, w in zip(res.columns, widths)) + " |", line]
    for r, orig in zip(rows, res.rows):
        out.append("| " + " | ".join(v.rjust(w) if isinstance(o, (int, float)) else v.ljust(w)
                                     for v, w, o in zip(r, widths, orig)) + " |")
    out.append(line)
    n = len(res.rows)
    more = f" (showing {max_rows})" if n > max_rows else ""
    out.append(f"{n} row{'s' if n != 1 else ''}{more}")
    return "\n".join(out)


HELP = """\
  .tables            list tables          .schema [table]   show CREATE statements
  .indexes           list indexes         .check            run the integrity check
  .read FILE         run a .sql file      .timer on|off     show how long queries take
  .quit              exit
  End every SQL statement with ;"""


def schema_sql(t, indexes):
    parts = []
    for i, c in enumerate(t["columns"]):
        s = f"  {c['name']}" + (f" {c['type']}" if c["type"] else "")
        if i == t["rowid_col"]:
            s += " PRIMARY KEY"
        if c["notnull"]:
            s += " NOT NULL"
        if c["default"] is not None:
            s += " DEFAULT " + (repr(c["default"]) if isinstance(c["default"], str) else str(c["default"]))
        parts.append(s)
    out = [f"CREATE TABLE {t['name']} (\n" + ",\n".join(parts) + "\n);"]
    for _, ix in indexes:
        u = "UNIQUE " if ix["unique"] else ""
        note = "  -- automatic" if ix["auto"] else ""
        out.append(f"CREATE {u}INDEX {ix['name']} ON {t['name']} ({ix['column']});{note}")
    return "\n".join(out)


def shell(path):
    db = Database(path)
    if db.pager.recovered:
        print("Recovered from an interrupted commit: the database was restored to its last good state.")
    print(f"FranzDB 1.0 — {path}\nType .help for commands. End SQL statements with ;")
    timer, buf = False, ""
    interactive = sys.stdin.isatty()
    while True:
        try:
            line = input(("franzdb> " if not buf else "     ...> ") if interactive else "")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not buf and line.strip().startswith("."):
            cmd, _, arg = line.strip().partition(" ")
            arg = arg.strip()
            if cmd in (".quit", ".exit"):
                break
            elif cmd == ".help":
                print(HELP)
            elif cmd == ".tables":
                print("  ".join(t["name"] for t in db.pager.cat["tables"].values()) or "(no tables)")
            elif cmd == ".indexes":
                for ix in db.pager.cat["indexes"].values():
                    print(f"{ix['name']} on {ix['table']}({ix['column']}){' UNIQUE' if ix['unique'] else ''}")
            elif cmd == ".schema":
                for name, t in db.pager.cat["tables"].items():
                    if not arg or arg.lower() == name:
                        print(schema_sql(t, db._indexes(name)))
            elif cmd == ".check":
                print("\n".join(db.integrity_check()))
            elif cmd == ".timer":
                timer = arg.lower() == "on"
            elif cmd == ".read":
                try:
                    with open(arg, encoding="utf-8") as f:
                        res = db.execute(f.read())
                    print(format_result(res))
                except (DBError, OSError) as e:
                    print(f"Error: {e}")
            else:
                print(f"unknown command {cmd}; try .help")
            continue
        buf += line + "\n"
        if not buf.strip() or not buf.rstrip().endswith(";"):
            if not buf.strip():
                buf = ""
            continue
        t0 = time.perf_counter()
        try:
            print(format_result(db.execute(buf)))
        except DBError as e:
            print(f"Error: {e}")
        if timer:
            print(f"Run time: {(time.perf_counter() - t0) * 1000:.2f} ms")
        buf = ""
    db.close()


if __name__ == "__main__":
    shell(sys.argv[1] if len(sys.argv) > 1 else ":memory:")
