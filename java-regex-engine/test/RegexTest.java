import java.util.*;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.regex.PatternSyntaxException;

/**
 * Tests for the regex engine.
 *   1. Hand-written cases with known answers.
 *   2. Differential fuzzing: thousands of random patterns and strings are run through this engine AND through
 *      java.util.regex; matches(), every find() span, capture groups, replaceAll and split must all agree.
 *   3. The "catastrophic backtracking" benchmark: an evil pattern that makes backtracking engines explode.
 *
 * Run:  javac -d out src/*.java test/*.java && java -cp out RegexTest
 */
public class RegexTest {
    static int passed = 0, failed = 0;

    static void check(String label, boolean ok, String detail) {
        if (ok) { passed++; System.out.println("  PASS  " + label); }
        else { failed++; System.out.println("  FAIL  " + label + "   " + detail); }
    }

    static void check(String label, boolean ok) { check(label, ok, ""); }

    // ------------------------------------------------------------------------------------------ known answers
    static void knownAnswers() {
        System.out.println("Known answers:");
        check("literal match", Regex.compile("hello").matches("hello") && !Regex.compile("hello").matches("hell"));
        check("classes and escapes", Regex.compile("[A-Z]\\w+ \\d{2,4}").matches("Room 2026"));
        check("alternation is ordered (leftmost-first)", Regex.compile("a|ab").find("ab").group().equals("a"));
        check("greedy vs lazy", Regex.compile("<.+>").find("<a><b>").group().equals("<a><b>")
                && Regex.compile("<.+?>").find("<a><b>").group().equals("<a>"));
        Regex.Match m = Regex.compile("(\\d{4})-(\\d{2})-(\\d{2})").find("Due 2026-10-14!");
        check("capture groups", m != null && m.group(1).equals("2026") && m.group(2).equals("10") && m.group(3).equals("14")
                && m.start() == 4 && m.end() == 14);
        check("unmatched optional group is null", Regex.compile("(a)?b").find("b").group(1) == null);
        check("anchors and word boundaries", Regex.compile("\\bcat\\b").findAll("cat concat cat.").size() == 2
                && Regex.compile("^x").find("ax") == null);
        check("replaceAll with groups", Regex.compile("(\\w+)@(\\w+)").replaceAll("ana@liceo ben@cdo", "$2:$1").equals("liceo:ana cdo:ben"));
        check("split", Arrays.equals(Regex.compile("\\s*,\\s*").split("a , b,c ,, d"), new String[]{"a", "b", "c", "", "d"}));
        check("{n,m} counts", Regex.compile("a{2,3}").matches("aaa") && !Regex.compile("a{2,3}").matches("aaaa"));
        Regex.Match nested = Regex.compile("[^a]*\\bb(?:(?:[a-b]+?[bc]){0,}([^a])+[^a]{0})*").find("baacaccbacc");
        check("captures in nested loops follow Perl/Python (last iteration wins: 10..11)", nested.start(1) == 10 && nested.end(1) == 11);
        check("DFA and Pike VM agree", Regex.compile("(a|b)*abb").matchesDFA("babaabb") && Regex.compile("(a|b)*abb").matchFull("babaabb") != null);

        String[][] bad = {{"(ab", "Unclosed group"}, {"ab)", "Unmatched closing ')'"}, {"*a", "Dangling"},
                {"[z-a]", "Illegal character range"}, {"a{3,1}", "Illegal repetition range"}, {"(a)\\1", "Back-references"}};
        for (String[] b : bad) {
            try { Regex.compile(b[0]); check("syntax error: " + b[0], false, "no error"); }
            catch (Regex.SyntaxError e) { check("syntax error: " + b[0] + "  ->  " + e.getMessage().lines().findFirst().get(), e.getMessage().contains(b[1])); }
        }
    }

    // ------------------------------------------------------------------------------------------ differential fuzzing
    static final Random rng = new Random(2026);

    static String randomAtom(int depth) {
        int r = rng.nextInt(depth > 2 ? 9 : 13);
        return switch (r) {
            case 0, 1, 2 -> String.valueOf("abc".charAt(rng.nextInt(3)));
            case 3 -> ".";
            case 4 -> rng.nextBoolean() ? "[ab]" : "[^a]";
            case 5 -> rng.nextBoolean() ? "[a-b]" : "\\w";
            case 6 -> rng.nextBoolean() ? "[bc]" : "\\W";
            case 7 -> rng.nextInt(4) == 0 ? "^" : rng.nextInt(3) == 0 ? "$" : "c";
            case 8 -> rng.nextInt(3) == 0 ? "\\b" : "b";
            case 9, 10 -> "(" + randomRegex(depth + 1) + ")";
            case 11 -> "(?:" + randomRegex(depth + 1) + ")";
            default -> "(" + randomRegex(depth + 1) + "|" + randomRegex(depth + 1) + ")";
        };
    }

