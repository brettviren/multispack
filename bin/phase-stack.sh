#!/usr/bin/env bash
# Install every ROOT flavour.  Long.  Everything lands in the buildcache after
# `multispack.sh buildcache`, so this only costs full price once.
. /opt/multispack/bin/common.sh
use_spack

[ -f "${ENVDIR}/spack.lock" ] || { echo "run 'multispack.sh concretize' first" >&2; exit 1; }

say "installing with -j${SPACK_JOBS}"
spack -e "${ENVDIR}" install -j"${SPACK_JOBS}" --no-check-signature --fail-fast

spack -e "${ENVDIR}" find -lv > "${META}/stack-installed.txt" 2>&1 || true

python3 - <<'PY' > "${META}/stack.detail.json"
import json, os, subprocess
spack = os.path.join(os.environ["SPACK_ROOT"], "bin", "spack")
envdir = os.environ["ENVDIR"]
def run(*a):
    try: return subprocess.run(a, capture_output=True, text=True, timeout=600).stdout
    except Exception as e: return "ERROR: %s" % e
listing = run(spack, "-e", envdir, "find", "--format", "{name}@{version} {/hash} {prefix}")
lines = [l for l in listing.splitlines() if l.strip()]
root_prefixes = {}
for cx in os.environ.get("CXXSTD_LIST", "").split():
    out = run(spack, "-e", envdir, "find", "--format", "{prefix}",
              "%s cxxstd=%s" % (os.environ.get("ROOT_PKG", "root"), cx)).strip()
    if out:
        root_prefixes["cxxstd=%s" % cx] = out.splitlines()[0].strip()
tree = os.path.join(os.environ["CVMFS_ROOT"], "opt")
total = files = 0
for d, _, fs in os.walk(tree):
    for f in fs:
        p = os.path.join(d, f)
        if not os.path.islink(p):
            try: total += os.path.getsize(p); files += 1
            except OSError: pass
print(json.dumps({
    "phase": "stack",
    "installed_count": len(lines),
    "root_prefixes": root_prefixes,
    "install_tree": tree,
    "install_tree_files": files,
    "install_tree_bytes": total,
    "install_tree_gib": round(total / 2**30, 2),
}, indent=2))
PY
say "stack complete"
