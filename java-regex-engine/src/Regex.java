import java.util.*;

/**
 * A regular-expression engine written from scratch.
 *
 * <pre>
 *   pattern ──parser──► syntax tree ──compiler──► NFA program ──┬──► Pike VM   (find, groups, replace, split)
 *                                                               └──► lazy DFA  (matches: one table lookup per char)
 * </pre>
 *
 * Unlike backtracking engines (including java.util.regex), both engines run in time proportional to
 * (pattern size × input size), so "evil" patterns like (a+)+b can never take exponential time.
 *
 * Supported: literals, . , [abc] [^a-z] classes, \d \w \s \D \W \S, escapes, ( ) groups, (?: ) non-capturing
 * groups, | alternation, * + ? {n} {n,} {n,m} quantifiers and their lazy versions (*? +? ?? {n,m}?),
 * anchors ^ $, word boundaries \b \B. Matching follows Java's leftmost-first (Perl-style) rules.
 */
public final class Regex {

    // ======================================================================== character sets
    /** A set of chars stored as sorted, non-overlapping inclusive ranges [lo0, hi0, lo1, hi1, ...]. */
    static final class CharSet {
        final int[] r;

        CharSet(int[] ranges) { this.r = ranges; }

        static CharSet of(int lo, int hi) { return new CharSet(new int[]{lo, hi}); }

        static CharSet union(List<int[]> ranges) {
            ranges.sort(Comparator.comparingInt(a -> a[0]));
            List<int[]> merged = new ArrayList<>();
            for (int[] x : ranges) {
                if (!merged.isEmpty() && x[0] <= merged.get(merged.size() - 1)[1] + 1) {
                    int[] last = merged.get(merged.size() - 1);
                    last[1] = Math.max(last[1], x[1]);
                } else merged.add(new int[]{x[0], x[1]});
            }
            int[] out = new int[merged.size() * 2];
            for (int i = 0; i < merged.size(); i++) { out[2 * i] = merged.get(i)[0]; out[2 * i + 1] = merged.get(i)[1]; }
            return new CharSet(out);
        }

        CharSet complement() {
            List<int[]> out = new ArrayList<>();
            int next = 0;
            for (int i = 0; i < r.length; i += 2) {
                if (r[i] > next) out.add(new int[]{next, r[i] - 1});
                next = r[i + 1] + 1;
            }
            if (next <= 0xFFFF) out.add(new int[]{next, 0xFFFF});
            return union(out);
        }

        void addTo(List<int[]> list) { for (int i = 0; i < r.length; i += 2) list.add(new int[]{r[i], r[i + 1]}); }

        boolean contains(int c) {
            int lo = 0, hi = r.length / 2 - 1;           // binary search over the ranges
            while (lo <= hi) {
                int mid = (lo + hi) >>> 1;
                if (c < r[2 * mid]) hi = mid - 1;
                else if (c > r[2 * mid + 1]) lo = mid + 1;
                else return true;
            }
            return false;
        }
    }

    static final CharSet DIGIT = CharSet.of('0', '9');
    static final CharSet WORD = CharSet.union(new ArrayList<>(List.of(
            new int[]{'0', '9'}, new int[]{'A', 'Z'}, new int[]{'_', '_'}, new int[]{'a', 'z'})));
    static final CharSet SPACE = CharSet.union(new ArrayList<>(List.of(new int[]{'\t', '\r'}, new int[]{' ', ' '})));
    static final CharSet DOT = CharSet.union(new ArrayList<>(List.of(          // anything except line terminators
            new int[]{'\n', '\n'}, new int[]{'\r', '\r'}, new int[]{0x85, 0x85}, new int[]{0x2028, 0x2029}))).complement();

    // ======================================================================== syntax tree
    sealed interface Node permits Chars, Concat, Alt, Repeat, Group, Assert, Empty {}
    record Chars(CharSet set) implements Node {}
    record Concat(List<Node> items) implements Node {}
    record Alt(List<Node> options) implements Node {}
    record Repeat(Node body, int min, int max, boolean greedy) implements Node {}   // max = -1 means unbounded
    record Group(Node body, int index) implements Node {}                          // index = -1 means (?:...)
    record Assert(char kind) implements Node {}                                    // ^  $  b  B
    record Empty() implements Node {}

    public static final class SyntaxError extends IllegalArgumentException {
        public SyntaxError(String msg, String pattern, int index) {
            super(msg + " near index " + index + "\n" + pattern + "\n" + " ".repeat(Math.max(index, 0)) + "^");
        }
    }

