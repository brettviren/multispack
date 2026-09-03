#!/usr/bin/env bash
#
# multispack.sh -- build and validate a distribution-independent ("Strategy B")
# Spack install area using podman containers and three podman volumes.
#
# See PLAN.md for the design.  Run `./multispack.sh help` for subcommands.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="${MULTISPACK_CONF:-$HERE/multispack.conf}"
# shellcheck disable=SC1090
[ -f "$CONF" ] && . "$CONF"

### ------------------------- configuration ---------------------------------
### Override any of these in ./multispack.conf or in the environment.

: "${ENGINE:=podman}"

# Fictional CVMFS repository.  This is the *canonical* deployment path; it is
# baked into every install prefix and must be identical in build and validation.
: "${CVMFS_HOST:=multispack.example.org}"
: "${CVMFS_ROOT:=/cvmfs/${CVMFS_HOST}}"

# Spack itself.  Spack >= 1.0 keeps the package recipes in a separate repo.
: "${SPACK_GIT:=https://github.com/spack/spack.git}"
: "${SPACK_REF:=v1.2.2}"
: "${SPACK_PACKAGES_GIT:=https://github.com/spack/spack-packages.git}"
: "${SPACK_PACKAGES_REF:=v1.2.2}"

# The portability contract.
: "${TARGET:=x86_64_v3}"
: "${BUILDER_BASE:=docker.io/library/almalinux:8}"   # glibc 2.28 == manylinux_2_28

# Toolchain built by Spack on top of the base image's system gcc.  This is the
# intermediate rung of the bootstrap ladder (base gcc 8.5 -> GCC_SPEC) and the
# compiler that builds the rest of the stack, including GCC_TARGET_SPEC.
: "${GCC_SPEC:=gcc@14}"
: "${GCC_LANGS:=c,c++,fortran}"

# The portability payload.  The top rung of the ladder (GCC_SPEC -> GCC_TARGET_SPEC),
# built with %GCC_SPEC and then validated for portability across VALIDATORS by the
# `compiler-validate` subcommand.  +binutils makes it ship its own as/ld so it is
# self-contained in a bare container (nothing from the distribution but glibc).
: "${GCC_TARGET_SPEC:=gcc@15}"
: "${GCC_TARGET_VARIANTS:=+binutils}"

# The validation payload: two ROOTs, shared C++17 dependencies.
: "${ROOT_PKG:=root}"
: "${ROOT_VARIANTS:=~x ~opengl ~examples ~tmva}"
: "${CXXSTD_LIST:=17 23}"
: "${BASE_CXXSTD:=17}"     # cxxstd preference applied to every dependency

# Relocation.  padded_length=0 disables padding (shorter paths, but the tree can
# then only be relocated to an equal-or-longer path).  See PLAN.md.
: "${PADDED_LENGTH:=128}"
: "${ORIGINIZE_RUNPATH:=0}"   # 1 => also convert DT_RPATH to DT_RUNPATH

: "${BUILD_JOBS:=0}"          # 0 => nproc on this host

# Images and volumes.
: "${IMG_PREFIX:=localhost/multispack}"
: "${IMG_TAG:=1}"
: "${VOL_CVMFS:=multispack-cvmfs}"
: "${VOL_CACHE:=multispack-cache}"
: "${VOL_WORK:=multispack-work}"

: "${VALIDATORS:=alma8 alma9 debian12 debian13 sles15 alpine}"

# Non-bare validators: each is a bare distro plus the MINIMAL packages the shipped
# compiler needs to build C++ (see containers/Containerfile.*-devel).  They prove
# the distro can host a build against gcc@15 -- where a bare validator only proves
# the compiler runs -- and document, one package at a time, the minimal host
# requirements for a build.  Future stack-specific variants (e.g. CUDA Toolkit)
# layer on top of these.  No alpine: Strategy B does not support musl.
: "${DEVEL_VALIDATORS:=alma8-devel alma9-devel debian12-devel debian13-devel sles15-devel}"

# Default image for `makenv`.  It must be able to run Spack (python3, git, ...)
# AND supply the OS-level build dependencies packages need.  The builder is
# exactly that: the manylinux base image + Spack's prerequisites + glibc-dev
# (pulled in by gcc-c++).  A CUDA (or other) build image should be built FROM the
# builder so it keeps Spack and the glibc floor, then passed via `makenv --image`.
: "${MAKENV_IMAGE:=builder}"

# makenv env style: 0 => directory env under /cvmfs/.../env/<name> (addressed by
# path); 1 => managed NAMED env (shown by `spack env list`, activated by name).
# Communities expect managed envs for official releases; --managed overrides this.
: "${MAKENV_MANAGED:=0}"

