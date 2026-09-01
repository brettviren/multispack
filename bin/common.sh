# Sourced by every in-container phase script.  Not executable on its own.
set -euo pipefail

: "${CVMFS_ROOT:?CVMFS_ROOT must be set by multispack.sh}"

export SPACK_ROOT="${CVMFS_ROOT}/spack"
export SPACK_DISABLE_LOCAL_CONFIG=1          # ignore ~/.spack and /etc/spack
export SPACK_USER_CACHE_PATH=/multispack/work/spack-user-cache
export TMPDIR=/multispack/work/tmp

export ENVDIR="${CVMFS_ROOT}/env/root"
export META=/multispack/meta
export MSBIN=/opt/multispack/bin
export MSCFG=/opt/multispack/config
export MSTESTS=/opt/multispack/tests

: "${SPACK_JOBS:=1}"
# The microarch is passed in as MULTISPACK_TARGET, NOT TARGET: `TARGET` is one of
# the most common Makefile variable names (ICU's stubdata Makefile uses $(TARGET)
# as a build target, autotools projects use it, ...), so exporting a bare TARGET
# into the build environment breaks those packages.  We keep TARGET as a LOCAL,
# UNEXPORTED shell variable for building spec strings; child build processes never
# see it.  Renders that need it in the environment get MULTISPACK_TARGET (which is
# a bespoke name that collides with nothing).
: "${MULTISPACK_TARGET:=x86_64_v3}"
TARGET="${MULTISPACK_TARGET}"          # deliberately not `export`ed
: "${BASE_CXXSTD:=17}"
: "${PADDED_LENGTH:=128}"

mkdir -p "$TMPDIR" "$SPACK_USER_CACHE_PATH" "$META" \
         /multispack/work/{stage,misc,test} \
         /multispack/cache/{source,buildcache}

say() { printf '\033[1;32m  ..\033[0m %s\n' "$*" >&2; }

use_spack() {
    [ -f "${SPACK_ROOT}/share/spack/setup-env.sh" ] \
        || { echo "no Spack at ${SPACK_ROOT}; run 'multispack.sh bootstrap' first" >&2; exit 1; }
    # shellcheck disable=SC1091
    . "${SPACK_ROOT}/share/spack/setup-env.sh"
}

render() { python3 "${MSBIN}/render.py" "$1" "$2"; }