    // ======================================================================== parser (recursive descent)
    static final class Parser {
        final String p;
        int i = 0, groups = 0;

        Parser(String p) { this.p = p; }

        SyntaxError err(String msg) { return new SyntaxError(msg, p, i); }

        boolean more() { return i < p.length(); }

        char peek() { return p.charAt(i); }

        Node parse() {
            Node n = alternation();
            if (more()) throw err(peek() == ')' ? "Unmatched closing ')'" : "Unexpected character");
            return n;
        }

        Node alternation() {
            List<Node> options = new ArrayList<>(List.of(concat()));
            while (more() && peek() == '|') {
                i++;
                options.add(concat());
            }
            return options.size() == 1 ? options.get(0) : new Alt(options);
        }

        Node concat() {
            List<Node> items = new ArrayList<>();
            while (more() && peek() != '|' && peek() != ')') items.add(quantified());
            return items.isEmpty() ? new Empty() : items.size() == 1 ? items.get(0) : new Concat(items);
        }

        Node quantified() {
            Node atom = atom();
            if (!more()) return atom;
            int min, max;
            char c = peek();
            if (c == '*') { min = 0; max = -1; i++; }
            else if (c == '+') { min = 1; max = -1; i++; }
            else if (c == '?') { min = 0; max = 1; i++; }
            else if (c == '{') {
                int start = i++;
                min = number();
                max = min;
                if (more() && peek() == ',') {
                    i++;
                    max = more() && Character.isDigit(peek()) ? number() : -1;
                }
                if (!more() || peek() != '}') { i = start; throw err("Illegal repetition"); }
                i++;
                if (max != -1 && max < min) throw err("Illegal repetition range");
                if (Math.max(min, max) > 1000) throw err("Repetition count too large (max 1000)");
            } else return atom;
            boolean greedy = true;
            if (more() && peek() == '?') { greedy = false; i++; }
            else if (more() && peek() == '+') throw err("Possessive quantifiers are not supported");
            if (more() && "*+?{".indexOf(peek()) >= 0) throw err("Dangling meta character '" + peek() + "'");
            return new Repeat(atom, min, max, greedy);
        }

        int number() {
            int start = i;
            while (more() && Character.isDigit(peek())) i++;
            if (start == i) throw err("Illegal repetition");
            return Integer.parseInt(p.substring(start, i));
        }

        Node atom() {
            char c = p.charAt(i++);
            switch (c) {
                case '(' -> {
                    int index = -1;
                    if (p.startsWith("?:", i)) i += 2;
                    else if (more() && peek() == '?') throw err("Unsupported group type");
                    else index = ++groups;
                    Node body = alternation();
                    if (!more() || peek() != ')') throw err("Unclosed group");
                    i++;
                    return new Group(body, index);
                }
                case '[' -> { return new Chars(charClass()); }
                case '.' -> { return new Chars(DOT); }
                case '^', '$' -> { return new Assert(c); }
                case '\\' -> {
                    if (!more()) throw err("Unexpected internal error");
                    char e = p.charAt(i);
                    if (e == 'b' || e == 'B') { i++; return new Assert(e); }
                    return new Chars(escape());
                }
                case '*', '+', '?' -> { i--; throw err("Dangling meta character '" + c + "'"); }
                case '{' -> { i--; throw err("Illegal repetition"); }
                default -> { return new Chars(CharSet.of(c, c)); }
            }
        }

        /** Called after a backslash. */
        CharSet escape() {
            char e = p.charAt(i++);
            return switch (e) {
                case 'd' -> DIGIT;
                case 'D' -> DIGIT.complement();
                case 'w' -> WORD;
                case 'W' -> WORD.complement();
                case 's' -> SPACE;
                case 'S' -> SPACE.complement();
                case 'n' -> CharSet.of('\n', '\n');
                case 't' -> CharSet.of('\t', '\t');
                case 'r' -> CharSet.of('\r', '\r');
                case 'f' -> CharSet.of('\f', '\f');
                case 'e' -> CharSet.of(27, 27);
                default -> {
                    if (Character.isDigit(e)) { i--; throw err("Back-references are not supported (they need backtracking)"); }
                    if (Character.isLetter(e)) { i--; throw err("Illegal/unsupported escape sequence"); }
                    yield CharSet.of(e, e);
                }
            };
        }