# Bind-mount the host copies of bin/ config/ tests/ over the baked-in ones so
# scripts can be edited without rebuilding images.  Set 0 for a sealed build.
: "${DEV_MOUNTS:=1}"

# SELinux volume suffix; set to ":z" on an enforcing host if mounts are denied.
: "${SEL:=}"

META="$HERE/meta"
BUILDER_IMG="${IMG_PREFIX}/builder:${IMG_TAG}"

### ------------------------------ helpers ----------------------------------

msg()  { printf '\033[1;34m[multispack]\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33m[multispack]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[multispack]\033[0m %s\n' "$*" >&2; exit 1; }

jobs_n() { if [ "$BUILD_JOBS" -gt 0 ] 2>/dev/null; then echo "$BUILD_JOBS"; else nproc; fi; }

# Fixed phase ordering so the report can sort deterministically.
phase_order() {
    case "$1" in
        volumes)     echo 10 ;;
        images)      echo 20 ;;
        bootstrap)   echo 30 ;;
        compiler)    echo 40 ;;
        compiler-validate)   echo 45 ;;
        compiler-validate-*) echo 46 ;;
        concretize)  echo 50 ;;
        stack)       echo 60 ;;
        originize)   echo 70 ;;
        audit)       echo 80 ;;
        buildcache)  echo 90 ;;
        deploy)      echo 95 ;;
        validate-*)  echo 96 ;;
        report)      echo 99 ;;
        *)           echo 50 ;;
    esac
}

json_str() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/\t/\\t/g'; }

# stage_run <phase> <description> -- <command...>
# Times the command, streams output to a log, and writes meta/NN-<phase>.json.
stage_run() {
    local phase="$1" desc="$2"; shift 2
    [ "${1:-}" = "--" ] && shift
    local ord t0 t1 iso0 iso1 status rc log
    ord="$(phase_order "$phase")"
    log="$META/${ord}-${phase}.log"
    mkdir -p "$META"
    t0="$(date +%s)"; iso0="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    msg "phase '$phase': $desc"
    set +e
    ( "$@" ) 2>&1 | tee "$log"
    rc="${PIPESTATUS[0]}"
    set -e
    t1="$(date +%s)"; iso1="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if [ "$rc" -eq 0 ]; then status=ok; else status=fail; fi
    cat > "$META/${ord}-${phase}.json" <<EOJ
{
  "order": $ord,
  "phase": "$(json_str "$phase")",
  "description": "$(json_str "$desc")",
  "status": "$status",
  "returncode": $rc,
  "started": "$iso0",
  "finished": "$iso1",
  "duration_s": $((t1 - t0)),
  "command": "$(json_str "$*")",
  "log": "$(json_str "${ord}-${phase}.log")",
  "config": {
    "cvmfs_root": "$(json_str "$CVMFS_ROOT")",
    "spack_ref": "$(json_str "$SPACK_REF")",
    "target": "$(json_str "$TARGET")",
    "gcc_spec": "$(json_str "$GCC_SPEC")",
    "gcc_target_spec": "$(json_str "$GCC_TARGET_SPEC")",
    "builder_base": "$(json_str "$BUILDER_BASE")",
    "padded_length": $PADDED_LENGTH,
    "cxxstd_list": "$(json_str "$CXXSTD_LIST")",
    "base_cxxstd": "$(json_str "$BASE_CXXSTD")",
    "build_jobs": $(jobs_n)
  }
}
EOJ
    [ "$rc" -eq 0 ] || die "phase '$phase' failed (rc=$rc); see $log"
}

# Volume/bind arguments shared by build and validation containers.
vol_args() {
    local ro="${1:-rw}"
    local a=()
    if [ "$ro" = "ro" ]; then
        a+=(-v "${VOL_CVMFS}:/cvmfs:ro${SEL}")
    else
        a+=(-v "${VOL_CVMFS}:/cvmfs${SEL}")
        a+=(-v "${VOL_CACHE}:/multispack/cache${SEL}")
        a+=(-v "${VOL_WORK}:/multispack/work${SEL}")
    fi
    a+=(-v "${META}:/multispack/meta${SEL}")
    if [ "$DEV_MOUNTS" = "1" ]; then
        a+=(-v "${HERE}/bin:/opt/multispack/bin:ro${SEL}")
        a+=(-v "${HERE}/config:/opt/multispack/config:ro${SEL}")
        a+=(-v "${HERE}/tests:/opt/multispack/tests:ro${SEL}")
    fi
    printf '%s\n' "${a[@]}"
}

