"""
franzsat: a CDCL SAT solver written from scratch in pure Python.

The same algorithm that powers modern solvers like MiniSat and CaDiCaL:
  * two watched literals for fast unit propagation
  * conflict analysis with the first unique implication point (1UIP) and clause learning
  * recursive learned-clause minimization
  * non-chronological backjumping
  * VSIDS variable activity with phase saving
  * Luby restarts
  * learned-clause database reduction using LBD ("glue")
  * DRAT proof output, so an UNSAT answer can be checked by an independent program

Literals use the DIMACS convention on the outside (3 means x3, -3 means not x3).
Inside, literal +v is stored as 2v and -v as 2v+1, so negation is `lit ^ 1`.

Usage:
    python sat.py problem.cnf                 # prints s SATISFIABLE / s UNSATISFIABLE and the model
    python sat.py problem.cnf --proof out.drat
"""
import heapq
import sys
import time

TRUE, FALSE, UNSET = 1, -1, 0


def luby(i):
    """The i-th element (1-based) of the Luby sequence 1 1 2 1 1 2 4 1 1 2 1 1 2 4 8 ..."""
    k = 1
    while (1 << k) - 1 < i:
        k += 1
    while True:
        if i == (1 << k) - 1:
            return 1 << (k - 1)
        i -= (1 << (k - 1)) - 1
        k = 1
        while (1 << k) - 1 < i:
            k += 1


