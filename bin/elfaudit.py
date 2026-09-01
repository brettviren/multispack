#!/usr/bin/env python3
"""Audit a Spack install tree for Strategy B portability.

Answers the three questions that decide whether the tree is portable:

  1. What is the glibc symbol-version floor?   (the manylinux/auditwheel question)
  2. Which DT_NEEDED libraries are NOT satisfied inside the tree?
  3. Are the rpaths $ORIGIN-relative, or do they still pin absolute paths?

Usage: elfaudit.py --root <install-tree> [--json FILE]
"""
import argparse, json, os, re, sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from elf import Elf64, NotElf  # noqa: E402

GLIBC_RE = re.compile(rb"GLIBC_(\d+)\.(\d+)(?:\.(\d+))?\x00")

# Tier 1: the C library itself.  Strategy B says the host provides exactly this.
LIBC_CORE = {
    "libc.so.6", "libm.so.6", "libpthread.so.0", "libdl.so.2", "librt.so.1",
    "libutil.so.1", "libresolv.so.2", "libanl.so.1", "libnsl.so.1",
    "ld-linux-x86-64.so.2", "linux-vdso.so.1", "libmvec.so.1",
}
# Tier 2: kernel/driver-bound or site-bound libraries that MUST come from the
# host and cannot be shipped.  Expected only if the corresponding variant is on.
HOST_INJECTED = {
    "libcuda.so.1", "libnvidia-ml.so.1", "libGL.so.1", "libGLX.so.0",
    "libEGL.so.1", "libOpenGL.so.0", "libGLdispatch.so.0",
    "libibverbs.so.1", "librdmacm.so.1", "libfabric.so.1",
    "libpmi2.so.0", "libpmix.so.2", "libpsm2.so.2", "libcxi.so.1",
}
# Tier 3: present on most glibc distros but NOT part of glibc.  Linking these
# silently narrows portability -- report them.
GREY = {
    "libcrypt.so.1", "libcrypt.so.2", "libselinux.so.1", "libcap.so.2",
    "libtinfo.so.6", "libstdc++.so.6", "libgcc_s.so.1", "libgomp.so.1",
    "libsystemd.so.0", "libudev.so.1", "libgfortran.so.5", "libquadmath.so.0",
}


def glibc_floor(blob):
    best = (0, 0, 0)
    for m in GLIBC_RE.finditer(blob):
        v = (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))
        best = max(best, v)
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--json")
    args = ap.parse_args()
    root = os.path.normpath(os.path.realpath(args.root))

    objects, provided = [], {}
    for dirpath, _d, files in os.walk(root):
        for name in files:
            p = os.path.join(dirpath, name)
            if os.path.islink(p) or not os.path.isfile(p):
                continue
            try:
                e = Elf64(p)
            except (NotElf, OSError):
                continue
            son = e.soname()
            if son:
                provided.setdefault(son, p)
            provided.setdefault(os.path.basename(p), p)
            objects.append((p, e))
    # Symlinks inside the tree also satisfy a DT_NEEDED.
    for dirpath, _d, files in os.walk(root):
        for name in files:
            p = os.path.join(dirpath, name)
            if os.path.islink(p):
                provided.setdefault(name, p)

    floor = (0, 0, 0)
    floor_files = []
    needed_missing = Counter()
    missing_by_lib = defaultdict(list)
    rpath_kind = Counter()
    interps = Counter()
    absolute_rpaths = []

    for p, e in objects:
        v = glibc_floor(e.dynstr_blob())
        if v > floor:
            floor, floor_files = v, [p]
        elif v == floor and v > (0, 0, 0) and len(floor_files) < 10:
            floor_files.append(p)

        if e.interp:
            interps[e.interp] += 1

        for n in e.needed():
            if not n:
                continue
            if n not in provided:
                needed_missing[n] += 1
                if len(missing_by_lib[n]) < 5:
                    missing_by_lib[n].append(p)

        for r in e.rpath_entries():
            for comp in r["value"].split(":"):
                if not comp:
                    continue
                if comp.startswith("$ORIGIN") or comp.startswith("${ORIGIN}"):
                    rpath_kind["origin"] += 1
                elif comp.startswith(root):
                    rpath_kind["absolute-in-tree"] += 1
                    if len(absolute_rpaths) < 25:
                        absolute_rpaths.append({"file": p, "component": comp})
                elif comp.startswith("/"):
                    rpath_kind["absolute-foreign"] += 1
                    if len(absolute_rpaths) < 25:
                        absolute_rpaths.append({"file": p, "component": comp})
                else:
                    rpath_kind["relative"] += 1

    def tier(lib):
        if lib in LIBC_CORE:
            return "libc"
        if lib in HOST_INJECTED:
            return "host-injected"
        if lib in GREY:
            return "grey"
        return "unexpected"

    external = []
    for lib, count in needed_missing.most_common():
        external.append({
            "library": lib, "tier": tier(lib), "referencing_objects": count,
            "examples": missing_by_lib[lib],
        })

    findings = [x for x in external if x["tier"] in ("grey", "unexpected")]

    result = {
        "phase": "audit",
        "root": root,
        "elf_objects": len(objects),
        "glibc_floor": "%d.%d.%d" % floor if floor[2] else "%d.%d" % floor[:2],
        "glibc_floor_tuple": list(floor),
        "glibc_floor_examples": floor_files,
        "program_interpreters": dict(interps),
        "rpath_components": dict(rpath_kind),
        "absolute_rpath_examples": absolute_rpaths,
        "external_libraries": external,
        "findings": findings,
        "verdict": "clean" if not findings else "review",
    }

    if args.json:
        with open(args.json, "w") as fp:
            json.dump(result, fp, indent=2)

    print("ELF objects .............. %d" % result["elf_objects"])
    print("glibc floor .............. %s" % result["glibc_floor"])
    print("program interpreters ..... %s" % ", ".join(interps) or "(none)")
    print("rpath components ......... %s" % dict(rpath_kind))
    print("external libraries:")
    for x in external:
        print("  [%-13s] %-28s (%d objects)" % (x["tier"], x["library"], x["referencing_objects"]))
    print("verdict: %s" % result["verdict"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
