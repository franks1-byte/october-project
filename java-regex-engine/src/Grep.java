import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

/**
 * A tiny grep built on the Regex engine. Matches are highlighted in color.
 *
 *   java -cp out Grep [-n] [-c] [-v] [--program] PATTERN FILE...
 *     -n          show line numbers
 *     -c          only print how many lines matched
 *     -v          print the lines that do NOT match
 *     --program   print the compiled NFA program for PATTERN and exit
 */
public class Grep {
    public static void main(String[] args) throws IOException {
        boolean numbers = false, count = false, invert = false, program = false;
        List<String> rest = new ArrayList<>();
        for (String a : args) {
            switch (a) {
                case "-n" -> numbers = true;
                case "-c" -> count = true;
                case "-v" -> invert = true;
                case "--program" -> program = true;
                default -> rest.add(a);
            }
        }
        if (rest.isEmpty() || (!program && rest.size() < 2)) {
            System.err.println("usage: java Grep [-n] [-c] [-v] [--program] PATTERN FILE...");
            System.exit(2);
        }
        Regex re;
        try {
            re = Regex.compile(rest.get(0));
        } catch (Regex.SyntaxError e) {
            System.err.println("bad pattern: " + e.getMessage());
            System.exit(2);
            return;
        }
        if (program) {
            System.out.print(re.dump());
            return;
        }
        boolean color = System.console() != null;
        int total = 0;
        for (String file : rest.subList(1, rest.size())) {
            List<String> lines = Files.readAllLines(Path.of(file));
            int matched = 0;
            for (int i = 0; i < lines.size(); i++) {
                String line = lines.get(i);
                List<Regex.Match> hits = re.findAll(line);
                boolean hit = !hits.isEmpty();
                if (hit == invert) continue;
                matched++;
                if (count) continue;
                String prefix = (rest.size() > 2 ? file + ":" : "") + (numbers ? (i + 1) + ":" : "");
                System.out.println(prefix + (color && !invert ? highlight(line, hits) : line));
            }
            if (count) System.out.println((rest.size() > 2 ? file + ":" : "") + matched);
            total += matched;
        }
        System.exit(total > 0 ? 0 : 1);
    }

    static String highlight(String line, List<Regex.Match> hits) {
        StringBuilder sb = new StringBuilder();
        int last = 0;
        for (Regex.Match m : hits) {
            if (m.end() == m.start()) continue;
            sb.append(line, last, m.start()).append("\u001b[1;31m").append(m.group()).append("\u001b[0m");
            last = m.end();
        }
        return sb.append(line.substring(last)).toString();
    }
}
