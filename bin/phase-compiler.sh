#!/usr/bin/env bash
# Build the Spack GCC toolchain: a self-hosted base compiler, then everything
# else built from it.
#
#   system gcc  ->  GCC_SPEC stage-1  ->  GCC_SPEC stage-2 (self-hosted BASE)
#                                          |
#                                          +-> GCC_TARGET_SPEC (payload)
#                                          +-> EXTRA_GCC_SPECS... (requested)
#
# GCC_SPEC (e.g. gcc@12) is the 2-rung, self-hosted base: stage-1 is built by the
# system gcc and thrown away; stage-2 is built by stage-1 with its whole
# dependency closure forced onto GCC_SPEC, so it -- and everything built from it
# -- carries only gcc-runtime of GCC_SPEC, never the system gcc's.  It is the
# stack's default compiler.  GCC_TARGET_SPEC is the portability payload, proven
# to run in every VALIDATORS distribution by `compiler-validate`.  EXTRA_GCC_SPECS
# (from `mspack compiler SPEC...`) are additional compilers built from the base
# for envs that pin them; a spec older than the base is built best-effort with a
# warning.  From stage-2 on, nothing shipped was compiled by the distribution.
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

# --- extra requested compilers (from `mspack compiler SPEC...`) --------------
# Each is built FROM the GCC_SPEC base, still with the require active so its
# dependency closure stays on GCC_SPEC (no system-gcc residue).  Built while only
# stage-1 is registered so %${GCC_SPEC} is unambiguous.  A spec older than the
# base is unlikely to compile with it -- warn and try anyway (best effort); any
# failure is non-fatal (subshell + set -e) so the rest of the phase completes.
EXTRA_PREFIXES=""
BASE_MAJOR="$(echo "${GCC_SPEC#gcc@}" | cut -d. -f1)"
for xspec in ${EXTRA_GCC_SPECS:-}; do
    xmajor="$(echo "${xspec#gcc@}" | cut -d. -f1)"
    if [ -n "$xmajor" ] && [ "$xmajor" -lt "$BASE_MAJOR" ] 2>/dev/null; then
        say "warning: extra ${xspec} is older than the ${GCC_SPEC} base; building"
        say "         it with ${GCC_SPEC} is best-effort and may fail"
    fi
    say "installing extra compiler ${xspec} %${GCC_SPEC}"
    if xp="$(install_rung ${xspec} languages=${GCC_LANGS} target=${TARGET} %${GCC_SPEC})" \
       && [ -n "$xp" ]; then
        EXTRA_PREFIXES="${EXTRA_PREFIXES} ${xp}"
        say "extra compiler ${xspec} at ${xp}"
    else
        say "warning: extra compiler ${xspec} failed to build (skipped)"
    fi
done

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
for xp in ${EXTRA_PREFIXES}; do                                # register extras
    spack compiler find --scope site "${xp}" 2>/dev/null \
        || spack compiler find "${xp}" || true
done
spack compiler rm --scope site "${BOOTSTRAP_GCC}" || true     # drop system gcc

# Uninstall stage-1 and garbage-collect the now-orphaned system-gcc build residue
# (the gmp/mpfr/mpc/zlib/... it linked, and gcc-runtime of the system gcc).  The
# store is then free of every system-gcc node, so even a reuse:true env cannot
# pull one back in.
say "purging stage-1 ${GCC_SPEC} (/${S1_HASH}) and its system-gcc residue"
spack uninstall -y --force "/${S1_HASH}" \
    || say "warning: could not uninstall stage-1 ${GCC_SPEC} (/${S1_HASH})"
spack gc -y || say "note: spack gc removed nothing (or failed)"

# Safety net for later builds: prefer GCC_SPEC as the provider of each language
# virtual, so an auto-detected system gcc is never chosen by default (the store
# now holds no system-gcc node to reuse, but the base image's /usr/bin/gcc is
# still discoverable).  Set it per-virtual (c/cxx/fortran) rather than as a bare
# `packages:all:prefer:%gcc@14` -- Spack warns the latter is a blanket dependency
# constraint that "can lead to unexpected concretizations" (e.g. fighting an env
# that pins a different compiler like largroups' gcc@12).
for _lang in c cxx fortran; do
    spack config --scope site add "packages:${_lang}:prefer:[\"${GCC_SPEC}\"]" \
        || say "warning: could not prefer ${GCC_SPEC} for language ${_lang}"
done

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
