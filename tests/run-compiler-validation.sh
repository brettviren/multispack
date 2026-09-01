#!/bin/sh
#
# Runs inside a bare validation container.  POSIX sh only -- Alpine has no bash.
# /cvmfs is mounted READ-ONLY.  Nothing is installed from the distribution.
#
# The payload here is the shipped GCC (GCC_TARGET_SPEC), not ROOT.  We prove the
# compiler itself is portable: the gcc/g++/gfortran drivers start, cc1plus can
# preprocess against the shipped libstdc++ headers, and -- when the compiler
# ships its own binutils -- a program it builds links and runs.  All of it is
# carried by rpaths alone; if it works without LD_LIBRARY_PATH, $ORIGIN (or, run
# before `originize`, the absolute /cvmfs store path) is doing the work.
#
set -u

CVMFS_ROOT="${CVMFS_ROOT:-/cvmfs/multispack.example.org}"
DISTRO="${DISTRO:-unknown}"
GCC_TARGET_SPEC="${GCC_TARGET_SPEC:-gcc@15}"
TESTS="$(dirname "$0")"
META=/multispack/meta
RESULTS="$(mktemp)"
export HOME=/tmp TMPDIR=/tmp

# The single most important line in this file: if the compiler and the programs
# it builds run without this, the rpaths are carrying the whole load.
unset LD_LIBRARY_PATH

# The GCC major version we expect to find, extracted from e.g. "gcc@15" or
# "gcc@15.1.0" -> "15".
WANT_MAJOR=$(printf '%s' "$GCC_TARGET_SPEC" | sed -e 's/^[^@]*@//' -e 's/[.@].*//')

record() {  # record <name> <status> <detail>
    printf '%s\t%s\t%s\n' "$1" "$2" "$(printf '%s' "$3" | tr -d '"\\' | tr '\n\t' '  ')" >> "$RESULTS"
    printf '  %-24s %-8s %s\n' "$1" "$2" "$3"
}

# ---- host facts -------------------------------------------------------------
OSID=$(. /etc/os-release 2>/dev/null; echo "${ID:-unknown}-${VERSION_ID:-}")
KERNEL=$(uname -r)
LIBC=$(ldd --version 2>&1 | head -1 || echo unknown)
ARCH=$(uname -m)

# The shipped gcc is a glibc binary: PT_INTERP points at the host's glibc ld.so.
# On musl (Alpine) that loader is absent and nothing starts -- an EXPECTED
# failure that marks the boundary of Strategy B, not a regression.
LOADER=/lib64/ld-linux-x86-64.so.2
if [ -e "$LOADER" ]; then EXPECT=pass; else EXPECT=xfail; fi

echo "=============================================================="
echo " multispack compiler validation: $DISTRO ($OSID)"
echo "   kernel:  $KERNEL  arch: $ARCH"
echo "   libc:    $LIBC"
echo "   target:  $GCC_TARGET_SPEC (want major $WANT_MAJOR)"
echo "   loader:  $LOADER $( [ -e "$LOADER" ] && echo present || echo MISSING )"
echo "   expect:  $EXPECT"
echo "=============================================================="

OVERALL=pass
record host-loader "$( [ -e "$LOADER" ] && echo pass || echo fail )" "$LOADER"

if [ ! -d "$CVMFS_ROOT" ]; then
    record cvmfs-mount fail "$CVMFS_ROOT not mounted"
    OVERALL=fail
fi

ENVSH="$CVMFS_ROOT/env/gcc/env.sh"
if [ ! -f "$ENVSH" ]; then
    record gcc-env fail "missing $ENVSH (run 'multispack.sh compiler' first)"
    OVERALL=fail
