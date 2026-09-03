#!/usr/bin/env bash
# makenv -- concretize and install an arbitrary user-supplied Spack environment
# into the shared /cvmfs install tree and binary buildcache, using the portable
# gcc@15 toolchain and site config already on the /cvmfs volume.
#
# Before the (long) install it verifies THIS container actually supplies the
# non-Spack, OS-level dependencies the environment needs, so an insufficient
# container fails in seconds instead of hours deep in a source build.  Three
# gates, cheapest first:
#   A. compile+link+run a tiny C++ program with gcc@15  -> catches missing
#      libc-dev (headers + startfiles) and a missing assembler/linker.
#   B. `spack concretize`                               -> catches unknown specs,
#      unsatisfiable constraints, an unavailable compiler, unresolvable externals.
#   C. every external in the concretized lockfile must have a real prefix here
#      -> catches a container missing e.g. the CUDA toolkit an external points at.
. /opt/multispack/bin/common.sh
use_spack

: "${MAKENV_NAME:?}"
NOCHECK="${MAKENV_NOCHECK:-0}"
MAKENV_IMAGE="${MAKENV_IMAGE:-?}"
SRC=/multispack/input/${MAKENV_YAML:-spack.yaml}
[ -f "$SRC" ] || { echo "makenv: no manifest bind-mounted at $SRC" >&2; exit 1; }

# Copy the WHOLE input dir into an env directory so relative `include:` files come
# with the manifest.  $ENVD/spack.yaml is what `spack -e` reads.
populate_env() {  # $1 = target env dir
    mkdir -p "$1"
    cp -rL /multispack/input/. "$1/" 2>/dev/null || cp "$SRC" "$1/spack.yaml"
    [ "$(basename "$SRC")" = spack.yaml ] || cp "$SRC" "$1/spack.yaml"
}

# The environment can be built two ways:
#   directory (default): an independent env dir under /cvmfs/.../env/<name>,
#     addressed by path (spack -e <path>).  Not shown by `spack env list`.
#   managed  (--managed):  a NAMED env under Spack's environments_root (also on
#     /cvmfs), shown by `spack env list` and activatable as `spack env activate
#     <name>`.  This is what a community expects for official releases.
# ENVREF is what we hand to `spack -e` (a name or a path); ENVD is the on-disk
# env directory (for reading spack.lock, writing meta).
if [ "${MAKENV_MANAGED:-0}" = 1 ]; then
    if spack env list 2>/dev/null | tr -d ' ' | grep -qx "$MAKENV_NAME"; then
        ENVD="$(spack location -e "$MAKENV_NAME")"
        say "makenv '${MAKENV_NAME}': updating existing managed env at ${ENVD}"
    else
        spack env create "$MAKENV_NAME" "$SRC" >&2
        ENVD="$(spack location -e "$MAKENV_NAME")"
        say "makenv '${MAKENV_NAME}': created managed env at ${ENVD}"
    fi
    populate_env "$ENVD"                  # keep manifest + includes in sync with input
    ENVREF="$MAKENV_NAME"
else
    ENVD="${CVMFS_ROOT}/env/${MAKENV_NAME}"
    populate_env "$ENVD"
    ENVREF="$ENVD"
fi
say "makenv '${MAKENV_NAME}' -> ${ENVD}  (image: ${MAKENV_IMAGE}, ref: ${ENVREF})"
say "spack.yaml:"; sed 's/^/    /' "${ENVD}/spack.yaml" >&2

