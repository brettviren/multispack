# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0

"""Monolithic runtime images: ``mspack image`` (OCI, via podman) and
``mspack sif`` (Apptainer).

``image`` bakes ONE built Spack environment -- its install closure, the Spack
clone + site config, the env dir with its view and lock, and the recipe repos
-- into a single extra layer on top of a runtime base (by default the
``<BASE_DISTRO>-devel`` validator, so the shipped gcc can compile).  Spack keeps
working inside the image, read-only: ``spack find``, ``spack load``,
``spack env activate`` and (with the clingo bootstrap baked in) concretization.

How, and why this way:

1. ``spack bootstrap now`` runs IN THE BASE IMAGE with the store volume
   mounted, into ``$CVMFS_ROOT/bootstrap``: clingo is per-python, and in the
   image Spack runs on the base's python, not the builder's.
2. ``bin/image-plan.py`` (``spack -e ENV python``, in the builder) chooses what
   ships and renders the files that must differ in a read-only image (pruned DB,
   site config without volumes/mirror/locks, repo destinations, profile.d).
3. ``bin/image-payload.sh`` (same container) writes that as one tar layer.
4. Here, on the host, the layer is appended to the base image's docker-archive
   (from ``podman save``) and the result streamed into ``podman load``.

No ``podman build`` is used: with podman <= 4.3 a build commit stages each layer
in /var/tmp regardless of TMPDIR, which a many-GB payload cannot fit, while
``podman load`` honors the configured temp dir.  Everything runs rootless.
"""

import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .container import ContainerError, Engine
from .meta import stage

_SCOPES = ("closure", "all")
_COMPILERS = ("target", "all", "none")


def _work_root() -> Path:
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir())


def resolve_base(cfg: Config, base: str | None) -> str:
    """``--base``: a bare name is an mspack image (``IMG_PREFIX/NAME:IMG_TAG``);
    anything with a '/' or ':' is a full image reference."""
    if not base:
        base = f"{cfg.base_distro or 'alma9'}-devel"
    if "/" in base or ":" in base:
        return base
    return cfg.image(base)


def spack_base_tag(cfg: Config, base_img: str) -> str:
    """Tag of the base + Spack-runtime-tools image derived from ``base_img``."""
    prefix = f"{cfg['IMG_PREFIX']}/"
    if base_img.startswith(prefix):
        name = base_img[len(prefix):].split(":", 1)[0]
    else:
        name = "base-" + hashlib.sha256(base_img.encode()).hexdigest()[:10]
    return cfg.image(f"{name}-spack")


def build_spack_base(cfg: Config, engine: Engine, base_img: str,
                     log_path: Path | None = None) -> str:
    """Build (layer-cached, small) ``<base>-spack``: base + git/python3/..."""
    tag = spack_base_tag(cfg, base_img)
    cf = cfg.root / "containers" / "Containerfile.spack-runtime"
    print(f"[mspack] image: runtime base {tag} (= {base_img} + Spack's OS tools)")
    engine.build(tag, cf, cfg.root, build_args={"BASE_IMG": base_img})
    return tag


def default_tag(cfg: Config, env: str) -> str:
    return f"{cfg['IMG_PREFIX']}/{env}:{datetime.now().strftime('%Y%m%d')}"


def _git_commit(root: Path) -> str:
    try:
        head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain",
                                "--untracked-files=no"],
                               capture_output=True, text=True).stdout.strip()
    except OSError:
        return "unknown"
    return (head or "unknown") + ("-dirty" if dirty else "")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


_PROBE_SCRIPT = (
    'echo "BASE_PATH=$PATH"; '
    'PY="$(command -v python3 || command -v python)" && '
    'echo "BASE_PYTHON=$PY" && '
    '"$PY" -c "import sys; print(\'BASE_PYVER=%d.%d\' % sys.version_info[:2])"; true'
)

