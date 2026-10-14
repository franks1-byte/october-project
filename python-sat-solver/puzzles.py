"""
Puzzles solved by turning them into SAT: Sudoku (any size), N-queens, and the pigeonhole problem.

    python puzzles.py sudoku 800000000003600000070090000050007000000045700000100030001000068008500010090000400
    python puzzles.py queens 8            # one solution, drawn on a board
    python puzzles.py queens 8 --count    # count every solution with blocking clauses
    python puzzles.py pigeons 8           # 9 pigeons, 8 holes: unsatisfiable, with a checked proof
"""
import math
import sys
import time

from sat import Solver


def at_most_one(lits):
    return [[-a, -b] for i, a in enumerate(lits) for b in lits[i + 1:]]


def exactly_one(lits):
    return [list(lits)] + at_most_one(lits)


# ---------------------------------------------------------------------- Sudoku
def sudoku_clauses(grid):
    """grid: string of N*N characters, '0' or '.' for blanks; digits 1-9 then A-Z for N > 9."""
    cells = len(grid)
    n = math.isqrt(cells)
    b = math.isqrt(n)
    assert n * n == cells and b * b == n, "grid must be N*N with N a perfect square"
    var = lambda r, c, d: (r * n + c) * n + d + 1
    clauses = []
    for r in range(n):
        for c in range(n):
            clauses += exactly_one([var(r, c, d) for d in range(n)])
    for d in range(n):
        for i in range(n):
            clauses += exactly_one([var(i, c, d) for c in range(n)])                    # row i
            clauses += exactly_one([var(r, i, d) for r in range(n)])                    # column i
            br, bc = (i // b) * b, (i % b) * b
            clauses += exactly_one([var(br + r, bc + c, d) for r in range(b) for c in range(b)])   # box i
    symbols = "123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    for k, ch in enumerate(grid):
        if ch not in "0.":
            clauses.append([var(k // n, k % n, symbols.index(ch.upper()))])
    return n, var, clauses


def solve_sudoku(grid, check_unique=True):
    """Returns (solution string or None, unique: bool or None, solver)."""
    n, var, clauses = sudoku_clauses(grid)
    s = Solver(n ** 3)
    for c in clauses:
        s.add_clause(c)
    if not s.solve():
        return None, None, s
    symbols = "123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    sol = "".join(symbols[d] for r in range(n) for c in range(n) for d in range(n) if s.value(var(r, c, d)))
    unique = None
    if check_unique:                                   # forbid this answer; if nothing else fits, it was unique
        s.add_clause([-var(k // n, k % n, symbols.index(ch)) for k, ch in enumerate(sol)])
        unique = s.solve() is False
    return sol, unique, s


def sudoku_valid(sol, grid):
    n = math.isqrt(len(sol))
    b = math.isqrt(n)
    if any(g not in "0." and g.upper() != x for g, x in zip(grid, sol)):
        return False
    groups = []
    for i in range(n):
        groups.append([sol[i * n + c] for c in range(n)])
        groups.append([sol[r * n + i] for r in range(n)])
        br, bc = (i // b) * b, (i % b) * b
        groups.append([sol[(br + r) * n + bc + c] for r in range(b) for c in range(b)])
    return all(len(set(g)) == n for g in groups)


def show_sudoku(sol):
    n = math.isqrt(len(sol))
    b = math.isqrt(n)
    lines = []
    for r in range(n):
        if r and r % b == 0:
            lines.append("-+-".join(["-" * (2 * b - 1)] * b))
        row = sol[r * n:(r + 1) * n]
        lines.append(" | ".join(" ".join(row[i:i + b]) for i in range(0, n, b)))
    return "\n".join(lines)


# ---------------------------------------------------------------------- N-queens
def queens_clauses(n):
    var = lambda r, c: r * n + c + 1
    clauses = []
    for r in range(n):
        clauses += exactly_one([var(r, c) for c in range(n)])           # one queen per row
    for c in range(n):
        clauses += at_most_one([var(r, c) for r in range(n)])           # at most one per column
    for k in range(-n + 1, n):
        clauses += at_most_one([var(r, r - k) for r in range(n) if 0 <= r - k < n])       # diagonals
        clauses += at_most_one([var(r, k + n - 1 - r) for r in range(n) if 0 <= k + n - 1 - r < n])
    return var, clauses


def queens(n, count=False):
    """One solution (list of column per row), or the number of solutions if count=True."""
    var, clauses = queens_clauses(n)
    s = Solver(n * n)
    for c in clauses:
        s.add_clause(c)
    found = 0
    while s.solve():
        cols = [next(c for c in range(n) if s.value(var(r, c))) for r in range(n)]
        if not count:
            return cols
        found += 1
        s.add_clause([-var(r, cols[r]) for r in range(n)])              # block this placement, look again
    return found if count else None


# ---------------------------------------------------------------------- pigeonhole
def pigeonhole_clauses(holes):
    """holes+1 pigeons into `holes` holes, one per hole: unsatisfiable, and famously hard for resolution."""
    p = holes + 1
    var = lambda i, j: i * holes + j + 1
    clauses = [[var(i, j) for j in range(holes)] for i in range(p)]
    for j in range(holes):
        clauses += at_most_one([var(i, j) for i in range(p)])
    return clauses


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 1
    t = time.time()
    if argv[1] == "sudoku":
        sol, unique, s = solve_sudoku(argv[2])
        print(show_sudoku(sol) if sol else "no solution")
        if sol:
            print(f"\n{'unique' if unique else 'NOT unique'} solution, {s.stats['conflicts']} conflicts, {time.time() - t:.2f} s")
    elif argv[1] == "queens":
        n = int(argv[2])
        if "--count" in argv:
            print(f"{n}-queens has {queens(n, count=True)} solutions ({time.time() - t:.2f} s)")
        else:
            cols = queens(n)
            if cols is None:
                print("no solution")
            else:
                for c in cols:
                    print(" ".join("Q" if x == c else "." for x in range(n)))
                print(f"({time.time() - t:.2f} s)")
    elif argv[1] == "pigeons":
        from drat_check import check
        from sat import solve_clauses
        h = int(argv[2])
        cls = pigeonhole_clauses(h)
        proof = []
        r, _, s = solve_clauses(cls, proof=proof)
        print(f"{h + 1} pigeons in {h} holes: {'SAT' if r else 'UNSAT'} after {s.stats['conflicts']} conflicts ({time.time() - t:.2f} s)")
        if r is False:
            ok, msg = check(cls, proof)
            print(("proof VERIFIED: " if ok else "proof FAILED: ") + msg)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