# ---- gate A: can this container compile+link+run C++ at all? -----------------
# The most common insufficiency is a missing libc-dev (headers + crt*.o).  Probe
# with the portable gcc@15 (which ships its own as/ld); a runtime-only image
# fails here in a second rather than deep in the first package build.
if [ "$NOCHECK" != 1 ] && [ -f "${CVMFS_ROOT}/env/gcc/env.sh" ]; then
    say "gate A: this container can compile+link+run C++? (libc-dev present?)"
    GXX="$( . "${CVMFS_ROOT}/env/gcc/env.sh"; command -v g++ || true )"
    if [ -z "$GXX" ]; then
        say "  portable g++ not found via env/gcc/env.sh; skipping compile probe"
    else
        d="$(mktemp -d "${TMPDIR}/makenv-check.XXXXXX")"
        printf '#include <features.h>\n#include <string>\n#include <vector>\nint main(){std::vector<std::string> v{"o","k"};return v.size()==2?0:1;}\n' \
            > "$d/t.cpp"
        if "$GXX" -std=c++17 -O0 "$d/t.cpp" -o "$d/t" > "$d/log" 2>&1 && "$d/t"; then
            say "  compile+link+run OK ($("$GXX" -dumpfullversion 2>/dev/null || echo gcc))"
            rm -rf "$d"
        else
            echo "makenv: container '${MAKENV_IMAGE}' cannot compile+link+run a C++ program:" >&2
            sed 's/^/    /' "$d/log" >&2
            echo "makenv: it is missing OS-level build dependencies -- typically the libc dev" >&2
            echo "        package (glibc-devel / libc6-dev) and/or an assembler+linker." >&2
            echo "        Use a sufficient --image (the default builder provides glibc-dev)." >&2
            rm -rf "$d"
            exit 3
        fi
    fi
elif [ "$NOCHECK" = 1 ]; then
    say "gate A: skipped (--no-check)"
else
    say "gate A: skipped (no ${CVMFS_ROOT}/env/gcc/env.sh; run 'compiler' first for this check)"
fi

# ---- gate R: package repos referenced by the environment resolve? ------------
# A spack.yaml may declare custom package repos (`repos:`), whose paths are only
# valid if those repos are present in THIS container.  Spack merely warns and
# continues when one is missing, then fails much later with "unknown package", so
# catch it now.  Provide the repos with `makenv --repos DIR` (bound at
# $spack/../repos == ${CVMFS_ROOT}/repos).
if [ "$NOCHECK" != 1 ]; then
    say "gate R: environment package repos resolve in this container?"
    repo_out="$(spack -e "$ENVREF" repo list 2>&1)"
    if printf '%s' "$repo_out" | grep -qiE "No repo\.yaml found|Error constructing repository"; then
        echo "makenv: the environment declares package repos that are not present here:" >&2
        printf '%s\n' "$repo_out" | grep -iE "No repo\.yaml|Error constructing repository" \
            | sed 's/^/    /' | sort -u >&2
        echo "makenv: assemble those repos and pass them with --repos <dir> (mounted at" >&2
        echo "        \$spack/../repos == ${CVMFS_ROOT}/repos), or fix the spack.yaml paths." >&2
        [ -n "${MAKENV_REPOS:-}" ] \
            && echo "makenv: (--repos ${MAKENV_REPOS} was mounted; check its subdirectory layout)" >&2
        exit 7
    fi
fi

# ---- gate B: concretize ------------------------------------------------------
say "gate B: concretizing '${MAKENV_NAME}' (fast; iterate here before installing)"
if ! spack -e "$ENVREF" concretize -f; then
    echo "makenv: concretization failed (see above).  This commonly means a required" >&2
    echo "        compiler or external package is unavailable in this container." >&2
    echo "        Fix the spack.yaml, or pass a sufficient --image." >&2
    exit 4
fi
spack -e "$ENVREF" find -c > "${META}/makenv-${MAKENV_NAME}-specs.txt" 2>&1 || true

# ---- gate C: every external the plan relies on must exist here ---------------
# Externals ARE the "non-Spack dependencies" the container must supply.  Spack
# trusts a declared external during concretization; here we confirm each one's
# prefix is real, so a missing CUDA toolkit (etc.) is caught now, not mid-build.
if [ "$NOCHECK" != 1 ]; then
    say "gate C: external (non-Spack) dependency prefixes present in this container?"
    if ! python3 - "$ENVD" <<'PY'
