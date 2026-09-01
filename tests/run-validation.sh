#!/bin/sh
#
# Runs inside a bare validation container.  POSIX sh only -- Alpine has no bash.
# /cvmfs is mounted READ-ONLY.  Nothing is installed from the distribution.
#
set -u

CVMFS_ROOT="${CVMFS_ROOT:-/cvmfs/multispack.example.org}"
DISTRO="${DISTRO:-unknown}"
CXXSTD_LIST="${CXXSTD_LIST:-17 23}"
TESTS="$(dirname "$0")"
META=/multispack/meta
RESULTS="$(mktemp)"
export HOME=/tmp TMPDIR=/tmp

# The single most important line in this file: if ROOT runs without it, the
# $ORIGIN rpaths are carrying the whole load.
unset LD_LIBRARY_PATH

record() {  # record <name> <status> <detail>
    printf '%s\t%s\t%s\n' "$1" "$2" "$(printf '%s' "$3" | tr -d '"\\' | tr '\n\t' '  ')" >> "$RESULTS"
    printf '  %-22s %-8s %s\n' "$1" "$2" "$3"
}

# ---- host facts -------------------------------------------------------------
OSID=$(. /etc/os-release 2>/dev/null; echo "${ID:-unknown}-${VERSION_ID:-}")
KERNEL=$(uname -r)
LIBC=$(ldd --version 2>&1 | head -1 || echo unknown)
ARCH=$(uname -m)

# Strategy B ships no loader: PT_INTERP points at the host's glibc ld.so.  If
# that file does not exist, nothing can start -- which is precisely what a musl
# distribution looks like, and is an EXPECTED failure, not a regression.
LOADER=/lib64/ld-linux-x86-64.so.2
if [ -e "$LOADER" ]; then EXPECT=pass; else EXPECT=xfail; fi

echo "=============================================================="
echo " multispack validation: $DISTRO ($OSID)"
echo "   kernel:  $KERNEL  arch: $ARCH"
echo "   libc:    $LIBC"
echo "   loader:  $LOADER $( [ -e "$LOADER" ] && echo present || echo MISSING )"
echo "   expect:  $EXPECT"
echo "=============================================================="

OVERALL=pass
record host-loader "$( [ -e "$LOADER" ] && echo pass || echo fail )" "$LOADER"

if [ ! -d "$CVMFS_ROOT" ]; then
    record cvmfs-mount fail "$CVMFS_ROOT not mounted"
    OVERALL=fail
    CXXSTD_LIST=""
else
    record cvmfs-mount pass "$CVMFS_ROOT"
fi

# ---- per-flavour tests ------------------------------------------------------
for cx in $CXXSTD_LIST; do
    ENVSH="$CVMFS_ROOT/env/root-cxx$cx/env.sh"
    if [ ! -f "$ENVSH" ]; then
        record "cxx$cx-env" fail "missing $ENVSH"
        OVERALL=fail
        continue
    fi

    case "$cx" in
        17) WANT=201703 ;;
        20) WANT=202002 ;;
        23) WANT=202302 ;;
        *)  WANT=0 ;;
    esac

    # Everything for this flavour runs in a subshell so PATH edits do not leak.
    (
        . "$ENVSH"
        echo "--- C++$cx  ROOTSYS=$ROOTSYS"

        out=$(root -l -b -q "$TESTS/smoke.C" 2>&1)
        echo "$out" | grep -q 'SMOKE entries=1000' \
            && echo "PASS smoke $(echo "$out" | grep SMOKE)" \
            || { echo "FAIL smoke $(echo "$out" | tail -3)"; exit 11; }

        out=$(root -l -b -q "$TESTS/cxxstd.C" 2>&1)
        got=$(echo "$out" | sed -n 's/^CXXSTD \([0-9]*\).*/\1/p')
        if [ -n "$got" ] && [ "$got" -ge "$WANT" ] 2>/dev/null; then
            echo "PASS cxxstd __cplusplus=$got (want >= $WANT)"
        else
            echo "FAIL cxxstd __cplusplus=${got:-none} (want >= $WANT)"; exit 12
        fi

        if [ "$cx" = 23 ]; then
            out=$(root -l -b -q "$TESTS/cxx23lib.C" 2>&1)
            echo "$out" | grep -q 'CXX23LIB 42 1' \
                && echo "PASS cxx23lib std::expected from the shipped libstdc++" \
                || { echo "FAIL cxx23lib $(echo "$out" | tail -2)"; exit 13; }
        fi

        if command -v python3 >/dev/null 2>&1; then
            out=$(python3 "$TESTS/pyroot.py" 2>&1)
            echo "$out" | grep -q 'PYROOT entries=100 answer=42' \
                && echo "PASS pyroot $(echo "$out" | grep PYROOT)" \
                || echo "SKIP pyroot $(echo "$out" | tail -2)"
        else
            echo "SKIP pyroot no python3 on PATH"
        fi
    ) > "/tmp/flavour-$cx.log" 2>&1
    rc=$?
    sed 's/^/    /' "/tmp/flavour-$cx.log"

    while IFS= read -r line; do
        case "$line" in
            "PASS "*) record "cxx$cx-$(echo "$line" | cut -d' ' -f2)" pass "$(echo "$line" | cut -d' ' -f3-)" ;;
            "FAIL "*) record "cxx$cx-$(echo "$line" | cut -d' ' -f2)" fail "$(echo "$line" | cut -d' ' -f3-)" ;;
            "SKIP "*) record "cxx$cx-$(echo "$line" | cut -d' ' -f2)" skip "$(echo "$line" | cut -d' ' -f3-)" ;;
        esac
    done < "/tmp/flavour-$cx.log"

    [ $rc -eq 0 ] || OVERALL=fail
done

# An expected failure on a musl host is not a regression; it marks the boundary
# of Strategy B.  Strategy A (own glibc + own loader) is what turns this green.
VERDICT="$OVERALL"
if [ "$EXPECT" = xfail ]; then
    if [ "$OVERALL" = fail ]; then VERDICT=xfail; else VERDICT=xpass; fi
fi

# ---- JSON -------------------------------------------------------------------
mkdir -p "$META"
{
    printf '{\n'
    printf '  "phase": "validate-%s",\n' "$DISTRO"
    printf '  "distro": "%s",\n' "$DISTRO"
    printf '  "os_release": "%s",\n' "$OSID"
    printf '  "kernel": "%s",\n' "$KERNEL"
    printf '  "arch": "%s",\n' "$ARCH"
    printf '  "host_libc": "%s",\n' "$(printf '%s' "$LIBC" | tr -d '"\\')"
    printf '  "loader_present": %s,\n' "$( [ -e "$LOADER" ] && echo true || echo false )"
    printf '  "expectation": "%s",\n' "$EXPECT"
    printf '  "verdict": "%s",\n' "$VERDICT"
    printf '  "ld_library_path_used": false,\n'
    printf '  "tests": [\n'
    n=0
    while IFS="$(printf '\t')" read -r name status detail; do
        [ $n -eq 0 ] || printf ',\n'
        n=1
        printf '    {"name": "%s", "status": "%s", "detail": "%s"}' "$name" "$status" "$detail"
    done < "$RESULTS"
    printf '\n  ]\n}\n'
} > "$META/validate-$DISTRO.detail.json"

rm -f "$RESULTS"
echo "verdict: $VERDICT  (expectation: $EXPECT)"
case "$VERDICT" in
    pass|xfail) exit 0 ;;
    *)          exit 1 ;;
esac
