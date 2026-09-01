#!/usr/bin/env bash
# Clone Spack onto the /cvmfs volume, install the site configuration, and get
# the concretizer working.  Idempotent.
. /opt/multispack/bin/common.sh

: "${SPACK_GIT:?}" "${SPACK_REF:?}"

if [ ! -d "${SPACK_ROOT}/.git" ]; then
    say "cloning ${SPACK_GIT} @ ${SPACK_REF} -> ${SPACK_ROOT}"
    mkdir -p "$(dirname "$SPACK_ROOT")"
    git clone --depth 1 --branch "${SPACK_REF}" "${SPACK_GIT}" "${SPACK_ROOT}"
else
    say "Spack already present at ${SPACK_ROOT}"
fi

use_spack
say "spack version: $(spack --version)"

# ---- site configuration -----------------------------------------------------
mkdir -p "${SPACK_ROOT}/etc/spack"
export BUILD_JOBS="${SPACK_JOBS}"
for f in config packages concretizer mirrors; do
    render "${MSCFG}/${f}.yaml.in" "${SPACK_ROOT}/etc/spack/${f}.yaml"
done

# ---- package recipes --------------------------------------------------------
# Spack >= 1.0 keeps the builtin package recipes in a separate repository.
# Depending on the release it is vendored, fetched on demand, or must be added
# by hand.  Cope with all three.
if ! spack list root 2>/dev/null | grep -qx root; then
    say "builtin package repo does not provide 'root'; trying to attach spack-packages"
    PKGS=/multispack/work/spack-packages
    if [ ! -d "${PKGS}/.git" ]; then
        git clone --depth 1 --branch "${SPACK_PACKAGES_REF:-develop}" \
            "${SPACK_PACKAGES_GIT}" "${PKGS}" \
            || git clone --depth 1 "${SPACK_PACKAGES_GIT}" "${PKGS}"
    fi
    for cand in "${PKGS}/repos/spack_repo/builtin" "${PKGS}/repos/builtin" "${PKGS}"; do
        if [ -f "${cand}/repo.yaml" ]; then
            say "spack repo add ${cand}"
            spack repo add --scope site "${cand}" || spack repo add "${cand}"
            break
        fi
    done
fi
spack list root 2>/dev/null | grep -qx root \
    || { echo "FATAL: Spack cannot see the 'root' package.  See PLAN.md, Risk 1." >&2; exit 1; }

# ---- buildcache mirror index ------------------------------------------------
# A brand-new mirror has no index, and Spack warns "the mirror ... cannot be used
# in concretization (no index found)" on every concretization until the first
# push.  This is HARMLESS -- there are no cached binaries to reuse yet -- and it
# CANNOT be pre-fixed: `spack buildcache update-index` errors on an empty mirror
# ("Failed to get list of entries").  The index appears automatically on the
# first `buildcache` phase (which pushes with --update-index), after which the
# warning disappears and cached binaries are reused.  So: nothing to do here.
say "note: the 'no index found' buildcache warning is expected until the first"
say "      'buildcache' push; it is harmless (empty cache = nothing to reuse)."

# ---- concretizer ------------------------------------------------------------
say "bootstrapping clingo"
spack bootstrap now

# ---- bootstrap compiler -----------------------------------------------------
# The base image's system gcc.  It is used for exactly one thing: building the
# Spack gcc.  Nothing it produces is shipped except that compiler.
spack compiler find --scope site 2>/dev/null || spack compiler find || true
spack compiler list || true

# ---- detail record ----------------------------------------------------------
python3 - <<'PY' > "${META}/bootstrap.detail.json"
import json, os, subprocess, platform

def run(*a):
    try:
        return subprocess.run(a, capture_output=True, text=True, timeout=300).stdout.strip()
    except Exception as e:
        return "ERROR: %s" % e

spack = os.path.join(os.environ["SPACK_ROOT"], "bin", "spack")
libc = ""
try:
    libc = subprocess.run(["ldd", "--version"], capture_output=True, text=True).stdout.splitlines()[0]
except Exception:
    pass

print(json.dumps({
    "phase": "bootstrap",
    "spack_root": os.environ["SPACK_ROOT"],
    "spack_version": run(spack, "--version"),
    "spack_ref": os.environ.get("SPACK_REF"),
    "spack_commit": run("git", "-C", os.environ["SPACK_ROOT"], "rev-parse", "HEAD"),
    "build_host_kernel": platform.release(),
    "build_host_libc": libc,
    "repos": run(spack, "repo", "list"),
    "compilers": run(spack, "compiler", "list"),
    "site_config_dir": os.path.join(os.environ["SPACK_ROOT"], "etc", "spack"),
}, indent=2))
PY
say "bootstrap complete"