env_args() {
    printf '%s\n' \
        -e "CVMFS_HOST=${CVMFS_HOST}" \
        -e "CVMFS_ROOT=${CVMFS_ROOT}" \
        -e "SPACK_GIT=${SPACK_GIT}" \
        -e "SPACK_REF=${SPACK_REF}" \
        -e "SPACK_PACKAGES_GIT=${SPACK_PACKAGES_GIT}" \
        -e "SPACK_PACKAGES_REF=${SPACK_PACKAGES_REF}" \
        -e "MULTISPACK_TARGET=${TARGET}" \
        -e "GCC_SPEC=${GCC_SPEC}" \
        -e "GCC_LANGS=${GCC_LANGS}" \
        -e "GCC_TARGET_SPEC=${GCC_TARGET_SPEC}" \
        -e "GCC_TARGET_VARIANTS=${GCC_TARGET_VARIANTS}" \
        -e "ROOT_PKG=${ROOT_PKG}" \
        -e "ROOT_VARIANTS=${ROOT_VARIANTS}" \
        -e "CXXSTD_LIST=${CXXSTD_LIST}" \
        -e "BASE_CXXSTD=${BASE_CXXSTD}" \
        -e "PADDED_LENGTH=${PADDED_LENGTH}" \
        -e "ORIGINIZE_RUNPATH=${ORIGINIZE_RUNPATH}" \
        -e "SPACK_JOBS=$(jobs_n)" \
        -e "BUILDER_BASE=${BUILDER_BASE}"
}

in_builder() {
    mapfile -t _v < <(vol_args rw)
    mapfile -t _e < <(env_args)
    "$ENGINE" run --rm -i "${_v[@]}" "${_e[@]}" "$BUILDER_IMG" "$@"
}

### ----------------------------- subcommands --------------------------------

cmd_volumes() {
    for v in "$VOL_CVMFS" "$VOL_CACHE" "$VOL_WORK"; do
        if "$ENGINE" volume exists "$v" 2>/dev/null; then
            msg "volume $v already exists"
        else
            "$ENGINE" volume create "$v" >/dev/null
            msg "created volume $v"
        fi
    done
    # Seed the directory layout on the volumes.
    mapfile -t _v < <(vol_args rw)
    "$ENGINE" run --rm "${_v[@]}" "$BUILDER_BASE" \
        /bin/sh -c "mkdir -p '$CVMFS_ROOT' \
            /multispack/cache/source /multispack/cache/buildcache \
            /multispack/work/stage /multispack/work/tmp /multispack/work/misc \
            /multispack/work/test /multispack/work/spack-user-cache \
            && ls -la /cvmfs /multispack/cache /multispack/work"
}

