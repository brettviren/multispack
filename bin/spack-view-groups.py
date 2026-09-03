#!/usr/bin/env python3
"""spack-view-groups -- analyze & check a hand-curated grouped spack.yaml.

This is a READ-ONLY analyzer/checker over a native-Spack grouped environment (one
whose ``specs:`` uses ``group:``/``needs:`` and, optionally, ``view:`` selecting
groups).  It does NOT derive, synthesize, or emit groups -- the group DAG is human
input.  It concretizes (or reads an existing ``spack.lock``) and reports the things
that bite a grouped LArSoft-style stack:

  * needs-DAG sanity (references resolve, no cycles) and topological build order;
  * effective ``concretizer:reuse`` (env scope vs the site's ``reuse:true``);
  * multi-version packages, split into link/run (deployable -> real collision risk)
    vs build-only (benign store forks), with their consumers;
  * link/run forks whose package is NOT pinned in ``packages:`` -> a "pin this"
    hint (per the largroups lesson: only ``packages: require`` forces one version);
  * per-``view:`` collisions (two versions of a package in one flat view);
  * cross-group sharing (how many concrete nodes each pair of groups shares).

Hard checks (non-zero exit): an invalid needs DAG; ``--require-single PKG`` when
that package has >1 link/run version; ``--strict-single`` when ANY package does.

Runs where ``spack`` is available (the multispack builder); reads the env's
``spack.lock`` and ``spack config get`` output.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple


# --------------------------------------------------------------------------- #
# YAML: PyYAML if present, else Spack's vendored ruamel (adds $SPACK_ROOT path). #
# --------------------------------------------------------------------------- #
def _yaml_loader():
    try:
        import yaml  # type: ignore
        return lambda text: yaml.safe_load(text)
    except Exception:
        pass
    spack_root = os.environ.get("SPACK_ROOT")
    if spack_root:
        libspack = os.path.join(spack_root, "lib", "spack")
        if libspack not in sys.path:
            sys.path.insert(0, libspack)
    try:
        from spack.vendor.ruamel import yaml as ry  # type: ignore
    except Exception as exc:  # pragma: no cover
        sys.exit("spack-view-groups: no YAML library (PyYAML or Spack ruamel): %s" % exc)
    _Y = ry.YAML(typ="safe")
    return lambda text: _Y.load(text)


yaml_load = _yaml_loader()

RESET, BOLD, RED, YEL, GRN, CYN = (
    "\033[0m", "\033[1m", "\033[1;31m", "\033[1;33m", "\033[1;32m", "\033[1;36m")


def log(msg: str) -> None:
    sys.stderr.write("%s[viewgroups]%s %s\n" % (CYN, RESET, msg))
    sys.stderr.flush()


# --------------------------------------------------------------------------- #
# Spack + lockfile plumbing                                                      #
# --------------------------------------------------------------------------- #
def run_spack(spack: str, envdir: str, *args: str, timeout: Optional[float] = None) -> Tuple[int, str]:
    proc = subprocess.run([spack, "-e", envdir, *args],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=timeout, text=True)
    return proc.returncode, proc.stdout


def read_lock(envdir: str) -> Dict[str, Any]:
    with open(os.path.join(envdir, "spack.lock")) as fh:
        return json.load(fh)


def dep_edges(node: Dict[str, Any]) -> List[Tuple[str, Set[str]]]:
    """(dep_hash, deptypes) for each dependency edge of a lock node."""
    out = []
    for d in node.get("dependencies", []) or []:
        if isinstance(d, dict) and d.get("hash"):
            dts = set((d.get("parameters", {}) or {}).get("deptypes", d.get("type", [])))
            out.append((d["hash"], dts))
    return out


LINKRUN = {"link", "run"}


def closure(roots: List[str], cs: Dict[str, Any], deptypes: Optional[Set[str]] = None) -> Set[str]:
    """Reachable node set from roots; if deptypes given, only follow those edges."""
    seen: Set[str] = set()
    stack = list(roots)
    while stack:
        h = stack.pop()
        if h in seen or h not in cs:
            continue
        seen.add(h)
        for dh, dts in dep_edges(cs[h]):
            if deptypes is None or (dts & deptypes):
                stack.append(dh)
    return seen


# --------------------------------------------------------------------------- #
# Parse the raw manifest: groups, needs, views, loose specs                     #
# --------------------------------------------------------------------------- #
def parse_manifest(spack_yaml: str) -> Dict[str, Any]:
    with open(spack_yaml) as fh:
        doc = yaml_load(fh.read())
    if not doc or "spack" not in doc:
        sys.exit("spack-view-groups: %s has no top-level 'spack:'" % spack_yaml)
    env = doc["spack"]
    groups: Dict[str, List[str]] = {}   # name -> needs
    loose: List[str] = []
    for item in (env.get("specs") or []):
        if isinstance(item, dict) and "group" in item:
            groups[item["group"]] = list(item.get("needs", []) or [])
        elif isinstance(item, str):
            loose.append(item)
    views: Dict[str, Dict[str, Any]] = {}
    v = env.get("view")
    if isinstance(v, dict):
        for name, cfg in v.items():
            cfg = dict(cfg or {})
            g = cfg.get("group")
            if isinstance(g, str):
                g = [g]
            views[name] = {"groups": g or [], "link": cfg.get("link", "all"),
                           "select": cfg.get("select")}
    return {"groups": groups, "loose": loose, "views": views}


def check_needs_dag(groups: Dict[str, List[str]]) -> Tuple[bool, List[str], List[str]]:
    """Validate needs references and acyclicity; return (ok, order, problems)."""
    problems: List[str] = []
    for g, needs in groups.items():
        for n in needs:
            if n not in groups:
                problems.append("group %r needs %r, which is not a defined group" % (g, n))
    # Kahn topological sort over the (valid) needs edges.
    indeg = {g: 0 for g in groups}
    for g, needs in groups.items():
        for n in needs:
            if n in groups:
                indeg[g] += 1
    order: List[str] = []
    ready = sorted([g for g, d in indeg.items() if d == 0])
    while ready:
        g = ready.pop(0)
        order.append(g)
        for h, needs in groups.items():
            if g in needs and h not in order:
                indeg[h] -= 1
                if indeg[h] == 0:
                    ready.append(h)
        ready.sort()
    if len(order) != len(groups):
        problems.append("needs DAG has a cycle among: %s"
                        % ", ".join(sorted(set(groups) - set(order))))
    return (not problems), order, problems


# --------------------------------------------------------------------------- #
# Analyses over the concretized lock                                            #
# --------------------------------------------------------------------------- #
def roots_by_group(lock: Dict[str, Any]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = defaultdict(list)
    for r in lock.get("roots", []):
        if isinstance(r, dict):
            out[r.get("group", "default")].append(r["hash"])
    return out


def merged_pins(spack: str, envdir: str) -> Set[str]:
    """Package names that carry a version 'require' in the merged packages: config."""
    rc, out = run_spack(spack, envdir, "config", "get", "packages")
    pinned: Set[str] = set()
    if rc != 0:
        return pinned
    try:
        pkgs = (yaml_load(out) or {}).get("packages", {})
    except Exception:
        return pinned
    for name, cfg in pkgs.items():
        if not isinstance(cfg, dict):
            continue
        req = cfg.get("require")
        reqs = req if isinstance(req, list) else [req]
        for r in reqs:
            if isinstance(r, str) and "@" in r:
                pinned.add(name.rstrip(":"))
    return pinned


def multiversion(cs: Dict[str, Any], deployable: Set[str]) -> Dict[str, Dict[str, Any]]:
    """name -> {linkrun:[versions], buildonly:[versions], users:{version:set(names)}}."""
    by_name: Dict[str, Dict[str, Set[str]]] = defaultdict(lambda: {"lr": set(), "bo": set()})
    hashes_by_name_ver: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for h, n in cs.items():
        name, ver = n.get("name", "?"), str(n.get("version", ""))
        (by_name[name]["lr"] if h in deployable else by_name[name]["bo"]).add(ver)
        hashes_by_name_ver[(name, ver)].append(h)
    # consumers: for each (name,version) node, the link/run parents' names
    parents: Dict[str, Set[str]] = defaultdict(set)
    for ph, pn in cs.items():
        for dh, dts in dep_edges(pn):
            if dts & LINKRUN:
                parents[dh].add(pn.get("name", "?"))
    out: Dict[str, Dict[str, Any]] = {}
    for name, d in by_name.items():
        allv = d["lr"] | d["bo"]
        if len(allv) < 2:
            continue
        users = {}
        for ver in sorted(allv):
            us: Set[str] = set()
            for h in hashes_by_name_ver[(name, ver)]:
                us |= parents.get(h, set())
            users[ver] = us
        out[name] = {"linkrun": sorted(d["lr"]), "buildonly": sorted(d["bo"] - d["lr"]),
                     "users": users}
    return out


# runtime packages Spack dedupes to newest in a view (never a real view collision)
RUNTIME_PKGS = {"gcc-runtime", "compiler-wrapper"}


def view_collisions(views: Dict[str, Dict[str, Any]], rbg: Dict[str, List[str]],
                    cs: Dict[str, Any]) -> Dict[str, Dict[str, List[str]]]:
    """Per view: packages appearing at >1 version in the view's link/run closure."""
    out: Dict[str, Dict[str, List[str]]] = {}
    for vname, cfg in views.items():
        roots = [h for g in cfg["groups"] for h in rbg.get(g, [])]
        cl = closure(roots, cs, LINKRUN)
        ver_by_name: Dict[str, Set[str]] = defaultdict(set)
        for h in cl:
            n = cs[h]
            if n.get("name") in RUNTIME_PKGS:
                continue
            ver_by_name[n.get("name", "?")].add(str(n.get("version", "")))
        coll = {nm: sorted(vs) for nm, vs in ver_by_name.items() if len(vs) > 1}
        if coll:
            out[vname] = coll
    return out


