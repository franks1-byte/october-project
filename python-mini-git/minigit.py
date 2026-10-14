#!/usr/bin/env python3
"""
minigit: a working version control system that speaks git's real on-disk format.

Repositories it creates are real git repositories: `git log`, `git status` and `git fsck` work on them,
and minigit can read commits made by real git (loose objects).

    python minigit.py init
    python minigit.py add FILE...            (or  add .)
    python minigit.py commit -m "message"
    python minigit.py status
    python minigit.py diff
    python minigit.py log
    python minigit.py branch [NAME]
    python minigit.py checkout BRANCH        (or  checkout -b NEW)
    python minigit.py cat-file -p OBJECT
    python minigit.py ls-files
    python minigit.py hash-object FILE

How git stores things (and so does this):
    blob    the bytes of one file                       stored as  "blob <size>\\0<bytes>"
    tree    one directory: (mode, name, hash) entries   stored as  "tree <size>\\0<entries>"
    commit  a tree + parent commits + author + message  stored as  "commit <size>\\0<text>"
Every object is named by the SHA-1 of its stored bytes and saved zlib-compressed in .git/objects/ab/cdef...
The index (.git/index) is a binary file listing what is staged for the next commit.
"""
import difflib
import hashlib
import os
import struct
import sys
import time
import zlib

GIT_DIR = ".git"


class GitError(Exception):
    pass


# ============================================================================ repository discovery
def find_repo(start="."):
    path = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(path, GIT_DIR)):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            raise GitError("not a git repository (or any of the parent directories)")
        path = parent


