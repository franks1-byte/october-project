"""
Tests minigit against the REAL git. Requires git to be installed.

  1. Hashes: blobs and trees must get exactly the same SHA-1 as real git computes
  2. Compatibility: repositories made by minigit pass `git fsck --strict`; `git log`, `git status` and
     `git clone` work on them; and minigit can read commits made by real git
  3. Differential status: hundreds of random edits (create, modify, delete, stage, commit); after each one
     minigit's status must equal `git status --porcelain`
  4. Branches: checkout restores the exact files of each branch, and real git agrees the tree is clean

Run:  python test_minigit.py
"""
import os
import random
import shutil
import subprocess
import sys
import tempfile

import minigit

PASSED, FAILED = [0], [0]
ENV = dict(os.environ, GIT_AUTHOR_NAME="John Francis", GIT_AUTHOR_EMAIL="john@example.com",
           GIT_COMMITTER_NAME="John Francis", GIT_COMMITTER_EMAIL="john@example.com",
           GIT_CONFIG_NOSYSTEM="1", HOME=tempfile.mkdtemp(prefix="minigit_home_"))
os.environ.update({k: ENV[k] for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")})


def check(label, ok, detail=""):
    if ok:
        PASSED[0] += 1
        print(f"  PASS  {label}")
    else:
        FAILED[0] += 1
        print(f"  FAIL  {label}  {detail}")


def git(repo_dir, *args, ok_codes=(0,)):
    r = subprocess.run(["git", "-c", "status.renames=false", "-c", "core.quotepath=false", *args], cwd=repo_dir,
                       env=ENV, capture_output=True, text=True)
    if r.returncode not in ok_codes:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr}")
    return r.stdout


def write(root, rel, text, executable=False):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o755 if executable else 0o644)


def fresh_repo():
    d = tempfile.mkdtemp(prefix="minigit_")
    return d, minigit.Repo.init(d)


# ============================================================================ 1 + 2. hashes and compatibility
def test_compatibility():
    print("Compatibility with real git:")
    d, repo = fresh_repo()
    rng = random.Random(1)
    same = 0
    for i in range(200):
        text = "".join(rng.choice("abc\n é漢") for _ in range(rng.randint(0, 300)))
        write(d, f"h{i}.txt", text)
        same += repo.hash_file(f"h{i}.txt") == git(d, "hash-object", f"h{i}.txt").strip()
    check("200 random files (incl. unicode and empty): blob hashes identical to `git hash-object`", same == 200)
    for f in os.listdir(d):
        if f.startswith("h"):
            os.remove(os.path.join(d, f))

    # names chosen to hit git's tricky tree sort rule: "a-b" < "a.txt" < "a/" because '-' < '.' < '/'
    files = {"a-b": "dash\n", "a.txt": "dot\n", "a/inner.txt": "inside a\n", "a/deeper/z.md": "# deep\n",
             "README.md": "# hello\n", "src/main.py": "print('hi')\n", "empty.txt": "", "名前.txt": "unicode name\n"}
    for rel, text in files.items():
        write(d, rel, text)
    write(d, "run.sh", "#!/bin/sh\necho hi\n", executable=True)
    repo.add([d])
    tree = repo.write_tree(repo.read_index())
    check(f"real git reads minigit's binary index ({len(repo.read_index())} files): `git write-tree` gives the same tree hash",
          git(d, "write-tree").strip() == tree and len(repo.read_index()) == 9)
    c1 = repo.commit("First commit")
    write(d, "src/main.py", "print('hello world')\n")
    os.remove(os.path.join(d, "a-b"))
    repo.add([d])
    c2 = repo.commit("Second commit\n\nWith a longer description.")
    fsck = subprocess.run(["git", "fsck", "--strict", "--full"], cwd=d, env=ENV, capture_output=True, text=True)
    check("`git fsck --strict` finds no problems in a minigit repository", fsck.returncode == 0 and "error" not in fsck.stderr,
          fsck.stderr)
    check("`git log` shows minigit's commits", git(d, "log", "--format=%H %s").split("\n")[:2] ==
          [f"{c2} Second commit", f"{c1} First commit"])
    check("`git status` says the minigit working tree is clean", git(d, "status", "--porcelain", "-uall") == "")
    check("executable bit stored as mode 100755", "100755 blob" in git(d, "ls-tree", "HEAD", "run.sh"))
    clone = tempfile.mkdtemp(prefix="minigit_clone_")
    shutil.rmtree(clone)
    subprocess.run(["git", "clone", "-q", d, clone], env=ENV, check=True)
    check("`git clone` of a minigit repository gives identical files",
          all(open(os.path.join(clone, r), encoding="utf-8").read() == open(os.path.join(d, r), encoding="utf-8").read()
              for r in repo.read_index()))

    write(d, "from_git.txt", "made by real git\n")
    git(d, "add", "from_git.txt")
    git(d, "commit", "-q", "-m", "Commit made by real git")
    log = repo.log()
    check("minigit reads commits made by real git (and their trees)",
          log[0]["message"].strip() == "Commit made by real git" and len(log) == 3
          and "from_git.txt" in repo.commit_tree(repo.head_commit()) and repo.status() == ([], [], []))