class Solver:
    def __init__(self, num_vars=0, proof=None):
        self.n = 0
        self.val = [UNSET, UNSET]          # indexed by internal literal
        self.level = [0]                   # indexed by variable
        self.reason = [None]
        self.activity = [0.0]
        self.polarity = [1]                # saved phase: 1 = try false first
        self.watches = [[], []]            # watches[lit] = clauses watching lit
        self.seen = [False]
        self.trail = []
        self.trail_lim = []
        self.qhead = 0
        self.clauses = []                  # original clauses (length >= 2)
        self.learnts = []                  # learned clauses (length >= 2)
        self.lbd = {}                      # id(clause) -> glue
        self.var_inc = 1.0
        self.heap = []
        self.ok = True
        self.proof = proof                 # list collecting ("a"|"d", [dimacs lits]) or None
        self.stats = dict(decisions=0, propagations=0, conflicts=0, restarts=0, learned=0, deleted=0, minimized=0)
        self.max_learnts = 2000
        self.to_clear = []
        self.logged_units = set()
        self.ensure_vars(num_vars)

    # ------------------------------------------------------------------ variables and clauses
    def ensure_vars(self, n):
        while self.n < n:
            self.n += 1
            self.val += [UNSET, UNSET]
            self.level.append(0)
            self.reason.append(None)
            self.activity.append(0.0)
            self.polarity.append(1)
            self.watches += [[], []]
            self.seen.append(False)
            heapq.heappush(self.heap, (0.0, self.n))

    @staticmethod
    def to_internal(d):
        return 2 * d if d > 0 else -2 * d + 1

    @staticmethod
    def to_dimacs(lit):
        return lit >> 1 if not lit & 1 else -(lit >> 1)

    def log(self, kind, lits):
        if self.proof is not None:
            self.proof.append((kind, [self.to_dimacs(l) for l in lits]))

    def add_clause(self, dimacs_lits):
        """Add a clause (list of DIMACS ints). Can be called between solve() calls, e.g. to block a model."""
        if not self.ok:
            return False
        self.cancel_until(0)
        self.ensure_vars(max((abs(l) for l in dimacs_lits), default=0))
        lits = []
        for d in dimacs_lits:
            l = self.to_internal(d)
            if l ^ 1 in lits or self.val[l] == TRUE:
                return True                                   # tautology or already satisfied
            if l not in lits and self.val[l] != FALSE:
                lits.append(l)
        if len(lits) < len(dimacs_lits) and self.proof is not None and lits:
            self.log("a", lits)                               # the shortened clause follows by unit propagation
        if not lits:
            self.ok = False
            self.log("a", [])
            return False
        if len(lits) == 1:
            self.enqueue(lits[0], None)
            if self.propagate() is not None:
                self.ok = False
                self.log("a", [])
            return self.ok
        self.clauses.append(lits)
        self.watches[lits[0]].append(lits)
        self.watches[lits[1]].append(lits)
        return True

    # ------------------------------------------------------------------ assignment
    def enqueue(self, lit, reason):
        self.val[lit] = TRUE
        self.val[lit ^ 1] = FALSE
        v = lit >> 1
        self.level[v] = len(self.trail_lim)
        self.reason[v] = reason
        self.trail.append(lit)

    def cancel_until(self, lvl):
        if len(self.trail_lim) <= lvl:
            return
        start = self.trail_lim[lvl]
        val, polarity, reason, heap, activity = self.val, self.polarity, self.reason, self.heap, self.activity
        for lit in self.trail[start:]:
            v = lit >> 1
            val[lit] = val[lit ^ 1] = UNSET
            polarity[v] = lit & 1                              # phase saving
            reason[v] = None
            heapq.heappush(heap, (-activity[v], v))
        del self.trail[start:]
        del self.trail_lim[lvl:]
        self.qhead = start

    def propagate(self):
        """Unit propagation with two watched literals. Returns a conflicting clause or None."""
        val, watches, trail = self.val, self.watches, self.trail
        level_now = len(self.trail_lim)
        level, reason = self.level, self.reason
        while self.qhead < len(trail):
            false_lit = trail[self.qhead] ^ 1
            self.qhead += 1
            self.stats["propagations"] += 1
            ws = watches[false_lit]
            i = j = 0
            n = len(ws)
            while i < n:
                c = ws[i]
                i += 1
                if c[0] == false_lit:                          # keep the false watch in position 1
                    c[0], c[1] = c[1], false_lit
                first = c[0]
                if val[first] == TRUE:
                    ws[j] = c
                    j += 1
                    continue
                for k in range(2, len(c)):                     # look for a new literal to watch
                    if val[c[k]] != FALSE:
                        c[1], c[k] = c[k], false_lit
                        watches[c[1]].append(c)
                        break
                else:
                    ws[j] = c
                    j += 1
                    if val[first] == FALSE:                    # every literal is false: conflict
                        while i < n:
                            ws[j] = ws[i]
                            j += 1
                            i += 1
                        del ws[j:]
                        return c
                    val[first] = TRUE                          # unit: c[0] is forced (inlined enqueue)
                    val[first ^ 1] = FALSE
                    v = first >> 1
                    level[v] = level_now
                    reason[v] = c
                    trail.append(first)
            del ws[j:]
        return None

    # ------------------------------------------------------------------ conflict analysis
    def bump(self, v):
        self.activity[v] += self.var_inc
        if self.activity[v] > 1e100:                           # rescale everything to avoid overflow
            self.activity = [a * 1e-100 for a in self.activity]
            self.var_inc *= 1e-100
            self.heap = [(-self.activity[u], u) for u in range(1, self.n + 1) if self.val[2 * u] == UNSET]
            heapq.heapify(self.heap)
        if self.val[2 * v] == UNSET:
            heapq.heappush(self.heap, (-self.activity[v], v))

    def analyze(self, confl):
        """1UIP learning: walk back along the trail until one literal of the current level is left."""
        seen, level, reason, trail = self.seen, self.level, self.reason, self.trail
        cur = len(self.trail_lim)
        learnt = [None]
        counter = 0
        p = None
        idx = len(trail) - 1
        c = confl
        while True:
            if id(c) in self.lbd:
                self.lbd[id(c)] = min(self.lbd[id(c)], self.compute_lbd(c))
            for q in (c if p is None else c[1:]):
                v = q >> 1
                if not seen[v] and level[v] > 0:
                    seen[v] = True
                    self.bump(v)
                    if level[v] >= cur:
                        counter += 1
                    else:
                        learnt.append(q)
            while not seen[trail[idx] >> 1]:
                idx -= 1
            p = trail[idx]
            idx -= 1
            c = reason[p >> 1]
            seen[p >> 1] = False
            counter -= 1
            if counter == 0:
                break
        learnt[0] = p ^ 1

        # Recursive minimization: drop literals implied by the others.
        before = len(learnt)
        levels = {level[l >> 1] for l in learnt[1:]}
        kept = [learnt[0]] + [l for l in learnt[1:] if reason[l >> 1] is None or not self.redundant(l, levels)]
        self.stats["minimized"] += before - len(kept)
        for l in learnt:
            seen[l >> 1] = False
        for v in self.to_clear:
            seen[v] = False
        self.to_clear = []
        learnt = kept

        if len(learnt) == 1:
            back = 0
        else:                                                  # put the highest-level literal in position 1
            m = max(range(1, len(learnt)), key=lambda k: level[learnt[k] >> 1])
            learnt[1], learnt[m] = learnt[m], learnt[1]
            back = level[learnt[1] >> 1]
        return learnt, back

    def redundant(self, lit, levels):
        """Is lit implied by literals already in the learned clause? (depth-first through reasons)"""
        seen, level, reason = self.seen, self.level, self.reason
        stack = [lit]
        added = []
        while stack:
            c = reason[stack.pop() >> 1]
            for q in c[1:]:
                v = q >> 1
                if not seen[v] and level[v] > 0:
                    if reason[v] is not None and level[v] in levels:
                        seen[v] = True
                        stack.append(q)
                        added.append(v)
                    else:
                        for u in added:
                            seen[u] = False
                        return False
        self.to_clear.extend(added)
        return True

    def compute_lbd(self, c):
        return len({self.level[l >> 1] for l in c})

    # ------------------------------------------------------------------ search
    def pick_branch(self):
        if len(self.heap) > 8 * self.n + 1000:                  # too many stale entries: rebuild
            self.heap = [(-self.activity[u], u) for u in range(1, self.n + 1) if self.val[2 * u] == UNSET]
            heapq.heapify(self.heap)
        heap, val, activity = self.heap, self.val, self.activity
        while heap:
            a, v = heapq.heappop(heap)
            if val[2 * v] == UNSET and -a == activity[v]:
                return 2 * v + self.polarity[v]
        for v in range(1, self.n + 1):                         # stale heap entries only: fall back to a scan
            if val[2 * v] == UNSET:
                return 2 * v + self.polarity[v]
        return None

    def reduce_db(self):
        """At level 0: drop half of the learned clauses (worst glue first), and simplify with level-0 facts."""
        self.cancel_until(0)
        keep, cands = [], []
        for c in self.learnts:
            (keep if self.lbd[id(c)] <= 2 else cands).append(c)
        cands.sort(key=lambda c: self.lbd[id(c)])
        keep += cands[: len(cands) // 2]
        for c in cands[len(cands) // 2:]:
            self.log("d", c)
            del self.lbd[id(c)]
            self.stats["deleted"] += 1
        self.learnts = keep
        self.simplify()
        self.max_learnts = int(self.max_learnts * 1.1)

    def simplify(self):
        """Remove satisfied clauses and false literals (at level 0) and rebuild the watch lists."""
        val = self.val
        if self.proof is not None:                             # record level-0 facts before their reasons go away
            for lit in self.trail:
                if lit not in self.logged_units:
                    self.logged_units.add(lit)
                    self.log("a", [lit])
        for lst in (self.clauses, self.learnts):
            out = []
            for c in lst:
                if any(val[l] == TRUE for l in c):
                    self.log("d", c)
                    self.lbd.pop(id(c), None)
                    continue
                if any(val[l] == FALSE for l in c):
                    short = [l for l in c if val[l] != FALSE]
                    self.log("a", short)
                    self.log("d", c)
                    if id(c) in self.lbd:
                        self.lbd[id(short)] = self.lbd.pop(id(c))
                    c = short
                out.append(c)
            lst[:] = out
        self.watches = [[] for _ in range(2 * self.n + 2)]
        for c in self.clauses + self.learnts:
            self.watches[c[0]].append(c)
            self.watches[c[1]].append(c)

    def solve(self, max_conflicts=None, time_limit=None):
        """Returns True (satisfiable, model in self.model()), False (unsatisfiable) or None (gave up)."""
        if not self.ok:
            return False
        self.cancel_until(0)
        if self.propagate() is not None:
            self.ok = False
            self.log("a", [])
            return False
        self.simplify()
        start = time.time()
        restart_no = 0
        conflicts_total = 0
        while True:
            restart_no += 1
            budget = 100 * luby(restart_no)
            while True:
                confl = self.propagate()
                if confl is not None:
                    self.stats["conflicts"] += 1
                    conflicts_total += 1
                    budget -= 1
                    if not self.trail_lim:
                        self.ok = False
                        self.log("a", [])
                        return False
                    learnt, back = self.analyze(confl)
                    self.cancel_until(back)
                    self.log("a", learnt)
                    self.stats["learned"] += 1
                    if len(learnt) == 1:
                        self.enqueue(learnt[0], None)
                    else:
                        self.learnts.append(learnt)
                        self.lbd[id(learnt)] = self.compute_lbd(learnt)
                        self.watches[learnt[0]].append(learnt)
                        self.watches[learnt[1]].append(learnt)
                        self.enqueue(learnt[0], learnt)
                    self.var_inc /= 0.95
                    continue
                if max_conflicts is not None and conflicts_total >= max_conflicts:
                    return None
                if time_limit is not None and time.time() - start > time_limit:
                    return None
                if budget <= 0:
                    self.stats["restarts"] += 1
                    self.cancel_until(0)
                    if len(self.learnts) >= self.max_learnts:
                        self.reduce_db()
                    break
                lit = self.pick_branch()
                if lit is None:
                    return True                                # every variable assigned, no conflict
                self.stats["decisions"] += 1
                self.trail_lim.append(len(self.trail))
                self.enqueue(lit, None)

    def model(self):
        """The satisfying assignment as a list of DIMACS literals (unassigned variables reported false)."""
        return [v if self.val[2 * v] == TRUE else -v for v in range(1, self.n + 1)]

    def value(self, d):
        return self.val[self.to_internal(d)] == TRUE


# ---------------------------------------------------------------------- DIMACS I/O
def parse_dimacs(text):
    num_vars, clauses, cur = 0, [], []
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in "c%":
            continue
        if line[0] == "p":
            num_vars = int(line.split()[2])
            continue
        for tok in line.split():
            x = int(tok)
            if x == 0:
                clauses.append(cur)
                cur = []
            else:
                cur.append(x)
    if cur:
        clauses.append(cur)
    return num_vars, clauses


def solve_clauses(clauses, num_vars=None, proof=None, **kw):
    """Convenience: returns (result, model, solver)."""
    s = Solver(num_vars or max((abs(l) for c in clauses for l in c), default=0), proof=proof)
    for c in clauses:
        if not s.add_clause(c):
            break
    r = s.solve(**kw)
    return r, (s.model() if r else None), s


def write_drat(proof, path):
    with open(path, "w") as f:
        for kind, lits in proof:
            f.write(("d " if kind == "d" else "") + " ".join(map(str, lits)) + (" 0\n" if lits else "0\n"))


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    with open(argv[1]) as f:
        nv, clauses = parse_dimacs(f.read())
    proof = [] if "--proof" in argv else None
    t = time.time()
    r, model, s = solve_clauses(clauses, nv, proof=proof)
    print(f"c {nv} variables, {len(clauses)} clauses, {time.time() - t:.2f} s")
    print("c " + ", ".join(f"{k} {v}" for k, v in s.stats.items()))
    if r:
        print("s SATISFIABLE")
        line = "v"
        for lit in model:
            line += f" {lit}"
            if len(line) > 70:
                print(line)
                line = "v"
        print(line + " 0")
    else:
        print("s UNSATISFIABLE")
        if proof is not None:
            write_drat(proof, argv[argv.index("--proof") + 1])
            print(f"c proof written ({len(proof)} lines)")
    return 10 if r else 20


if __name__ == "__main__":
    sys.exit(main(sys.argv))