cmd_images() {
    local want=("$@")
    [ ${#want[@]} -eq 0 ] && want=(builder $VALIDATORS $DEVEL_VALIDATORS)
    for name in "${want[@]}"; do
        local cf="$HERE/containers/Containerfile.${name}"
        [ -f "$cf" ] || die "no such Containerfile: $cf"
        local img="${IMG_PREFIX}/${name}:${IMG_TAG}"
        msg "building image $img from $cf"
        "$ENGINE" build \
            --build-arg "BUILDER_BASE=${BUILDER_BASE}" \
            --build-arg "CVMFS_HOST=${CVMFS_HOST}" \
            --build-arg "BUILDER_IMG=${BUILDER_IMG}" \
            -t "$img" -f "$cf" "$HERE"
    done
    # Record the base image digests: this is the "build environment captured"
    # artifact.  Pin these in multispack.conf for a reproducible rebuild.
    {
        echo '{'
        echo '  "phase": "images",'
        echo '  "images": ['
        local first=1
        for name in "${want[@]}"; do
            local img="${IMG_PREFIX}/${name}:${IMG_TAG}"
            local id
            id="$("$ENGINE" image inspect --format '{{.Id}}' "$img" 2>/dev/null || echo unknown)"
            [ $first -eq 1 ] || echo ','
            first=0
            printf '    {"name": "%s", "image": "%s", "id": "%s"}' "$name" "$img" "$id"
        done
        echo
        echo '  ]'
        echo '}'
    } > "$META/images.detail.json"
}

cmd_bootstrap()  { in_builder /opt/multispack/bin/phase-bootstrap.sh; }
cmd_compiler()   { in_builder /opt/multispack/bin/phase-compiler.sh; }
cmd_concretize() { in_builder /opt/multispack/bin/phase-concretize.sh; }
cmd_stack()      { in_builder /opt/multispack/bin/phase-stack.sh; }
cmd_originize()  { in_builder /opt/multispack/bin/phase-originize.sh; }
cmd_audit()      { in_builder /opt/multispack/bin/phase-audit.sh; }
cmd_buildcache() { in_builder /opt/multispack/bin/phase-buildcache.sh; }
cmd_deploy()     { in_builder /opt/multispack/bin/phase-deploy.sh; }

cmd_validate() {
    local want=("$@")
    [ ${#want[@]} -eq 0 ] && want=($VALIDATORS)
    local rc_all=0
    for d in "${want[@]}"; do
        local img="${IMG_PREFIX}/${d}:${IMG_TAG}"
        mapfile -t _v < <(vol_args ro)
        # Note: /cvmfs is mounted READ-ONLY here.  That is deliberate: it is how
        # the real deployment looks and it catches anything that writes into the
        # install tree at runtime.
        set +e
        "$ENGINE" run --rm "${_v[@]}" \
            -e "CVMFS_ROOT=${CVMFS_ROOT}" -e "DISTRO=${d}" \
            -e "CXXSTD_LIST=${CXXSTD_LIST}" \
            "$img" /bin/sh /opt/multispack/tests/run-validation.sh
        local rc=$?
        set -e
        [ $rc -eq 0 ] || { warn "validation on $d returned $rc"; rc_all=1; }
    done
    return $rc_all
}

cmd_compiler_validate() {
    local want=("$@")
    [ ${#want[@]} -eq 0 ] && want=($VALIDATORS $DEVEL_VALIDATORS)
    local rc_all=0
    for d in "${want[@]}"; do
        local img="${IMG_PREFIX}/${d}:${IMG_TAG}"
        mapfile -t _v < <(vol_args ro)
        # Same contract as `validate`: /cvmfs read-only, nothing installed from the
        # distribution.  Here the payload is GCC_TARGET_SPEC itself -- we prove the
        # shipped compiler runs, preprocesses and (with +binutils) compiles+links a
        # program that then runs, all carried by $ORIGIN/absolute rpaths alone.
        set +e
        "$ENGINE" run --rm "${_v[@]}" \
            -e "CVMFS_ROOT=${CVMFS_ROOT}" -e "DISTRO=${d}" \
            -e "GCC_TARGET_SPEC=${GCC_TARGET_SPEC}" \
            "$img" /bin/sh /opt/multispack/tests/run-compiler-validation.sh
        local rc=$?
        set -e
        [ $rc -eq 0 ] || { warn "compiler validation on $d returned $rc"; rc_all=1; }
    done
    return $rc_all
}

# makenv [--image NAME] [--name ENV] [--no-check] <spack.yaml>
# Concretize + install an arbitrary user Spack environment into the shared
# install tree and buildcache, in the chosen build container.  The container is
# checked for the required non-Spack (OS-level) dependencies before installing.
cmd_makenv() {
    local image="${MAKENV_IMAGE}" name="" yaml="" nocheck=0 repos="" managed="${MAKENV_MANAGED}"
    while [ $# -gt 0 ]; do
        case "$1" in
            -i|--image) image="${2:?--image needs a value}"; shift 2 ;;
            -n|--name)  name="${2:?--name needs a value}"; shift 2 ;;
            -r|--repos) repos="${2:?--repos needs a value}"; shift 2 ;;
            --managed)  managed=1; shift ;;
            --directory) managed=0; shift ;;
            --no-check) nocheck=1; shift ;;
            -h|--help)
                echo "usage: multispack.sh makenv [--image NAME] [--name ENV] [--repos DIR] [--managed] [--no-check] <spack.yaml>"
                return 0 ;;
            --) shift; [ -z "$yaml" ] && [ $# -gt 0 ] && { yaml="$1"; shift; }; break ;;
            -*) die "makenv: unknown option: $1" ;;
            *)  if [ -z "$yaml" ]; then yaml="$1"; else die "makenv: unexpected argument: $1"; fi; shift ;;
        esac
    done

    [ -n "$yaml" ] || die "makenv: missing required <spack.yaml> argument"
    [ -f "$yaml" ] || die "makenv: no such file: $yaml"
    yaml="$(cd "$(dirname "$yaml")" && pwd)/$(basename "$yaml")"

    # Default the environment name to the spack.yaml's parent directory.
    if [ -z "$name" ]; then
        name="$(basename "$(dirname "$yaml")")"
        case "$name" in ""|"/"|".") name=custom ;; esac
    fi
    case "$name" in
        *[!A-Za-z0-9._-]*) die "makenv: --name must be a simple path component: '$name'" ;;
    esac

    local img="${IMG_PREFIX}/${image}:${IMG_TAG}"
    "$ENGINE" image exists "$img" 2>/dev/null \
        || die "makenv: image not built: ${img}  (build it: ./multispack.sh images ${image})"

    # Custom package repos referenced by the spack.yaml.  Its `repos:` paths use
    # `$spack/../repos/...`, which in the container is ${CVMFS_ROOT}/repos, so we
    # bind the assembled repo tree there (read-only).  Assembling those repos is
    # the upstream env's job (e.g. xerosere's mr/.mrconfig); makenv just consumes.
    local repo_args=()
    if [ -n "$repos" ]; then
        [ -d "$repos" ] || die "makenv: --repos is not a directory: $repos"
        repos="$(cd "$repos" && pwd)"
        repo_args=(-v "${repos}:${CVMFS_ROOT}/repos:ro${SEL}")
        msg "makenv: mounting repos ${repos} -> ${CVMFS_ROOT}/repos"
    fi

    # Mount the whole env DIRECTORY (not just the yaml) so relative `include:` files
    # (repos.yaml, packages.yaml, groups/*.yaml, ...) come along; MAKENV_YAML names
    # the manifest within it (usually spack.yaml).
    local yamldir; yamldir="$(dirname "$yaml")"
    msg "makenv: build env '${name}' from ${yaml} in image '${image}'"
    mapfile -t _v < <(vol_args rw)
    mapfile -t _e < <(env_args)
    "$ENGINE" run --rm -i "${_v[@]}" "${repo_args[@]}" "${_e[@]}" \
        -v "${yamldir}:/multispack/input:ro${SEL}" \
        -e "MAKENV_YAML=$(basename "$yaml")" \
        -e "MAKENV_NAME=${name}" \
        -e "MAKENV_NOCHECK=${nocheck}" \
        -e "MAKENV_IMAGE=${image}" \
        -e "MAKENV_REPOS=${repos}" \
        -e "MAKENV_MANAGED=${managed}" \
        "$img" /opt/multispack/bin/phase-makenv.sh
}