# Bootstrap runs in the BUILDER (Spack's bootstrap needs git, which a bare
# runtime base lacks) but with an interpreter of the BASE's python version:
# clingo is per python X.Y, and in the image Spack runs on the base's python.
_BOOTSTRAP_SCRIPT = r'''
set -e
PY="$(command -v "python$IMG_PYVER" || true)"
if [ -z "$PY" ]; then
    for c in /usr/bin/python3 /usr/local/bin/python3; do
        [ -x "$c" ] && [ "$("$c" -c 'import sys;print("%d.%d"%sys.version_info[:2])')" = "$IMG_PYVER" ] && PY="$c" && break
    done
fi
[ -n "$PY" ] || { echo "image: the builder has no python$IMG_PYVER to bootstrap clingo for the base image (use --no-bootstrap)" >&2; exit 3; }
echo "[image] bootstrapping clingo for $("$PY" --version 2>&1) into $CVMFS_ROOT/bootstrap" >&2
export SPACK_PYTHON="$PY" SPACK_DISABLE_LOCAL_CONFIG=1 \
       SPACK_USER_CACHE_PATH=/multispack/work/spack-user-cache TMPDIR=/multispack/work/tmp
mkdir -p "$TMPDIR"
"$CVMFS_ROOT/spack/bin/spack" -c "bootstrap:root:$CVMFS_ROOT/bootstrap" bootstrap now
"$CVMFS_ROOT/spack/bin/spack" -c "bootstrap:root:$CVMFS_ROOT/bootstrap" find -b 2>/dev/null | tail -n +1 >&2 || true
'''


_PLAN_SCRIPT = r'''
. /opt/multispack/bin/common.sh
use_spack
E="$CVMFS_ROOT/env/$IMG_ENV"; [ -d "$E" ] || E="$IMG_ENV"
spack -e "$E" python /opt/multispack/bin/image-plan.py
/opt/multispack/bin/image-payload.sh
'''


