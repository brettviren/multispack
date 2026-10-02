# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0

"""Scheme 2: convert installed Spack packages into a conda channel (pixi,
micromamba, conda).

Port of multispack.sh conda-export (the local-directory path).  Runs the
`spaxi` tool (via uv, from SPAXI_SRC) in a container against a Spack store,
handing all specs to one `spaxi conda --specs-from -` pass.  The store comes
from one of two sources:

* the build volumes (default): the store volume mounted into the builder image;
* ``--from-image REF``: a runtime image made by ``mspack image`` (a podman image
  reference, or an Apptainer ``.sif`` file).  The store is the image's own
  read-only ``$CVMFS_ROOT``; only the channel dir, a host scratch dir, the
  spaxi source and the uv cache are bound in.  This needs nothing but the image.

Optionally a meta-package (``--meta NAME[=VERSION]``) is added that pins the
env's roots (plus any ``--spec``) and carries the env's activation, so a whole
stack installs with one ``micromamba create ... NAME``.

scp-target draining is not ported here -- for a remote target use
`multispack.sh conda-export host:path`.
"""

import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from . import register
from .base import DeployMethod, DeployRequest

# The in-container conversion.  CE_MODE=volume (the build store volume in the
# builder image, unchanged from multispack.sh conda-export) or CE_MODE=image (a
# runtime image's baked, read-only store).  Set up spack, pick the spec set (env
# specs + extra seeds / seeds / whole store), build the spaxi venv once, hand
# every spec to a single `spaxi conda --specs-from -`, then optionally build a
# meta-package of the env roots + seeds.
_SCRIPT = r'''
W="${CE_WORK:-/multispack/work}"
if [ "$CE_MODE" = image ]; then
    # The image ENV puts its Spack view first on PATH and sets PYTHONPATH /
    # LD_LIBRARY_PATH into it: neither spaxi's own python nor Spack may see that.
    unset PYTHONPATH PYTHONHOME PYTHONSTARTUP LD_LIBRARY_PATH SPACK_ENV SPACK_ENV_VIEW
    export PATH="$W/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    : "${CVMFS_ROOT:=${SPACK_ROOT%/spack}}"
    export CVMFS_ROOT HOME="$W/home" UV_CACHE_DIR="${CE_UVCACHE:-$W/uv-cache}" \
           UV_PYTHON_INSTALL_DIR="$W/uv-python"
else
    . "$CVMFS_ROOT/spack/share/spack/setup-env.sh"
    export HOME=/root UV_CACHE_DIR=/root/.cache/uv
fi
export SPACK_ROOT="$CVMFS_ROOT/spack" SPACK_DISABLE_LOCAL_CONFIG=1 \
       SPACK_USER_CACHE_PATH="$W/spack-user-cache" TMPDIR="$W/tmp" \
       UV_PROJECT_ENVIRONMENT="$W/spaxi-venv" UV_LINK_MODE=copy
mkdir -p "$TMPDIR" "$HOME" "$SPACK_USER_CACHE_PATH"
SP="$SPACK_ROOT/bin/spack"
SEEDS=("$@")
# --with-compiler: the image's shipped compiler, by the hash of its real install.
if [ -n "$CE_WITH_COMPILER" ]; then
    CS="${MSPACK_GCC_PREFIX:-}/.spack/spec.json"
    [ -f "$CS" ] || { echo "export-conda: no shipped compiler in this image (MSPACK_GCC_PREFIX)" >&2; exit 2; }
    SEEDS+=("$(/usr/bin/python3 -c 'import json,sys; n=json.load(open(sys.argv[1]))["spec"]["nodes"][0]; print(n["name"]+"/"+n["hash"])' "$CS")")
fi
set -- "${SEEDS[@]}"
DEPS=--deps ER=
if [ -n "$CONDA_ENV" ]; then
    leaf="${CONDA_ENV##*/}"; ER="$CONDA_ENV"; [ -d "$CVMFS_ROOT/env/$leaf" ] && ER="$CVMFS_ROOT/env/$leaf"
    # From an image: the env's ROOTS (+ seeds) and their runtime closure, so a
    # store-built compiler's bootstrap closure is reached only via its seed (and
    # gets relaxed pins).  The volume mode keeps converting every env spec.
    FX=; [ "$CE_MODE" = image ] && FX=-x
    set -- $("$SP" -e "$ER" find $FX --format "{name}/{hash}" 2>/dev/null) "${SEEDS[@]}"
elif [ "$#" -eq 0 ]; then
    DEPS=--no-deps
    set -- $("$SP" find --format "{name}/{hash}" 2>/dev/null)
fi
[ "$#" -gt 0 ] || { echo "export-conda: no specs to convert" >&2; exit 2; }
echo "export-conda: converting $# spec(s) from $CVMFS_ROOT" >&2
# spaxi (setuptools) writes egg-info at build; its source is mounted read-only,
# so copy the build essentials to a writable dir.
SPX="$W/spaxi-src"; rm -rf "$SPX"; mkdir -p "$SPX"
( cd /spaxi && cp -a pyproject.toml uv.lock src "$SPX"/ 2>/dev/null
  cp -a README* LICENSE* NOTICE* "$SPX"/ 2>/dev/null || true )
uv sync --frozen --no-dev --project "$SPX" >&2 || { echo "spaxi venv build failed" >&2; exit 1; }
SPAXI="$UV_PROJECT_ENVIRONMENT/bin/spaxi"
[ -x "$SPAXI" ] || { echo "spaxi not built at $SPAXI" >&2; exit 1; }
echo "export-conda: handing $# spec(s) to spaxi" >&2
GOPTS=(--spack-exe "$SP" --channel /out)
[ -n "$CE_LOG_SINK" ]  && GOPTS+=(-l "$CE_LOG_SINK")
[ -n "$CE_LOG_LEVEL" ] && GOPTS+=(-L "$CE_LOG_LEVEL")
COPTS=("$DEPS" -j "$CE_JOBS")
[ -n "$CE_ZLEVEL" ] && COPTS+=(-z "$CE_ZLEVEL")
printf "%s\n" "$@" | "$SPAXI" "${GOPTS[@]}" conda "${COPTS[@]}" --specs-from -
rc=$?
if [ -n "$CE_META" ]; then
    MOPTS=(meta "$CE_META")
    [ -n "$CE_META_VERSION" ] && MOPTS+=(--version "$CE_META_VERSION")
    [ -n "$ER" ] && MOPTS+=(--spack-env "$ER")
    echo "export-conda: meta-package $CE_META ${CE_META_VERSION}" >&2
    printf "%s\n" "${SEEDS[@]}" | "$SPAXI" "${GOPTS[@]}" "${MOPTS[@]}" --specs-from - || rc=1
fi
exit $rc
'''