        CharSet charClass() {
            boolean negate = more() && peek() == '^';
            if (negate) i++;
            List<int[]> ranges = new ArrayList<>();
            boolean first = true;
            while (true) {
                if (!more()) throw err("Unclosed character class");
                char c = p.charAt(i);
                if (c == ']' && !first) { i++; break; }
                if (c == '[') throw err("Nested character classes are not supported");
                first = false;
                i++;
                int lo;
                if (c == '\\') {
                    if (!more()) throw err("Unclosed character class");
                    CharSet esc = escape();
                    if (esc.r.length != 2 || esc.r[0] != esc.r[1]) { esc.addTo(ranges); continue; }   // \d \w ...
                    lo = esc.r[0];
                } else lo = c;
                if (more() && peek() == '-' && i + 1 < p.length() && p.charAt(i + 1) != ']') {
                    i++;
                    char d = p.charAt(i++);
                    int hi;
                    if (d == '\\') {
                        CharSet esc = escape();
                        if (esc.r.length != 2 || esc.r[0] != esc.r[1]) throw err("Illegal character range");
                        hi = esc.r[0];
                    } else hi = d;
                    if (hi < lo) throw err("Illegal character range");
                    ranges.add(new int[]{lo, hi});
                } else ranges.add(new int[]{lo, lo});
            }
            CharSet set = CharSet.union(ranges);
            return negate ? set.complement() : set;
        }
    }

    // ======================================================================== compiler: syntax tree -> NFA program
    static final int CHAR = 0, MATCH = 1, JMP = 2, SPLIT = 3, SAVE = 4, ASSERT = 5, MARK = 6, CHECK = 7;

    static final class Inst {
        final int op;
        int x, y;            // jump targets (SPLIT tries x first), or the capture slot for SAVE / MARK
        int slot;            // CHECK: which loop-start slot to compare with
        final CharSet set;
        final char kind;

        Inst(int op, int x, int y, CharSet set, char kind) { this.op = op; this.x = x; this.y = y; this.set = set; this.kind = kind; }

        @Override public String toString() {
            return switch (op) {
                case CHAR -> "char " + describe(set);
                case MATCH -> "match";
                case JMP -> "jmp " + x;
                case SPLIT -> "split " + x + ", " + y;
                case SAVE -> "save " + x;
                case MARK -> "mark loop-start " + x;
                case CHECK -> "if no progress since mark " + slot + " goto " + y + " else " + x;
                default -> "assert " + kind;
            };
        }
    }

