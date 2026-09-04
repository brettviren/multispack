# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0

"""Scheme 2: convert the installed store into a conda channel for pixi.

Port of multispack.sh conda-export (the local-directory path).  Runs the `spaxi`
tool (via uv, from SPAXI_SRC) inside the build image against the store, handing
all specs to one `spaxi conda --specs-from -` pass.  scp-target draining is not
ported here -- for a remote target use `multispack.sh conda-export host:path`.
"""

import os
import shutil
import subprocess
from pathlib import Path

from . import register
from .base import DeployMethod, DeployRequest

# The in-container conversion, byte-identical to multispack.sh conda-export: set
# up spack, pick the spec set (env roots / seeds / whole store), build the spaxi
# venv once, then hand every spec to a single `spaxi conda --specs-from -`.
_SCRIPT = r'''
. "$CVMFS_ROOT/spack/share/spack/setup-env.sh"
export SPACK_ROOT="$CVMFS_ROOT/spack" SPACK_DISABLE_LOCAL_CONFIG=1 \
       SPACK_USER_CACHE_PATH=/multispack/work/spack-user-cache \
       TMPDIR=/multispack/work/tmp UV_CACHE_DIR=/root/.cache/uv HOME=/root \
       UV_PROJECT_ENVIRONMENT=/multispack/work/spaxi-venv UV_LINK_MODE=copy
mkdir -p "$TMPDIR"
DEPS=--deps
if [ -n "$CONDA_ENV" ]; then
    leaf="${CONDA_ENV##*/}"; ER="$CONDA_ENV"; [ -d "$CVMFS_ROOT/env/$leaf" ] && ER="$CVMFS_ROOT/env/$leaf"
    set -- $(spack -e "$ER" find --format "{name}/{hash}" 2>/dev/null)
elif [ "$#" -eq 0 ]; then
    DEPS=--no-deps
    set -- $(spack find --format "{name}/{hash}" 2>/dev/null)
fi
[ "$#" -gt 0 ] || { echo "export-conda: no specs to convert" >&2; exit 2; }
echo "export-conda: converting $# spec(s)" >&2
# spaxi (setuptools) writes egg-info at build; its source is mounted read-only,
# so copy the build essentials to a writable dir.
SPX=/multispack/work/spaxi-src; rm -rf "$SPX"; mkdir -p "$SPX"
( cd /spaxi && cp -a pyproject.toml uv.lock src "$SPX"/ 2>/dev/null
  cp -a README* LICENSE* NOTICE* "$SPX"/ 2>/dev/null || true )
uv sync --frozen --project "$SPX" >&2 || { echo "spaxi venv build failed" >&2; exit 1; }
SPAXI="$UV_PROJECT_ENVIRONMENT/bin/spaxi"
[ -x "$SPAXI" ] || { echo "spaxi not built at $SPAXI" >&2; exit 1; }
echo "export-conda: handing $# spec(s) to spaxi" >&2
GOPTS=(--spack-exe "$SPACK_ROOT/bin/spack" --channel /out)
[ -n "$CE_LOG_SINK" ]  && GOPTS+=(-l "$CE_LOG_SINK")
[ -n "$CE_LOG_LEVEL" ] && GOPTS+=(-L "$CE_LOG_LEVEL")
printf "%s\n" "$@" | "$SPAXI" "${GOPTS[@]}" conda "$DEPS" -j "$CE_JOBS" --specs-from -
'''


def _uv_bin() -> str:
    return shutil.which("uv") or os.path.expanduser("~/.local/bin/uv")


def _uv_cache(uv: str) -> str:
    try:
        out = subprocess.run([uv, "cache", "dir"], capture_output=True,
                             text=True).stdout.strip()
    except OSError:
        out = ""
    return out or os.path.expanduser("~/.cache/uv")


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

        image = request.options.get("image") or cfg.get("MAKENV_IMAGE", "builder")
        img = cfg.image(image)
        if not engine.image_exists(img):
            raise FileNotFoundError(f"export-conda: image not built: {img}")

        dest = Path(request.dest).resolve()
        dest.mkdir(parents=True, exist_ok=True)
        uvcache = _uv_cache(uv)
        os.makedirs(uvcache, exist_ok=True)

        sel = cfg.sel
        mounts = cfg.volume_mounts(ro=False)
        mounts += ["-v", f"{dest}:/out{sel}"]
        mounts += ["-v", f"{src}:/spaxi:ro{sel}"]
        mounts += ["-v", f"{uv}:/usr/local/bin/uv:ro{sel}"]
        mounts += ["-v", f"{uvcache}:/root/.cache/uv{sel}"]

        jobs = request.options.get("jobs", 0)
        env = dict(cfg.container_env())
        env.update({
            "CONDA_ENV": request.env or "",
            "CE_JOBS": str(jobs),
            "CE_LOG_SINK": request.options.get("log_sink") or "",
            "CE_LOG_LEVEL": request.options.get("log_level") or "",
        })

        what = "everything installed"
        if request.env:
            what = f"env '{request.env}'"
        elif request.specs:
            what = f"specs {list(request.specs)}"
        print(f"[mspack] export-conda: {what} -> {dest} (jobs={jobs})")

        argv = ["/bin/bash", "-lc", _SCRIPT, "sh"] + list(request.specs)
        res = engine.run(img, argv, mounts=mounts, env=env, rm=True,
                         interactive=True, check=False)
        if res.returncode != 0:
            print(f"[mspack] export-conda: some specs failed to convert "
                  f"(rc={res.returncode}); channel at {dest}")
        else:
            print(f"[mspack] export-conda: channel at {dest} "
                  f"(serve it and point pixi at it)")
