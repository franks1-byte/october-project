# Regex Engine from Scratch (Java)

A regular-expression engine built from scratch: a parser, an NFA compiler, a **Pike VM** for searching with capture groups, and a **lazy DFA** for fast whole-string matching. It runs in **linear time**, so "evil" patterns that freeze backtracking engines finish instantly. It is fuzz-tested against Java's own `java.util.regex` on about 48,000 random cases.

```
Catastrophic backtracking:  ((a+)*)+b  against  "aaa...a"
        n=16   java.util.regex       8.5 ms   this engine  0.050 ms
        n=20   java.util.regex     119.5 ms   this engine  0.078 ms
        n=24   java.util.regex    1833.2 ms   this engine  0.072 ms
  this engine handles n=100,000 in 99 ms
```

## Run it
```bash
javac -d out src/*.java test/*.java
java -cp out RegexTest                                   # tests + fuzzing + benchmark (about 1 minute)
java -cp out Grep -n '[\w.]+@\w+(\.\w+)+' notes.txt      # grep with colored matches
java -cp out Grep --program 'a(b|c)*d'                   # see the compiled NFA program
```
Requires **Java 21+**.

## Using it
```java
Regex date = Regex.compile("(\\d{4})-(\\d{2})-(\\d{2})");
Regex.Match m = date.find("Due 2026-10-14!");
m.group(1);                                                // "2026"
date.replaceAll("2026-10-14", "$3/$2/$1");                 // "14/10/2026"
Regex.compile("\\s*,\\s*").split("a , b,c");               // ["a", "b", "c"]
Regex.compile("[A-Z]\\w+").matches("Hello");               // true (uses the DFA)
```

## Syntax
Literals, `.`, classes `[abc]` `[^a-z]`, `\d \w \s \D \W \S`, escapes (`\n \t \.` …), groups `( )`, non-capturing `(?: )`, alternation `|`, quantifiers `* + ? {n} {n,} {n,m}` and their lazy forms (`*?` …), anchors `^ $`, and word boundaries `\b \B`.

Back-references (`\1`) and look-around are rejected on purpose: they can't be done in linear time.

## How it works
```
pattern ──parser──► syntax tree ──compiler──► NFA program ──┬──► Pike VM   find, groups, replaceAll, split
                                                            └──► lazy DFA  matches()
```

**1. Parser.** Recursive descent builds a tree of `Chars`, `Concat`, `Alt`, `Repeat`, `Group`, and `Assert` nodes, with error messages that point at the problem:
```
Unclosed group near index 3
(ab
   ^
```

**2. Compiler.** It turns the tree into a small instruction program (Thompson's construction):
```
a(b|c)*d   →    1  char 'a'          5  char 'b'         9  jmp 2
                2  split 3, 10       6  jmp 8           10  char 'd'
                3  save 2            7  char 'c'        11  save 1
                4  split 5, 7        8  save 3          12  match
```
`split x, y` means "try x first, then y". Swapping the order is what makes a quantifier lazy.

**3. Pike VM.** It runs every possible path **at the same time**, one input character at a time. The threads are kept in priority order, so the answer is exactly what a backtracking engine would pick (leftmost-first), including capture groups. Two threads that reach the same instruction are merged, and that is what keeps it linear: at most (program size) threads per character, instead of the 2ⁿ paths a backtracker can try.

**4. Lazy DFA.** For `matches()`, the sets of NFA states become DFA states, built only when they're first needed and then cached. After warm-up, each input character is one array lookup.

**5. Copying Java's rule for empty loops.** When a loop iteration matches nothing (as in `(x?|a)*`), Java leaves the loop. The VM reproduces this with `mark` / `check` instructions that remember where each iteration started.

## Testing
- **Known answers:** groups, lazy vs greedy, anchors, replaceAll, split, and 6 kinds of syntax errors.
- **Differential fuzzing:** 4,000 random patterns × 12 random strings. `matches()`, every `find()` span, capture groups, `replaceAll`, and `split` are compared with `java.util.regex`. **Every case gives the same answer**, except 9 that fall into two documented Java quirks, where Java disagrees with Python's `re` and this engine agrees with Python:
  - Java can keep a **stale capture** from a loop iteration it later backtracked out of.
  - `^`, `$`, or `\b` inside a repeated group that can match nothing (Python refuses to compile these at all).
- **Catastrophic backtracking:** `((a+)*)+b` still makes Java 21 take exponential time, while this engine stays linear.