    static String describe(CharSet s) {
        if (s.r.length == 2 && s.r[0] == s.r[1]) return "'" + (char) s.r[0] + "'";
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < s.r.length && i < 12; i += 2)
            sb.append((char) s.r[i]).append(s.r[i] == s.r[i + 1] ? "" : "-" + (char) s.r[i + 1]);
        return sb.append(s.r.length > 12 ? "…]" : "]").toString();
    }

    private final List<Inst> prog = new ArrayList<>();
    private final String pattern;
    private final int groupCount;
    private int slots;                 // capture slots + one "loop start" slot per loop
    private final List<Integer> guardSlots = new ArrayList<>();
    private final boolean dfaSafe;
    final Node tree;

    private Regex(String pattern) {
        this.pattern = pattern;
        Parser parser = new Parser(pattern);
        tree = parser.parse();
        groupCount = parser.groups;
        slots = 2 * (groupCount + 1);
        emit(new Inst(SAVE, 0, 0, null, ' '));
        compile(tree);
        emit(new Inst(SAVE, 1, 0, null, ' '));
        emit(new Inst(MATCH, 0, 0, null, ' '));
        dfaSafe = prog.stream().noneMatch(in -> in.op == ASSERT);
        if (prog.size() > 100_000) throw new SyntaxError("Pattern too large", pattern, 0);
    }

    public static Regex compile(String pattern) { return new Regex(pattern); }

    public String pattern() { return pattern; }

    public int groupCount() { return groupCount; }

    /** The compiled NFA program, one instruction per line (handy for learning how it works). */
    public String dump() {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < prog.size(); i++) sb.append(String.format("%4d  %s%n", i, prog.get(i)));
        return sb.toString();
    }

    private int emit(Inst in) { prog.add(in); return prog.size() - 1; }

    private void compile(Node n) {
        switch (n) {
            case Chars c -> emit(new Inst(CHAR, 0, 0, c.set(), ' '));
            case Empty e -> { }
            case Assert a -> emit(new Inst(ASSERT, 0, 0, null, a.kind()));
            case Concat c -> c.items().forEach(this::compile);
            case Group g -> {
                if (g.index() < 0) { compile(g.body()); return; }
                emit(new Inst(SAVE, 2 * g.index(), 0, null, ' '));
                compile(g.body());
                emit(new Inst(SAVE, 2 * g.index() + 1, 0, null, ' '));
            }
            case Alt a -> {
                //   split L1, next ; L1: option1 ; jmp end ; next: split L2, ... ; last option ; end:
                List<Integer> jumps = new ArrayList<>();
                for (int k = 0; k < a.options().size(); k++) {
                    if (k < a.options().size() - 1) {
                        int split = emit(new Inst(SPLIT, prog.size() + 1, -1, null, ' '));
                        compile(a.options().get(k));
                        jumps.add(emit(new Inst(JMP, -1, 0, null, ' ')));
                        prog.get(split).y = prog.size();
                    } else compile(a.options().get(k));
                }
                for (int j : jumps) prog.get(j).x = prog.size();
            }
            case Repeat r -> {
                for (int k = 0; k < r.min(); k++) compile(r.body());
                boolean guard = nullable(r.body());   // could an iteration match nothing?
                int slot = guard ? slots++ : -1;
                if (guard) guardSlots.add(slot);
                if (r.max() == -1) {
                    //   L: split body, out ; [mark] body ; [check: no progress -> out] jmp L ; out:
                    int loop = emit(new Inst(SPLIT, -1, -1, null, ' '));
                    if (guard) emit(new Inst(MARK, slot, 0, null, ' '));
                    compile(r.body());
                    int check = guard ? emit(new Inst(CHECK, loop, -1, null, ' ')) : -1;
                    if (!guard) emit(new Inst(JMP, loop, 0, null, ' '));
                    setSplit(loop, loop + 1, prog.size(), r.greedy());
                    if (guard) { prog.get(check).y = prog.size(); prog.get(check).slot = slot; }
                } else {
                    //   each optional copy: split body, end ; body   (skipping one copy skips the rest)
                    List<Integer> splits = new ArrayList<>(), checks = new ArrayList<>();
                    for (int k = r.min(); k < r.max(); k++) {
                        splits.add(emit(new Inst(SPLIT, -1, -1, null, ' ')));
                        if (guard) emit(new Inst(MARK, slot, 0, null, ' '));
                        compile(r.body());
                        if (guard) checks.add(emit(new Inst(CHECK, prog.size() + 1, -1, null, ' ')));
                    }
                    for (int s : splits) setSplit(s, s + 1, prog.size(), r.greedy());
                    for (int c : checks) { prog.get(c).y = prog.size(); prog.get(c).slot = slot; }
                }
            }
        }
    }

    private void setSplit(int at, int body, int out, boolean greedy) {
        prog.get(at).x = greedy ? body : out;
        prog.get(at).y = greedy ? out : body;
    }

    /** True if some loop can repeat an empty match while containing a capture group (engines differ there). */
    boolean hasNullableLoopWithGroups() { return check(tree); }

    private static boolean check(Node n) {
        return switch (n) {
            case Repeat r -> (r.max() != 1 && nullable(r.body()) && hasGroup(r.body())) || check(r.body());
            case Concat c -> c.items().stream().anyMatch(Regex::check);
            case Alt a -> a.options().stream().anyMatch(Regex::check);
            case Group g -> check(g.body());
            default -> false;
        };
    }

    /** True if a capture group sits inside a loop that is itself inside a loop (java.util.regex has quirks there). */
    boolean hasGroupInNestedLoop() { return nestedGroup(tree, 0); }

    private static boolean nestedGroup(Node n, int loops) {
        return switch (n) {
            case Repeat r -> nestedGroup(r.body(), loops + (r.max() == 1 ? 0 : 1));
            case Group g -> (g.index() > 0 && loops >= 2) || nestedGroup(g.body(), loops);
            case Concat c -> c.items().stream().anyMatch(x -> nestedGroup(x, loops));
            case Alt a -> a.options().stream().anyMatch(x -> nestedGroup(x, loops));
            default -> false;
        };
    }

    /** True if ^ $ \b or \B sits inside a loop whose body can match empty (engines disagree wildly there). */
    boolean hasAssertInNullableLoop() { return assertLoop(tree); }

    private static boolean assertLoop(Node n) {
        return switch (n) {
            case Repeat r -> (r.max() != 1 && nullable(r.body()) && hasAssert(r.body())) || assertLoop(r.body());
            case Concat c -> c.items().stream().anyMatch(Regex::assertLoop);
            case Alt a -> a.options().stream().anyMatch(Regex::assertLoop);
            case Group g -> assertLoop(g.body());
            default -> false;
        };
    }

    private static boolean hasAssert(Node n) {
        return switch (n) {
            case Assert a -> true;
            case Concat c -> c.items().stream().anyMatch(Regex::hasAssert);
            case Alt a -> a.options().stream().anyMatch(Regex::hasAssert);
            case Group g -> hasAssert(g.body());
            case Repeat r -> hasAssert(r.body());
            default -> false;
        };
    }

    static boolean nullable(Node n) {
        return switch (n) {
            case Chars c -> false;
            case Empty e -> true;
            case Assert a -> true;
            case Concat c -> c.items().stream().allMatch(Regex::nullable);
            case Alt a -> a.options().stream().anyMatch(Regex::nullable);
            case Group g -> nullable(g.body());
            case Repeat r -> r.min() == 0 || nullable(r.body());
        };
    }

    static boolean hasGroup(Node n) {
        return switch (n) {
            case Group g -> g.index() > 0 || hasGroup(g.body());
            case Concat c -> c.items().stream().anyMatch(Regex::hasGroup);
            case Alt a -> a.options().stream().anyMatch(Regex::hasGroup);
            case Repeat r -> hasGroup(r.body());
            default -> false;
        };
    }

    // ======================================================================== Pike VM (NFA simulation with captures)
    /**
     * Threads in priority order. Normally each pc appears at most once per step (that is what keeps the
     * engine linear). Patterns with loops that can match empty also track, per loop, whether the current
     * iteration has made progress yet, so "same pc, different iteration" counts as a different state.
     */
    private static final class Threads {
        int[] pcs;
        int[][] caps;
        final int[] seen;
        final Set<Long> seenKeys;
        int size = 0, gen = 1;

        Threads(int n, boolean keyed) {
            pcs = new int[n]; caps = new int[n][]; seen = new int[n];
            seenKeys = keyed ? new HashSet<>() : null;
        }

        void add(int pc, int[] c) {
            if (size == pcs.length) { pcs = Arrays.copyOf(pcs, size * 2); caps = Arrays.copyOf(caps, size * 2); }
            pcs[size] = pc; caps[size] = c; size++;
        }

        void clear() { size = 0; gen++; if (seenKeys != null) seenKeys.clear(); }
    }

    private boolean firstVisit(Threads list, int pc, int[] caps, int pos) {
        if (list.seenKeys == null) {
            if (list.seen[pc] == list.gen) return false;
            list.seen[pc] = list.gen;
            return true;
        }
        long key = pc;
        for (int i = 0; i < guardSlots.size() && i < 40; i++)
            if (caps[guardSlots.get(i)] == pos) key |= 1L << (20 + i);
        return list.seenKeys.add(key);
    }

    private static boolean isWord(CharSequence s, int i) {
        if (i < 0 || i >= s.length()) return false;
        char c = s.charAt(i);
        return c == '_' || Character.isLetterOrDigit(c);
    }

    private static boolean assertion(char kind, CharSequence s, int pos) {
        int n = s.length();
        return switch (kind) {
            case '^' -> pos == 0;
            case '$' -> pos == n || (pos == n - 1 && s.charAt(n - 1) == '\n');   // Java: also before a final newline
            case 'b' -> isWord(s, pos - 1) != isWord(s, pos);
            default -> isWord(s, pos - 1) == isWord(s, pos);
        };
    }

    /** Follow jumps/splits/saves/assertions from pc (in priority order) and add the threads that wait on input. */
    private void addThread(Threads list, int pc0, int[] caps0, CharSequence s, int pos) {
        Deque<Object[]> stack = new ArrayDeque<>();
        stack.push(new Object[]{pc0, caps0});
        while (!stack.isEmpty()) {
            Object[] top = stack.pop();
            int pc = (int) top[0];
            int[] caps = (int[]) top[1];
            if (!firstVisit(list, pc, caps, pos)) continue;   // a higher-priority thread already got here
            Inst in = prog.get(pc);
            switch (in.op) {
                case JMP -> stack.push(new Object[]{in.x, caps});
                case SPLIT -> { stack.push(new Object[]{in.y, caps}); stack.push(new Object[]{in.x, caps}); }
                case SAVE, MARK -> {
                    int[] c2 = caps.clone();
                    c2[in.x] = pos;
                    stack.push(new Object[]{pc + 1, c2});
                }
                // Like Java/Perl: an iteration that matched nothing ends the loop instead of repeating forever
                case CHECK -> stack.push(new Object[]{caps[in.slot] == pos ? in.y : in.x, caps});
                case ASSERT -> { if (assertion(in.kind, s, pos)) stack.push(new Object[]{pc + 1, caps}); }
                default -> list.add(pc, caps);
            }
        }
    }

    /** Leftmost-first search starting at `from`. Returns capture positions [start0, end0, start1, end1, ...] or null. */
    private int[] pike(CharSequence s, int from, boolean anchored, boolean full) {
        int n = prog.size();
        Threads clist = new Threads(n, !guardSlots.isEmpty()), nlist = new Threads(n, !guardSlots.isEmpty());
        int[] empty = new int[slots];
        Arrays.fill(empty, -1);
        int[] best = null;
        for (int pos = from; pos <= s.length(); pos++) {
            boolean seeding = best == null && (!anchored || pos == from);
            if (seeding) addThread(clist, 0, empty, s, pos);    // a new attempt starting here, at the lowest priority
            if (clist.size == 0) {
                if (best != null || anchored) break;           // nothing left that could still match
                clist.clear();
                continue;                                      // unanchored: just try the next start position
            }
            int c = pos < s.length() ? s.charAt(pos) : -1;
            for (int t = 0; t < clist.size; t++) {
                Inst in = prog.get(clist.pcs[t]);
                if (in.op == CHAR) {
                    if (c >= 0 && in.set.contains(c)) addThread(nlist, clist.pcs[t] + 1, clist.caps[t], s, pos + 1);
                } else if (in.op == MATCH) {
                    if (full && pos != s.length()) continue;
                    best = clist.caps[t];
                    break;                                   // lower-priority threads can never win now
                }
            }
            Threads tmp = clist; clist = nlist; nlist = tmp;
            nlist.clear();
        }
        return best;
    }

    // ======================================================================== lazy DFA (for matches)
    private static final class DState {
        final int[] pcs;
        final boolean accept;
        final DState[] ascii = new DState[128];
        final Map<Integer, DState> other = new HashMap<>();

        DState(int[] pcs, boolean accept) { this.pcs = pcs; this.accept = accept; }
    }

    private final Map<String, DState> dstates = new HashMap<>();
    private DState dstart;

    private DState dstate(SortedSet<Integer> set) {
        String key = set.toString();
        DState st = dstates.get(key);
        if (st == null) {
            if (dstates.size() > 10_000) { dstates.clear(); dstart = null; }   // bounded memory: rebuild lazily
            int[] pcs = set.stream().mapToInt(Integer::intValue).toArray();
            boolean accept = set.stream().anyMatch(pc -> prog.get(pc).op == MATCH);
            st = new DState(pcs, accept);
            dstates.put(key, st);
        }
        return st;
    }

    private void closure(int pc0, SortedSet<Integer> out, Set<Integer> seen) {
        Deque<Integer> stack = new ArrayDeque<>(List.of(pc0));
        while (!stack.isEmpty()) {
            int pc = stack.pop();
            if (!seen.add(pc)) continue;
            Inst in = prog.get(pc);
            switch (in.op) {
                case JMP -> stack.push(in.x);
                case SPLIT -> { stack.push(in.y); stack.push(in.x); }
                case SAVE, MARK -> stack.push(pc + 1);
                case CHECK -> { stack.push(in.y); stack.push(in.x); }
                default -> out.add(pc);
            }
        }
    }

    private DState step(DState st, int c) {
        SortedSet<Integer> next = new TreeSet<>();
        Set<Integer> seen = new HashSet<>();
        for (int pc : st.pcs) {
            Inst in = prog.get(pc);
            if (in.op == CHAR && in.set.contains(c)) closure(pc + 1, next, seen);
        }
        DState to = dstate(next);
        if (c < 128) st.ascii[c] = to; else st.other.put(c, to);
        return to;
    }

    /** Whole-string match using the DFA. Each input char costs one cached table lookup. */
    boolean matchesDFA(CharSequence s) {
        if (dstart == null) {
            SortedSet<Integer> set = new TreeSet<>();
            closure(0, set, new HashSet<>());
            dstart = dstate(set);
        }
        DState st = dstart;
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            DState next = c < 128 ? st.ascii[c] : st.other.get((int) c);
            st = next != null ? next : step(st, c);
            if (st.pcs.length == 0) return false;       // dead state: no way to match any more
        }
        return st.accept;
    }

    // ======================================================================== public API (mirrors java.util.regex)
    public static final class Match {
        private final CharSequence input;
        private final int[] caps;
        private final int groups;

        Match(CharSequence input, int[] caps, int groups) { this.input = input; this.caps = caps; this.groups = groups; }

        public int start() { return caps[0]; }
        public int end() { return caps[1]; }
        public int start(int g) { return caps[2 * g]; }
        public int end(int g) { return caps[2 * g + 1]; }
        public String group() { return group(0); }
        public String group(int g) {
            return caps[2 * g] < 0 || caps[2 * g + 1] < 0 ? null : input.subSequence(caps[2 * g], caps[2 * g + 1]).toString();
        }
        public int groupCount() { return groups; }
        @Override public String toString() { return "Match[" + start() + ".." + end() + " \"" + group() + "\"]"; }
    }

    /** Does the WHOLE input match? (like String.matches) */
    public boolean matches(CharSequence s) {
        return dfaSafe ? matchesDFA(s) : pike(s, 0, true, true) != null;
    }

    /** Full match with capture groups, or null. */
    public Match matchFull(CharSequence s) {
        int[] caps = pike(s, 0, true, true);
        return caps == null ? null : new Match(s, caps, groupCount);
    }

    /** First match starting at or after `from`, or null. */
    public Match find(CharSequence s, int from) {
        int[] caps = pike(s, from, false, false);
        return caps == null ? null : new Match(s, caps, groupCount);
    }

    public Match find(CharSequence s) { return find(s, 0); }

    /** Every non-overlapping match, left to right, with Java's rule for empty matches. */
    public List<Match> findAll(CharSequence s) {
        List<Match> out = new ArrayList<>();
        int from = 0;
        while (from <= s.length()) {
            Match m = find(s, from);
            if (m == null) break;
            out.add(m);
            from = m.end() == m.start() ? m.end() + 1 : m.end();
        }
        return out;
    }

    /** Replace every match. $1..$9 (and longer numbers when such groups exist) insert groups; \$ is a literal $. */
    public String replaceAll(CharSequence s, String replacement) {
        StringBuilder sb = new StringBuilder();
        int last = 0;
        for (Match m : findAll(s)) {
            sb.append(s, last, m.start());
            appendReplacement(sb, m, replacement);
            last = m.end();
        }
        return sb.append(s, last, s.length()).toString();
    }

    private void appendReplacement(StringBuilder sb, Match m, String rep) {
        for (int i = 0; i < rep.length(); i++) {
            char c = rep.charAt(i);
            if (c == '\\' && i + 1 < rep.length()) sb.append(rep.charAt(++i));
            else if (c == '$') {
                if (i + 1 >= rep.length() || !Character.isDigit(rep.charAt(i + 1)))
                    throw new IllegalArgumentException("Illegal group reference");
                int g = rep.charAt(++i) - '0';
                if (g > groupCount) throw new IndexOutOfBoundsException("No group " + g);
                while (i + 1 < rep.length() && Character.isDigit(rep.charAt(i + 1))) {
                    int bigger = g * 10 + (rep.charAt(i + 1) - '0');
                    if (bigger > groupCount) break;
                    g = bigger;
                    i++;
                }
                String text = m.group(g);
                if (text != null) sb.append(text);
            } else sb.append(c);
        }
    }

    /** Split around matches, like String.split: trailing empty strings are removed. */
    public String[] split(CharSequence s) {
        List<String> parts = new ArrayList<>();
        int index = 0;
        boolean any = false;
        for (Match m : findAll(s)) {
            if (m.end() == 0) continue;                // a zero-width match at the start never makes an empty part
            any = true;
            parts.add(s.subSequence(index, m.start()).toString());
            index = m.end();
        }
        if (!any) return new String[]{s.toString()};
        parts.add(s.subSequence(index, s.length()).toString());
        int size = parts.size();
        while (size > 0 && parts.get(size - 1).isEmpty()) size--;
        return parts.subList(0, size).toArray(new String[0]);
    }
}
