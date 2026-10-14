/*
 * Tests for the FranzSheets formula engine.   Run:  node test_engine.js
 *
 *  1. Known answers (Excel's rules: -2^2 = 4, 2^3^2 = 64, errors, text, booleans, functions)
 *  2. Parser fuzzing: random formulas are built as trees, printed with the FEWEST parentheses Excel's
 *     precedence allows, parsed back, and evaluated. The result must equal evaluating the tree directly.
 *  3. Incremental recalculation: random sheets with chains, ranges and cycles get thousands of random edits.
 *     After every edit, each cell must equal a from-scratch recursive evaluation, and only the cells that
 *     depend on the edit may be recalculated.
 *  4. Copy/paste reference shifting.
 */
"use strict";
const E = require("./engine.js");

let passed = 0, failed = 0;
function check(label, ok, detail = "") {
  if (ok) { passed++; console.log("  PASS  " + label); }
  else { failed++; console.log("  FAIL  " + label + "  " + detail); }
}

// ============================================================================ 1. known answers
function knownAnswers() {
  console.log("Known answers:");
  const cases = [
    ["=1+2*3", "7"], ["=(1+2)*3", "9"], ["=-2^2", "4"], ["=2^3^2", "64"], ["=10-4-3", "3"], ["=2*-3", "-6"],
    ["=50%", "0.5"], ["=1/0", "#DIV/0!"], ["=0^0", "#NUM!"], ["=SQRT(-1)", "#NUM!"], ["=\"a\"+1", "#VALUE!"],
    ["=\"5\"+1", "6"], ["=TRUE+1", "2"], ["=1=1", "TRUE"], ["=\"abc\"=\"ABC\"", "TRUE"], ["=\"b\">\"a\"", "TRUE"],
    ["=1<\"a\"", "TRUE"], ["=\"x\"&1&TRUE", "x1TRUE"], ["=ROUND(2.5)", "3"], ["=ROUND(-2.5)", "-3"],
    ["=ROUND(3.14159, 2)", "3.14"], ["=MOD(-7, 3)", "2"], ["=MOD(7, -3)", "-2"], ["=IF(1>2, \"yes\")", "FALSE"],
    ["=IF(1/0, 1, 2)", "#DIV/0!"], ["=IF(TRUE, 1, 1/0)", "1"], ["=IFERROR(1/0, \"safe\")", "safe"],
    ["=AND(TRUE, 1, 2>1)", "TRUE"], ["=OR(FALSE, 0)", "FALSE"], ["=NOT(0)", "TRUE"], ["=LEN(\"hello\")", "5"],
    ["=UPPER(\"Liceo\")&LOWER(\"CDO\")", "LICEOcdo"], ["=CONCAT(\"a\", 1, \"b\")", "a1b"], ["=0.1+0.2", "0.3"],
    ["=nosuchfunc(1)", "#NAME?"], ["=hello", "#NAME?"], ["=1+", "#ERROR!"], ["=(1", "#ERROR!"], ["=SUM(1, \"2\", TRUE)", "4"],
    ["=ABS(-3)*PI()/PI()", "3"], ["=POWER(2, 10)", "1024"], ["=MIN()", "#VALUE!"], ["=COUNT(1, \"a\", 2)", "2"],
  ];
  let bad = [];
  for (const [f, want] of cases) {
    const s = new E.Sheet();
    s.set("A1", f);
    if (s.display("A1") !== want) bad.push(`${f} -> ${s.display("A1")} (want ${want})`);
  }
  check(`${cases.length} formulas give Excel's answers`, bad.length === 0, bad.join("; "));

  const s = new E.Sheet();
  ["10", "20", "abc", "", "30", "TRUE"].forEach((v, i) => s.set("A" + (i + 1), v));
  s.set("B1", "=SUM(A1:A6)"); s.set("B2", "=AVERAGE(A1:A6)"); s.set("B3", "=COUNT(A1:A6)"); s.set("B4", "=COUNTA(A1:A6)");
  s.set("B5", "=MAX(A1:A6)"); s.set("B6", "=A1:A3"); s.set("B7", "=COUNTIF(A1:A6, \">15\")"); s.set("B8", "=SUMIF(A1:A6, \"<>20\")");
  check("ranges skip text, blanks and booleans like Excel",
    ["60", "20", "3", "5", "30", "#VALUE!", "2", "40"].every((v, i) => s.display("B" + (i + 1)) === v),
    ["B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8"].map((k) => s.display(k)).join(","));
  s.set("C1", "=C2+1"); s.set("C2", "=C3+1"); s.set("C3", "=C1+1"); s.set("C4", "=C1*2");
  check("cycles are detected, and errors flow into cells that read a cycle",
    ["C1", "C2", "C3", "C4"].every((k) => s.display(k) === "#CYCLE!"));
  s.set("C3", "5");
  check("breaking the cycle recalculates every cell in it", s.display("C1") === "7" && s.display("C2") === "6" && s.display("C4") === "14");
  s.set("E1", "=E2"); s.set("E2", "=E1"); s.set("E3", "=IF(TRUE, 5, E1)");
  check("only cells ON a cycle are #CYCLE!: an IF can route around one", s.display("E1") === "#CYCLE!" && s.display("E3") === "5");
  s.set("D1", "=Z100+1"); s.set("Z100", "41");
  check("formulas can refer to cells that don't exist yet", s.display("D1") === "42");
}

