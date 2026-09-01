#!/usr/bin/env bash
# Write the per-flavour env.sh files and a deployment manifest under /cvmfs.
#
# Note what env.sh does NOT set: LD_LIBRARY_PATH.  If ROOT starts and finds its
# libraries, that is the $ORIGIN rpaths doing the work, which is the property
# the validation stage is there to prove.
. /opt/multispack/bin/common.sh
use_spack

: "${CXXSTD_LIST:?}"
mkdir -p "${CVMFS_ROOT}/env" "${CVMFS_ROOT}/meta"

PYPREFIX="$(spack -e "${ENVDIR}" find --format '{prefix}' python 2>/dev/null | head -1 || true)"

for cx in ${CXXSTD_LIST}; do
    PREFIX="$(spack -e "${ENVDIR}" find --format '{prefix}' \
                "${ROOT_PKG} cxxstd=${cx}" 2>/dev/null | head -1)"
    [ -n "${PREFIX}" ] || { echo "no installed ${ROOT_PKG} cxxstd=${cx}" >&2; exit 1; }
    d="${CVMFS_ROOT}/env/root-cxx${cx}"
    mkdir -p "$d"
    {
        echo "# multispack: ROOT built for C++${cx}.  POSIX sh, source me."
        echo "MULTISPACK_CXXSTD=${cx}; export MULTISPACK_CXXSTD"
        echo "ROOTSYS=${PREFIX}; export ROOTSYS"
        printf 'PATH=%s/bin' "${PREFIX}"
        [ -n "${PYPREFIX}" ] && printf ':%s/bin' "${PYPREFIX}"
        printf ':${PATH}; export PATH\n'
        echo "PYTHONPATH=${PREFIX}/lib\${PYTHONPATH:+:\$PYTHONPATH}; export PYTHONPATH"
        echo "# deliberately no LD_LIBRARY_PATH -- \$ORIGIN rpaths must suffice"
    } > "$d/env.sh"
    say "wrote $d/env.sh -> ${PREFIX}"
done

python3 - <<'PY' > "${CVMFS_ROOT}/meta/manifest.json"
import json, os, subprocess
spack = os.path.join(os.environ["SPACK_ROOT"], "bin", "spack")
envdir, cvmfs = os.environ["ENVDIR"], os.environ["CVMFS_ROOT"]
def run(*a):
    try: return subprocess.run(a, capture_output=True, text=True, timeout=600).stdout.strip()
    except Exception as e: return "ERROR: %s" % e
flavours = {}
for cx in os.environ["CXXSTD_LIST"].split():
    p = run(spack, "-e", envdir, "find", "--format", "{prefix}",
            "%s cxxstd=%s" % (os.environ.get("ROOT_PKG", "root"), cx)).splitlines()
    flavours["cxx%s" % cx] = {
        "prefix": p[0].strip() if p else None,
        "env_sh": os.path.join(cvmfs, "env", "root-cxx%s" % cx, "env.sh"),
    }
print(json.dumps({
    "repository": os.environ["CVMFS_HOST"],
    "root": cvmfs,
    "install_tree": os.path.join(cvmfs, "opt"),
    "spack_root": os.environ["SPACK_ROOT"],
    "spack_ref": os.environ.get("SPACK_REF"),
    "target": os.environ.get("TARGET"),
    "base_cxxstd": os.environ.get("BASE_CXXSTD"),
    "builder_base_image": os.environ.get("BUILDER_BASE"),
    "flavours": flavours,
}, indent=2))
PY
cp "${CVMFS_ROOT}/meta/manifest.json" "${META}/deploy.detail.json"
say "manifest at ${CVMFS_ROOT}/meta/manifest.json"
