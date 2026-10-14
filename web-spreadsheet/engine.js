/*
 * FranzSheets formula engine. No dependencies. Works in the browser (window.SheetEngine) and in Node (require).
 *
 *   tokenizer  ->  Pratt parser (Excel precedence)  ->  syntax tree  ->  evaluator
 *   every cell's references form a dependency graph; editing a cell recalculates only the cells that
 *   depend on it, in topological order (Kahn's algorithm). Cells caught in a cycle become #CYCLE!.
 */
(function (root) {
  "use strict";

  const COLS = 26, ROWS = 100;

  // ------------------------------------------------------------------ cell names
  function colName(c) { return String.fromCharCode(65 + c); }
  function keyOf(r, c) { return colName(c) + (r + 1); }
  function parseKey(key) {
    const m = /^([A-Z])([0-9]+)$/.exec(key);
    if (!m) return null;
    const c = m[1].charCodeAt(0) - 65, r = parseInt(m[2], 10) - 1;
    return r >= 0 && r < ROWS ? { r, c } : null;
  }

  // ------------------------------------------------------------------ errors and values
  class CellError {
    constructor(code) { this.code = code; }
    toString() { return this.code; }
  }
  const ERR = (code) => new CellError(code);
  const isErr = (v) => v instanceof CellError;

  function toNumber(v) {
    if (isErr(v)) return v;
    if (v === null || v === undefined || v === "") return 0;
    if (typeof v === "number") return v;
    if (typeof v === "boolean") return v ? 1 : 0;
    const t = String(v).trim();
    if (t !== "" && !isNaN(Number(t))) return Number(t);
    return ERR("#VALUE!");
  }
  function toText(v) {
    if (v === null || v === undefined) return "";
    if (typeof v === "boolean") return v ? "TRUE" : "FALSE";
    if (typeof v === "number") return formatNumber(v);
    return String(v);
  }
  function toBool(v) {
    if (isErr(v)) return v;
    if (typeof v === "boolean") return v;
    if (v === null || v === "") return false;
    if (typeof v === "number") return v !== 0;
    const t = String(v).toUpperCase();
    if (t === "TRUE") return true;
    if (t === "FALSE") return false;
    return ERR("#VALUE!");
  }
  function checkNum(x) { return Number.isFinite(x) ? x : ERR("#NUM!"); }
  function formatNumber(n) {
    if (Number.isInteger(n)) return String(n);
    return String(parseFloat(n.toPrecision(12)));
  }

  // ------------------------------------------------------------------ tokenizer
  const TOKEN = /\s*(?:(\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)|("(?:[^"]|"")*")|(\$?[A-Za-z]\$?\d+(?::\$?[A-Za-z]\$?\d+)?)(?![A-Za-z0-9_(])|([A-Za-z_][A-Za-z0-9_.]*)|(<>|<=|>=|[-+*/^&=<>%(),:]))/y;

  function tokenize(src) {
    const out = [];
    TOKEN.lastIndex = 0;
    let pos = 0;
    while (pos < src.length) {
      if (/^\s*$/.test(src.slice(pos))) break;
      TOKEN.lastIndex = pos;
      const m = TOKEN.exec(src);
      if (!m) throw new SyntaxError("Unexpected character at " + pos);
      pos = TOKEN.lastIndex;
      if (m[1] !== undefined) out.push({ t: "num", v: parseFloat(m[1]) });
      else if (m[2] !== undefined) out.push({ t: "str", v: m[2].slice(1, -1).replace(/""/g, '"') });
      else if (m[3] !== undefined) out.push({ t: "ref", v: m[3].toUpperCase() });
      else if (m[4] !== undefined) out.push({ t: "name", v: m[4].toUpperCase() });
      else out.push({ t: "op", v: m[5] });
    }
    out.push({ t: "eof" });
    return out;
  }

  // ------------------------------------------------------------------ parser (Pratt / precedence climbing)
  // Excel's order, lowest first:  comparisons < & < + - < * / < ^ < unary minus < %
  // Note: in Excel, -2^2 = 4 (unary minus binds tighter than ^), and ^ is left-associative: 2^3^2 = 64.
  const BINARY = {
    "=": 1, "<>": 1, "<": 1, ">": 1, "<=": 1, ">=": 1,
    "&": 2, "+": 3, "-": 3, "*": 4, "/": 4, "^": 5,
  };

  function parse(src) {
    const toks = tokenize(src);
    let i = 0;
    const peek = () => toks[i];
    const next = () => toks[i++];
    const expectOp = (v) => {
      const t = next();
      if (t.t !== "op" || t.v !== v) throw new SyntaxError("Expected " + v);
    };

    function expr(minPrec) {
      let left = unary();
      for (;;) {
        const t = peek();
        if (t.t !== "op" || !(t.v in BINARY) || BINARY[t.v] < minPrec) return left;
        next();
        const right = expr(BINARY[t.v] + 1);             // +1 = left-associative
        left = { type: "bin", op: t.v, a: left, b: right };
      }
    }
    function unary() {
      const t = peek();
      if (t.t === "op" && (t.v === "-" || t.v === "+")) {
        next();
        const operand = unary();
        return t.v === "-" ? { type: "neg", a: operand } : operand;
      }
      return postfix();
    }
    function postfix() {
      let e = primary();
      while (peek().t === "op" && peek().v === "%") { next(); e = { type: "pct", a: e }; }
      return e;
    }
    function primary() {
      const t = next();
      if (t.t === "num") return { type: "num", v: t.v };
      if (t.t === "str") return { type: "str", v: t.v };
      if (t.t === "ref") {
        if (t.v.includes(":")) {
          const [a, b] = t.v.split(":");
          return { type: "range", a: refInfo(a), b: refInfo(b) };
        }
        return { type: "ref", ...refInfo(t.v) };
      }
      if (t.t === "name") {
        if (peek().t === "op" && peek().v === "(") {
          next();
          const args = [];
          if (!(peek().t === "op" && peek().v === ")")) {
            for (;;) {
              args.push(expr(0));
              if (peek().t === "op" && peek().v === ",") { next(); continue; }
              break;
            }
          }
          expectOp(")");
          return { type: "call", name: t.v, args };
        }
        if (t.v === "TRUE" || t.v === "FALSE") return { type: "bool", v: t.v === "TRUE" };
        return { type: "badname", v: t.v };
      }
      if (t.t === "op" && t.v === "(") {
        const e = expr(0);
        expectOp(")");
        return e;
      }
      throw new SyntaxError("Unexpected " + (t.v === undefined ? "end of formula" : t.v));
    }
    const tree = expr(0);
    if (peek().t !== "eof") throw new SyntaxError("Unexpected " + peek().v);
    return tree;
  }

  function refInfo(text) {
    const m = /^(\$?)([A-Z])(\$?)(\d+)$/.exec(text);
    const c = m[2].charCodeAt(0) - 65, r = parseInt(m[4], 10) - 1;
    return { r, c, absC: m[1] === "$", absR: m[3] === "$", valid: r >= 0 && r < ROWS };
  }

  // ------------------------------------------------------------------ references a formula reads
  function collectRefs(node, out) {
    if (!node) return out;
    switch (node.type) {
      case "ref": if (node.valid) out.add(keyOf(node.r, node.c)); break;
      case "range": {
        const r1 = Math.min(node.a.r, node.b.r), r2 = Math.max(node.a.r, node.b.r);
        const c1 = Math.min(node.a.c, node.b.c), c2 = Math.max(node.a.c, node.b.c);
        for (let r = Math.max(r1, 0); r <= Math.min(r2, ROWS - 1); r++)
          for (let c = c1; c <= c2; c++) out.add(keyOf(r, c));
        break;
      }
      case "bin": collectRefs(node.a, out); collectRefs(node.b, out); break;
      case "neg": case "pct": collectRefs(node.a, out); break;
      case "call": node.args.forEach((a) => collectRefs(a, out)); break;
    }
    return out;
  }

  // ------------------------------------------------------------------ functions
  function flatten(args) {                    // range arguments arrive as arrays
    const out = [];
    for (const a of args) Array.isArray(a) ? out.push(...a) : out.push({ v: a, direct: true });
    return out;
  }
  function numbersOf(args) {
    const nums = [];
    for (const item of flatten(args)) {
      const v = item.v;
      if (isErr(v)) return v;
      if (item.direct) {                       // typed directly as an argument: convert like Excel
        const n = toNumber(v);
        if (isErr(n)) return n;
        nums.push(n);
      } else if (typeof v === "number") nums.push(v);   // inside ranges, text and blanks are skipped
    }
    return nums;
  }
  const FUNCS = {
    SUM: (args) => { const n = numbersOf(args); return isErr(n) ? n : n.reduce((s, x) => s + x, 0); },
    AVERAGE: (args) => { const n = numbersOf(args); return isErr(n) ? n : n.length ? n.reduce((s, x) => s + x, 0) / n.length : ERR("#DIV/0!"); },
    MIN: (args) => { const n = numbersOf(args); return isErr(n) ? n : n.length ? Math.min(...n) : 0; },
    MAX: (args) => { const n = numbersOf(args); return isErr(n) ? n : n.length ? Math.max(...n) : 0; },
    COUNT: (args) => flatten(args).filter((x) => typeof x.v === "number").length,
    COUNTA: (args) => flatten(args).filter((x) => x.v !== null && x.v !== "").length,
    ABS: ([x]) => { const n = toNumber(x); return isErr(n) ? n : Math.abs(n); },
    SQRT: ([x]) => { const n = toNumber(x); return isErr(n) ? n : n < 0 ? ERR("#NUM!") : Math.sqrt(n); },
    ROUND: ([x, d]) => {
      const n = toNumber(x), k = d === undefined ? 0 : toNumber(d);
      if (isErr(n)) return n;
      if (isErr(k)) return k;
      const f = Math.pow(10, Math.trunc(k));
      return Math.sign(n) * Math.round(Math.abs(n) * f + 1e-9) / f;     // halves round away from zero
    },
    MOD: ([a, b]) => {
      const x = toNumber(a), y = toNumber(b);
      if (isErr(x)) return x;
      if (isErr(y)) return y;
      if (y === 0) return ERR("#DIV/0!");
      return x - y * Math.floor(x / y);                                 // sign follows the divisor, like Excel
    },
    POWER: ([a, b]) => FUNCS_BIN("^", a, b),
    PI: () => Math.PI,
    LEN: ([x]) => isErr(x) ? x : toText(x).length,
    UPPER: ([x]) => isErr(x) ? x : toText(x).toUpperCase(),
    LOWER: ([x]) => isErr(x) ? x : toText(x).toLowerCase(),
    CONCAT: (args) => {
      let s = "";
      for (const item of flatten(args)) { if (isErr(item.v)) return item.v; s += toText(item.v); }
      return s;
    },
    NOT: ([x]) => { const b = toBool(x); return isErr(b) ? b : !b; },
    AND: (args) => {
      let result = true;
      for (const item of flatten(args)) {
        if (item.v === null || (!item.direct && typeof item.v === "string")) continue;
        const b = toBool(item.v);
        if (isErr(b)) return b;
        result = result && b;
      }
      return result;
    },
    OR: (args) => {
      let result = false;
      for (const item of flatten(args)) {
        if (item.v === null || (!item.direct && typeof item.v === "string")) continue;
        const b = toBool(item.v);
        if (isErr(b)) return b;
        result = result || b;
      }
      return result;
    },
  };
  function matchesCriteria(v, criteria) {           // COUNTIF / SUMIF rules: ">=60", "<>F", "A", 5
    if (typeof criteria === "number") return typeof v === "number" ? v === criteria : String(v) === String(criteria);
    const m = /^(>=|<=|<>|>|<|=)?(.*)$/.exec(String(criteria));
    const op = m[1] || "=", target = m[2];
    const num = target.trim() !== "" && !isNaN(Number(target)) ? Number(target) : null;
    if (num !== null && typeof v === "number") {
      return { "=": v === num, "<>": v !== num, ">": v > num, "<": v < num, ">=": v >= num, "<=": v <= num }[op];
    }
    if (num !== null) return op === "<>";
    const a = toText(v).toLowerCase(), b = target.toLowerCase();
    return { "=": a === b, "<>": a !== b, ">": a > b, "<": a < b, ">=": a >= b, "<=": a <= b }[op];
  }
  FUNCS.COUNTIF = (args) => {
    const [range, criteria] = args;
    if (args.length !== 2 || !Array.isArray(range)) return ERR("#VALUE!");
    if (isErr(criteria)) return criteria;
    return range.filter((x) => x.v !== null && !isErr(x.v) && matchesCriteria(x.v, criteria)).length;
  };
  FUNCS.SUMIF = (args) => {
    const [range, criteria, sumRange] = args;
    if (args.length < 2 || args.length > 3 || !Array.isArray(range) || (sumRange !== undefined && !Array.isArray(sumRange))) return ERR("#VALUE!");
    if (isErr(criteria)) return criteria;
    let total = 0;
    range.forEach((x, i) => {
      const target = sumRange ? sumRange[i] : x;
      if (target && x.v !== null && !isErr(x.v) && matchesCriteria(x.v, criteria) && typeof target.v === "number") total += target.v;
    });
    return total;
  };

  const LAZY = {                                   // these only evaluate the argument they need
    IF: (args, ev) => {
      if (args.length < 2 || args.length > 3) return ERR("#VALUE!");
      const c = toBool(ev(args[0]));
      if (isErr(c)) return c;
      return c ? ev(args[1]) : args.length > 2 ? ev(args[2]) : false;
    },
    IFERROR: (args, ev) => {
      if (args.length !== 2) return ERR("#VALUE!");
      const v = ev(args[0]);
      return isErr(v) ? ev(args[1]) : v;
    },
  };
  const ARITY = { ABS: [1, 1], SQRT: [1, 1], ROUND: [1, 2], MOD: [2, 2], POWER: [2, 2], PI: [0, 0], LEN: [1, 1],
    UPPER: [1, 1], LOWER: [1, 1], NOT: [1, 1] };

  function compareValues(a, b) {
    // Excel: numbers < text < booleans; text compares case-insensitively; blank acts like 0 or ""
    const rank = (v) => typeof v === "number" ? 0 : typeof v === "string" ? 1 : 2;
    if (a === null) a = typeof b === "string" ? "" : typeof b === "boolean" ? false : 0;
    if (b === null) b = typeof a === "string" ? "" : typeof a === "boolean" ? false : 0;
    if (rank(a) !== rank(b)) return rank(a) - rank(b);
    if (typeof a === "string") { a = a.toLowerCase(); b = b.toLowerCase(); }
    return a < b ? -1 : a > b ? 1 : 0;
  }

  function FUNCS_BIN(op, a, b) {
    if (isErr(a)) return a;
    if (isErr(b)) return b;
    if (op === "&") return toText(a) + toText(b);
    if (["=", "<>", "<", ">", "<=", ">="].includes(op)) {
      const c = compareValues(a, b);
      return { "=": c === 0, "<>": c !== 0, "<": c < 0, ">": c > 0, "<=": c <= 0, ">=": c >= 0 }[op];
    }
    const x = toNumber(a), y = toNumber(b);
    if (isErr(x)) return x;
    if (isErr(y)) return y;
    switch (op) {
      case "+": return checkNum(x + y);
      case "-": return checkNum(x - y);
      case "*": return checkNum(x * y);
      case "/": return y === 0 ? ERR("#DIV/0!") : checkNum(x / y);
      case "^": return x === 0 && y === 0 ? ERR("#NUM!") : checkNum(Math.pow(x, y));
    }
    return ERR("#VALUE!");
  }

  // ------------------------------------------------------------------ the sheet
  class Sheet {
    constructor() {
      this.cells = new Map();        // key -> { raw, tree, error, value, deps: Set }
      this.dependents = new Map();   // key -> Set of keys whose formulas read it
      this.recalcCount = 0;
    }

    raw(key) { const c = this.cells.get(key); return c ? c.raw : ""; }
    value(key) { const c = this.cells.get(key); return c ? c.value : null; }
    display(key) { const v = this.value(key); return v === null ? "" : isErr(v) ? v.code : toText(v); }
    keys() { return [...this.cells.keys()]; }

    /** Set a cell's contents. Returns the keys whose values were recalculated. */
    set(key, raw) {
      raw = raw === undefined || raw === null ? "" : String(raw);
      const old = this.cells.get(key);
      if (old) for (const d of old.deps) this.dependents.get(d)?.delete(key);
      if (raw === "") this.cells.delete(key);
      else {
        const cell = { raw, tree: null, error: null, value: null, deps: new Set() };
        if (raw.startsWith("=")) {
          if (raw.includes("#REF!")) cell.error = ERR("#REF!");          // a reference was pasted off the sheet
          else {
            try { cell.tree = parse(raw.slice(1)); cell.deps = collectRefs(cell.tree, new Set()); }
            catch (e) { cell.error = ERR("#ERROR!"); }
          }
        }
        for (const d of cell.deps) {
          if (!this.dependents.has(d)) this.dependents.set(d, new Set());
          this.dependents.get(d).add(key);
        }
        this.cells.set(key, cell);
      }
      return this.recalcFrom([key]);
    }

    /** Recalculate everything downstream of the given cells, in dependency order. */
    recalcFrom(starts) {
      const affected = new Set();
      const stack = [...starts];
      while (stack.length) {                       // every cell that (directly or indirectly) reads a start cell
        const k = stack.pop();
        if (affected.has(k)) continue;
        affected.add(k);
        for (const d of this.dependents.get(k) || []) stack.push(d);
      }
      // Tarjan's strongly-connected-components algorithm over the affected cells (edges: cell -> cells it reads).
      // It emits each group only after every group it depends on, which is exactly the order to evaluate in.
      // A group with more than one cell (or a cell that reads itself) is a cycle: those cells become #CYCLE!.
      // Cells that merely READ a cycle are evaluated normally (the error flows into them like any other).
      const index = new Map(), low = new Map(), onStack = new Set(), stackT = [];
      let counter = 0;
      const strongConnect = (v) => {
        index.set(v, counter); low.set(v, counter); counter++;
        stackT.push(v); onStack.add(v);
        const cell = this.cells.get(v);
        for (const w of cell ? cell.deps : []) {
          if (!affected.has(w)) continue;
          if (!index.has(w)) { strongConnect(w); low.set(v, Math.min(low.get(v), low.get(w))); }
          else if (onStack.has(w)) low.set(v, Math.min(low.get(v), index.get(w)));
        }
        if (low.get(v) === index.get(v)) {
          const group = [];
          let w;
          do { w = stackT.pop(); onStack.delete(w); group.push(w); } while (w !== v);
          const cyclic = group.length > 1 || (cell && cell.deps.has(v));
          for (const k of group) {
            if (cyclic) { const c = this.cells.get(k); if (c) c.value = ERR("#CYCLE!"); }
            else this.evaluateCell(k);
          }
        }
      };
      for (const k of affected) if (!index.has(k)) strongConnect(k);
      return [...affected];
    }

    evaluateCell(key) {
      const cell = this.cells.get(key);
      if (!cell) return;
      this.recalcCount++;
      if (cell.error) { cell.value = cell.error; return; }
      if (!cell.tree) { cell.value = literal(cell.raw); return; }
      const v = this.evaluate(cell.tree);
      cell.value = Array.isArray(v) ? ERR("#VALUE!") : v;       // a bare range like =A1:A3 is not a value
    }

    evaluate(node) {
      const ev = (n) => this.evaluate(n);
      switch (node.type) {
        case "num": return node.v;
        case "str": return node.v;
        case "bool": return node.v;
        case "badname": return ERR("#NAME?");
        case "ref": return node.valid ? this.value(keyOf(node.r, node.c)) : ERR("#REF!");
        case "range": {
          const out = [];
          for (const k of collectRefs(node, new Set())) out.push({ v: this.value(k), direct: false });
          return out;
        }
        case "neg": { const x = toNumber(ev(node.a)); return isErr(x) ? x : -x; }
        case "pct": { const x = toNumber(ev(node.a)); return isErr(x) ? x : x / 100; }
        case "bin": {
          const a = ev(node.a), b = ev(node.b);
          if (Array.isArray(a) || Array.isArray(b)) return ERR("#VALUE!");
          return FUNCS_BIN(node.op, a, b);
        }
        case "call": {
          if (LAZY[node.name]) return LAZY[node.name](node.args, ev);
          const fn = FUNCS[node.name];
          if (!fn) return ERR("#NAME?");
          const [lo, hi] = ARITY[node.name] || [1, 255];
          if (node.args.length < lo || node.args.length > hi) return ERR("#VALUE!");
          const args = node.args.map(ev);
          if (ARITY[node.name]) {
            for (const a of args) { if (isErr(a)) return a; if (Array.isArray(a)) return ERR("#VALUE!"); }
          }
          return fn(args);
        }
      }
      return ERR("#VALUE!");
    }

    toJSON() { return Object.fromEntries([...this.cells].map(([k, c]) => [k, c.raw])); }
    static fromJSON(obj) { const s = new Sheet(); for (const [k, raw] of Object.entries(obj)) s.set(k, raw); return s; }

    toCSV() {
      let maxR = -1, maxC = -1;
      for (const k of this.cells.keys()) { const p = parseKey(k); maxR = Math.max(maxR, p.r); maxC = Math.max(maxC, p.c); }
      const rows = [];
      for (let r = 0; r <= maxR; r++) {
        const row = [];
        for (let c = 0; c <= maxC; c++) {
          const t = this.display(keyOf(r, c));
          row.push(/[",\n]/.test(t) ? '"' + t.replace(/"/g, '""') + '"' : t);
        }
        rows.push(row.join(","));
      }
      return rows.join("\n");
    }
  }

  function literal(raw) {
    const t = raw.trim();
    if (t !== "" && !isNaN(Number(t))) return Number(t);
    if (/^-?\d+(\.\d+)?%$/.test(t)) return parseFloat(t) / 100;
    if (t.toUpperCase() === "TRUE") return true;
    if (t.toUpperCase() === "FALSE") return false;
    return raw;
  }

  /** Move a formula like Excel does when it is copied: relative refs shift, $-anchored parts stay. */
  function shiftFormula(raw, dr, dc) {
    if (!raw.startsWith("=")) return raw;
    let out = "", i = 0;
    const src = raw;
    const re = /(\$?)([A-Za-z])(\$?)(\d+)(?![A-Za-z0-9_(])/y;
    while (i < src.length) {
      if (src[i] === '"') {                               // copy string literals untouched
        const end = src.indexOf('"', i + 1);
        const stop = end < 0 ? src.length : end + 1;
        out += src.slice(i, stop);
        i = stop;
        continue;
      }
      const prevOk = i === 0 || !/[A-Za-z0-9_$]/.test(src[i - 1]);
      re.lastIndex = i;
      const m = prevOk ? re.exec(src) : null;
      if (m) {
        let c = m[2].toUpperCase().charCodeAt(0) - 65 + (m[1] ? 0 : dc);
        let r = parseInt(m[4], 10) - 1 + (m[3] ? 0 : dr);
        out += c < 0 || c >= COLS || r < 0 || r >= ROWS ? "#REF!" : m[1] + colName(c) + m[3] + (r + 1);
        i = re.lastIndex;
      } else {
        out += src[i];
        i++;
      }
    }
    return out;
  }

  const api = { Sheet, parse, tokenize, shiftFormula, keyOf, parseKey, colName, isErr, CellError, COLS, ROWS, formatNumber };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.SheetEngine = api;
})(typeof window !== "undefined" ? window : globalThis);
