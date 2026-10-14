"""
An independent DRAT proof checker (RUP lemmas only), used to double-check every UNSAT answer.

A proof is a list of steps: add a lemma, or delete a clause. A lemma is accepted only if it follows from the
current clauses by Reverse Unit Propagation: assume every literal of the lemma is false, run unit propagation,
and a conflict must appear. The proof is valid if every lemma is accepted and the empty clause is derived.

This file shares no code with sat.py, so a bug in the solver can't hide itself.

Usage: python drat_check.py problem.cnf proof.drat
"""
import sys
from collections import Counter, defaultdict


class Checker:
    def __init__(self, clauses):
        self.clauses = {}                    # clause id -> tuple of literals
        self.by_key = defaultdict(list)      # sorted literal tuple -> ids (for deletions)
        self.watch = defaultdict(list)       # literal -> ids of clauses watching it
        self.units = []                      # ids of unit clauses
        self.next_id = 0
        self.empty_in_formula = False
        for c in clauses:
            self.add(c)

    def add(self, lits):
        lits = tuple(dict.fromkeys(lits))
        if not lits:
            self.empty_in_formula = True
            return
        cid = self.next_id
        self.next_id += 1
        self.clauses[cid] = list(lits)
        self.by_key[tuple(sorted(lits))].append(cid)
        if len(lits) == 1:
            self.units.append(cid)
        else:
            self.watch[lits[0]].append(cid)
            self.watch[lits[1]].append(cid)

    def delete(self, lits):
        key = tuple(sorted(dict.fromkeys(lits)))
        ids = self.by_key.get(key)
        if ids:
            cid = ids.pop()
            del self.clauses[cid]            # watch entries become stale and are skipped
            return True
        return False

    def rup(self, lemma):
        """Does assuming not-lemma lead to a conflict by unit propagation?"""
        assign = {}
        queue = []

        def set_true(l):
            if assign.get(-l):
                return False
            if not assign.get(l):
                assign[l] = True
                queue.append(l)
            return True

        for l in lemma:
            if not set_true(-l):
                return True                  # the lemma is a tautology
        for cid in self.units:
            if cid in self.clauses and not set_true(self.clauses[cid][0]):
                return True
        i = 0
        while i < len(queue):
            false_lit = -queue[i]
            i += 1
            ws = self.watch[false_lit]
            keep = []
            conflict = False
            for k, cid in enumerate(ws):
                c = self.clauses.get(cid)
                if c is None or false_lit not in (c[0], c[1]):
                    continue                 # deleted clause or stale watch
                if conflict:
                    keep.append(cid)
                    continue
                if c[0] == false_lit:
                    c[0], c[1] = c[1], c[0]
                if assign.get(c[0]):
                    keep.append(cid)
                    continue
                for j in range(2, len(c)):
                    if not assign.get(-c[j]):
                        c[1], c[j] = c[j], c[1]
                        self.watch[c[1]].append(cid)
                        break
                else:
                    keep.append(cid)
                    if not set_true(c[0]):
                        conflict = True
            ws[:] = keep
            if conflict:
                return True
        return False


def check(clauses, proof):
    """clauses: list of lists of ints. proof: list of ("a"|"d", lits). Returns (ok, message)."""
    ch = Checker(clauses)
    if ch.empty_in_formula:
        return True, "formula contains the empty clause"
    for n, (kind, lits) in enumerate(proof):
        if kind == "d":
            ch.delete(lits)
            continue
        if not ch.rup(lits):
            return False, f"step {n}: lemma {lits} does not follow by unit propagation"
        if not lits:
            return True, f"empty clause derived at step {n}; {Counter(k for k, _ in proof[:n + 1])['a']} lemmas checked"
        ch.add(lits)
    return False, "the proof never derives the empty clause"


def parse_drat(text):
    steps = []
    for line in text.splitlines():
        t = line.split()
        if not t or t[0] == "c":
            continue
        kind = "d" if t[0] == "d" else "a"
        nums = [int(x) for x in (t[1:] if kind == "d" else t)]
        steps.append((kind, nums[:-1] if nums and nums[-1] == 0 else nums))
    return steps


if __name__ == "__main__":
    from sat import parse_dimacs
    _, cls = parse_dimacs(open(sys.argv[1]).read())
    ok, msg = check(cls, parse_drat(open(sys.argv[2]).read()))
    print(("s VERIFIED  " if ok else "s NOT VERIFIED  ") + msg)
    sys.exit(0 if ok else 1)