# viewgroups [opts] <input.spack.yaml> [more.yaml ...]
# Derive an optimal grouped spack.yaml from declared views, in the builder.
# The heavy lifting is bin/spack-view-groups.py (mounted at /opt/multispack/bin
# via DEV_MOUNTS); this wrapper mounts the repo rw at /ms-repo so the tool can
# read the input file(s) and write the generated grouped manifest, gives it the
# /cvmfs Spack + a writable scratch area, and forwards the options.
cmd_viewgroups() {
    [ "$DEV_MOUNTS" = 1 ] || die "viewgroups: needs DEV_MOUNTS=1 (mounts bin/ into the builder)"
    local image="${MAKENV_IMAGE}" env="" concretize=0 report=0 strict=0
    local reqsingle=()
    while [ $# -gt 0 ]; do
        case "$1" in
            -i|--image)  image="${2:?--image needs a value}"; shift 2 ;;
            --concretize) concretize=1; shift ;;
            --require-single) reqsingle+=("${2:?--require-single needs PKG}"); shift 2 ;;
            --strict-single) strict=1; shift ;;
            --report)    report=1; shift ;;
            -h|--help)
                echo "usage: multispack.sh viewgroups [--image N] [--concretize] [--require-single PKG]... [--strict-single] [--report] <env-spack.yaml-or-dir>"
                return 0 ;;
            -*) die "viewgroups: unknown option: $1" ;;
            *)  [ -z "$env" ] && env="$1" || die "viewgroups: unexpected argument: $1"; shift ;;
        esac
    done
    [ -n "$env" ] || die "viewgroups: missing <env-spack.yaml-or-dir>"

    # Translate a host path under the repo to its /ms-repo mount location.
    to_ctr() {
        local p; p="$(cd "$(dirname "$1")" 2>/dev/null && pwd)/$(basename "$1")"
        case "$p" in "$HERE"/*) printf '/ms-repo/%s\n' "${p#"$HERE"/}" ;;
            *) die "viewgroups: path must live under the repo ($HERE): $1" ;; esac
    }
    [ -e "$env" ] || die "viewgroups: no such path: $env"
    local img="${IMG_PREFIX}/${image}:${IMG_TAG}"
    "$ENGINE" image exists "$img" 2>/dev/null \
        || die "viewgroups: image not built: ${img}  (build it: ./multispack.sh images ${image})"

    local args=(python3 /opt/multispack/bin/spack-view-groups.py --spack spack)
    [ "$concretize" = 1 ] && args+=(--concretize)
    [ "$report" = 1 ] && args+=(--report)
    [ "$strict" = 1 ] && args+=(--strict-single)
    local p; for p in "${reqsingle[@]}"; do args+=(--require-single "$p"); done
    args+=("$(to_ctr "$env")")

    msg "viewgroups: analyze ${env}  (image: ${image})"
    mapfile -t _v < <(vol_args rw)
    mapfile -t _e < <(env_args)
    # Quote the tool argv so `sh -lc` (after sourcing setup-env) runs it verbatim.
    local q="" a; for a in "${args[@]}"; do q+=" $(printf '%q' "$a")"; done
    "$ENGINE" run --rm -i "${_v[@]}" "${_e[@]}" \
        -v "${HERE}:/ms-repo${SEL}" \
        "$img" /bin/sh -lc '. "$CVMFS_ROOT/spack/share/spack/setup-env.sh"; export SPACK_ROOT="$CVMFS_ROOT/spack"; exec'"$q"
}

# sbom [--format cyclonedx|spdx|text] <env>
# Emit a Software Bill of Materials for an environment (managed name, env dir, or
# a name under /cvmfs/.../env) and flag the CVE-prone leaves.  Read-only.
cmd_sbom() {
    local fmt=cyclonedx env=""
    while [ $# -gt 0 ]; do
        case "$1" in
            -f|--format) fmt="${2:?--format needs a value}"; shift 2 ;;
            -h|--help)
                echo "usage: multispack.sh sbom [--format cyclonedx|spdx|text] <env-name-or-dir>"
                return 0 ;;
            -*) die "sbom: unknown option: $1" ;;
            *)  if [ -z "$env" ]; then env="$1"; else die "sbom: unexpected argument: $1"; fi; shift ;;
        esac
    done
    [ -n "$env" ] || env="root"   # default to the ROOT-pipeline environment
    mapfile -t _v < <(vol_args ro)
    mapfile -t _e < <(env_args)
    "$ENGINE" run --rm -i "${_v[@]}" "${_e[@]}" \
        -e "SBOM_ENV=${env}" -e "SBOM_FORMAT=${fmt}" \
        "$BUILDER_IMG" /opt/multispack/bin/phase-sbom.sh
}

cmd_report() {
    mapfile -t _v < <(vol_args rw)
    "$ENGINE" run --rm "${_v[@]}" "$BUILDER_IMG" \
        python3 /opt/multispack/bin/report.py \
            --meta /multispack/meta --out /multispack/meta/report.html
    msg "report written to $META/report.html"
}

cmd_shell() {
    local which="${1:-builder}"
    local img="${IMG_PREFIX}/${which}:${IMG_TAG}"
    local mode=rw
    [ "$which" = builder ] || mode=ro
    mapfile -t _v < <(vol_args "$mode")
    mapfile -t _e < <(env_args)
    "$ENGINE" run --rm -it "${_v[@]}" "${_e[@]}" "$img" /bin/sh -l
}

# runenv [--image NAME] <env>
# Like `shell`, but sets up Spack and ACTIVATES a Spack environment, then drops
# into an interactive shell inside it.  <env> resolution (done in-container):
#   * a leaf directory under $CVMFS_ROOT/env/  -> a directory (by-path) env;
#   * otherwise                                -> a named (managed) env.
# A '/'-prefixed <env> is taken as a named env UNLESS its basename is such a leaf
# directory, in which case that directory env is used.  --image defaults to builder.
cmd_runenv() {
    local image="builder" env=""
    while [ $# -gt 0 ]; do
        case "$1" in
            -i|--image) image="${2:?--image needs a value}"; shift 2 ;;
            -h|--help)
                echo "usage: multispack.sh runenv [--image NAME] <env-name-or-dir>"; return 0 ;;
            -*) die "runenv: unknown option: $1" ;;
            *)  [ -z "$env" ] && env="$1" || die "runenv: unexpected argument: $1"; shift ;;
        esac
    done
    [ -n "$env" ] || die "runenv: missing <env> (a name, or a leaf under ${CVMFS_ROOT}/env)"
    local img="${IMG_PREFIX}/${image}:${IMG_TAG}"
    "$ENGINE" image exists "$img" 2>/dev/null \
        || die "runenv: image not built: ${img}  (build it: ./multispack.sh images ${image})"
    # rw: `spack env activate` writes a transaction lock under the env's .spack-env.
    mapfile -t _v < <(vol_args rw)
    mapfile -t _e < <(env_args)
    msg "runenv: activate '${env}' in image '${image}'"
    "$ENGINE" run --rm -it "${_v[@]}" "${_e[@]}" -e "RUNENV_TARGET=${env}" \
        "$img" /bin/bash -lc '
            . "$CVMFS_ROOT/spack/share/spack/setup-env.sh"
            E="$RUNENV_TARGET"; leaf="${E##*/}"; D=""
            case "$E" in
                /*) [ -d "$CVMFS_ROOT/env/$leaf" ] && D="$CVMFS_ROOT/env/$leaf" ;;
                *)  [ -d "$CVMFS_ROOT/env/$E" ]    && D="$CVMFS_ROOT/env/$E" ;;
            esac
            if [ -n "$D" ]; then
                echo "[runenv] directory env: $D" >&2
                spack env activate -d "$D" || echo "[runenv] activate failed" >&2
            else
                echo "[runenv] named env: $E" >&2
                spack env activate "$E" || echo "[runenv] activate failed (no such env?)" >&2
            fi
            exec bash -i
        '
}

cmd_status() {
    printf '%-14s %-8s %10s  %s\n' PHASE STATUS SECONDS FINISHED
    for f in "$META"/*.json; do
        [ -e "$f" ] || continue
        case "$f" in *detail.json) continue;; esac
        python3 - "$f" <<'EOP' 2>/dev/null || sed -n 's/.*"phase": "\([^"]*\)".*/\1/p' "$f"
