#!/usr/bin/env bash
# Push everything installed to the binary cache volume.  Do this AFTER
# originize so the cached tarballs already carry $ORIGIN rpaths.
. /opt/multispack/bin/common.sh
use_spack

say "pushing the environment to the 'multispack' mirror"
spack -e "${ENVDIR}" buildcache push --unsigned --update-index multispack || \
spack -e "${ENVDIR}" buildcache push --unsigned multispack

# The toolchain lives outside the environment; push it too.
say "pushing everything else in the install tree"
HASHES="$(spack find --format '/{hash}' 2>/dev/null | tr '\n' ' ')"
if [ -n "${HASHES}" ]; then
    # shellcheck disable=SC2086
    spack buildcache push --unsigned --update-index multispack ${HASHES} || true
fi
spack buildcache update-index multispack || true

python3 - <<'PY' > "${META}/buildcache.detail.json"
import json, os
cache = "/multispack/cache/buildcache"
total = files = specs = 0
for d, _dirs, fs in os.walk(cache):
    for f in fs:
        p = os.path.join(d, f)
        if os.path.islink(p):
            continue
        try:
            total += os.path.getsize(p); files += 1
        except OSError:
            pass
        if f.endswith((".spec.json", ".spec.json.sig")):
            specs += 1
print(json.dumps({
    "phase": "buildcache",
    "mirror": cache,
    "files": files,
    "spec_entries": specs,
    "bytes": total,
    "gib": round(total / 2**30, 2),
    "signed": False,
}, indent=2))
PY