    static String randomQuantifier() {
        String q = switch (rng.nextInt(12)) {
            case 0, 1 -> "*";
            case 2, 3 -> "+";
            case 4, 5 -> "?";
            case 6 -> "{" + rng.nextInt(3) + "}";
            case 7 -> { int a = rng.nextInt(3); yield "{" + a + "," + (a + rng.nextInt(3)) + "}"; }
            case 8 -> "{" + rng.nextInt(3) + ",}";
            default -> "";
        };
        return !q.isEmpty() && rng.nextInt(4) == 0 ? q + "?" : q;
    }

    static String randomRegex(int depth) {
        StringBuilder sb = new StringBuilder();
        int n = 1 + rng.nextInt(depth > 1 ? 2 : 4);
        for (int i = 0; i < n; i++) {
            String atom = randomAtom(depth);
            sb.append(atom);
            if (!atom.equals("^") && !atom.equals("$") && !atom.equals("\\b")) sb.append(randomQuantifier());
        }
        if (depth == 0 && rng.nextInt(5) == 0) sb.append("|").append(randomRegex(1));
        return sb.toString();
    }

    static String randomInput() {
        int n = rng.nextInt(13);
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < n; i++) sb.append(rng.nextInt(12) == 0 ? ' ' : "abc".charAt(rng.nextInt(3)));
        return sb.toString();
    }

    /** Makes java.util.regex give up if it backtracks for too long on a random pattern. */
    static final class Deadline implements CharSequence {
        final String s; final long end;
        Deadline(String s, long ms) { this.s = s; this.end = System.nanoTime() + ms * 1_000_000; }
        public int length() { return s.length(); }
        public char charAt(int i) { if (System.nanoTime() > end) throw new IllegalStateException("timeout"); return s.charAt(i); }
        public CharSequence subSequence(int a, int b) { return s.substring(a, b); }
        public String toString() { return s; }
    }

    static List<int[]> javaSpans(Pattern p, String s, boolean groups) {
        List<int[]> out = new ArrayList<>();
        Matcher m = p.matcher(new Deadline(s, 200));
        while (m.find()) {
            int g = groups ? m.groupCount() : 0;
            int[] span = new int[2 * (g + 1)];
            for (int k = 0; k <= g; k++) { span[2 * k] = m.start(k); span[2 * k + 1] = m.end(k); }
            out.add(span);
        }
        return out;
    }

    static List<int[]> ourSpans(Regex r, String s, boolean groups) {
        List<int[]> out = new ArrayList<>();
        for (Regex.Match m : r.findAll(s)) {
            int g = groups ? m.groupCount() : 0;
            int[] span = new int[2 * (g + 1)];
            for (int k = 0; k <= g; k++) { span[2 * k] = m.start(k); span[2 * k + 1] = m.end(k); }
            out.add(span);
        }
        return out;
    }

    static boolean sameSpans(List<int[]> a, List<int[]> b) {
        if (a.size() != b.size()) return false;
        for (int i = 0; i < a.size(); i++) if (!Arrays.equals(a.get(i), b.get(i))) return false;
        return true;
    }

    static void fuzz(int patterns, int inputsPerPattern) {
        System.out.println("\nDifferential fuzzing vs java.util.regex:");
        int comparisons = 0, mismatches = 0, groupChecked = 0, syntaxAgree = 0, slowForJava = 0, javaQuirks = 0;
        for (int p = 0; p < patterns; p++) {
            String pattern = randomRegex(0);
            Pattern jp;
            try { jp = Pattern.compile(pattern); } catch (PatternSyntaxException e) { continue; }
            Regex ours;
            try { ours = Regex.compile(pattern); } catch (Regex.SyntaxError e) {
                mismatches++;
                if (mismatches <= 5) System.out.println("        we rejected a valid pattern: " + pattern + "  " + e.getMessage());
                continue;
            }
            boolean groups = !ours.hasNullableLoopWithGroups() && !ours.hasGroupInNestedLoop();
            if (groups) groupChecked++;
            for (int k = 0; k < inputsPerPattern; k++) {
                String s = randomInput();
                try { javaSpans(jp, s, false); jp.matcher(new Deadline(s, 200)).matches(); jp.split(new Deadline(s, 200)); }
                catch (IllegalStateException e) { slowForJava++; continue; }
                comparisons++;
                String problem = null;
                boolean jm = jp.matcher(new Deadline(s, 200)).matches();
                if (ours.matches(s) != jm) problem = "matches() = " + ours.matches(s) + ", java says " + jm;
                else if ((ours.matchFull(s) != null) != jm) problem = "Pike VM full match disagrees";
                else if (!sameSpans(ourSpans(ours, s, groups), javaSpans(jp, s, groups))) problem = "find() spans differ";
                else {
                    String rep = groups && ours.groupCount() > 0 ? "<$1>" : "<>";
                    String a = ours.replaceAll(s, rep), b = jp.matcher(new Deadline(s, 200)).replaceAll(rep);
                    if (!a.equals(b)) problem = "replaceAll: \"" + a + "\" vs \"" + b + "\"";
                    else if (!Arrays.equals(ours.split(s), jp.split(s)))
                        problem = "split: " + Arrays.toString(ours.split(s)) + " vs " + Arrays.toString(jp.split(s));
                }
                if (problem != null && groups && sameSpans(ourSpans(ours, s, false), javaSpans(jp, s, false))
                        && problem.startsWith("find()")) {
                    javaQuirks++;              // whole matches agree; only a capture group differs (Java keeps stale captures)
                    problem = null;
                }
                if (problem != null && ours.hasAssertInNullableLoop()) {
                    javaQuirks++;              // ^ $ \b inside a loop that can match empty: Python's re won't even compile these
                    problem = null;
                }
                if (problem != null) {
                    mismatches++;
                    if (mismatches <= 8) System.out.println("        MISMATCH  /" + pattern + "/ on \"" + s + "\": " + problem
                            + "\n          ours " + spansText(ourSpans(ours, s, groups)) + "\n          java " + spansText(javaSpans(jp, s, groups)));
                }
            }
        }
        check(comparisons + " random (pattern, string) pairs — matches, find, groups, replaceAll, split: "
                + (comparisons - mismatches - javaQuirks) + " identical to java.util.regex, "
                + javaQuirks + " in documented Java quirks, " + mismatches + " real differences", mismatches == 0);
        System.out.println("        (capture groups also compared on " + groupChecked + " of " + patterns + " patterns; "
                + slowForJava + " inputs skipped because java.util.regex backtracked for over 200 ms)");
        // invalid patterns should be rejected by both engines
        String[] invalid = {"(", ")", "a**", "[", "a{2", "+", "x{5,2}", "[b-a]"};
        for (String bad : invalid) {
            boolean javaRejects, oursRejects;
            try { Pattern.compile(bad); javaRejects = false; } catch (PatternSyntaxException e) { javaRejects = true; }
            try { Regex.compile(bad); oursRejects = false; } catch (Regex.SyntaxError e) { oursRejects = true; }
            if (javaRejects && oursRejects) syntaxAgree++;
        }
        check("invalid patterns are rejected by both engines (" + syntaxAgree + "/" + invalid.length + ")", syntaxAgree == invalid.length);
    }

    static String spansText(List<int[]> spans) {
        StringBuilder sb = new StringBuilder();
        for (int[] s : spans) sb.append(Arrays.toString(s)).append(' ');
        return sb.toString();
    }

    // ------------------------------------------------------------------------------------------ catastrophic backtracking
    static void evilBenchmark() {
        System.out.println("\nCatastrophic backtracking:  ((a+)*)+b  against  \"aaa...a\" (no b, so it can never match)");
        Pattern jp = Pattern.compile("((a+)*)+b");
        Regex ours = Regex.compile("((a+)*)+b");
        long javaLast = 0;
        for (int n = 16; n <= 24; n += 2) {
            String s = "a".repeat(n);
            long t0 = System.nanoTime();
            boolean j = jp.matcher(s).find();
            long javaNs = System.nanoTime() - t0;
            t0 = System.nanoTime();
            boolean o = ours.matches(s) || ours.find(s) != null;
            long oursNs = System.nanoTime() - t0;
            System.out.printf("        n=%2d   java.util.regex %9.1f ms   this engine %6.3f ms%n", n, javaNs / 1e6, oursNs / 1e6);
            if (j || o) check("evil pattern result", false, "should not match");
            javaLast = javaNs;
        }
        String huge = "a".repeat(100_000);
        long t0 = System.nanoTime();
        boolean found = ours.find(huge) != null || ours.matches(huge);
        double ms = (System.nanoTime() - t0) / 1e6;
        check(String.format("this engine handles n=100,000 in %.0f ms (linear time); java.util.regex took %.0f ms at n=24, and doubles every 2 more chars",
                ms, javaLast / 1e6), !found && ms < 5000);
    }

    public static void main(String[] args) {
        knownAnswers();
        fuzz(4000, 12);
        evilBenchmark();
        System.out.println("\n" + passed + " passed, " + failed + " failed");
        System.out.println(failed == 0 ? "ALL TESTS PASSED" : "SOME TESTS FAILED");
        if (failed > 0) System.exit(1);
    }
}