else
    record gcc-env pass "$ENVSH"
    # PATH edits here do not matter -- this process exits when the script ends.
    . "$ENVSH"

    # --- driver liveness: each driver binary must load and report a version ---
    for tool in gcc g++ gfortran; do
        out=$("$tool" --version 2>&1)
        rc=$?
        ver=$("$tool" -dumpfullversion 2>/dev/null || "$tool" -dumpversion 2>/dev/null)
        major=$(printf '%s' "$ver" | sed 's/[.].*//')
        if [ $rc -ne 0 ] || [ -z "$ver" ]; then
            record "$tool-runs" fail "$(printf '%s' "$out" | head -1)"
            OVERALL=fail
        elif [ "$major" != "$WANT_MAJOR" ]; then
            record "$tool-version" fail "got $ver, want major $WANT_MAJOR"
            OVERALL=fail
        else
            record "$tool-version" pass "$ver"
        fi
    done

    # --- cc1plus liveness: compile C++ to assembly, no system headers ---------
    # This is the core proof that the *compiler itself* is portable.  `-S` on a
    # self-contained snippet runs cc1plus and its whole shared-library closure
    # (gmp/mpfr/mpc/isl/zstd + libstdc++/libgcc_s), all loaded via rpath against
    # the host glibc, and generates code -- needing no libc headers, no startup
    # files and no assembler/linker.  It works in a truly bare container.
    asm=$(printf 'template<class T> T sq(T x){return x*x;}\nint msvalidate(int x){return sq(x)+1;}\n' \
            | g++ -std=c++23 -O2 -S -x c++ - -o - 2>&1)
    if [ $? -eq 0 ] && printf '%s' "$asm" | grep -q 'msvalidate'; then
        record cc1plus-compile pass "cc1plus generates C++23 code"
    else
        record cc1plus-compile fail "$(printf '%s' "$asm" | tail -1)"
        OVERALL=fail
    fi

    # --- does this container provide libc headers + startup files? ------------
    # Strategy B ships the compiler, not a sysroot: a full compile+link needs the
    # distribution's libc dev headers (<features.h> et al.) and crt*.o.  Bare
    # validators deliberately ship none of that -- the compiler runs, but there
    # is nothing here to build a whole program against.  Detect it so the next
    # test SKIPs cleanly instead of failing on a missing system header.
    if printf '#include <features.h>\nint main(void){return 0;}\n' \
            | g++ -std=c++23 -E -x c++ - >/dev/null 2>&1; then
        HAVE_LIBC_HEADERS=yes
    else
        HAVE_LIBC_HEADERS=no
    fi

    # --- full compile + link + run (only where the distro can host a build) ---
    BIN=/tmp/gcc-hello
    if [ "$HAVE_LIBC_HEADERS" = no ]; then
        record compile-run skip "bare validator ships no libc dev headers/startfiles -- compiler runs, but a full build needs glibc-devel"
    else
        cout=$(g++ -std=c++23 -O2 "$TESTS/gcc-hello.cpp" -o "$BIN" 2>&1)
        crc=$?
        if [ $crc -eq 0 ] && [ -x "$BIN" ]; then
            rout=$("$BIN" 2>&1)
            if printf '%s' "$rout" | grep -q 'GCCHELLO cxxstd=202302 sum=5050 expected=42'; then
                record compile-run pass "$rout"
            else
                record compile-run fail "ran but wrong output: $(printf '%s' "$rout" | head -1)"
                OVERALL=fail
            fi
        elif printf '%s' "$cout" | grep -Eqi "error trying to exec .?(as|ld|collect2)|installation problem, cannot exec|(^|[ /])(as|ld): command not found|assembler .*not found"; then
            # Headers are present but no assembler/linker -- expected if the
            # compiler ships no binutils and the host has none.  Not a defect.
            record compile-run skip "no as/ld available -- build $GCC_TARGET_SPEC +binutils to ship them"
        else
            record compile-run fail "$(printf '%s' "$cout" | tail -1)"
            OVERALL=fail
        fi
        rm -f "$BIN"
    fi
fi

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
    printf '  "phase": "compiler-validate-%s",\n' "$DISTRO"
    printf '  "distro": "%s",\n' "$DISTRO"
    printf '  "target_gcc_spec": "%s",\n' "$GCC_TARGET_SPEC"
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
} > "$META/compiler-validate-$DISTRO.detail.json"

rm -f "$RESULTS"
echo "verdict: $VERDICT  (expectation: $EXPECT)"
case "$VERDICT" in
    pass|xfail) exit 0 ;;
    *)          exit 1 ;;
esac
