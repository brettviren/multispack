#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0
"""Plan a monolithic runtime image of one Spack environment (`mspack image`).

Run INSIDE the builder as ``spack -e <env> python image-plan.py`` with the
store volumes mounted at their build paths.  It decides WHAT goes into the
image and renders the files that must differ there; it copies nothing itself
(``image-payload.sh`` turns the plan into one tar layer).

Inputs (environment):
  CVMFS_ROOT      the install root (/cvmfs/<host>)
  IMG_ENV         env name (dir under $CVMFS_ROOT/env, or a managed env)
  IMG_SCOPE       closure (default) | all
  IMG_COMPILERS   target (default) | all | none   -- which store gccs to ship
  IMG_BASE_PATH   PATH of the runtime base image
  IMG_PYTHON      python interpreter Spack uses in the image
  IMG_OUT         output dir (default /out)

Outputs in IMG_OUT:
  plan.json       include paths, relocations, excludes, overlay files, env, labels
  overlay/        files that REPLACE or ADD to the copied tree (rooted at /)
"""

import hashlib
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone

import spack.config
import spack.environment as ev
import spack.repo
import spack.store
import spack.traverse
import spack.util.spack_yaml as syaml

R = os.environ["CVMFS_ROOT"].rstrip("/")
NAME = os.environ["IMG_ENV"]
SCOPE = os.environ.get("IMG_SCOPE", "closure")
COMPILERS = os.environ.get("IMG_COMPILERS", "target")
BASE_PATH = os.environ.get("IMG_BASE_PATH",
                           "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
PYTHON = os.environ.get("IMG_PYTHON", "/usr/bin/python3")
OUT = os.environ.get("IMG_OUT", "/out")
OVL = os.path.join(OUT, "overlay")
SPACK_ROOT = os.path.join(R, "spack")
WORK = "/multispack/work"
REPOS_DIR = os.path.join(R, "repos")


def say(msg):
    print(f"[image-plan] {msg}", file=sys.stderr, flush=True)


def die(msg):
    print(f"[image-plan] ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(2)


def overlay_write(abspath, text, mode=0o644):
    p = OVL + abspath
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(p, mode)
    overlay_files.append(abspath.lstrip("/"))


def under(path, root):
    path, root = os.path.normpath(path), os.path.normpath(root)
    return path == root or path.startswith(root + "/")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def yaml_load(path):
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return syaml.load(f)


def yaml_dump(data):
    return syaml.dump_config(data, default_flow_style=False)


overlay_files: list = []
include: list = []          # absolute paths copied verbatim
relocate: list = []         # [src_abs, dst_abs] copied to a new place
excludes: list = []         # tar --exclude patterns (member names, no leading /)

# --------------------------------------------------------------------------
env = ev.active_environment()
if env is None:
    die("no active environment (run as `spack -e ENV python image-plan.py`)")
envdir = os.path.realpath(env.path)
say(f"env '{NAME}' at {envdir}")
include.append(envdir)
excludes.append(os.path.join(envdir, ".spack-env", "transaction_lock").lstrip("/"))

# Views the env maintains (default: <envdir>/.spack-env/view, or a configured
# root).  A view outside the env dir must be shipped too.
views = {}
for vname, view in env.views.items():
    root = view.root
    views[vname] = root
    real = os.path.realpath(root) if os.path.exists(root) else root
    for p in {root, real}:
        if os.path.lexists(p) and not under(p, envdir):
            include.append(p)
default_view = views.get("default")

# --------------------------------------------------------------------------
# Which installed specs ship.
db = spack.store.STORE.db
installed = {s.dag_hash(): s for s in db.query(installed=True) if not s.external}
ship = {}

roots = [c for _, c in env.concretized_specs()]
if SCOPE == "all":
    ship = dict(installed)
else:
    for s in spack.traverse.traverse_nodes(roots, deptype=("link", "run")):
        h = s.dag_hash()
        if h in installed:
            ship[h] = installed[h]
missing_roots = [r.format("{name}@{version}/{hash:7}") for r in roots
                 if r.dag_hash() not in installed and not r.external]
if missing_roots:
    die(f"env roots not installed: {' '.join(missing_roots)} (run makenv first)")

# Compilers that live in the store.  'target' = GCC_TARGET_SPEC (env/gcc/env.sh).
compilers = [s for s in installed.values() if s.name in ("gcc", "llvm", "intel-oneapi-compilers")]
target_prefix = None
gccenv = os.path.join(R, "env", "gcc", "env.sh")
if os.path.isfile(gccenv):
    for line in open(gccenv, encoding="utf-8"):
        if line.startswith("GCC_PREFIX="):
            target_prefix = line.split("=", 1)[1].split(";")[0].strip()
want_comp = []
if COMPILERS == "all":
    want_comp = compilers
elif COMPILERS == "target":
    want_comp = [s for s in compilers if target_prefix and s.prefix == target_prefix]
    if not want_comp:
        say("warning: no target compiler found (env/gcc/env.sh); shipping none")
for c in want_comp:
    for s in spack.traverse.traverse_nodes([c], deptype=("link", "run")):
        if s.dag_hash() in installed:
            ship[s.dag_hash()] = installed[s.dag_hash()]
if want_comp and os.path.isdir(os.path.dirname(gccenv)):
    include.append(os.path.dirname(gccenv))
say(f"shipping {len(ship)} of {len(installed)} installed specs "
    f"(scope={SCOPE}, compilers={COMPILERS}: "
    f"{' '.join(c.format('{name}@{version}') for c in want_comp) or '-'})")
shipped_prefixes = {os.path.normpath(s.prefix) for s in ship.values()}
include += sorted(shipped_prefixes)

# --------------------------------------------------------------------------
# Spack itself + its DB.  The DB is pruned so `spack find` lists only what ships.
include.append(SPACK_ROOT)
for pat in ("spack/var/spack/cache", "spack/var/spack/stage",
            "spack/lib/spack/*__pycache__*"):
    excludes.append(f"{R.lstrip('/')}/{pat}")
dbdir = getattr(db, "database_directory", None) or os.path.join(spack.store.STORE.root, ".spack-db")
include.append(dbdir)
# sbang (long-shebang trampoline) lives in <unpadded install root>/bin.
sbang_dir = os.path.join(getattr(spack.store.STORE, "unpadded_root", os.path.join(R, "opt")), "bin")
if os.path.isdir(sbang_dir):
    include.append(sbang_dir)
# Keep the (empty) lock files: Spack opens them even with locks:false and
# cannot create them in a read-only image.  Failure markers are build state.
for f in ("prefix_failures", "failures"):
    excludes.append(f"*/.spack-db/{f}")
index = json.load(open(os.path.join(dbdir, "index.json"), encoding="utf-8"))
installs = index["database"]["installs"]
pruned = 0
store_opt = os.path.join(R, "opt")
for h, rec in installs.items():
    if not rec.get("installed") or h in ship:
        continue
    ext = rec["spec"].get("external")
    if ext:
        # an external whose prefix is an unshipped store install (e.g. the
        # gcc@12 base compiler, recorded as an external when used)
        xp = os.path.normpath((ext or {}).get("path") or rec.get("path") or "")
        if under(xp, store_opt) and xp not in shipped_prefixes:
            rec["installed"] = False
            pruned += 1
    elif h in installed:        # a real store prefix we are not shipping
        rec["installed"] = False
        pruned += 1
if pruned:
    excludes.append(os.path.join(dbdir, "index.json").lstrip("/"))
    overlay_write(os.path.join(dbdir, "index.json"), json.dumps(index))
say(f"database: {pruned} installed record(s) marked not-installed")

# --------------------------------------------------------------------------
# Recipe repos.  Git repos were cloned onto the work volume (package_repos);
# copy each checkout under $CVMFS_ROOT/repos/<name> and point Spack there via a
# `destination:` override (merged with the env's git: entry, so the env's
# spack.yaml stays verbatim).  Path repos under $CVMFS_ROOT ship as is.
repo_cfg = spack.config.get("repos") or {}
site_repos_path = os.path.join(SPACK_ROOT, "etc", "spack", "repos.yaml")
site_repos = (yaml_load(site_repos_path) or {}).get("repos", {}) or {}
repo_map = {}
for rname, desc in repo_cfg.items():
    d = spack.repo.parse_config_descriptor(rname, desc, spack.repo.package_repository_lock())
    if isinstance(d, spack.repo.RemoteRepoDescriptor):
        src = os.path.realpath(d.destination)
        if under(src, R):
            include.append(src)
            repo_map[rname] = {"git": d.repository, "path": src}
            continue
        if not os.path.isdir(os.path.join(src, ".git")):
            die(f"repo '{rname}' ({d.repository}) is not cloned at {src}")
        dst = os.path.join(REPOS_DIR, rname)
        relocate.append([src, dst])
        excludes.append(f"{src.lstrip('/')}/*__pycache__*")
        cur = site_repos.get(rname)
        entry = dict(cur) if isinstance(cur, dict) else {}
        entry["destination"] = dst
        site_repos[rname] = entry
        commit = subprocess.run(["git", "-C", src, "rev-parse", "HEAD"],
                                capture_output=True, text=True).stdout.strip()
        repo_map[rname] = {"git": d.repository, "commit": commit, "path": dst}
    else:
        path = os.path.realpath(d.path) if hasattr(d, "path") else str(desc)
        if under(path, R):
            include.append(path)
            repo_map[rname] = {"path": path}
        else:
            die(f"repo '{rname}' is a path outside {R} ({path}); it cannot ship")
if any("destination" in (v or {}) for v in site_repos.values() if isinstance(v, dict)):
    if os.path.isfile(site_repos_path):
        excludes.append(site_repos_path.lstrip("/"))
    overlay_write(site_repos_path,
                  "# mspack image: recipe repos relocated from the build work volume.\n"
                  + yaml_dump({"repos": site_repos}))
say("repos: " + ", ".join(f"{k} -> {v['path']}" for k, v in repo_map.items()))

# --------------------------------------------------------------------------
# Site config that must differ in a read-only image: no work/cache volumes, no
# buildcache mirror, a store-relative clingo bootstrap root, no locking (the
# store is read-only; nothing installs into it).
etc = os.path.join(SPACK_ROOT, "etc", "spack")
cfg_path = os.path.join(etc, "config.yaml")
cfg = yaml_load(cfg_path) or {"config": {}}
c = cfg.setdefault("config", {})
c["build_stage"] = ["$tempdir/$user/spack-stage"]
c["test_stage"] = "$user_cache_path/test"
c["source_cache"] = "$user_cache_path/source"
c["misc_cache"] = "$user_cache_path/cache"
c["locks"] = False
excludes.append(cfg_path.lstrip("/"))
overlay_write(cfg_path, "# mspack image: runtime (read-only store) config.\n" + yaml_dump(cfg))

mir_path = os.path.join(etc, "mirrors.yaml")
if os.path.isfile(mir_path):
    excludes.append(mir_path.lstrip("/"))
overlay_write(mir_path, "# mspack image: the build buildcache is not shipped.\nmirrors: {}\n")

# clingo: Spack rewrites <bootstrap root>/config/*.yaml on every bootstrap
# check, so the root must be writable -- it is the per-user
# $user_cache_path/bootstrap.  The baked store ($CVMFS_ROOT/bootstrap/store,
# built for the image's python) is linked in as its `store` by the `spack`
# wrapper on PATH (/usr/local/bin/spack), so concretizing works offline.  If
# the link cannot be made, Spack simply bootstraps from the network.
boot_root = os.path.join(R, "bootstrap")
boot_store = os.path.join(boot_root, "store")
boot_path = os.path.join(etc, "bootstrap.yaml")
if os.path.isfile(boot_path):
    excludes.append(boot_path.lstrip("/"))
overlay_write(boot_path, "# mspack image: per-user (writable) bootstrap root; see "
              "/usr/local/bin/spack.\n"
              + yaml_dump({"bootstrap": {"root": "$user_cache_path/bootstrap"}}))
if os.path.isdir(boot_store):
    include.append(boot_store)
    excludes.append(os.path.join(boot_root, "config").lstrip("/"))
else:
    say(f"warning: no {boot_store}; `spack concretize` will bootstrap from the network")
overlay_write("/usr/local/bin/spack", f"""#!/bin/sh
# mspack image: seed the per-user Spack bootstrap root with the clingo store
# baked into the image, then run Spack.  Spack rewrites the bootstrap root's
# config and locks the store's DB on use, so both must be writable: the store is
# a per-user dir holding a COPY of the (tiny) DB and symlinks to the read-only
# installs (whose DB records still point at the baked prefixes).
# Per-user Spack state goes to its own dir, not a host Spack's ~/.spack.
: "${{SPACK_USER_CACHE_PATH:=${{HOME:-/tmp}}/.mspack/{os.path.basename(R)}}}"
export SPACK_USER_CACHE_PATH
_b="$SPACK_USER_CACHE_PATH/bootstrap"
if [ ! -e "$_b/store/.spack-db" ] && [ -d "{boot_store}/.spack-db" ]; then
    mkdir -p "$_b/store" 2>/dev/null \\
    && cp -R "{boot_store}/.spack-db" "$_b/store/" 2>/dev/null \\
    && for _e in "{boot_store}"/*; do ln -sfn "$_e" "$_b/store/" 2>/dev/null; done
fi
exec "{SPACK_ROOT}/bin/spack" "$@"
""", mode=0o755)

# Externals pointing at store prefixes we do not ship (e.g. the gcc@12 base
# compiler when --compilers target) would make the concretizer pick a missing
# compiler; drop them.
for pk in (os.path.join(etc, "packages.yaml"), os.path.join(etc, "site", "packages.yaml")):
    data = yaml_load(pk)
    if not data or "packages" not in data:
        continue
    changed = False
    for pname, pcfg in list(data["packages"].items()):
        exts = (pcfg or {}).get("externals")
        if not exts:
            continue
        keep = [e for e in exts if not (under(e.get("prefix", ""), os.path.join(R, "opt"))
                                        and os.path.normpath(e["prefix"]) not in shipped_prefixes)]
        if len(keep) != len(exts):
            changed = True
            dropped = [e.get("spec") for e in exts if e not in keep]
            say(f"{pk}: dropping unshipped external(s) {dropped}")
            if keep:
                pcfg["externals"] = keep
            else:
                del pcfg["externals"]
    if changed:
        excludes.append(pk.lstrip("/"))
        overlay_write(pk, "# mspack image: unshipped store externals removed.\n" + yaml_dump(data))

# --------------------------------------------------------------------------
# Runtime environment: exactly what `spack env activate` sets, computed against
# the runtime base's clean PATH, plus the shipped compiler and Spack itself.
act_env = {"PATH": BASE_PATH, "HOME": "/tmp", "TMPDIR": "/tmp",
           "SPACK_DISABLE_LOCAL_CONFIG": "1",
           "SPACK_USER_CACHE_PATH": os.environ.get("SPACK_USER_CACHE_PATH",
                                                   f"{WORK}/spack-user-cache"),
           "LANG": "C.UTF-8"}
envarg = ["-d", envdir] if os.path.isdir(os.path.join(R, "env", NAME)) else [NAME]
proc = subprocess.run([os.path.join(SPACK_ROOT, "bin", "spack"), "env", "activate",
                       "--sh"] + envarg, env=act_env, capture_output=True, text=True)
if proc.returncode != 0:
    die(f"spack env activate --sh failed: {proc.stderr.strip()}")
runtime = {}
for stmt in proc.stdout.replace("\n", ";").split(";"):
    stmt = stmt.strip()
    if not stmt.startswith("export "):
        continue
    try:
        toks = shlex.split(stmt[len("export "):])
    except ValueError:
        continue
    if not toks or "=" not in toks[0]:
        continue
    k, v = toks[0].split("=", 1)
    if k in ("PS1", "SPACK_OLD_PS1") or k.startswith("__"):
        continue
    runtime[k] = v
path = [p for p in runtime.get("PATH", BASE_PATH).split(":") if p]
if want_comp:
    tbin = os.path.join(target_prefix or want_comp[0].prefix, "bin")
    if tbin not in path:
        # just after the view's bin so the view still wins for its own tools
        vbin = os.path.join(default_view, "bin") if default_view else None
        at = path.index(vbin) + 1 if vbin in path else 0
        path.insert(at, tbin)
sbin = os.path.join(SPACK_ROOT, "bin")
if sbin not in path:
    path.append(sbin)
runtime["PATH"] = ":".join(path)
runtime.update({
    "SPACK_ROOT": SPACK_ROOT,
    "SPACK_PYTHON": PYTHON,
    "SPACK_DISABLE_LOCAL_CONFIG": "1",
    "MSPACK_ENV": NAME,
    "MSPACK_ENV_DIR": envdir,
})
if os.path.isdir(boot_store):
    runtime["MSPACK_BOOTSTRAP_STORE"] = boot_store
if default_view:
    runtime["MSPACK_VIEW"] = default_view
if want_comp:
    runtime["MSPACK_GCC_PREFIX"] = target_prefix or want_comp[0].prefix

prof = [f"# mspack image: runtime environment of Spack env '{NAME}'.",
        "# The same values are baked into the image config (ENV), so non-login",
        "# `podman run`/`apptainer exec` see them too.  This file re-applies them",
        "# for login shells (whose /etc/profile resets PATH) and adds the `spack`",
        "# shell function (needed for `spack env activate`/`spack load`).", ""]
for k in sorted(runtime):
    prof.append(f"export {k}={shlex.quote(runtime[k])}")
prof += ["# per-user Spack state (caches, clingo bootstrap root), apart from a host ~/.spack",
         f': "${{SPACK_USER_CACHE_PATH:=${{HOME:-/tmp}}/.mspack/{os.path.basename(R)}}}"',
         "export SPACK_USER_CACHE_PATH"]
if os.path.isdir(boot_store):
    prof += ["",
             "# seed the per-user bootstrap root with the baked clingo (see /usr/local/bin/spack)",
             '_b="$SPACK_USER_CACHE_PATH/bootstrap"',
             'if [ ! -e "$_b/store/.spack-db" ] && [ -d "$MSPACK_BOOTSTRAP_STORE/.spack-db" ]; then',
             '    mkdir -p "$_b/store" 2>/dev/null && cp -R "$MSPACK_BOOTSTRAP_STORE/.spack-db" "$_b/store/" 2>/dev/null &&',
             '    for _e in "$MSPACK_BOOTSTRAP_STORE"/*; do ln -sfn "$_e" "$_b/store/" 2>/dev/null; done',
             "fi", "unset _b _e"]
prof += ["",
         'if [ -n "${BASH_VERSION:-}${ZSH_VERSION:-}" ] && [ -f "$SPACK_ROOT/share/spack/setup-env.sh" ]; then',
         '    . "$SPACK_ROOT/share/spack/setup-env.sh"',
         "fi", ""]
overlay_write(f"/etc/profile.d/mspack-{NAME}.sh", "\n".join(prof))

# --------------------------------------------------------------------------
lock = os.path.join(envdir, "spack.lock")
spack_commit = subprocess.run(["git", "-C", SPACK_ROOT, "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
record = {
    "env": NAME,
    "env_dir": envdir,
    "view": default_view,
    "scope": SCOPE,
    "compilers": [c.format("{name}@{version}/{hash}") for c in want_comp],
    "roots": [r.format("{name}@{version}/{hash}") for r in roots],
    "specs_shipped": len(ship),
    "specs_installed": len(installed),
    "spack_lock_sha256": sha256_file(lock) if os.path.isfile(lock) else "",
    "spack_commit": spack_commit,
    "repos": repo_map,
    "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}
overlay_write(os.path.join(envdir, "mspack-image.json"), json.dumps(record, indent=2) + "\n")

# Drop duplicates and paths nested in another included path (tar would
# otherwise emit their members twice).
cands = sorted({os.path.normpath(p) for p in include if os.path.lexists(p)})
inc = []
for p in cands:
    if not any(under(p, q) for q in inc):
        inc.append(p)
plan = {"cvmfs_root": R, "include": inc, "relocate": relocate,
        "exclude": sorted(set(excludes)), "overlay": overlay_files,
        "env": runtime, "record": record}
with open(os.path.join(OUT, "plan.json"), "w", encoding="utf-8") as f:
    json.dump(plan, f, indent=2)
say(f"plan: {len(inc)} paths, {len(relocate)} relocations, "
    f"{len(overlay_files)} overlay files -> {OUT}/plan.json")
