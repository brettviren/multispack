#!/usr/bin/env bash
# Rewrite absolute RPATHs into store-relative $ORIGIN form.  After this the
# whole install tree relocates as a unit with NO install-time rewriting.
. /opt/multispack/bin/common.sh

TREE="${CVMFS_ROOT}/opt"
EXTRA=""
[ "${ORIGINIZE_RUNPATH:-0}" = "1" ] && EXTRA="--runpath"

# Prefer the user's own spaxi if it is installed; fall back to the bundled
# rewriter, which implements the same shrink-only, in-place strategy.
if command -v spaxi >/dev/null 2>&1; then
    say "using spaxi for rpath rewriting"
    spaxi relocate --origin-rpaths "${TREE}" || true
fi

say "originizing ${TREE}"
python3 "${MSBIN}/originize.py" --root "${TREE}" ${EXTRA} \
    --json "${META}/originize.detail.json"