# --------------------------------------------------------------------------- #
# Reporting                                                                     #
# --------------------------------------------------------------------------- #
def report(manifest: Dict[str, Any], lock: Dict[str, Any], cs: Dict[str, Any],
           rbg: Dict[str, List[str]], deployable: Set[str], mv: Dict[str, Dict[str, Any]],
           pinned: Set[str], order: List[str], reuse: Any, full: bool) -> None:
    groups = manifest["groups"]
    print("%s== grouped environment ==%s" % (BOLD, RESET))
    print("  concrete nodes: %d   deployable (link/run): %d   groups: %d   loose specs: %d"
          % (len(cs), len(deployable), len(groups), len(manifest["loose"])))
    print("  concretizer:reuse (effective): %s" % reuse)
    print("  build order: %s" % " -> ".join(order))
    for g in order:
        rs = rbg.get(g, [])
        cl = closure(rs, cs)
        print("    %-10s roots=%-3d closure=%-4d needs=[%s]"
              % (g, len(rs), len(cl), ",".join(groups.get(g, []))))

    # cross-group sharing (dag_hash intersection of full closures)
    if len(groups) > 1 and full:
        print("%s== cross-group shared nodes ==%s" % (BOLD, RESET))
        clo = {g: closure(rbg.get(g, []), cs) for g in order}
        for i, a in enumerate(order):
            for b in order[i + 1:]:
                s = len(clo[a] & clo[b])
                if s:
                    print("    %-10s ∩ %-10s = %d" % (a, b, s))

    # multi-version
    lr_forks = {n: d for n, d in mv.items() if len(d["linkrun"]) > 1}
    bo_forks = {n: d for n, d in mv.items() if len(d["linkrun"]) <= 1}
    print("%s== multi-version packages ==%s" % (BOLD, RESET))
    if not mv:
        print("  none")
    if lr_forks:
        print("  %sLINK/RUN forks (deployment risk):%s" % (RED, RESET))
        for n in sorted(lr_forks):
            d = lr_forks[n]
            pin = "" if n in pinned else "  %s<- not pinned in packages:%s" % (YEL, RESET)
            print("    %-16s %s%s" % (n, d["linkrun"], pin))
            if full:
                for ver in d["linkrun"]:
                    us = sorted(d["users"][ver])
                    print("        @%-10s used by %d: %s" % (ver, len(us), us[:6]))
    if bo_forks and full:
        print("  build-only forks (benign; never deployed):")
        for n in sorted(bo_forks):
            print("    %-16s link/run=%s build-only=%s"
                  % (n, bo_forks[n]["linkrun"], bo_forks[n]["buildonly"]))

    # unpinned link/run forks -> hint
    unpinned = sorted(n for n in lr_forks if n not in pinned)
    if unpinned:
        print("%s== hint ==%s pin these in packages: to force one version: %s"
              % (BOLD, RESET, ", ".join(unpinned)))


