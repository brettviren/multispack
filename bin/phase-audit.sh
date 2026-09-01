#!/usr/bin/env bash
# Portability audit of the install tree: glibc floor, unresolved DT_NEEDED,
# rpath composition.  This is the artifact to show people who ask "does it
# really only need libc?".
. /opt/multispack/bin/common.sh
python3 "${MSBIN}/elfaudit.py" --root "${CVMFS_ROOT}/opt" \
    --json "${META}/audit.detail.json" | tee "${META}/audit-summary.txt"