// ============================================================================ 2. parser fuzzing
const PREC = { "+": 3, "-": 3, "*": 4, "/": 4, "^": 5 };

function randomTree(rng, depth) {
  if (depth === 0 || rng() < 0.25) {
    const n = Math.floor(rng() * 9) + (rng() < 0.3 ? 0.5 : 0);
    return { k: "num", v: n };
  }
  if (rng() < 0.15) return { k: "neg", a: randomTree(rng, depth - 1) };
  const ops = Object.keys(PREC);
  return { k: "bin", op: ops[Math.floor(rng() * ops.length)], a: randomTree(rng, depth - 1), b: randomTree(rng, depth - 1) };
}

// print with the minimum parentheses Excel needs: unary minus binds tighter than ^, all binary ops are left-associative
function render(t) {
  if (t.k === "num") return String(t.v);
  if (t.k === "neg") {
    const inner = render(t.a);
    return "-" + (t.a.k === "bin" ? "(" + inner + ")" : inner);
  }
  const p = PREC[t.op];
  const left = t.a.k === "bin" && PREC[t.a.op] < p ? "(" + render(t.a) + ")" : render(t.a);
  const right = t.b.k === "bin" && PREC[t.b.op] <= p ? "(" + render(t.b) + ")" : render(t.b);
  return left + t.op + right;
}

function oracle(t) {          // returns a number or an error code string
  if (t.k === "num") return t.v;
  if (t.k === "neg") { const x = oracle(t.a); return typeof x === "string" ? x : -x; }
  const a = oracle(t.a), b = oracle(t.b);
  if (typeof a === "string") return a;
  if (typeof b === "string") return b;
  let r;
  switch (t.op) {
    case "+": r = a + b; break;
    case "-": r = a - b; break;
    case "*": r = a * b; break;
    case "/": if (b === 0) return "#DIV/0!"; r = a / b; break;
    case "^": if (a === 0 && b === 0) return "#NUM!"; r = Math.pow(a, b); break;
  }
  return Number.isFinite(r) ? r : "#NUM!";
}

function mulberry(seed) {
  return () => { seed |= 0; seed = seed + 0x6D2B79F5 | 0; let t = Math.imul(seed ^ seed >>> 15, 1 | seed);
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; };
}

function parserFuzz(n = 20000) {
  console.log("\nParser fuzzing:");
  const rng = mulberry(42);
  let bad = 0, example = "";
  for (let i = 0; i < n; i++) {
    const t = randomTree(rng, 1 + Math.floor(rng() * 5));
    const f = "=" + render(t);
    const s = new E.Sheet();
    s.set("A1", f);
    const got = s.value("A1"), want = oracle(t);
    const ok = typeof want === "string" ? E.isErr(got) && got.code === want : got === want;
    if (!ok) { bad++; if (!example) example = `${f}: got ${s.display("A1")}, want ${want}`; }
  }
  check(`${n} random formulas printed with minimal parentheses parse and evaluate exactly right`, bad === 0, `${bad} wrong, e.g. ${example}`);
}