def image(cfg: Config, engine: Engine, env: str, *, base: str | None = None,
          tag: str | None = None, scope: str = "closure",
          compilers: str = "target", repos=(), bootstrap: bool = True,
          spack_deps: bool = True, workdir: str | None = None,
          keep: bool = False) -> dict:
    """Build a monolithic OCI image of env ``env``; returns the record.

    With ``spack_deps`` (default) the payload goes over ``<base>-spack``, the
    base plus the OS tools Spack needs to run (Containerfile.spack-runtime).
    """
    from .phases import parse_repos, repo_mounts, _ENV_NAME_RE

    if not _ENV_NAME_RE.match(env):
        raise ValueError(f"image: --env must be a simple name: '{env}'")
    if scope not in _SCOPES:
        raise ValueError(f"image: scope must be one of {_SCOPES}")
    if compilers not in _COMPILERS:
        raise ValueError(f"image: --compilers must be one of {_COMPILERS}")
    base_img = resolve_base(cfg, base)
    tag = tag or default_tag(cfg, env)
    R = cfg.cvmfs_root
    for img in (cfg.builder_img, base_img):
        if not engine.image_exists(img):
            raise FileNotFoundError(f"image: image not available: {img}")
    parsed = parse_repos(list(repos))
    wd = Path(workdir) if workdir else _work_root() / f"mspack-image-{env}"
    if wd.exists() and not engine.dry_run:
        shutil.rmtree(wd)
    wd.mkdir(parents=True, exist_ok=True)

    record: dict = {"tag": tag, "base": base_img, "env": env}
    with stage(cfg, f"image-{env}", f"monolithic image {tag} of env {env}",
               command=f"mspack image --env {env}") as h:
        if spack_deps:
            base_img = build_spack_base(cfg, engine, base_img)
            record["base"] = base_img
        mounts = cfg.volume_mounts(ro=False)
        rm_mounts, _ = repo_mounts(cfg, parsed)
        mounts += rm_mounts

        # 1. what the base offers (PATH, python), then clingo for that python.
        spack_python, base_path, pyver = "/usr/bin/python3", "", ""
        res = engine.run(base_img, ["/bin/sh", "-c", _PROBE_SCRIPT], rm=True,
                         capture=True)
        for line in (res.output or "").splitlines():
            k, _, v = line.partition("=")
            if k == "BASE_PATH":
                base_path = v.strip()
            elif k == "BASE_PYTHON":
                spack_python = v.strip()
            elif k == "BASE_PYVER":
                pyver = v.strip()
        if not pyver and not engine.dry_run:
            raise ContainerError(f"image: base {base_img} has no python3; Spack "
                                 f"cannot run in it (pick another --base)")
        if bootstrap:
            print(f"[mspack] image: bootstrap clingo for python {pyver or '?'} "
                  f"into {R}/bootstrap")
            benv = dict(cfg.container_env())
            benv["IMG_PYVER"] = pyver
            engine.run(cfg.builder_img, ["/bin/bash", "-c", _BOOTSTRAP_SCRIPT],
                       mounts=mounts, env=benv, rm=True)

        # 2+3. plan + payload layer, in the builder.
        print(f"[mspack] image: plan + payload for env '{env}' (scope={scope}, "
              f"compilers={compilers}) -> {wd}")
        penv = dict(cfg.container_env())
        penv.update({"IMG_ENV": env, "IMG_SCOPE": scope,
                     "IMG_COMPILERS": compilers, "IMG_OUT": "/out",
                     "IMG_PYTHON": spack_python,
                     "IMG_BASE_PATH": base_path or
                     "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"})
        engine.run(cfg.builder_img, ["/bin/bash", "-c", _PLAN_SCRIPT],
                   mounts=mounts + ["-v", f"{wd}:/out{cfg.sel}"], env=penv,
                   rm=True, log_path=h.log_path)
        if engine.dry_run:
            return record

        plan = json.loads((wd / "plan.json").read_text())
        payload = wd / "payload.tar"
        labels = {
            "org.opencontainers.image.title": f"mspack env {env}",
            "org.opencontainers.image.created": plan["record"]["created"],
            "org.opencontainers.image.revision": _git_commit(cfg.root),
            "org.opencontainers.image.base.name": record["base"],
            "io.mspack.env": env,
            "io.mspack.cvmfs-root": R,
            "io.mspack.scope": scope,
            "io.mspack.spack-lock.sha256": plan["record"]["spack_lock_sha256"],
            "io.mspack.spack.commit": plan["record"]["spack_commit"],
            "io.mspack.multispack.commit": _git_commit(cfg.root),
            "io.mspack.view": plan["record"].get("view") or "",
        }
        # 4. base docker-archive + payload layer -> podman load.
        print(f"[mspack] image: assembling {tag} "
              f"({payload.stat().st_size / 2**30:.2f} GiB payload)")
        layer_sha = _sha256(payload)
        base_tar = wd / "base.docker.tar"
        engine._exec([engine.engine, "save", "--format", "docker-archive",
                      "-o", str(base_tar), base_img])
        _load_with_layer(engine, base_tar, payload, layer_sha, tag,
                         env_vars=plan["env"], labels=labels)
        img_id = engine.image_id(tag)
        record.update(plan["record"])
        record.update({"image_id": img_id, "labels": labels,
                       "payload_bytes": payload.stat().st_size,
                       "layer_sha256": layer_sha})
        (cfg.meta_dir / f"image-{env}.detail.json").write_text(
            json.dumps(record, indent=2) + "\n")
        print(f"[mspack] image: built {tag} ({img_id[:12]})")
    if not keep:
        shutil.rmtree(wd, ignore_errors=True)
    else:
        print(f"[mspack] image: kept work dir {wd}")
    return record


