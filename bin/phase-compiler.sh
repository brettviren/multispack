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

hash_of() { basename "$1" | sed 's/.*-//'; }   # <name>-<ver>-<hash> -> <hash>

# The bootstrap compiler is the base image's system gcc (e.g. gcc@8.5.0 on
# AlmaLinux 8, glibc 2.28).  Capture it now: once our own toolchain is
# self-hosted, nothing shipped may be built by it, so it gets un-registered.
BOOTSTRAP_GCC="gcc@$(/usr/bin/gcc -dumpfullversion 2>/dev/null || /usr/bin/gcc -dumpversion 2>/dev/null)"
say "bootstrap compiler: ${BOOTSTRAP_GCC}"

# --- rung 1: base gcc builds STAGE-1 of GCC_SPEC -----------------------------
# Stage-1 is built BY the system gcc, so it -- and the gmp/mpfr/mpc/zlib it links
# at RUNTIME -- carry gcc-runtime of the OLD system gcc.  That residue is exactly
# what pollutes a reused closure (gcc-runtime@8.5.0), so stage-1 is a throwaway:
# used only to build a clean stage-2, then uninstalled below.
say "installing ${GCC_SPEC} (stage 1, built by ${BOOTSTRAP_GCC})"
S1_PREFIX="$(install_rung ${GCC_SPEC} languages=${GCC_LANGS} target=${TARGET})"
[ -n "${S1_PREFIX}" ] || die_rung "no prefix for stage-1 ${GCC_SPEC}"
S1_HASH="$(hash_of "${S1_PREFIX}")"
say "stage-1 ${GCC_SPEC} at ${S1_PREFIX} (/${S1_HASH})"
spack compiler find --scope site "${S1_PREFIX}" 2>/dev/null \
    || spack compiler find "${S1_PREFIX}" || true

# From here every build must use OUR gcc, so its whole dependency closure
# (gmp, mpfr, mpc, zlib, ...) is rebuilt with GCC_SPEC instead of reusing the
# system-gcc copies stage-1 dragged in.  A hard `require` does that; it is added
# only now (stage-1 itself needed the system gcc) and dropped once the toolchain
# is built.  Verified: with it, the self-hosted gcc's closure is gcc-runtime of
# GCC_SPEC alone -- zero system-gcc nodes.
spack config --scope site add "packages:all:require:[\"%${GCC_SPEC}\"]" \
    || die_rung "could not require %${GCC_SPEC}"

# --- rung 2: STAGE-1 builds STAGE-2 of GCC_SPEC (self-hosted, clean) ----------
# Built by stage-1 with every dependency forced onto GCC_SPEC, so its runtime
# closure is gcc-runtime of GCC_SPEC alone.  This is the compiler the stack and
# every env use, and it has no system-gcc residue.
say "installing ${GCC_SPEC} (stage 2, self-hosted by stage-1)"
GCC_PREFIX="$(install_rung ${GCC_SPEC} languages=${GCC_LANGS} target=${TARGET} %${GCC_SPEC})"
[ -n "${GCC_PREFIX}" ] || die_rung "no prefix for stage-2 ${GCC_SPEC}"
GCC_HASH="$(hash_of "${GCC_PREFIX}")"
[ "${GCC_HASH}" != "${S1_HASH}" ] || die_rung "stage-2 ${GCC_SPEC} did not differ from stage-1"
GCC_FULL="gcc@$("${GCC_PREFIX}/bin/gcc" -dumpfullversion 2>/dev/null || echo "${GCC_SPEC#gcc@}")"
say "stage-2 ${GCC_SPEC} (${GCC_FULL}) at ${GCC_PREFIX} (/${GCC_HASH})"

# --- rung 3: STAGE-1 builds GCC_TARGET_SPEC (clean via the require) -----------
# Still built with the single (stage-1) %${GCC_SPEC} compiler -- registering
# stage-2 now would make %${GCC_SPEC} match two gccs -- while the require keeps
# its dependency closure free of system-gcc.
say "installing ${GCC_TARGET_SPEC} ${GCC_TARGET_VARIANTS} %${GCC_SPEC}"
TGCC_PREFIX="$(install_rung ${GCC_TARGET_SPEC} ${GCC_TARGET_VARIANTS} languages=${GCC_LANGS} target=${TARGET} %${GCC_SPEC})"
[ -n "${TGCC_PREFIX}" ] || die_rung "no prefix for ${GCC_TARGET_SPEC}"
say "spack ${GCC_TARGET_SPEC} (target compiler) at ${TGCC_PREFIX}"

# The toolchain is built; drop the global require so later phases/envs may still
# choose another compiler when they explicitly need one (e.g. a CUDA host gcc).
spack config --scope site rm "packages:all:require" || true

# --- seal the toolchain: stage-2 becomes the only GCC_SPEC, stage-1 is purged -
# Swap the registered GCC_SPEC compiler from the polluted stage-1 to the clean
# stage-2, register GCC_TARGET_SPEC, and un-register the system gcc so nothing
# later silently builds with it.
say "sealing toolchain: ${GCC_SPEC} stage-1 -> stage-2; dropping ${BOOTSTRAP_GCC}"
spack compiler rm --scope site "${GCC_FULL}" 2>/dev/null \
    || spack compiler rm --scope site "${GCC_SPEC}" || true   # remove stage-1 (sole entry)
spack compiler find --scope site "${GCC_PREFIX}" 2>/dev/null \
    || spack compiler find "${GCC_PREFIX}" || true            # register stage-2
spack compiler find --scope site "${TGCC_PREFIX}" 2>/dev/null \
    || spack compiler find "${TGCC_PREFIX}" || true           # register GCC_TARGET_SPEC
spack compiler rm --scope site "${BOOTSTRAP_GCC}" || true     # drop system gcc

# Uninstall stage-1 and garbage-collect the now-orphaned system-gcc build residue
# (the gmp/mpfr/mpc/zlib/... it linked, and gcc-runtime of the system gcc).  The
# store is then free of every system-gcc node, so even a reuse:true env cannot
# pull one back in.
say "purging stage-1 ${GCC_SPEC} (/${S1_HASH}) and its system-gcc residue"
spack uninstall -y --force "/${S1_HASH}" \
    || say "warning: could not uninstall stage-1 ${GCC_SPEC} (/${S1_HASH})"
spack gc -y || say "note: spack gc removed nothing (or failed)"

# Safety net for later builds: prefer GCC_SPEC so an auto-detected system gcc is
# never chosen by default (the store now holds no system-gcc node to reuse, but
# the base image's /usr/bin/gcc is still discoverable).
spack config --scope site add "packages:all:prefer:[\"%${GCC_SPEC}\"]" || true

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
