#!/usr/bin/env bash
# Turn an `mspack image` plan (/out/plan.json from image-plan.py) into ONE
# uncompressed tar layer, /out/payload.tar.  Runs in the builder with the store
# volumes mounted at their build paths (read-only is enough).
#
# Every member is owned by root:root (numeric), paths are relative to /, recipe
# repos cloned on the work volume are renamed into $CVMFS_ROOT/repos/<name> on
# the fly (tar --transform), and the overlay files (pruned DB, runtime site
# config, profile.d script, ...) are appended after the excludes removed the
# originals -- so no member appears twice.
set -euo pipefail
OUT="${IMG_OUT:-/out}"
PLAN="$OUT/plan.json"
[ -f "$PLAN" ] || { echo "image-payload: no plan at $PLAN" >&2; exit 2; }

python3 - "$PLAN" "$OUT" <<'PY'
import json, sys
plan = json.load(open(sys.argv[1]))
out = sys.argv[2]
rel = lambda p: p.lstrip("/")
def bre(s):   # escape for a sed BRE with ',' as the delimiter
    return "".join("\\" + ch if ch in ".[]*^$\\," else ch for ch in s)
with open(f"{out}/include.txt", "w") as f:
    for p in plan["include"]:
        f.write(rel(p) + "\n")
    for src, _ in plan["relocate"]:
        f.write(rel(src) + "\n")
with open(f"{out}/exclude.txt", "w") as f:
    for p in plan["exclude"]:
        f.write(p + "\n")
with open(f"{out}/transform.txt", "w") as f:
    # one sed expression per relocation; S = leave symlink targets alone
    for src, dst in plan["relocate"]:
        f.write("s,^%s\\(/\\|$\\),%s\\1,S\n" % (bre(rel(src)), rel(dst).replace(",", "\\,")))
with open(f"{out}/overlay.txt", "w") as f:
    for p in plan["overlay"]:
        f.write(p + "\n")
PY

xform=()
while IFS= read -r t; do xform+=("--transform=$t"); done < "$OUT/transform.txt"

rm -f "$OUT/payload.tar"
echo "[image-payload] archiving $(wc -l < "$OUT/include.txt") paths" >&2
tar -C / -cf "$OUT/payload.tar" --numeric-owner --owner=0 --group=0 \
    --exclude-from="$OUT/exclude.txt" "${xform[@]}" -T "$OUT/include.txt"

# Overlay files plus a world-writable tutorial work dir (/work, mode 1777: a
# bind target for podman -v / apptainer --bind, writable even when not bound).
mkdir -p "$OUT/overlay/work"
chmod 1777 "$OUT/overlay/work"
echo work >> "$OUT/overlay.txt"
tar -C "$OUT/overlay" -rf "$OUT/payload.tar" --numeric-owner --owner=0 --group=0 \
    --no-recursion -T "$OUT/overlay.txt"
ls -l "$OUT/payload.tar" >&2