# Where the from-image binds land in the container.
_IMG_WORK = "/mspack-work"
_IMG_UVCACHE = "/mspack-uvcache"


def _uv_bin() -> str:
    return shutil.which("uv") or os.path.expanduser("~/.local/bin/uv")


def _uv_cache(uv: str) -> str:
    try:
        out = subprocess.run([uv, "cache", "dir"], capture_output=True,
                             text=True).stdout.strip()
    except OSError:
        out = ""
    return out or os.path.expanduser("~/.cache/uv")


def is_sif(ref: str) -> bool:
    """A ``--from-image`` value names an Apptainer SIF (not a podman ref) when
    it ends in ``.sif`` or is an existing file."""
    return ref.endswith(".sif") or Path(ref).is_file()


def default_workdir(ref: str) -> Path:
    """``$TMPDIR/mspack-conda-<leaf>`` for an image ref or SIF path."""
    leaf = Path(ref).name if is_sif(ref) else ref.rsplit("/", 1)[-1]
    leaf = re.sub(r"[^A-Za-z0-9._-]+", "-", leaf).strip("-") or "image"
    root = os.environ.get("TMPDIR") or tempfile.gettempdir()
    return Path(root) / f"mspack-conda-{leaf}"


def parse_meta(meta: str | None) -> tuple[str, str]:
    """``NAME[=VERSION]`` -> (name, version); version defaults to today."""
    if not meta:
        return "", ""
    name, _, version = meta.partition("=")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", name):
        raise ValueError(f"export-conda: --meta name must be a lowercase "
                         f"conda package name: '{name}'")
    return name, version or datetime.now().strftime("%Y.%m.%d")