def _load_with_layer(engine: Engine, base_tar: Path, payload: Path,
                     layer_sha: str, tag: str, *, env_vars: dict,
                     labels: dict) -> None:
    """Append ``payload`` to the docker-archive ``base_tar`` and ``podman load``
    the result, streamed (no second on-disk copy of the payload)."""
    with tarfile.open(base_tar) as bt:
        manifest = json.load(bt.extractfile("manifest.json"))
        if len(manifest) != 1:
            raise ContainerError("image: base archive holds more than one image")
        m = manifest[0]
        cfgdoc = json.load(bt.extractfile(m["Config"]))
        layers = list(m["Layers"])
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        cc = cfgdoc.setdefault("config", {})
        envd = dict(e.split("=", 1) for e in (cc.get("Env") or []) if "=" in e)
        envd.update(env_vars)
        cc["Env"] = [f"{k}={v}" for k, v in envd.items()]
        lab = dict(cc.get("Labels") or {})
        lab.update(labels)
        cc["Labels"] = lab
        cc["WorkingDir"] = "/work"
        cc["Cmd"] = ["/bin/bash", "-l"]
        cc.pop("Entrypoint", None)
        cfgdoc["created"] = now
        cfgdoc.setdefault("rootfs", {"type": "layers", "diff_ids": []})
        cfgdoc["rootfs"]["diff_ids"].append(f"sha256:{layer_sha}")
        cfgdoc.setdefault("history", []).append({
            "created": now, "created_by": f"mspack image {labels.get('io.mspack.env')}",
            "comment": "Spack env payload (one layer)"})
        cfgdoc.pop("container_config", None)
        cfgbytes = json.dumps(cfgdoc).encode()
        cfgname = hashlib.sha256(cfgbytes).hexdigest() + ".json"
        layername = f"{layer_sha}/layer.tar"
        newman = json.dumps([{"Config": cfgname, "RepoTags": [tag],
                              "Layers": layers + [layername]}]).encode()

        argv = [engine.engine, "load"]
        engine.calls.append(argv)
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE)
        try:
            with tarfile.open(fileobj=proc.stdin, mode="w|",
                              format=tarfile.PAX_FORMAT) as out:
                for name in layers:
                    ti = bt.getmember(name)
                    if ti.issym():         # docker-archive may symlink dup layers
                        out.addfile(ti)
                        continue
                    out.addfile(ti, bt.extractfile(ti))

                def add_bytes(name, data):
                    ti = tarfile.TarInfo(name)
                    ti.size = len(data)
                    ti.mtime = int(time.time())
                    out.addfile(ti, io.BytesIO(data))
                ti = tarfile.TarInfo(f"{layer_sha}")
                ti.type = tarfile.DIRTYPE
                ti.mode = 0o755
                out.addfile(ti)
                ti = out.gettarinfo(str(payload), arcname=layername)
                ti.uid = ti.gid = 0
                ti.uname = ti.gname = ""
                with open(payload, "rb") as pf:
                    out.addfile(ti, pf)
                add_bytes(cfgname, cfgbytes)
                add_bytes("manifest.json", newman)
        finally:
            if proc.stdin:
                try:
                    proc.stdin.close()
                except BrokenPipeError:
                    pass
            rc = proc.wait()
    if rc != 0:
        raise ContainerError(f"image: podman load failed (rc={rc})")


def sif(cfg: Config, engine: Engine, image_ref: str, out: str | None = None, *,
        workdir: str | None = None, force: bool = False,
        apptainer: str = "apptainer") -> Path:
    """Convert a podman image to an Apptainer SIF (via an oci-archive)."""
    if not engine.image_exists(image_ref):
        raise FileNotFoundError(f"sif: no such image: {image_ref}")
    if out is None:
        leaf = image_ref.rsplit("/", 1)[-1].replace(":", "-")
        out = f"{leaf}.sif"
    outp = Path(out).resolve()
    if outp.exists() and not force:
        raise FileExistsError(f"sif: {outp} exists (use --force)")
    wd = Path(tempfile.mkdtemp(prefix="mspack-sif-",
                               dir=workdir or str(_work_root())))
    try:
        oci = wd / "image.oci.tar"
        print(f"[mspack] sif: podman save {image_ref} -> {oci}")
        engine._exec([engine.engine, "save", "--format", "oci-archive",
                      "-o", str(oci), image_ref])
        env = dict(os.environ)
        env.setdefault("APPTAINER_TMPDIR", str(wd / "tmp"))
        env.setdefault("APPTAINER_CACHEDIR", str(wd / "cache"))
        for k in ("APPTAINER_TMPDIR", "APPTAINER_CACHEDIR"):
            Path(env[k]).mkdir(parents=True, exist_ok=True)
        argv = [apptainer, "build"] + (["--force"] if force else []) + \
            [str(outp), f"oci-archive://{oci}"]
        print("[mspack] sif: " + " ".join(argv))
        engine.calls.append(argv)
        if not engine.dry_run:
            rc = subprocess.run(argv, env=env).returncode
            if rc != 0:
                raise ContainerError(f"sif: apptainer build failed (rc={rc})")
            print(f"[mspack] sif: wrote {outp} "
                  f"({outp.stat().st_size / 2**30:.2f} GiB)")
    finally:
        shutil.rmtree(wd, ignore_errors=True)
    return outp
