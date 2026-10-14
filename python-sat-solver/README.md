# franzsat: a CDCL SAT solver in pure Python

A SAT solver decides whether a logic formula like `(x1 or not x2) and (x2 or x3) and ...` can be made true, and if so, how. Many hard problems can be turned into SAT: puzzles, scheduling, hardware verification, and package dependency resolution. This solver uses the same algorithm as modern solvers like MiniSat and CaDiCaL, written from scratch with no libraries.

It doesn't just answer "unsatisfiable"; it **proves** it. Every UNSAT answer comes with a DRAT proof, which a separate checker (`drat_check.py`, sharing no code with the solver) verifies step by step.

## Run it
```bash
python sat.py examples/random_3sat_100.cnf                 # standard DIMACS input/output
python sat.py examples/pigeonhole_7_6.cnf --proof p.drat   # write a proof of unsatisfiability
python drat_check.py examples/pigeonhole_7_6.cnf p.drat    # verify it independently

python puzzles.py sudoku 800000000003600000070090200050007000000045700000100030001000068008500010090000400
python puzzles.py queens 8 --count     # 92
python puzzles.py pigeons 7            # 8 pigeons can't fit in 7 holes, with a checked proof
```
```
8 1 2 | 7 5 3 | 6 4 9
9 4 3 | 6 8 2 | 1 7 5
6 7 5 | 4 9 1 | 2 8 3
------+-------+------
...
unique solution, 65 conflicts, 0.04 s
```

## How it works
| Technique | What it does |
|---|---|
| **Two watched literals** | Each clause watches two of its literals and is only looked at when one of them becomes false. Most clauses are never touched during propagation |
| **Conflict analysis (1UIP)** | On a contradiction, it walks the implication graph back to the *first unique implication point* and learns a new clause that blocks this mistake and many similar ones |
| **Clause minimization** | Learned literals already implied by other literals in the clause are removed (this removed 2.3 literals per clause on the pigeonhole problem) |
| **Backjumping** | It jumps back over decisions that had nothing to do with the conflict, not just one level |
| **VSIDS + phase saving** | Branches on the variables involved in recent conflicts, and remembers the last value each variable had |
| **Luby restarts** | Restarts on the schedule 1 1 2 1 1 2 4 … × 100 conflicts, keeping everything it learned |
| **Clause database reduction** | Throws away half the learned clauses, keeping "glue" clauses (LBD ≤ 2) that link few decision levels |
| **DRAT proofs** | Logs every learned and deleted clause so UNSAT can be verified by reverse unit propagation |

Puzzles are encoded as clauses ("exactly one digit per cell", "at most one queen per diagonal"). To check that a Sudoku is **unique**, the solver adds a clause forbidding the solution it found and solves again; UNSAT means there is no other answer. N-queens solutions are counted the same way.

## Testing
```bash
pip install python-sat   # optional: lets the tests compare against CaDiCaL
python test_sat.py       # about 10 seconds
```
Every answer is certified: models are plugged back into the formula, and UNSAT proofs go through the independent checker.

| Test | Result |
|---|---|
| 600 random formulas vs **brute force** (every assignment tried) | same answer every time; 182 UNSAT proofs verified |
| 300 random 3-SAT at the hard 4.26 ratio, 20–90 variables, vs **CaDiCaL** | same answer every time; all certified |
| Random 3-SAT, 200 variables | solved in about 3 s |
| Pigeonhole 8→7 (exponentially hard for this kind of proof) | UNSAT in 1.9 s, 3,134-lemma proof verified |
| N-queens counts for N = 1..8 | 1 0 0 2 10 4 40 92 (correct) |
| Inkala's "world's hardest Sudoku", Platinum Blonde, 17-clue Sudoku | solved and **proven unique** in about 0.05 s each |
| 16×16 Sudoku (4,096 variables) | solved in 0.3 s |
| Proof checker sanity | rejects fake proofs |
