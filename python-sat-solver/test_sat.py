"""
Tests for franzsat. Every SAT answer is checked by plugging the model into the formula, every UNSAT answer
is checked with an independent DRAT proof checker, and answers are compared with brute force and with
CaDiCaL (a competition-winning solver, via `pip install python-sat`, skipped if missing).

Run:  python test_sat.py
"""
import itertools
import random
import sys
import time

from drat_check import check
from puzzles import pigeonhole_clauses, queens, solve_sudoku, sudoku_valid
from sat import Solver, luby, parse_dimacs, solve_clauses

passed = failed = 0


def ok(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


def random_ksat(rng, n, m, k=3):
    return [[rng.choice([-1, 1]) * v for v in rng.sample(range(1, n + 1), k)] for _ in range(m)]


def satisfies(model, clauses):
    ms = set(model)
    return all(any(l in ms for l in c) for c in clauses)


def brute_force(n, clauses):
    for bits in itertools.product([False, True], repeat=n):
        if all(any(bits[abs(l) - 1] == (l > 0) for l in c) for c in clauses):
            return True
    return False


rng = random.Random(2026)

print("Basics:")
ok("Luby sequence 1 1 2 1 1 2 4 1 1 2 1 1 2 4 8", [luby(i) for i in range(1, 16)] == [1, 1, 2, 1, 1, 2, 4, 1, 1, 2, 1, 1, 2, 4, 8])
ok("empty formula is SAT", solve_clauses([], 3)[0] is True)
ok("empty clause is UNSAT", solve_clauses([[1], []], 1)[0] is False)
ok("x and not x is UNSAT", solve_clauses([[1], [-1]])[0] is False)
ok("tautologies and duplicate literals are handled", solve_clauses([[1, -1], [2, 2, 2], [-2, 3, 3]])[1][1:] == [2, 3])
nv, cls = parse_dimacs("c comment\np cnf 3 2\n1 -3 0\n2 3\n-1 0\n")
ok("DIMACS parser (clauses may span lines)", nv == 3 and cls == [[1, -3], [2, 3, -1]], cls)

print("\nRandom 3-SAT vs brute force (every assignment tried):")
mism = bad_models = bad_proofs = sat_n = 0
for t in range(600):
    n = rng.randint(1, 12)
    clauses = random_ksat(rng, n, rng.randint(1, 7 * n), k=min(3, n))
    proof = []
    r, model, _ = solve_clauses(clauses, n, proof=proof)
    if r != brute_force(n, clauses):
        mism += 1
    if r:
        sat_n += 1
        bad_models += not satisfies(model, clauses)
    else:
        bad_proofs += not check(clauses, proof)[0]
ok(f"600 formulas ({sat_n} SAT, {600 - sat_n} UNSAT): same answer as brute force every time", mism == 0, mism)
ok("every model satisfies its formula", bad_models == 0, bad_models)
ok("every UNSAT proof is accepted by the independent DRAT checker", bad_proofs == 0, bad_proofs)

try:
    from pysat.solvers import Cadical153
except ImportError:
    Cadical153 = None
print("\nRandom 3-SAT at the hard ratio (4.26 clauses per variable) vs CaDiCaL:")
if Cadical153 is None:
    print("  SKIP  python-sat not installed")
else:
    mism = bad = unsat = 0
    t0 = time.time()
    for t in range(300):
        n = rng.randint(20, 90)
        clauses = random_ksat(rng, n, round(4.26 * n))
        proof = []
        r, model, _ = solve_clauses(clauses, n, proof=proof)
        with Cadical153(bootstrap_with=clauses) as c:
            mism += r != c.solve()
        if r:
            bad += not satisfies(model, clauses)
        else:
            unsat += 1
            bad += not check(clauses, proof)[0]
    ok(f"300 formulas with 20-90 variables ({300 - unsat} SAT, {unsat} UNSAT): same answer as CaDiCaL", mism == 0, mism)
    ok(f"all 300 answers certified: models checked, UNSAT proofs verified ({time.time() - t0:.1f} s)", bad == 0, bad)

print("\nHarder instances:")
for n in (150, 200):
    clauses = random_ksat(rng, n, round(4.2 * n))
    t0 = time.time()
    r, model, s = solve_clauses(clauses, n)
    ref = None
    if Cadical153:
        with Cadical153(bootstrap_with=clauses) as c:
            ref = c.solve()
    ok(f"random 3-SAT, {n} variables / {len(clauses)} clauses: {'SAT' if r else 'UNSAT'} in {time.time() - t0:.1f} s, "
       f"{s.stats['conflicts']} conflicts", (ref is None or r == ref) and (not r or satisfies(model, clauses)))
for h in (6, 7):
    clauses = pigeonhole_clauses(h)
    proof = []
    t0 = time.time()
    r, _, s = solve_clauses(clauses, proof=proof)
    good, msg = check(clauses, proof)
    ok(f"pigeonhole {h + 1}->{h}: UNSAT in {time.time() - t0:.1f} s, proof verified ({msg.split('; ')[-1]})", r is False and good, msg)

bogus = [("a", [1]), ("a", [])]
ok("the checker rejects a bogus proof (claims x1 without reason)", not check(pigeonhole_clauses(3), bogus)[0])
ok("the checker rejects an 'empty clause' claimed for a satisfiable formula", not check([[1, 2], [-1, 2]], [("a", [2]), ("a", [])])[0])

print("\nPuzzles:")
counts = [queens(n, count=True) for n in range(1, 9)]
ok(f"N-queens solution counts for N=1..8 by blocking clauses: {counts}", counts == [1, 0, 0, 2, 10, 4, 40, 92])
cols = queens(40)
ok("40-queens: a valid placement", len(set(cols)) == 40 and len({r + c for r, c in enumerate(cols)}) == 40
   and len({r - c for r, c in enumerate(cols)}) == 40)
HARD = {
    "Arto Inkala's 'world's hardest'": "800000000003600000070090200050007000000045700000100030001000068008500010090000400",
    "Platinum Blonde": "000000012000000003002300400001800005060070800000009000008500000900040500470006000",
    "17 clues (minimum possible)": "000000010400000000020000000000050407008000300001090000300400200050100000000806000",
}
for name, grid in HARD.items():
    t0 = time.time()
    sol, unique, s = solve_sudoku(grid)
    ok(f"Sudoku {name}: valid and proven unique ({time.time() - t0:.2f} s)", sol and sudoku_valid(sol, grid) and unique)
ok("a Sudoku with two answers is reported as not unique", solve_sudoku("0" * 81)[1] is False)
ok("an impossible Sudoku (two 5s in a row) is UNSAT", solve_sudoku("55" + "0" * 79)[0] is None)
# 16x16: build a valid full grid from a pattern, hide 55% of it, solve
B, N = 4, 16
full = [[(B * (r % B) + r // B + c) % N for c in range(N)] for r in range(N)]
digits = "123456789ABCDEFG"
grid = "".join(digits[full[r][c]] if rng.random() > 0.55 else "0" for r in range(N) for c in range(N))
t0 = time.time()
sol, _, s = solve_sudoku(grid, check_unique=False)
ok(f"16x16 Sudoku ({grid.count('0')} blanks, 4096 variables): valid ({time.time() - t0:.1f} s)", sol and sudoku_valid(sol, grid))

print("\nIncremental use:")
s = Solver(3)
for c in [[1, 2, 3]]:
    s.add_clause(c)
models = set()
while s.solve():
    m = tuple(s.model())
    models.add(m)
    s.add_clause([-l for l in m])
ok("enumerating all models of (x1 or x2 or x3) by blocking finds exactly 7", len(models) == 7)

print(f"\n{passed} passed, {failed} failed")
print("ALL TESTS PASSED" if failed == 0 else "SOME TESTS FAILED")
sys.exit(1 if failed else 0)