import json,sys
d=json.load(open(sys.argv[1]))
print("%-14s %-8s %10s  %s" % (d.get("phase"), d.get("status"), d.get("duration_s"), d.get("finished")))
EOP
    done
}

cmd_clean() {
    warn "removing contents of the work volume ($VOL_WORK)"
    mapfile -t _v < <(vol_args rw)
    "$ENGINE" run --rm "${_v[@]}" "$BUILDER_BASE" \
        /bin/sh -c 'rm -rf /multispack/work/stage/* /multispack/work/tmp/* /multispack/work/test/*; true'
}

cmd_nuke() {
    warn "this removes ALL multispack volumes and images"
    read -r -p "type 'yes' to continue: " a
    [ "$a" = yes ] || die "aborted"
    for v in "$VOL_CVMFS" "$VOL_CACHE" "$VOL_WORK"; do "$ENGINE" volume rm -f "$v" || true; done
    for n in builder $VALIDATORS $DEVEL_VALIDATORS; do "$ENGINE" rmi -f "${IMG_PREFIX}/${n}:${IMG_TAG}" || true; done
}

cmd_all() {
    stage_run volumes    "create podman volumes"                       -- cmd_volumes
    stage_run images     "build builder and validator images"          -- cmd_images
    stage_run bootstrap  "clone Spack $SPACK_REF into $CVMFS_ROOT"     -- cmd_bootstrap
    stage_run compiler   "build $GCC_SPEC then $GCC_TARGET_SPEC"       -- cmd_compiler
    for d in $VALIDATORS $DEVEL_VALIDATORS; do
        set +e
        stage_run "compiler-validate-$d" "run $GCC_TARGET_SPEC validation on $d" -- cmd_compiler_validate "$d"
        set -e
    done
    stage_run concretize "concretize the ROOT environment"             -- cmd_concretize
    stage_run stack      "install ROOT for C++ $CXXSTD_LIST"           -- cmd_stack
    stage_run originize  "rewrite RPATHs to \$ORIGIN"                  -- cmd_originize
    stage_run audit      "ELF audit: glibc floor and external deps"    -- cmd_audit
    stage_run buildcache "push the install tree to the binary cache"   -- cmd_buildcache
    stage_run deploy     "generate env.sh and the deployment manifest" -- cmd_deploy
    for d in $VALIDATORS; do
        set +e
        stage_run "validate-$d" "run ROOT validation on $d" -- cmd_validate "$d"
        set -e
    done
    stage_run report     "render the HTML summary"                     -- cmd_report
}