import json, os, sys
envd = sys.argv[1]
try:
    lock = json.load(open(os.path.join(envd, "spack.lock")))
except Exception as e:
    print("makenv: cannot read spack.lock: %s" % e); sys.exit(0)
nodes = lock.get("concrete_specs", {})
def ext_path(n):
    if isinstance(n.get("external_path"), str):
        return n["external_path"]
    e = n.get("external")
    if isinstance(e, dict):
        return e.get("path")
    return None
externals, missing = [], []
for n in nodes.values():
    p = ext_path(n)
    if p:
        externals.append((n.get("name"), p))
        if not os.path.exists(p):
            missing.append((n.get("name"), p))
for name, p in sorted(externals):
    print("    %-22s %s%s" % (name, p, "" if os.path.exists(p) else "   <-- MISSING"))
if not externals:
    print("    (no path-based externals declared; builds entirely from source)")
if missing:
    print("makenv: this container is missing %d external dependency prefix(es) above." % len(missing))
    print("        Supply them in the image (a --image that installs them) or adjust the")
    print("        environment's packages: externals to match this container.")
    sys.exit(5)
PY
    then
        exit 5
    fi
fi

# ---- install -----------------------------------------------------------------
# No --fail-fast: build every independent package we can in one pass, so a run
# surfaces ALL fixable failures at once instead of stopping at the first.  The
# install tree persists on the volume, so a re-run resumes; nothing is wasted.
say "installing '${MAKENV_NAME}' with -j${SPACK_JOBS} (the long step; no --fail-fast)"
set +e
spack -e "$ENVREF" install -j"${SPACK_JOBS}" --no-check-signature
install_rc=$?
set -e

# ---- push results to the buildcache volume (even on partial failure) ---------
# Push whatever installed so partial progress is captured to the cache too, not
# just the persistent install tree.  `push` only publishes installed specs.
say "pushing '${MAKENV_NAME}' to the 'multispack' buildcache (post-install)"
spack -e "$ENVREF" buildcache push --unsigned --update-index multispack 2>/dev/null \
    || spack -e "$ENVREF" buildcache push --unsigned multispack || true

if [ "$install_rc" -ne 0 ]; then
    echo "makenv: install finished with failures (rc=${install_rc}) -- see the errors" >&2
    echo "        above.  Every independent package that COULD build did; fix the failing" >&2
    echo "        ones (a missing OS library -> --image; a glibc-2.28 quirk -> site config)" >&2
    echo "        and re-run makenv to resume (installed packages are skipped)." >&2
    exit 6
fi

# ---- detail record -----------------------------------------------------------
python3 - "$ENVD" "$MAKENV_NAME" "$MAKENV_IMAGE" > "${META}/makenv-${MAKENV_NAME}.detail.json" <<'PY'
import json, os, sys
envd, name, image = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    lock = json.load(open(os.path.join(envd, "spack.lock")))
except Exception:
    lock = {}
nodes = lock.get("concrete_specs", {})
def ext_path(n):
    if isinstance(n.get("external_path"), str):
        return n["external_path"]
    e = n.get("external")
    if isinstance(e, dict):
        return e.get("path")
    return None
externals = sorted({"%s@%s" % (n.get("name"), n.get("version", "")): ext_path(n)
                    for n in nodes.values() if ext_path(n)}.items())
print(json.dumps({
    "phase": "makenv-%s" % name,
    "name": name,
    "image": image,
    "environment": envd,
    "spack_yaml": os.path.join(envd, "spack.yaml"),
    "roots": lock.get("roots", []),
    "concrete_node_count": len(nodes),
    "externals": [{"spec": k, "prefix": v} for k, v in externals],
}, indent=2))
PY
say "makenv '${MAKENV_NAME}' complete.  Activate with: spack env activate ${ENVD}"
