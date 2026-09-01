#!/usr/bin/env bash
# Build the Spack GCC bootstrap ladder, then register each rung.
#
#   base image gcc 8.5  ->  GCC_SPEC (e.g. gcc@14)  ->  GCC_TARGET_SPEC (gcc@15)
#
# GCC_SPEC is the toolchain that builds the rest of the stack.  GCC_TARGET_SPEC
# is the portability payload: it is built by GCC_SPEC and then proven to run in
# every VALIDATORS distribution by `multispack.sh compiler-validate`.  From the
# first rung on, nothing in the shipped tree was compiled by the distribution.
. /opt/multispack/bin/common.sh
use_spack

: "${GCC_SPEC:?}" "${GCC_LANGS:?}" "${GCC_TARGET_SPEC:?}"
: "${GCC_TARGET_VARIANTS:=}"

# Resolving a rung by "gcc@14" breaks as soon as more than one gcc@14 is known
# -- a previous run's build plus the external that `spack compiler find` records
# both match, and `spack location -i gcc@14` errors with "matches multiple
# packages".  Concretize the exact spec we want to its DAG hash and drive
# everything (install target, prefix lookup) off that hash instead: a hash is
# always unique, however many same-version installs exist.
concretize_hash() {
    spack spec --json "$@" 2>/dev/null | python3 -c '
import json, sys
d = json.load(sys.stdin)
spec = d.get("spec", d)
nodes = spec.get("nodes") or spec.get("_nodes") or []
print(nodes[0]["hash"] if nodes else "")
'
}

install_rung() {  # install_rung <full spec...> ; echoes the installed prefix
    local h
    h="$(concretize_hash "$@" || true)"
    [ -n "$h" ] || die_rung "could not concretize: $*"
    spack install -j"${SPACK_JOBS}" --no-check-signature --fail-fast "$@" >&2
    spack location -i "/$h"
}

die_rung() { echo "compiler phase: $*" >&2; exit 1; }

# --- rung 1: base gcc 8.5 builds GCC_SPEC ------------------------------------
say "installing ${GCC_SPEC} languages=${GCC_LANGS} target=${TARGET} (built by base gcc)"
GCC_PREFIX="$(install_rung ${GCC_SPEC} languages=${GCC_LANGS} target=${TARGET})"
[ -n "${GCC_PREFIX}" ] || die_rung "no prefix for ${GCC_SPEC}"
say "spack ${GCC_SPEC} at ${GCC_PREFIX}"

# Register it as a usable compiler so GCC_TARGET_SPEC can be built with %GCC_SPEC.
# (This is also what creates the duplicate external the hash lookup above copes
# with, so it must happen only after GCC_PREFIX is resolved.)
spack compiler find --scope site "${GCC_PREFIX}" 2>/dev/null \
    || spack compiler find "${GCC_PREFIX}" || true

# --- rung 2: GCC_SPEC builds GCC_TARGET_SPEC ---------------------------------
say "installing ${GCC_TARGET_SPEC} ${GCC_TARGET_VARIANTS} languages=${GCC_LANGS} target=${TARGET} %${GCC_SPEC}"
TGCC_PREFIX="$(install_rung ${GCC_TARGET_SPEC} ${GCC_TARGET_VARIANTS} languages=${GCC_LANGS} target=${TARGET} %${GCC_SPEC})"
[ -n "${TGCC_PREFIX}" ] || die_rung "no prefix for ${GCC_TARGET_SPEC}"
say "spack ${GCC_TARGET_SPEC} (target compiler) at ${TGCC_PREFIX}"

spack compiler find --scope site "${TGCC_PREFIX}" 2>/dev/null \
    || spack compiler find "${TGCC_PREFIX}" || true
spack compiler list || true

# --- deployable env for the target compiler ----------------------------------
# The compiler-validate stage sources this inside bare validator containers.  It
# deliberately does NOT set LD_LIBRARY_PATH: if gcc/cc1plus and the programs it
# builds run, the $ORIGIN (or, pre-originize, absolute /cvmfs) rpaths carry the
# load, which is the whole point of the test.
GCCENV="${CVMFS_ROOT}/env/gcc"
mkdir -p "${GCCENV}"
{
    echo "# multispack: portable GCC (${GCC_TARGET_SPEC}).  POSIX sh, source me."
    echo "GCC_PREFIX=${TGCC_PREFIX}; export GCC_PREFIX"
    echo "PATH=${TGCC_PREFIX}/bin:\${PATH}; export PATH"
    echo "# deliberately no LD_LIBRARY_PATH -- rpaths must suffice"
} > "${GCCENV}/env.sh"
say "wrote ${GCCENV}/env.sh -> ${TGCC_PREFIX}"

python3 - "$GCC_PREFIX" "$TGCC_PREFIX" <<'PY' > "${META}/compiler.detail.json"
import json, os, subprocess, sys
prefix, tprefix = sys.argv[1], sys.argv[2]
def run(*a):
    try: return subprocess.run(a, capture_output=True, text=True, timeout=120).stdout.strip()
    except Exception as e: return "ERROR: %s" % e
spack = os.path.join(os.environ["SPACK_ROOT"], "bin", "spack")
gcc  = os.path.join(prefix, "bin", "gcc")
tgcc = os.path.join(tprefix, "bin", "gcc")
print(json.dumps({
    "phase": "compiler",
    "bootstrap_compiler": run("/usr/bin/gcc", "--version").splitlines()[0] if os.path.exists("/usr/bin/gcc") else "",
    "spack_gcc_prefix": prefix,
    "spack_gcc_version": run(gcc, "-dumpfullversion") or run(gcc, "-dumpversion"),
    "spack_gcc_target": run(gcc, "-dumpmachine"),
    "target_gcc_prefix": tprefix,
    "target_gcc_spec": os.environ["GCC_TARGET_SPEC"],
    "target_gcc_variants": os.environ.get("GCC_TARGET_VARIANTS", ""),
    "target_gcc_version": run(tgcc, "-dumpfullversion") or run(tgcc, "-dumpversion"),
    "target_gcc_target": run(tgcc, "-dumpmachine"),
    "languages": os.environ.get("GCC_LANGS"),
    "spec": run(spack, "find", "--format", "{name}@{version} /{hash}", "/%s" % os.path.basename(prefix).split("-")[-1]),
    "target_spec": run(spack, "find", "--format", "{name}@{version} /{hash}", "/%s" % os.path.basename(tprefix).split("-")[-1]),
    "compilers": run(spack, "compiler", "list"),
}, indent=2))
PY
say "compiler phase complete"