@register("conda")
class CondaDeploy(DeployMethod):
    name = "conda"

    def run(self, cfg, engine, request: DeployRequest) -> None:
        src = Path(cfg.get("SPAXI_SRC"))
        if not (src / "pyproject.toml").is_file():
            raise FileNotFoundError(
                f"export-conda: no spaxi source at '{src}' (set SPAXI_SRC)")
        uv = _uv_bin()
        if not os.access(uv, os.X_OK):
            raise FileNotFoundError(f"export-conda: uv not found at '{uv}'")

        opts = request.options
        from_image = opts.get("from_image")
        if from_image and opts.get("image"):
            raise ValueError("export-conda: --image and --from-image are exclusive")
        meta_name, meta_version = parse_meta(opts.get("meta"))
        if opts.get("with_compiler") and not from_image:
            raise ValueError("export-conda: --with-compiler needs --from-image "
                             "(the image records its shipped compiler); with the "
                             "volumes, pass --spec gcc/<hash>")
        if meta_name and not (request.env or request.specs
                              or opts.get("with_compiler")):
            raise ValueError("export-conda: --meta needs --env and/or --spec "
                             "(the packages it pins)")

        dest = Path(request.dest).resolve()
        dest.mkdir(parents=True, exist_ok=True)
        uvcache = _uv_cache(uv)
        os.makedirs(uvcache, exist_ok=True)

        jobs = opts.get("jobs", 0)
        env = {
            "CONDA_ENV": request.env or "",
            "CE_JOBS": str(jobs),
            "CE_LOG_SINK": opts.get("log_sink") or "",
            "CE_LOG_LEVEL": opts.get("log_level") or "",
            "CE_META": meta_name,
            "CE_META_VERSION": meta_version,
            "CE_ZLEVEL": str(opts.get("zlevel") or ""),
            "CE_WITH_COMPILER": "1" if opts.get("with_compiler") else "",
        }
        what = "everything installed"
        if request.env:
            what = f"env '{request.env}'" + (
                f" + specs {list(request.specs)}" if request.specs else "")
            if opts.get("with_compiler"):
                what += " + its compiler"
        elif request.specs:
            what = f"specs {list(request.specs)}"
        elif opts.get("with_compiler"):
            what = "the image's compiler"
        argv_tail = ["sh"] + list(request.specs)

        if from_image:
            res = self._run_from_image(cfg, engine, from_image, dest, src, uv,
                                       uvcache, env, argv_tail, what, jobs,
                                       opts.get("workdir"))
        else:
            res = self._run_volume(cfg, engine, opts.get("image"), dest, src,
                                   uv, uvcache, env, argv_tail, what, jobs)
        if res.returncode != 0:
            print(f"[mspack] export-conda: some specs failed to convert "
                  f"(rc={res.returncode}); channel at {dest}")
        else:
            print(f"[mspack] export-conda: channel at {dest} "
                  f"(file://{dest} or serve it over HTTP)")

    # -- the build store volumes (builder image) ----------------------------
    def _run_volume(self, cfg, engine, image, dest, src, uv, uvcache, env,
                    argv_tail, what, jobs):
        img = cfg.image(image or cfg.get("MAKENV_IMAGE", "builder"))
        if not engine.image_exists(img):
            raise FileNotFoundError(f"export-conda: image not built: {img}")
        sel = cfg.sel
        mounts = cfg.volume_mounts(ro=False)
        mounts += ["-v", f"{dest}:/out{sel}"]
        mounts += ["-v", f"{src}:/spaxi:ro{sel}"]
        mounts += ["-v", f"{uv}:/usr/local/bin/uv:ro{sel}"]
        mounts += ["-v", f"{uvcache}:/root/.cache/uv{sel}"]
        cenv = dict(cfg.container_env())
        cenv.update(env)
        cenv["CE_MODE"] = "volume"
        print(f"[mspack] export-conda: {what} -> {dest} (jobs={jobs})")
        return engine.run(img, ["/bin/bash", "-lc", _SCRIPT] + argv_tail,
                          mounts=mounts, env=cenv, rm=True, interactive=True,
                          check=False)

    # -- a runtime image's baked store (podman ref or SIF) ------------------
    def _run_from_image(self, cfg, engine, ref, dest, src, uv, uvcache, env,
                        argv_tail, what, jobs, workdir):
        sif = is_sif(ref)
        if sif:
            ref = str(Path(ref).resolve())
            if not Path(ref).is_file() and not engine.dry_run:
                raise FileNotFoundError(f"export-conda: no such SIF: {ref}")
        elif not engine.image_exists(ref):
            raise FileNotFoundError(f"export-conda: image not available: {ref}")
        wd = Path(workdir).resolve() if workdir else default_workdir(ref)
        (wd / "bin").mkdir(parents=True, exist_ok=True)
        # The host uv goes into the scratch dir: one bind fewer, and no file
        # mount point needed inside a read-only image or SIF.
        uv_dst = wd / "bin" / "uv"
        if not engine.dry_run:
            if (not uv_dst.exists()
                    or uv_dst.stat().st_size != os.stat(uv).st_size):
                shutil.copy2(uv, uv_dst)
        cenv = dict(env)
        cenv.update({"CE_MODE": "image", "CE_WORK": _IMG_WORK,
                     "CE_UVCACHE": _IMG_UVCACHE})
        binds = [(str(dest), "/out", ""), (str(wd), _IMG_WORK, ""),
                 (str(src), "/spaxi", "ro"), (uvcache, _IMG_UVCACHE, "")]
        script = ["/bin/bash", "-c", _SCRIPT] + argv_tail
        print(f"[mspack] export-conda: {what} from {'SIF' if sif else 'image'} "
              f"{ref} -> {dest} (jobs={jobs}, workdir={wd})")
        if sif:
            argv = ["apptainer", "exec", "--containall"]
            for host, cont, mode in binds:
                argv += ["--bind", f"{host}:{cont}" + (f":{mode}" if mode else "")]
            for k, v in cenv.items():
                argv += ["--env", f"{k}={v}"]
            argv += [ref] + script
            return engine._exec(argv, check=False)
        sel = cfg.sel
        mounts: list[str] = []
        for host, cont, mode in binds:
            opt = ",".join(x for x in (mode, sel.lstrip(":")) if x)
            mounts += ["-v", f"{host}:{cont}" + (f":{opt}" if opt else "")]
        # --read-only: the image's /cvmfs (and everything else outside the
        # binds) is immutable, proving the export writes nothing into it.
        return engine.run(ref, script, mounts=mounts, env=cenv, rm=True,
                          interactive=True, extra=["--read-only"], check=False)