# --------------------------------------------------------------------------- #
# CLI                                                                           #
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="spack-view-groups",
        description="Analyze & check a hand-curated grouped spack.yaml (read-only).")
    ap.add_argument("env", help="path to the environment's spack.yaml (or its directory)")
    ap.add_argument("--spack", default=os.environ.get("SPACK", "spack"),
                    help="spack executable (default: 'spack' on PATH)")
    ap.add_argument("--concretize", action="store_true",
                    help="run 'spack concretize -f' first (default: reuse spack.lock if present)")
    ap.add_argument("--timeout", type=float, default=0, metavar="SECS",
                    help="timeout for a triggered concretize (default: none)")
    ap.add_argument("--require-single", action="append", default=[], metavar="PKG",
                    help="FAIL if PKG has >1 link/run version (repeatable)")
    ap.add_argument("--strict-single", action="store_true",
                    help="FAIL if ANY package has >1 link/run version")
    ap.add_argument("--report", action="store_true", help="verbose report (consumers, sharing)")
    args = ap.parse_args(argv)

    envdir = args.env
    if os.path.isfile(envdir):
        envdir = os.path.dirname(os.path.abspath(envdir)) or "."
    envdir = os.path.abspath(envdir)
    spack_yaml = os.path.join(envdir, "spack.yaml")
    if not os.path.isfile(spack_yaml):
        sys.exit("spack-view-groups: no spack.yaml in %s" % envdir)

    manifest = parse_manifest(spack_yaml)

    # needs-DAG check first (cheap, no concretize needed)
    ok_dag, order, problems = check_needs_dag(manifest["groups"])
    for p in problems:
        log("%sneeds-DAG: %s%s" % (RED, p, RESET))
    if not ok_dag:
        return 2

    lockpath = os.path.join(envdir, "spack.lock")
    if args.concretize or not os.path.isfile(lockpath):
        log("concretizing %s ..." % envdir)
        rc, out = run_spack(args.spack, envdir, "concretize", "-f",
                            timeout=(args.timeout or None))
        if rc != 0:
            log("%sconcretize failed (rc=%d)%s" % (RED, rc, RESET))
            sys.stderr.write("\n".join(out.splitlines()[-40:]) + "\n")
            return 2

    lock = read_lock(envdir)
    cs = lock.get("concrete_specs", {})
    rbg = roots_by_group(lock)
    all_roots = [h for hs in rbg.values() for h in hs]
    deployable = closure(all_roots, cs, LINKRUN)
    pinned = merged_pins(args.spack, envdir)
    mv = multiversion(cs, deployable)

    rc2, cz = run_spack(args.spack, envdir, "config", "get", "concretizer")
    reuse = "?"
    try:
        reuse = (yaml_load(cz) or {}).get("concretizer", {}).get("reuse", "<unset>")
    except Exception:
        pass

    report(manifest, lock, cs, rbg, deployable, mv, pinned, order, reuse, args.report)

    # per-view collisions
    vcoll = view_collisions(manifest["views"], rbg, cs)
    if manifest["views"]:
        print("%s== views ==%s" % (BOLD, RESET))
        for vname in manifest["views"]:
            if vname in vcoll:
                print("  %sCOLLISION%s %-12s %s" % (RED, RESET, vname,
                      ", ".join("%s{%s}" % (k, ",".join(v)) for k, v in vcoll[vname].items())))
            else:
                print("  %sok%s        %s" % (GRN, RESET, vname))

    # hard checks
    exit_code = 0
    lr_forks = {n: d for n, d in mv.items() if len(d["linkrun"]) > 1}
    for pkg in args.require_single:
        vers = mv.get(pkg, {}).get("linkrun", [])
        if len(vers) > 1:
            log("%srequire-single FAILED: %s has link/run versions %s%s"
                % (RED, pkg, vers, RESET))
            exit_code = 2
        else:
            log("%srequire-single ok: %s%s" % (GRN, pkg, RESET))
    if args.strict_single and lr_forks:
        log("%sstrict-single FAILED: link/run forks: %s%s"
            % (RED, ", ".join(sorted(lr_forks)), RESET))
        exit_code = 2
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
