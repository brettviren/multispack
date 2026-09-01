#!/usr/bin/env bash
#
# pthread / glibc-2.34 symbol-version demonstration.
#
# Builds the same tiny pthread program (test.c) against AlmaLinux 8, 9 and 10
# (glibc 2.28, 2.34, 2.39), records which versioned pthread symbols each binary
# requires, then runs every binary under every distro's glibc.  The resulting
# matrix shows that:
#   * a binary built on the OLDEST glibc (AL8) runs everywhere;
#   * binaries built on AL9/AL10 (glibc >= 2.34) FAIL on AL8 -- the real cliff is
#     newer->older, the OPPOSITE direction from the criticism this rebuts.
# See README.md for the full writeup.
#
# Requirements: podman (or set ENGINE=docker) and network access to pull the
# almalinux:{8,9,10} base images.  Honors CONTAINER_HOST for a remote engine.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE="${ENGINE:-podman}"
IMG="${IMG_PREFIX:-localhost/multispack-pthread}"

ORDER=(al8 al9 al10)
declare -A BASE=(
    [al8]="docker.io/library/almalinux:8"
    [al9]="docker.io/library/almalinux:9"
    [al10]="docker.io/library/almalinux:10"
)

msg()  { printf '\033[1;34m[pthread-test]\033[0m %s\n' "$*"; }
line() { printf -- '---------------------------------------------------------------\n'; }

# True if glibc version $1 >= $2 (so a binary needing $2 runs on a host with $1).
ge_glibc() { [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -1)" = "$1" ]; }

# ---- build one image per distro (compiles test.c against that distro's glibc) -
for d in "${ORDER[@]}"; do
    msg "building ${IMG}-${d}  (FROM ${BASE[$d]})"
    "$ENGINE" build --build-arg "BASE=${BASE[$d]}" \
        -t "${IMG}-${d}" -f "$HERE/Containerfile.${d}" "$HERE" >/dev/null
done

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

declare -A GLIBC
for d in "${ORDER[@]}"; do
    GLIBC[$d]="$("$ENGINE" run --rm "${BASE[$d]}" \
        /bin/sh -c 'ldd --version 2>/dev/null | head -1 | grep -oE "[0-9]+\.[0-9]+$"')"
    cid="$("$ENGINE" create "${IMG}-${d}")"
    "$ENGINE" cp "${cid}:/pthread-test" "$WORK/${d}.bin"
    "$ENGINE" rm "$cid" >/dev/null
done

echo; line
msg "glibc per distro:"
for d in "${ORDER[@]}"; do printf '    %-4s : glibc %s\n' "$d" "${GLIBC[$d]}"; done

echo; line
msg "pthread symbol versions REQUIRED by each binary (readelf --dyn-syms):"
for d in "${ORDER[@]}"; do
    syms="$("$ENGINE" run --rm "${IMG}-${d}" /bin/sh -c \
        'readelf -W --dyn-syms /pthread-test' 2>/dev/null \
        | grep -oE 'pthread_[a-z_]+@GLIBC_[0-9.]+' | sort -u | paste -sd' ' -)"
    printf '    built on %-4s (glibc %-5s): %s\n' "$d" "${GLIBC[$d]}" "$syms"
done

# ---- cross-run matrix --------------------------------------------------------
echo; line
msg "cross-run matrix -- can the binary built on <row> run under <col>'s glibc?"
printf '    %-18s' "built-on \\ run-on"
for r in "${ORDER[@]}"; do printf '%-16s' "${r}(${GLIBC[$r]})"; done; echo

mismatch=0
for b in "${ORDER[@]}"; do
    printf '    %-18s' "$b (${GLIBC[$b]})"
    for r in "${ORDER[@]}"; do
        out="$("$ENGINE" run --rm -v "$WORK:/w:ro" "${BASE[$r]}" "/w/${b}.bin" 2>&1 || true)"
        if printf '%s' "$out" | grep -q PTHREAD_OK; then
            actual=RUN; cell=RUN
        elif printf '%s' "$out" | grep -qiE 'GLIBC_[0-9.]+.*not found'; then
            actual=FAIL; cell="FAIL($(printf '%s' "$out" | grep -oE 'GLIBC_2\.[0-9]+' | head -1))"
        else
            actual=ERR; cell=ERR
        fi
        # Expectation from the forward-compatibility rule: runs iff host glibc >= build glibc.
        if ge_glibc "${GLIBC[$r]}" "${GLIBC[$b]}"; then expect=RUN; else expect=FAIL; fi
        [ "$actual" = "$expect" ] || { cell="${cell}!"; mismatch=1; }
        printf '%-16s' "$cell"
    done
    echo
done

echo; line
if [ "$mismatch" -eq 0 ]; then
    msg "RESULT: every cell matches the forward-compatibility rule"
    msg "  (a binary runs iff host_glibc >= build_glibc).  Conclusions upheld:"
    echo "    * Build on the OLDEST glibc (AL8/2.28): the binary runs on AL8, AL9, AL10."
    echo "    * Build on AL9/AL10 (glibc >= 2.34): FAILS on AL8 -- the cliff is"
    echo "      newer->older, NOT AL9->AL10 (which works).  See README.md."
    exit 0
else
    msg "RESULT: one or more cells (marked '!') did not match the expected rule."
    msg "  Investigate before trusting the conclusions; see README.md."
    exit 1
fi
