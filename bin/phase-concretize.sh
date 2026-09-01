#!/usr/bin/env bash
# Create the ROOT environment and concretize it.  This is the FAST GATE: run it
# before committing to a multi-hour build, and iterate on ROOT_VARIANTS here.
. /opt/multispack/bin/common.sh
use_spack

: "${ROOT_PKG:?}" "${CXXSTD_LIST:?}" "${GCC_SPEC:?}"

SPECS=""
for cx in ${CXXSTD_LIST}; do
    SPECS="${SPECS}  - ${ROOT_PKG} ${ROOT_VARIANTS} cxxstd=${cx} %${GCC_SPEC}
"
done
export SPECS

mkdir -p "${ENVDIR}"
render "${MSCFG}/env-root.yaml.in" "${ENVDIR}/spack.yaml"
say "environment manifest:"; cat "${ENVDIR}/spack.yaml"

say "concretizing (this can take a few minutes)"
spack -e "${ENVDIR}" concretize -f

spack -e "${ENVDIR}" find -c > "${META}/concretize-specs.txt" 2>&1 || true

python3 - <<'PY' > "${META}/concretize.detail.json"
# Read spack.lock and report how much the C++17 and C++23 ROOTs actually share.
import json, os, collections

envdir = os.environ["ENVDIR"]
lock = json.load(open(os.path.join(envdir, "spack.lock")))
nodes = lock.get("concrete_specs", {})

def field(n, k, default=""):
    return n.get(k, default)

roots = []
for h, n in nodes.items():
    if field(n, "name") == os.environ.get("ROOT_PKG", "root"):
        roots.append((h, n))

# Build the dependency closure of each root from the lock's edge lists.
def closure(h, seen=None):
    seen = seen if seen is not None else set()
    if h in seen or h not in nodes:
        return seen
    seen.add(h)
    for dep in nodes[h].get("dependencies", []) or []:
        dh = dep.get("hash") if isinstance(dep, dict) else None
        if dh:
            closure(dh, seen)
    return seen

closures = {}
for h, n in roots:
    cx = (n.get("parameters", {}) or {}).get("cxxstd")
    if isinstance(cx, list):
        cx = cx[0] if cx else "?"
    closures["cxxstd=%s" % cx] = closure(h)

shared = set.intersection(*closures.values()) if len(closures) > 1 else set()
allnodes = set.union(*closures.values()) if closures else set()

names = lambda hs: sorted({nodes[x]["name"] for x in hs if x in nodes})

print(json.dumps({
    "phase": "concretize",
    "environment": envdir,
    "specs": lock.get("roots", []),
    "total_nodes": len(allnodes),
    "closure_sizes": {k: len(v) for k, v in closures.items()},
    "shared_nodes": len(shared),
    "shared_fraction": (round(len(shared) / len(allnodes), 4) if allnodes else 0),
    "unshared_by_flavour": {
        k: names(v - shared) for k, v in closures.items()
    },
    "shared_packages": names(shared),
}, indent=2))
PY

say "concretize complete; see ${META}/concretize.detail.json for dependency sharing"
