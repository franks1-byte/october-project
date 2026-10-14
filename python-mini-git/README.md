# minigit: git from scratch (Python)

A working version control system that uses **git's real on-disk format**. The repositories it makes are real git repositories: `git log`, `git status`, `git clone`, and even `git fsck --strict` work on them. It can also read commits made by real git.

```
$ python minigit.py init
Initialized empty minigit repository in /home/john/demo/.git
$ python minigit.py add .
$ python minigit.py commit -m "First commit"
[main 3c8ad6b] First commit
$ echo "hello world" > hello.txt
$ python minigit.py diff
diff --git a/hello.txt b/hello.txt
--- a/hello.txt
+++ b/hello.txt
@@ -1 +1 @@
-hello
+hello world
$ git log --oneline          # the REAL git reads it
3c8ad6b First commit
```

## Commands
| Command | Does |
|---|---|
| `init` | Create `.git/` with `HEAD`, `objects/`, `refs/` |
| `add FILE…` / `add .` | Store files as blobs and stage them in the binary index (also stages deletions) |
| `commit -m MSG` | Build tree objects from the index, write a commit, and move the branch |
| `status` | Staged, not-staged, and untracked changes (respects `.gitignore`) |
| `diff` | Unified diff of your working files against what's staged |
| `log` | Walk the chain of parent commits |
| `branch [NAME]`, `checkout [-b] NAME` | Create and switch branches; checkout refuses to destroy uncommitted work |
| `cat-file -p OBJ`, `ls-files`, `hash-object [-w] FILE` | Look inside the object database |

## How git stores everything
Git is a **content-addressed** database. Every object is named by the SHA-1 hash of its contents and stored zlib-compressed in `.git/objects/ab/cdef…`:

| Object | Holds | Stored as |
|---|---|---|
| **blob** | the bytes of one file | `blob <size>\0<bytes>` |
| **tree** | one folder: `(mode, name, hash)` per entry | `tree <size>\0<entries>` |
| **commit** | a tree, parent commits, author, and message | `commit <size>\0<text>` |

Because names *are* hashes, identical files are stored only once, and changing any file changes every hash up to the commit. That is how git detects tampering.

Details this implementation gets exactly right (all checked by real git):
- **Tree sort order:** git sorts folder names as if they ended in `/`, so `a-b` < `a.txt` < `a/` (because `-` < `.` < `/`). Get it wrong and `git fsck` reports the tree as corrupt.
- **The index** (`.git/index`) is git's binary "DIRC" file: a header, then one 62-byte record per file (timestamps, size, mode, hash, flags) plus its name padded to 8 bytes, and a SHA-1 checksum at the end. Real git reads minigit's index, and minigit reads real git's, skipping the extensions git adds.
- **Fast status:** like git, minigit compares the saved size and modification time first and only re-hashes files that look changed.
- **File modes:** `100644` for normal files, `100755` for executables, `40000` for folders.

## Testing against the real git
`python test_minigit.py` (requires git installed):

| Test | Result |
|---|---|
| 200 random files (unicode, empty) | blob hashes identical to `git hash-object` |
| Tree built from minigit's index | identical to `git write-tree` |
| `git fsck --strict --full` on a minigit repo | no problems |
| `git log`, `git status`, `git clone` on a minigit repo | all work; tree clean |
| Commit made by real git | minigit reads it, and its files |
| **600 random edits, stages, deletes, and commits** | after every single step, minigit's status equals `git status --porcelain` |
| Branch checkout | restores exact files; git agrees the tree is clean; uncommitted work is protected |

## Not included
Packed objects (`.git/objects/pack`, created by `git gc`), merging, remotes, and symlinks.
