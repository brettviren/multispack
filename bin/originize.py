#!/usr/bin/env python3
"""Rewrite absolute RPATH/RUNPATH entries in a Spack install tree to be
store-relative $ORIGIN paths.

Why store-relative and not view-relative: with the flat install projection
every prefix is a sibling, so '$ORIGIN/../../<pkg>/lib' is valid from inside the
store itself.  That makes the WHOLE STORE relocatable as a unit and keeps
Spack's symlink views working (a symlink view resolves $ORIGIN back to the real
store path, which is exactly where the libraries are).

Padding, not truncation: an ELF string table can share suffixes between
strings, so shortening a string and writing a NUL can corrupt an unrelated
entry.  Instead the replacement is padded with trailing '/' characters to the
exact original length.  POSIX collapses repeated slashes, so 'lib///' resolves
identically to 'lib', and no other string in .dynstr is touched.

Usage: originize.py --root <install-tree> [--runpath] [--dry-run] [--json FILE]
"""
import argparse, json, os, shutil, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from elf import Elf64, NotElf, DT_RPATH, DT_RUNPATH  # noqa: E402
import struct  # noqa: E402


def originize_component(comp, filedir, root):
    """Map one rpath component to $ORIGIN-relative if it lives under root."""
    if comp.startswith("$ORIGIN") or comp.startswith("${ORIGIN}"):
        return comp, "already-origin"
    if not comp.startswith("/"):
        return comp, "relative"
    norm = os.path.normpath(comp)
    if not (norm == root or norm.startswith(root + os.sep)):
        return comp, "foreign"
    rel = os.path.relpath(norm, filedir)
    return ("$ORIGIN" if rel == "." else os.path.join("$ORIGIN", rel)), "rewritten"


def process(path, root, to_runpath, dry_run):
    try:
        elf = Elf64(path)
    except (NotElf, OSError):
        return None
    rp = elf.rpath_entries()
    if not rp:
        return None

    # $ORIGIN expands relative to the object's REAL directory (symlinks are
    # resolved by the loader), so compute against realpath.
    filedir = os.path.dirname(os.path.realpath(path))
    rec = {"file": path, "interp": elf.interp, "entries": []}

    for e in rp:
        old = e["value"]
        parts, kinds, seen = [], set(), set()
        for comp in old.split(":"):
            if not comp:
                continue
            new_comp, kind = originize_component(comp, filedir, root)
            kinds.add(kind)
            if new_comp not in seen:          # dedupe, preserve order
                seen.add(new_comp)
                parts.append(new_comp)
        new = ":".join(parts)

        info = {"tag": e["tagname"], "old": old, "new": new, "kinds": sorted(kinds)}
        if new == old:
            info["action"] = "unchanged"
        elif len(new) > len(old):
            info["action"] = "skipped-too-long"
        else:
            padded = new + "/" * (len(old) - len(new))
            info["action"] = "rewritten"
            info["padding"] = len(old) - len(new)
            if not dry_run:
                fd = os.open(path, os.O_RDWR)
                try:
                    os.pwrite(fd, padded.encode(), e["str_off"])
                    if to_runpath and e["tag"] == DT_RPATH:
                        os.pwrite(fd, struct.pack("<q", DT_RUNPATH), e["entry_off"])
                        info["retagged"] = "DT_RPATH->DT_RUNPATH"
                finally:
                    os.close(fd)
        rec["entries"].append(info)
    return rec


def break_hardlink(path):
    """Give `path` its own inode (copying content + mode), leaving other links
    to the old inode untouched.  Needed only for files hardlinked at DIFFERENT
    directory depths: a single $ORIGIN rpath is depth-relative and cannot be
    correct for two depths at once (e.g. binutils installs `as`/`ld` in both
    <prefix>/bin and <prefix>/<triple>/bin as one inode)."""
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".originize-")
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


def realdepth(path):
    return os.path.dirname(os.path.realpath(path)).count(os.sep)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="Spack install tree root")
    ap.add_argument("--runpath", action="store_true",
                    help="also convert DT_RPATH to DT_RUNPATH (lets LD_LIBRARY_PATH "
                         "override, e.g. for host libcuda injection -- but RUNPATH is "
                         "NOT inherited by dlopen, which ROOT relies on heavily)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", help="write a summary here")
    args = ap.parse_args()

    root = os.path.normpath(os.path.realpath(args.root))
    stats = {"scanned": 0, "with_rpath": 0, "rewritten": 0, "unchanged": 0,
             "skipped_too_long": 0, "foreign_components": 0,
             "hardlinks_split": 0, "interps": {}}
    details, toolong, foreign = [], [], []

    def account(p, rec):
        if not rec:
            return
        stats["with_rpath"] += 1
        if rec["interp"]:
            stats["interps"][rec["interp"]] = stats["interps"].get(rec["interp"], 0) + 1
        for e in rec["entries"]:
            if e["action"] == "rewritten":
                stats["rewritten"] += 1
            elif e["action"] == "unchanged":
                stats["unchanged"] += 1
            elif e["action"] == "skipped-too-long":
                stats["skipped_too_long"] += 1
                toolong.append({"file": p, "old": e["old"], "new": e["new"]})
            if "foreign" in e["kinds"]:
                stats["foreign_components"] += 1
                foreign.append({"file": p, "rpath": e["old"]})
        details.append(rec)

    # Pass 1: group candidate files by inode so hardlinked copies are handled
    # together.  A $ORIGIN rpath is depth-relative, so if one inode is linked
    # into directories at different depths we must break the links and rewrite
    # each path for its own depth; a single in-place write cannot serve both.
    groups, order = {}, []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = os.path.join(dirpath, name)
            if os.path.islink(p) or not os.path.isfile(p):
                continue
            stats["scanned"] += 1
            try:
                st = os.stat(p)
            except OSError:
                continue
            key = (st.st_dev, st.st_ino)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(p)

    # Pass 2: rewrite.  Same-inode paths at one depth share a correct rpath, so
    # rewrite once in place (hardlink preserved).  Paths spanning depths get the
    # hardlink broken and each is rewritten for its own location.
    for key in order:
        paths = groups[key]
        if len(paths) > 1 and len({realdepth(p) for p in paths}) > 1:
            stats["hardlinks_split"] += len(paths)
            for p in paths:
                if not args.dry_run:
                    break_hardlink(p)
                account(p, process(p, root, args.runpath, args.dry_run))
        else:
            account(paths[0], process(paths[0], root, args.runpath, args.dry_run))

    summary = {
        "phase": "originize",
        "root": root,
        "dry_run": args.dry_run,
        "retagged_to_runpath": args.runpath,
        "stats": stats,
        "skipped_too_long_examples": toolong[:25],
        "foreign_rpath_examples": foreign[:25],
    }
    if args.json:
        with open(args.json, "w") as fp:
            json.dump(summary, fp, indent=2)
    json.dump(summary["stats"], sys.stdout, indent=2)
    print()
    if stats["skipped_too_long"]:
        print("WARNING: %d rpath entries were too long to rewrite in place; "
              "see the JSON summary." % stats["skipped_too_long"], file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