# ============================================================================ 3. differential status
def porcelain_sets(d):
    staged, unstaged, untracked = set(), set(), set()
    for line in git(d, "status", "--porcelain", "-uall").splitlines():
        x, y, path = line[0], line[1], line[3:].strip('"')
        if x == "?":
            untracked.add(path)
            continue
        if x != " ":
            staged.add((x, path))
        if y != " ":
            unstaged.add((y, path))
    return staged, unstaged, untracked


def test_random_status(steps=600, seed=7):
    print("\nDifferential testing vs `git status`:")
    d, repo = fresh_repo()
    rng = random.Random(seed)
    names = [f"{folder}{name}" for folder in ("", "lib/", "lib/util/", "docs/") for name in ("a.txt", "b.py", "c.md")]
    mismatches, commits = 0, 0
    for step in range(steps):
        op = rng.random()
        existing = [n for n in names if os.path.exists(os.path.join(d, n))]
        if op < 0.35 or not existing:
            n = rng.choice(names)
            write(d, n, f"line {rng.randint(0, 9)}\n" * rng.randint(0, 4))
        elif op < 0.45:
            os.remove(os.path.join(d, rng.choice(existing)))
        elif op < 0.75:
            target = rng.choice(names + ["."]) if rng.random() < 0.8 else "lib"
            if target == "." or os.path.exists(os.path.join(d, target)) or target in repo.read_index():
                repo.add([os.path.join(d, target)])
        else:
            try:
                repo.commit(f"commit {step}")
                commits += 1
            except minigit.GitError:
                pass
        ours = repo.status()
        theirs = porcelain_sets(d)
        mine = (set(ours[0]), set(ours[1]), set(ours[2]))
        if mine != theirs:
            mismatches += 1
            if mismatches <= 3:
                print(f"        step {step}: minigit {mine}\n                 git     {theirs}")
    fsck = subprocess.run(["git", "fsck", "--strict"], cwd=d, env=ENV, capture_output=True, text=True)
    check(f"{steps} random edits/stages/{commits} commits: minigit status identical to git every time", mismatches == 0)
    check("repository still passes `git fsck --strict`", fsck.returncode == 0, fsck.stderr)


# ============================================================================ 4. branches
def test_branches():
    print("\nBranches and checkout:")
    d, repo = fresh_repo()
    write(d, "story.txt", "Once upon a time\n")
    write(d, "shared/notes.md", "shared\n")
    repo.add([d])
    repo.commit("main: start")
    repo.checkout("feature", create=True)
    write(d, "story.txt", "Once upon a time, in a crypt\n")
    write(d, "feature/new.py", "print('feature')\n")
    os.remove(os.path.join(d, "shared/notes.md"))
    repo.add([d])
    repo.commit("feature: rewrite")
    feature_files = {p: open(os.path.join(d, p)).read() for p in repo.read_index()}

    repo.checkout("main")
    main_ok = (open(os.path.join(d, "story.txt")).read() == "Once upon a time\n"
               and not os.path.exists(os.path.join(d, "feature")) and os.path.exists(os.path.join(d, "shared/notes.md")))
    check("checkout main restores main's files and removes the feature's", main_ok)
    check("real git agrees: correct branch and a clean tree",
          git(d, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main" and git(d, "status", "--porcelain", "-uall") == "")
    repo.checkout("feature")
    check("checkout feature brings back exactly the feature's files",
          {p: open(os.path.join(d, p)).read() for p in repo.read_index()} == feature_files
          and git(d, "status", "--porcelain", "-uall") == "")
    write(d, "story.txt", "uncommitted change\n")
    try:
        repo.checkout("main")
        refused = False
    except minigit.GitError:
        refused = True
    check("checkout refuses to throw away uncommitted changes", refused)
    check("git log --all sees both branches", len(git(d, "log", "--all", "--format=%s").split()) >= 4
          and sorted(repo.branches()) == ["feature", "main"])


if __name__ == "__main__":
    test_compatibility()
    test_random_status()
    test_branches()
    print(f"\n{PASSED[0]} passed, {FAILED[0]} failed")
    print("ALL TESTS PASSED" if FAILED[0] == 0 else "SOME TESTS FAILED")
    sys.exit(1 if FAILED[0] else 0)