class Repo:
    def __init__(self, root):
        self.root = root
        self.git = os.path.join(root, GIT_DIR)

    def path(self, *parts):
        return os.path.join(self.git, *parts)

    # ------------------------------------------------------------------------ init
    @staticmethod
    def init(root="."):
        root = os.path.abspath(root)
        git = os.path.join(root, GIT_DIR)
        if os.path.exists(git):
            raise GitError(f"repository already exists in {git}")
        for d in ("objects", "refs/heads", "refs/tags"):
            os.makedirs(os.path.join(git, d))
        with open(os.path.join(git, "HEAD"), "w") as f:
            f.write("ref: refs/heads/main\n")
        with open(os.path.join(git, "config"), "w") as f:
            f.write("[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n")
        with open(os.path.join(git, "description"), "w") as f:
            f.write("Unnamed repository; edit this file to name it.\n")
        return Repo(root)

    # ------------------------------------------------------------------------ objects
    def write_object(self, kind, data):
        store = f"{kind} {len(data)}".encode() + b"\0" + data
        sha = hashlib.sha1(store).hexdigest()
        path = self.path("objects", sha[:2], sha[2:])
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(zlib.compress(store))
            os.replace(tmp, path)
            os.chmod(path, 0o444)                     # objects are immutable, like real git's
        return sha

    def read_object(self, sha):
        sha = self.resolve(sha)
        path = self.path("objects", sha[:2], sha[2:])
        if not os.path.exists(path):
            raise GitError(f"object {sha} not found (packed objects are not supported)")
        with open(path, "rb") as f:
            raw = zlib.decompress(f.read())
        header, _, data = raw.partition(b"\0")
        kind, size = header.decode().split()
        if int(size) != len(data):
            raise GitError(f"object {sha} is corrupt")
        return kind, data

    def resolve(self, name):
        """Turn a branch name, HEAD, or (abbreviated) hash into a full hash."""
        if name == "HEAD":
            sha = self.head_commit()
            if sha is None:
                raise GitError("HEAD has no commits yet")
            return sha
        ref = self.path("refs", "heads", name)
        if os.path.isfile(ref):
            return open(ref).read().strip()
        if 4 <= len(name) <= 40 and all(c in "0123456789abcdef" for c in name):
            folder = self.path("objects", name[:2])
            if len(name) == 40:
                return name
            if os.path.isdir(folder):
                hits = [name[:2] + f for f in os.listdir(folder) if f.startswith(name[2:])]
                if len(hits) == 1:
                    return hits[0]
                if len(hits) > 1:
                    raise GitError(f"short object ID {name} is ambiguous")
        raise GitError(f"unknown revision '{name}'")

    # ------------------------------------------------------------------------ refs
    def head_ref(self):
        head = open(self.path("HEAD")).read().strip()
        return head[5:] if head.startswith("ref: ") else None

    def current_branch(self):
        ref = self.head_ref()
        return ref.split("/", 2)[2] if ref else None

    def head_commit(self):
        ref = self.head_ref()
        if ref is None:
            return open(self.path("HEAD")).read().strip()
        path = self.path(*ref.split("/"))
        return open(path).read().strip() if os.path.exists(path) else None

    def update_ref(self, ref, sha):
        path = self.path(*ref.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(sha + "\n")

    def branches(self):
        out = []
        base = self.path("refs", "heads")
        for dirpath, _, files in os.walk(base):
            for fname in files:
                out.append(os.path.relpath(os.path.join(dirpath, fname), base).replace(os.sep, "/"))
        return sorted(out)

    # ------------------------------------------------------------------------ the index (git's binary staging file)
    def read_index(self):
        """Returns {path: entry}. Entry = dict(mode, sha, size, mtime, ...) in git's DIRC version 2 format."""
        path = self.path("index")
        if not os.path.exists(path):
            return {}
        data = open(path, "rb").read()
        if hashlib.sha1(data[:-20]).digest() != data[-20:]:
            raise GitError("index file is corrupt (bad checksum)")
        sig, version, count = struct.unpack(">4sII", data[:12])
        if sig != b"DIRC" or version not in (2, 3):
            raise GitError("unsupported index format")
        entries, pos = {}, 12
        for _ in range(count):
            fields = struct.unpack(">10I20sH", data[pos:pos + 62])
            flags = fields[11]
            name_len = flags & 0xFFF
            extended = flags & 0x4000
            name_start = pos + 62 + (2 if extended else 0)
            end = data.index(b"\0", name_start)
            name = data[name_start:end].decode("utf-8")
            entries[name] = {"ctime": fields[0:2], "mtime": fields[2:4], "dev": fields[4], "ino": fields[5],
                             "mode": fields[6], "uid": fields[7], "gid": fields[8], "size": fields[9],
                             "sha": fields[10].hex()}
            entry_len = (62 + (2 if extended else 0) + len(name.encode()) + 8) // 8 * 8
            pos += entry_len
            del name_len
        return entries

    def write_index(self, entries):
        out = [struct.pack(">4sII", b"DIRC", 2, len(entries))]
        for name in sorted(entries, key=lambda n: n.encode()):
            e = entries[name]
            encoded = name.encode()
            out.append(struct.pack(">10I20sH", *e["ctime"], *e["mtime"], e["dev"] & 0xFFFFFFFF, e["ino"] & 0xFFFFFFFF,
                                   e["mode"], e["uid"], e["gid"], e["size"] & 0xFFFFFFFF, bytes.fromhex(e["sha"]),
                                   min(len(encoded), 0xFFF)))
            out.append(encoded)
            entry_len = 62 + len(encoded)
            out.append(b"\0" * ((entry_len + 8) // 8 * 8 - entry_len))    # 1-8 NUL bytes of padding
        body = b"".join(out)
        tmp = self.path("index.lock")
        with open(tmp, "wb") as f:
            f.write(body + hashlib.sha1(body).digest())
        os.replace(tmp, self.path("index"))

    def stat_entry(self, rel, sha):
        st = os.stat(os.path.join(self.root, rel))
        mode = 0o100755 if st.st_mode & 0o111 else 0o100644
        return {"ctime": (int(st.st_ctime), st.st_ctime_ns % 10 ** 9), "mtime": (int(st.st_mtime), st.st_mtime_ns % 10 ** 9),
                "dev": st.st_dev, "ino": st.st_ino, "mode": mode, "uid": st.st_uid & 0xFFFFFFFF,
                "gid": st.st_gid & 0xFFFFFFFF, "size": st.st_size, "sha": sha}

    # ------------------------------------------------------------------------ working tree helpers
    def rel(self, path):
        return os.path.relpath(os.path.abspath(path), self.root).replace(os.sep, "/")

    def working_files(self):
        ignore = self.ignore_patterns()
        out = []
        for dirpath, dirnames, files in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d != GIT_DIR and not self.ignored(self.rel(os.path.join(dirpath, d)) + "/", ignore))
            for fname in files:
                rel = self.rel(os.path.join(dirpath, fname))
                if not self.ignored(rel, ignore) and os.path.isfile(os.path.join(self.root, rel)) \
                        and not os.path.islink(os.path.join(self.root, rel)):
                    out.append(rel)
        return sorted(out)

    def ignore_patterns(self):
        path = os.path.join(self.root, ".gitignore")
        if not os.path.exists(path):
            return []
        return [ln.strip() for ln in open(path, encoding="utf-8") if ln.strip() and not ln.startswith("#")]

    @staticmethod
    def ignored(rel, patterns):
        import fnmatch
        name = rel.rstrip("/").split("/")[-1]
        for p in patterns:
            if p.endswith("/"):
                if rel.endswith("/") and fnmatch.fnmatch(name, p.rstrip("/")):
                    return True
            elif fnmatch.fnmatch(name, p) or fnmatch.fnmatch(rel.rstrip("/"), p):
                return True
        return False

    def hash_file(self, rel, write=False):
        with open(os.path.join(self.root, rel), "rb") as f:
            data = f.read()
        if write:
            return self.write_object("blob", data)
        return hashlib.sha1(f"blob {len(data)}".encode() + b"\0" + data).hexdigest()

    # ------------------------------------------------------------------------ trees
    def write_tree(self, entries):
        """Build tree objects for every directory in the index, bottom-up. Returns the root tree hash."""
        root = {}
        for path, e in entries.items():
            node = root
            parts = path.split("/")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = ("file", e)

        def build(node):
            items = []
            for name, child in node.items():
                if isinstance(child, tuple):
                    items.append((name, f"{child[1]['mode']:o}", child[1]["sha"]))
                else:
                    items.append((name, "40000", build(child)))
            # git sorts entries by name, comparing a directory as if its name ended with "/"
            items.sort(key=lambda it: (it[0] + ("/" if it[1] == "40000" else "")).encode())
            data = b"".join(f"{mode} {name}".encode() + b"\0" + bytes.fromhex(sha) for name, mode, sha in items)
            return self.write_object("tree", data)

        return build(root)

    def read_tree(self, sha, prefix=""):
        """Flatten a tree object into {path: (mode, blob sha)}."""
        kind, data = self.read_object(sha)
        if kind != "tree":
            raise GitError(f"{sha} is a {kind}, not a tree")
        out, pos = {}, 0
        while pos < len(data):
            space = data.index(b" ", pos)
            nul = data.index(b"\0", space)
            mode = data[pos:space].decode()
            name = data[space + 1:nul].decode()
            child = data[nul + 1:nul + 21].hex()
            pos = nul + 21
            if mode == "40000":
                out.update(self.read_tree(child, prefix + name + "/"))
            else:
                out[prefix + name] = (int(mode, 8), child)
        return out

    def commit_tree(self, commit_sha):
        if commit_sha is None:
            return {}
        return self.read_tree(self.parse_commit(commit_sha)["tree"])

    # ------------------------------------------------------------------------ commits
    def parse_commit(self, sha):
        kind, data = self.read_object(sha)
        if kind != "commit":
            raise GitError(f"{sha} is a {kind}, not a commit")
        head, _, message = data.decode("utf-8").partition("\n\n")
        info = {"parents": [], "message": message, "sha": sha}
        for line in head.splitlines():
            key, _, value = line.partition(" ")
            if key == "parent":
                info["parents"].append(value)
            elif key in ("tree", "author", "committer"):
                info[key] = value
        return info

    @staticmethod
    def identity(kind):
        name = os.environ.get(f"GIT_{kind}_NAME") or Repo.config_value("user", "name") or "minigit user"
        email = os.environ.get(f"GIT_{kind}_EMAIL") or Repo.config_value("user", "email") or "user@example.com"
        when = int(time.time())
        offset = -time.altzone if time.localtime().tm_isdst > 0 else -time.timezone
        tz = f"{'+' if offset >= 0 else '-'}{abs(offset) // 3600:02d}{abs(offset) % 3600 // 60:02d}"
        return f"{name} <{email}> {when} {tz}"

    @staticmethod
    def config_value(section, key):
        path = os.path.expanduser("~/.gitconfig")
        if not os.path.exists(path):
            return None
        current = None
        for line in open(path, encoding="utf-8", errors="replace"):
            line = line.strip()
            if line.startswith("["):
                current = line.strip("[]").split()[0].lower()
            elif current == section and "=" in line:
                k, v = (x.strip() for x in line.split("=", 1))
                if k.lower() == key:
                    return v
        return None

    def commit(self, message):
        entries = self.read_index()
        if not entries:
            raise GitError("nothing to commit (use add to stage files)")
        tree = self.write_tree(entries)
        parent = self.head_commit()
        if parent and self.parse_commit(parent)["tree"] == tree:
            raise GitError("nothing to commit, working tree clean")
        lines = [f"tree {tree}"] + ([f"parent {parent}"] if parent else [])
        lines += [f"author {self.identity('AUTHOR')}", f"committer {self.identity('COMMITTER')}", "", message.rstrip("\n") + "\n"]
        sha = self.write_object("commit", "\n".join(lines).encode())
        ref = self.head_ref()
        if ref:
            self.update_ref(ref, sha)
        else:
            with open(self.path("HEAD"), "w") as f:
                f.write(sha + "\n")
        return sha

    # ------------------------------------------------------------------------ commands
    def add(self, paths):
        entries = self.read_index()
        targets = set()
        for p in paths:
            full = os.path.abspath(p)
            rel = self.rel(full)
            if os.path.isdir(full):
                prefix = "" if rel == "." else rel + "/"
                targets |= {f for f in self.working_files() if f.startswith(prefix)}
                targets |= {f for f in entries if f.startswith(prefix)}           # deletions inside the folder
            elif os.path.isfile(full) or rel in entries:
                targets.add(rel)
            else:
                raise GitError(f"pathspec '{p}' did not match any files")
        for rel in targets:
            if os.path.isfile(os.path.join(self.root, rel)):
                old = entries.get(rel)
                st = os.stat(os.path.join(self.root, rel))
                if old and old["size"] == st.st_size and old["mtime"] == (int(st.st_mtime), st.st_mtime_ns % 10 ** 9):
                    continue                                                    # unchanged since last add
                entries[rel] = self.stat_entry(rel, self.hash_file(rel, write=True))
            else:
                entries.pop(rel, None)                                          # the file was deleted
        self.write_index(entries)

    def status(self):
        """Returns (staged, unstaged, untracked); staged/unstaged are lists of (letter, path)."""
        index = self.read_index()
        head = {p: sha for p, (mode, sha) in self.commit_tree(self.head_commit()).items()}
        staged = []
        for p in sorted(set(head) | set(index)):
            if p not in head:
                staged.append(("A", p))
            elif p not in index:
                staged.append(("D", p))
            elif head[p] != index[p]["sha"]:
                staged.append(("M", p))
        unstaged = []
        work = set(self.working_files())
        for p in sorted(index):
            full = os.path.join(self.root, p)
            if not os.path.isfile(full):
                unstaged.append(("D", p))
                continue
            st = os.stat(full)
            e = index[p]
            same_stat = e["size"] == st.st_size and e["mtime"] == (int(st.st_mtime), st.st_mtime_ns % 10 ** 9)
            if not same_stat and self.hash_file(p) != e["sha"]:
                unstaged.append(("M", p))
        untracked = sorted(work - set(index))
        return staged, unstaged, untracked

    def diff(self):
        """Unified diff of the working tree against the index."""
        index = self.read_index()
        out = []
        for letter, p in self.status()[1]:
            _, old = self.read_object(index[p]["sha"])
            new = b"" if letter == "D" else open(os.path.join(self.root, p), "rb").read()
            try:
                a, b = old.decode().splitlines(keepends=True), new.decode().splitlines(keepends=True)
            except UnicodeDecodeError:
                out.append(f"Binary files a/{p} and b/{p} differ\n")
                continue
            out.append(f"diff --git a/{p} b/{p}\n")
            out.extend(difflib.unified_diff(a, b, f"a/{p}", "/dev/null" if letter == "D" else f"b/{p}"))
        return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in out)

    def log(self, start="HEAD", limit=None):
        sha = self.head_commit() if start == "HEAD" else self.resolve(start)
        commits = []
        while sha and (limit is None or len(commits) < limit):
            c = self.parse_commit(sha)
            commits.append(c)
            sha = c["parents"][0] if c["parents"] else None
        return commits

    def checkout(self, target, create=False):
        branches = self.branches()
        if create:
            if target in branches:
                raise GitError(f"a branch named '{target}' already exists")
            head = self.head_commit()
            if head:
                self.update_ref(f"refs/heads/{target}", head)
            with open(self.path("HEAD"), "w") as f:
                f.write(f"ref: refs/heads/{target}\n")
            return
        if target not in branches:
            raise GitError(f"pathspec '{target}' did not match any branch")
        staged, unstaged, _ = self.status()
        if staged or unstaged:
            raise GitError("your local changes would be overwritten by checkout; commit them first")
        old = self.commit_tree(self.head_commit())
        new_commit = self.resolve(target)
        new = self.commit_tree(new_commit)
        for p in old:
            if p not in new and os.path.exists(os.path.join(self.root, p)):
                os.remove(os.path.join(self.root, p))
                folder = os.path.dirname(os.path.join(self.root, p))
                while folder != self.root and not os.listdir(folder):     # tidy up empty folders
                    os.rmdir(folder)
                    folder = os.path.dirname(folder)
        entries = {}
        for p, (mode, sha) in new.items():
            full = os.path.join(self.root, p)
            if old.get(p) != (mode, sha) or not os.path.exists(full):
                os.makedirs(os.path.dirname(full) or self.root, exist_ok=True)
                with open(full, "wb") as f:
                    f.write(self.read_object(sha)[1])
                os.chmod(full, 0o755 if mode == 0o100755 else 0o644)
            entries[p] = self.stat_entry(p, sha)
        self.write_index(entries)
        with open(self.path("HEAD"), "w") as f:
            f.write(f"ref: refs/heads/{target}\n")


# ============================================================================ command line
def pretty_object(repo, name):
    kind, data = repo.read_object(name)
    if kind == "tree":
        lines, pos = [], 0
        while pos < len(data):
            space = data.index(b" ", pos)
            nul = data.index(b"\0", space)
            mode = data[pos:space].decode()
            sha = data[nul + 1:nul + 21].hex()
            lines.append(f"{mode.zfill(6)} {'tree' if mode == '40000' else 'blob'} {sha}\t{data[space + 1:nul].decode()}")
            pos = nul + 21
        return "\n".join(lines)
    return data.decode("utf-8", errors="replace").rstrip("\n")


def main(argv):
    if not argv:
        print(__doc__.split("How git stores")[0].strip())
        return 0
    cmd, args = argv[0], argv[1:]
    if cmd == "init":
        repo = Repo.init(args[0] if args else ".")
        print(f"Initialized empty minigit repository in {repo.git}")
        return 0
    repo = Repo(find_repo())
    if cmd == "add":
        repo.add(args or ["."])
    elif cmd == "commit":
        if len(args) < 2 or args[0] != "-m":
            raise GitError('usage: commit -m "message"')
        sha = repo.commit(args[1])
        print(f"[{repo.current_branch() or 'detached HEAD'} {sha[:7]}] {args[1]}")
    elif cmd == "status":
        staged, unstaged, untracked = repo.status()
        print(f"On branch {repo.current_branch()}")
        if not repo.head_commit():
            print("\nNo commits yet")
        names = {"A": "new file:", "M": "modified:", "D": "deleted: "}
        if staged:
            print("\nChanges to be committed:")
            print("\n".join(f"\t\033[32m{names[l]}   {p}\033[0m" for l, p in staged))
        if unstaged:
            print("\nChanges not staged for commit:")
            print("\n".join(f"\t\033[31m{names[l]}   {p}\033[0m" for l, p in unstaged))
        if untracked:
            print("\nUntracked files:")
            print("\n".join(f"\t\033[31m{p}\033[0m" for p in untracked))
        if not (staged or unstaged or untracked):
            print("nothing to commit, working tree clean")
    elif cmd == "diff":
        sys.stdout.write(repo.diff())
    elif cmd == "log":
        for c in repo.log():
            name, _, rest = c["author"].rpartition(">")
            stamp, tz = rest.split()
            print(f"\033[33mcommit {c['sha']}\033[0m\nAuthor: {name}>\nDate:   "
                  f"{time.strftime('%a %b %d %H:%M:%S %Y', time.localtime(int(stamp)))} {tz}\n")
            print("\n".join("    " + line for line in c["message"].rstrip().splitlines()) + "\n")
    elif cmd == "branch":
        if args:
            head = repo.head_commit()
            if not head:
                raise GitError("cannot create a branch before the first commit")
            if args[0] in repo.branches():
                raise GitError(f"a branch named '{args[0]}' already exists")
            repo.update_ref(f"refs/heads/{args[0]}", head)
        else:
            current = repo.current_branch()
            for b in repo.branches():
                print(("* \033[32m" if b == current else "  ") + b + ("\033[0m" if b == current else ""))
    elif cmd == "checkout":
        if args[:1] == ["-b"]:
            repo.checkout(args[1], create=True)
            print(f"Switched to a new branch '{args[1]}'")
        else:
            repo.checkout(args[0])
            print(f"Switched to branch '{args[0]}'")
    elif cmd == "cat-file":
        if len(args) != 2 or args[0] not in ("-p", "-t"):
            raise GitError("usage: cat-file (-p | -t) OBJECT")
        print(repo.read_object(args[1])[0] if args[0] == "-t" else pretty_object(repo, args[1]))
    elif cmd == "ls-files":
        print("\n".join(sorted(repo.read_index(), key=lambda n: n.encode())))
    elif cmd == "hash-object":
        print("\n".join(repo.hash_file(repo.rel(a), write="-w" in args) for a in args if a != "-w"))
    else:
        raise GitError(f"'{cmd}' is not a minigit command")
    return 0


if __name__ == "__main__":
    if os.name == "nt":
        os.system("")          # turns on color codes in the Windows terminal
    try:
        sys.exit(main(sys.argv[1:]))
    except GitError as e:
        print(f"fatal: {e}", file=sys.stderr)
        sys.exit(128)
