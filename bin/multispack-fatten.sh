# multispack-fatten.sh -- SOURCE this to make Spack usable in a bare container.
#
# Spack is a Python program and also wants `git`; a minimal distro (the bare
# validators) ships neither.  Rather than bake per-distro packages into a family of
# *-run images, this finds the Spack store's OWN python and git -- portable,
# glibc-floor binaries already installed under $CVMFS_ROOT/opt (Strategy B: no
# distro packages, no network) -- prepends whichever are MISSING to PATH, then
# sources Spack's setup-env.  Distro-independent by construction: the same store
# binaries run on any glibc distro the shipped software already targets.
#
# Usage (CVMFS_ROOT must be set):
#     . /opt/multispack/bin/multispack-fatten.sh
#     spack env activate -d "$CVMFS_ROOT/env/<name>"   # then e.g. spack load <pkg>
#
# Note: uses the store's Python, so a truly EMPTY store has nothing to find.  In
# that case install something first, or fall back to a distro python / `uv`.

: "${CVMFS_ROOT:?multispack-fatten: CVMFS_ROOT must be set}"

# Echo the first executable matching $CVMFS_ROOT/opt/<any depth 1..6>/$1, where $1
# is a glob leaf such as 'git-*/bin/git'.  The depth varies with install_tree
# padding, so try a range; $1 is left unquoted so its globs expand.
_ms_find() {
    for _g in "$CVMFS_ROOT"/opt/*/$1 \
              "$CVMFS_ROOT"/opt/*/*/$1 \
              "$CVMFS_ROOT"/opt/*/*/*/$1 \
              "$CVMFS_ROOT"/opt/*/*/*/*/$1 \
              "$CVMFS_ROOT"/opt/*/*/*/*/*/$1 \
              "$CVMFS_ROOT"/opt/*/*/*/*/*/*/$1; do
        [ -x "$_g" ] && { printf '%s\n' "$_g"; return 0; }
    done
    return 1
}

if ! command -v python3 >/dev/null 2>&1; then
    _ms_py=$(_ms_find "python-3.11.*/bin/python3" || _ms_find "python-3.*/bin/python3")
    if [ -n "$_ms_py" ]; then
        PATH="$(dirname "$_ms_py"):$PATH"; export PATH
    else
        echo "multispack-fatten: no store python found under $CVMFS_ROOT/opt" >&2
    fi
fi

if ! command -v git >/dev/null 2>&1; then
    _ms_git=$(_ms_find "git-*/bin/git")
    if [ -n "$_ms_git" ]; then
        PATH="$(dirname "$_ms_git"):$PATH"; export PATH
    else
        echo "multispack-fatten: no store git found under $CVMFS_ROOT/opt" >&2
    fi
fi

command -v python3 >/dev/null 2>&1 \
    || echo "multispack-fatten: WARNING no python3 on PATH -- spack will not run" >&2
echo "multispack-fatten: python=$(command -v python3 || echo none) git=$(command -v git || echo none)" >&2

if [ -f "$CVMFS_ROOT/spack/share/spack/setup-env.sh" ]; then
    . "$CVMFS_ROOT/spack/share/spack/setup-env.sh"
else
    echo "multispack-fatten: no Spack at $CVMFS_ROOT/spack" >&2
fi

unset _ms_py _ms_git _g
