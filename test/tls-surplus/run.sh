#!/usr/bin/env bash
#
# static-TLS-surplus demonstration.
#
# TLS here = Thread-Local Storage (__thread / thread_local), NOT SSL/TLS.
#
# A plugin host that dlopen()s many libraries built with the INITIAL-EXEC TLS
# model exhausts glibc's small fixed "static TLS surplus" and dlopen() starts
# failing with "cannot allocate memory in static TLS block".  How many fit is a
# property of the RUNTIME glibc, so the same workload can succeed on one distro
# and fail on another (a real "works on AL9, fails on AL10" mechanism that the
# pthread symbol-version story does NOT cover).
#
# This builds a probe + N libraries in each TLS model once (on AL8, so they run
# everywhere), then runs the probe under AL8/AL9/AL10 glibc and shows:
#   * INITIAL-EXEC libs: dlopen fails after a small, glibc-version-dependent count;
#   * GLOBAL-DYNAMIC libs (the mitigation): all N dlopen on every distro.
#
# Requirements: podman (or ENGINE=docker) + network to pull almalinux:{8,9,10}.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE="${ENGINE:-podman}"
N="${N:-32}"
# Per-lib initial-exec TLS.  >= ~2 KB makes a single dlopen exceed what glibc will
# grow for a module once another thread exists, so the first initial-exec dlopen
# fails outright -- the clearest form of the phenomenon.  (At <= ~1 KB glibc grows
# the block and they all load; try TLS_BYTES=1024 to see that regime.)
TLS_BYTES="${TLS_BYTES:-4096}"

ORDER=(al8 al9 al10)
declare -A BASE=(
    [al8]="docker.io/library/almalinux:8"
    [al9]="docker.io/library/almalinux:9"
    [al10]="docker.io/library/almalinux:10"
)

msg()  { printf '\033[1;34m[tls-surplus]\033[0m %s\n' "$*"; }
line() { printf -- '---------------------------------------------------------------\n'; }

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

# ---- compile the probe + libs once, on AL8 (portable to AL8/9/10) ------------
# Done in an ephemeral container (no image commit) so it needs no build scratch.
msg "compiling probe + ${N} ie-lib + ${N} gd-lib (TLS_BYTES=${TLS_BYTES}) on AL8"
"$ENGINE" run --rm \
    -v "$HERE:/src:ro" -v "$WORK:/out" \
    "${BASE[al8]}" /bin/sh -c \
    "dnf -y install gcc glibc-devel >/dev/null 2>&1 && sh /src/build.sh /src /out ${N} ${TLS_BYTES}"

# ---- run the probe under each distro's glibc ---------------------------------
declare -A GLIBC IE GD
for d in "${ORDER[@]}"; do
    GLIBC[$d]="$("$ENGINE" run --rm "${BASE[$d]}" \
        /bin/sh -c 'ldd --version 2>/dev/null | head -1 | grep -oE "[0-9]+\.[0-9]+$"')"
    IE[$d]="$("$ENGINE" run --rm -v "$WORK:/out:ro" "${BASE[$d]}" \
        /out/probe /out ie-lib "$N" 2>>"$WORK/ie-$d.err" | sed -n 's/^OPENED //p')"
    GD[$d]="$("$ENGINE" run --rm -v "$WORK:/out:ro" "${BASE[$d]}" \
        /out/probe /out gd-lib "$N" 2>/dev/null | sed -n 's/^OPENED //p')"
done

echo; line
msg "how many dlopen()s succeed before failure  (max attempted N=${N}, TLS_BYTES=${TLS_BYTES})"
printf '    %-16s %-16s %-22s\n' "distro (glibc)" "initial-exec" "global-dynamic"
for d in "${ORDER[@]}"; do
    printf '    %-16s %-16s %-22s\n' \
        "${d} (${GLIBC[$d]})" "${IE[$d]} of ${N}" "${GD[$d]} of ${N}"
done
echo
msg "the dlopen failure (initial-exec) looks like:"
grep -h "static TLS" "$WORK"/ie-*.err 2>/dev/null | head -1 | sed 's/^/    /' \
    || grep -h "failed:" "$WORK"/ie-*.err 2>/dev/null | head -1 | sed 's/^/    /' || true

# ---- self-validation ---------------------------------------------------------
echo; line
fail=0
for d in "${ORDER[@]}"; do
    # global-dynamic must open all N everywhere (the mitigation works)
    [ "${GD[$d]:-0}" = "$N" ] || { msg "UNEXPECTED: global-dynamic opened ${GD[$d]}/${N} on ${d}"; fail=1; }
    # initial-exec must hit the limit (fewer than N) -- the phenomenon exists
    [ "${IE[$d]:-0}" -lt "$N" ] || { msg "UNEXPECTED: initial-exec opened all ${N} on ${d} (raise N)"; fail=1; }
done
ie_vals="$(for d in "${ORDER[@]}"; do echo "${IE[$d]}"; done | sort -u | paste -sd, -)"

if [ "$fail" -eq 0 ]; then
    msg "RESULT (conclusions upheld):"
    echo "    * INITIAL-EXEC TLS in a dlopen()ed lib is capped by glibc's static-TLS"
    echo "      surplus once another thread is running: initial-exec opened {${ie_vals}}"
    echo "      of ${N} here.  This is a real, threading-related dlopen failure mode --"
    echo "      the class the pthread symbol-version test does NOT cover, and the most"
    echo "      likely thing a plugin-heavy host (Phlex especially) actually hits."
    echo "    * GLOBAL-DYNAMIC TLS has no such cap (all ${N} opened on every distro):"
    echo "      building plugin libraries with global-dynamic TLS is the fix."
    echo "    * NOTE: AL8/AL9/AL10 (glibc 2.28/2.34/2.39) behave the SAME here, so this"
    echo "      run does not by itself explain 'AL9 vs AL10' -- see README.md for what"
    echo "      that would take and the mitigations multispack should adopt."
    exit 0
else
    msg "RESULT: something did not match expectations; see above and README.md."
    exit 1
fi