// ============================================================================ 3. incremental recalculation vs from-scratch evaluation
function recalcFuzz(rounds = 40, editsPerRound = 120) {
  console.log("\nIncremental recalculation vs from-scratch evaluation:");
  const rng = mulberry(7);
  const cols = "ABCDEF", rows = 8;
  const keys = [];
  for (const c of cols) for (let r = 1; r <= rows; r++) keys.push(c + r);
  const pick = () => keys[Math.floor(rng() * keys.length)];

  function randomFormula() {          // returns { raw, node } so the oracle evaluates the same thing
    const r = rng();
    if (r < 0.25) { const v = Math.floor(rng() * 20) - 5; return { raw: String(v), node: { k: "num", v } }; }
    if (r < 0.32) return { raw: "", node: null };
    if (r < 0.5) { const a = pick(), b = pick(); const op = "+-*/"[Math.floor(rng() * 4)];
      return { raw: `=${a}${op}${b}`, node: { k: "bin", op, a: { k: "ref", key: a }, b: { k: "ref", key: b } } }; }
    if (r < 0.65) { const a = pick(); const v = Math.floor(rng() * 5) + 1;
      return { raw: `=${a}*${v}+1`, node: { k: "bin", op: "+", a: { k: "bin", op: "*", a: { k: "ref", key: a }, b: { k: "num", v } }, b: { k: "num", v: 1 } } }; }
    if (r < 0.85) {
      const c = cols[Math.floor(rng() * cols.length)], r1 = 1 + Math.floor(rng() * rows), r2 = Math.min(rows, r1 + Math.floor(rng() * 4));
      const ks = []; for (let x = r1; x <= r2; x++) ks.push(c + x);
      return { raw: `=SUM(${c}${r1}:${c}${r2})`, node: { k: "sum", keys: ks } };
    }
    const a = pick(), b = pick(), t = Math.floor(rng() * 10);
    return { raw: `=IF(${a}>${t},${b},0)`, node: { k: "if", a, t, b } };
  }

  function deps(node, out = new Set()) {
    if (!node) return out;
    if (node.k === "ref") out.add(node.key);
    if (node.k === "bin") { deps(node.a, out); deps(node.b, out); }
    if (node.k === "sum") node.keys.forEach((k) => out.add(k));
    if (node.k === "if") { out.add(node.a); out.add(node.b); }
    return out;
  }

  function oracleSheet(formulas) {    // brand-new evaluation of every cell, with recursion + cycle detection
    const memo = new Map(), CYC = { err: "#CYCLE!" };   // eslint-disable-line
    const onCycle = new Set();
    // a cell is #CYCLE! only if it lies ON a cycle: it can reach itself by following dependencies
    for (const k of keys) {
      const seen = new Set(), todo = [...deps(formulas[k] && formulas[k].node)];
      while (todo.length) {
        const d = todo.pop();
        if (d === k) { onCycle.add(k); break; }
        if (seen.has(d)) continue;
        seen.add(d);
        todo.push(...deps(formulas[d] && formulas[d].node));
      }
    }
    function val(k) {
      if (memo.has(k)) return memo.get(k);
      const f = formulas[k];
      let v;
      if (!f || !f.node) v = null;
      else if (onCycle.has(k)) v = CYC;
      else v = ev(f.node);
      memo.set(k, v);
      return v;
    }
    const num = (x) => x === null ? 0 : x;
    function ev(n) {
      switch (n.k) {
        case "num": return n.v;
        case "ref": return val(n.key);
        case "bin": {
          const a = ev(n.a), b = ev(n.b);
          if (a && a.err) return a;
          if (b && b.err) return b;
          const x = num(a), y = num(b);
          if (n.op === "/") return y === 0 ? { err: "#DIV/0!" } : x / y;
          return n.op === "+" ? x + y : n.op === "-" ? x - y : x * y;
        }
        case "sum": {
          let s = 0;
          for (const k of n.keys) { const v = val(k); if (v && v.err) return v; if (typeof v === "number") s += v; }
          return s;
        }
        case "if": {
          const a = val(n.a);
          if (a && a.err) return a;
          return num(a) > n.t ? val(n.b) : 0;
        }
      }
    }
    const out = {};
    keys.forEach((k) => { const v = val(k); out[k] = v === null ? "" : v.err ? v.err : E.formatNumber(v); });
    return out;
  }

  let mismatches = 0, edits = 0, cycleEdits = 0, overWork = 0, example = "";
  for (let round = 0; round < rounds; round++) {
    const sheet = new E.Sheet();
    const formulas = {};
    for (let e = 0; e < editsPerRound; e++) {
      const key = pick(), f = randomFormula();
      formulas[key] = f;
      const before = sheet.recalcCount;
      const changed = sheet.set(key, f.raw);
      edits++;
      // only the edited cell and cells that (transitively) read it may be recalculated
      const allowed = new Set([key]);
      let grew = true;
      while (grew) { grew = false; for (const k of keys) if (!allowed.has(k) && [...deps(formulas[k] && formulas[k].node)].some((d) => allowed.has(d))) { allowed.add(k); grew = true; } }
      if (changed.some((k) => !allowed.has(k)) || sheet.recalcCount - before > allowed.size) overWork++;
      const want = oracleSheet(formulas);
      if (Object.values(want).includes("#CYCLE!")) cycleEdits++;
      for (const k of keys) {
        if (sheet.display(k) !== want[k]) {
          mismatches++;
          if (!example) {
            example = `after setting ${key} to "${f.raw}": ${k} is ${sheet.display(k)}, expected ${want[k]}`;
            if (process.env.DEBUG) console.log(Object.entries(formulas).filter(([, x]) => x.raw).map(([kk, x]) => kk + ": " + x.raw + " -> " + sheet.display(kk)).join("\n"));
          }
          break;
        }
      }
    }
    const fresh = E.Sheet.fromJSON(sheet.toJSON());          // rebuilding from scratch gives the same answers
    if (keys.some((k) => fresh.display(k) !== sheet.display(k))) mismatches++;
  }
  check(`${edits} random edits (${cycleEdits} while a cycle existed): every cell equals a from-scratch evaluation`, mismatches === 0, example);
  check("each edit recalculates only the cells that depend on it", overWork === 0, `${overWork} edits did extra work`);
}