usage() {
    cat <<'EOU'
multispack.sh -- Strategy B portable Spack stack, built and validated in podman.

Build pipeline (each writes meta/NN-<phase>.json):
  volumes            create the three podman volumes and seed their layout
  images [name...]   build container images
                     (default: builder + bare validators + -devel validators)
  bootstrap          clone Spack into /cvmfs, install site config, bootstrap clingo
  compiler           build the Spack GCC ladder: GCC_SPEC (by the base gcc) then
                     GCC_TARGET_SPEC (by GCC_SPEC) -- the portability payload
  compiler-validate [distro...]
                     run GCC_TARGET_SPEC in the validators (default: bare + -devel)
                     -- the FAST portability gate; run before the long stack build.
                     Bare validators prove the compiler RUNS (compile-run skips,
                     no libc-dev); -devel validators prove it can BUILD C++.
  concretize         concretize the ROOT environment and dump the lockfile
                     (FAST GATE -- run this before committing to a long build)
  stack              install ROOT for every cxxstd in CXXSTD_LIST
  originize          rewrite absolute RPATHs to store-relative $ORIGIN
  audit              ELF audit: glibc symbol floor, unresolved DT_NEEDED, rpaths
  buildcache         push the whole install tree to the binary cache volume
  deploy             write per-cxxstd env.sh and a deployment manifest

Custom environments:
  makenv [opts] <spack.yaml>
                     concretize + install an arbitrary Spack environment into the
                     shared install tree and buildcache, in a build container.
                     Checks the container supplies the required non-Spack (OS)
                     dependencies BEFORE the long install.  Options:
                       --image NAME   build container (default: builder;
                                      use a FROM-builder image that adds e.g. CUDA)
                       --name  ENV    environment name under /cvmfs/env
                                      (default: the spack.yaml's parent dir name)
                       --repos DIR    assembled custom package repos, mounted at
                                      $spack/../repos (== /cvmfs/.../repos)
                       --managed      build a NAMED env (spack env list / activate
                                      <name>) instead of a by-path directory env;
                                      the form communities expect for releases
                       --no-check     skip the pre-install container capability gates

  viewgroups [opts] <env-spack.yaml-or-dir>
                     analyze & check a hand-curated grouped spack.yaml (read-only):
                     needs-DAG sanity, effective concretizer:reuse, multi-version
                     packages (link/run vs build-only) and which are unpinned,
                     per-view collisions, and cross-group sharing.
                       --concretize        run 'spack concretize -f' first
                                           (default: reuse the env's spack.lock)
                       --require-single PKG FAIL if PKG has >1 link/run version
                       --strict-single     FAIL on ANY link/run multi-version
                       --report            verbose (consumers, sharing)

Validation (mounts /cvmfs read-only, installs nothing from the distro):
  validate [distro...]   default: alma8 alma9 debian12 debian13 sles15 alpine

Reporting and utility:
  sbom [--format cyclonedx|spdx|text] <env>
                     write a bill of materials (meta/sbom-<env>.*) from an
                     environment's spack.lock and flag the CVE-prone leaves;
                     feed it to grype/trivy/osv-scanner
  report             merge meta/*.json into meta/report.html
  status             one-line summary of every phase that has run
  shell [image]      interactive shell with the volumes mounted (default builder)
  runenv [--image NAME] <env>
                     like shell, but activate a Spack environment first.  <env> is
                     a leaf dir under /cvmfs/.../env (directory env) or a managed
                     env name; --image defaults to builder
  clean              empty the work volume (stage/tmp/test)
  nuke               remove all multispack volumes and images
  all                run the whole pipeline end to end

Configuration lives in ./multispack.conf (see multispack.conf.example).
EOU
}

main() {
    local sub="${1:-help}"; shift || true
    case "$sub" in
        volumes)    stage_run volumes    "create podman volumes"                   -- cmd_volumes ;;
        images)     stage_run images     "build container images"                  -- cmd_images "$@" ;;
        bootstrap)  stage_run bootstrap  "clone Spack $SPACK_REF into $CVMFS_ROOT" -- cmd_bootstrap ;;
        compiler)   stage_run compiler   "build $GCC_SPEC and $GCC_TARGET_SPEC"    -- cmd_compiler ;;
        compiler-validate)
            if [ $# -eq 0 ]; then
                for d in $VALIDATORS $DEVEL_VALIDATORS; do
                    set +e; stage_run "compiler-validate-$d" "$GCC_TARGET_SPEC validation on $d" -- cmd_compiler_validate "$d"; set -e
                done
            else
                for d in "$@"; do
                    set +e; stage_run "compiler-validate-$d" "$GCC_TARGET_SPEC validation on $d" -- cmd_compiler_validate "$d"; set -e
                done
            fi ;;
        concretize) stage_run concretize "concretize the ROOT environment"         -- cmd_concretize ;;
        stack)      stage_run stack      "install ROOT for C++ $CXXSTD_LIST"       -- cmd_stack ;;
        originize)  stage_run originize  "rewrite RPATHs to \$ORIGIN"              -- cmd_originize ;;
        audit)      stage_run audit      "ELF audit"                               -- cmd_audit ;;
        buildcache) stage_run buildcache "push to the binary cache"                -- cmd_buildcache ;;
        deploy)     stage_run deploy     "generate env.sh and manifest"            -- cmd_deploy ;;
        validate)
            if [ $# -eq 0 ]; then
                for d in $VALIDATORS; do
                    set +e; stage_run "validate-$d" "ROOT validation on $d" -- cmd_validate "$d"; set -e
                done
            else
                for d in "$@"; do
                    set +e; stage_run "validate-$d" "ROOT validation on $d" -- cmd_validate "$d"; set -e
                done
            fi ;;
        makenv)     cmd_makenv "$@" ;;
        viewgroups) cmd_viewgroups "$@" ;;
        sbom)       cmd_sbom "$@" ;;
        report)     cmd_report ;;
        status)     cmd_status ;;
        shell)      cmd_shell "$@" ;;
        runenv)     cmd_runenv "$@" ;;
        clean)      cmd_clean ;;
        nuke)       cmd_nuke ;;
        all)        cmd_all ;;
        help|-h|--help) usage ;;
        *)          usage; die "unknown subcommand: $sub" ;;
    esac
}

main "$@"
