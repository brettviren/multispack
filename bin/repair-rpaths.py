#!/usr/bin/env python3
"""Repair store executables whose $ORIGIN rpath was written for the WRONG depth.

originize.py (before the hardlink fix) rewrote a hardlinked inode using the
depth of whichever path os.walk hit last.  binutils installs `as`, `ld`, ... in
BOTH <prefix>/bin and <prefix>/<triple>/bin as one inode, at different depths, so
the shallow <prefix>/bin copies ended up with an rpath valid only for the deeper
path (e.g. `$ORIGIN/../../lib` instead of `$ORIGIN/../lib`) and could not load
libbfd -- breaking the compiler.

For each inode hardlinked at more than one depth, the DEEPEST path's current
rpath is correct for its own location, so we expand it back to absolute targets,
break the hardlink, and rewrite EACH path's rpath for its own depth.  Same
in-place, shrink-only, '/'-padded discipline as originize.py (never grows a
string, never touches an unrelated .dynstr entry).

Usage: repair-rpaths.py --root <install-tree> [--dry-run] [--json FILE]
"""
import argparse, json, os, shutil, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from elf import Elf64, NotElf  # noqa: E402


def realdepth(path):
    return os.path.dirname(os.path.realpath(path)).count(os.sep)


def break_hardlink(path):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".repair-")
    try:
        with open(path, "rb") as src, os.fdopen(fd, "wb") as dst:
            shutil.copyfileobj(src, dst)
        os.chmod(tmp, os.stat(path).st_mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def expand_origin(comp, refdir):
    for token in ("${ORIGIN}", "$ORIGIN"):
        if comp.startswith(token):
            rest = comp[len(token):].lstrip("/")
            return os.path.normpath(os.path.join(refdir, rest) if rest else refdir)
    return comp  # absolute or non-origin: leave as-is


def to_origin(target, filedir):
    if not target.startswith("/"):
        return target
    rel = os.path.relpath(target, filedir)
    return "$ORIGIN" if rel == "." else os.path.join("$ORIGIN", rel)


def rewrite_path(path, refdir, dry_run):
    """Rewrite `path`'s rpath entries: expand each component against refdir (the
    depth the string was written for), then re-express relative to `path`."""
    try:
        elf = Elf64(path)
    except (NotElf, OSError):
        return None
    entries = elf.rpath_entries()
    if not entries:
        return None
    filedir = os.path.dirname(os.path.realpath(path))
    rec = {"file": path, "entries": []}
    for e in entries:
        old = e["value"]
        seen, parts = set(), []
        for comp in old.split(":"):
            if not comp:
                continue
            newc = to_origin(expand_origin(comp, refdir), filedir)
            if newc not in seen:
                seen.add(newc)
                parts.append(newc)
        new = ":".join(parts)
        info = {"tag": e["tagname"], "old": old, "new": new}
        if new == old:
            info["action"] = "unchanged"
        elif len(new) > len(old):
            info["action"] = "skipped-too-long"
        else:
            info["action"] = "rewritten"
            if not dry_run:
                fd = os.open(path, os.O_RDWR)
                try:
                    os.pwrite(fd, (new + "/" * (len(old) - len(new))).encode(), e["str_off"])
                finally:
                    os.close(fd)
        rec["entries"].append(info)
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json")
    args = ap.parse_args()
    root = os.path.normpath(os.path.realpath(args.root))

    # Group regular files by inode.
    groups, order = {}, []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = os.path.join(dirpath, name)
            if os.path.islink(p) or not os.path.isfile(p):
                continue
            try:
                st = os.stat(p)
            except OSError:
                continue
            key = (st.st_dev, st.st_ino)
            groups.setdefault(key, [])
            if key not in groups or not groups[key]:
                order.append(key)
            groups[key].append(p)

    stats = {"inodes_multidepth": 0, "paths_repaired": 0, "entries_rewritten": 0,
             "entries_unchanged": 0, "skipped_too_long": 0}
    changed = []
    for key in order:
        paths = groups[key]
        if len(paths) < 2 or len({realdepth(p) for p in paths}) < 2:
            continue  # only inodes hardlinked across differing depths are suspect
        stats["inodes_multidepth"] += 1
        refdir = os.path.dirname(os.path.realpath(max(paths, key=realdepth)))
        for p in paths:
            if not args.dry_run:
                break_hardlink(p)
            rec = rewrite_path(p, refdir, args.dry_run)
            if not rec:
                continue
            acted = [e for e in rec["entries"] if e["action"] != "unchanged"]
            for e in rec["entries"]:
                if e["action"] == "rewritten":
                    stats["entries_rewritten"] += 1
                elif e["action"] == "unchanged":
                    stats["entries_unchanged"] += 1
                elif e["action"] == "skipped-too-long":
                    stats["skipped_too_long"] += 1
            if acted:
                stats["paths_repaired"] += 1
                changed.append(rec)

    summary = {"phase": "repair-rpaths", "root": root, "dry_run": args.dry_run,
               "stats": stats, "changed_examples": changed[:40]}
    if args.json:
        with open(args.json, "w") as fp:
            json.dump(summary, fp, indent=2)
    json.dump(stats, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