// ============================================================================ 4. copy / paste
function shifting() {
  console.log("\nCopy/paste reference shifting:");
  const cases = [
    ["=A1+B2", 1, 1, "=B2+C3"], ["=$A$1+A$1+$A1", 2, 3, "=$A$1+D$1+$A3"], ["=SUM(A1:B3)*2", 5, 0, "=SUM(A6:B8)*2"],
    ["=A1&\"A1\"", 1, 0, "=A2&\"A1\""], ["=A1", -1, 0, "=#REF!"], ["=Z1", 0, 1, "=#REF!"], ["=LOG10(A1)", 1, 0, "=LOG10(A2)"],
    ["hello A1", 3, 3, "hello A1"], ["=a1+b1", 1, 0, "=A2+B2"],
  ];
  const bad = cases.filter(([f, dr, dc, want]) => E.shiftFormula(f, dr, dc) !== want)
    .map(([f, dr, dc, want]) => `${f} by (${dr},${dc}) -> ${E.shiftFormula(f, dr, dc)} (want ${want})`);
  check(`${cases.length} shifting rules ($ anchors, ranges, strings, off-sheet -> #REF!)`, bad.length === 0, bad.join("; "));
  const s = new E.Sheet();
  s.set("A1", "=" + "B1+C1");
  s.set("A2", E.shiftFormula(s.raw("A1"), 1, 0));
  s.set("B2", "3"); s.set("C2", "4"); s.set("A3", E.shiftFormula("=A1-1", -2, 0));
  check("pasted formulas recalculate in their new place; off-sheet refs show #REF!", s.display("A2") === "7" && s.display("A3") === "#REF!");
}

knownAnswers();
parserFuzz();
recalcFuzz();
shifting();
console.log(`\n${passed} passed, ${failed} failed`);
console.log(failed === 0 ? "ALL TESTS PASSED" : "SOME TESTS FAILED");
process.exit(failed ? 1 : 0);
