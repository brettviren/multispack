# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0

"""The build phases.

``volumes`` and ``images`` are pure engine glue and are implemented natively.
``bootstrap`` and ``compiler`` are the hard, hard-won build logic: mspack
*delegates* them to the existing, validated ``bin/phase-*.sh`` -- run in the
builder image with exactly the mounts and environment multispack.sh uses -- so
there is a single source of build truth during the transition.
"""

import json
from pathlib import Path

from .config import Config
from .container import Engine
from .meta import stage

# The layout seeded onto the volumes (== cmd_volumes in multispack.sh).
_SEED_DIRS = [
    "/multispack/cache/source", "/multispack/cache/buildcache",
    "/multispack/work/stage", "/multispack/work/tmp", "/multispack/work/misc",
    "/multispack/work/test", "/multispack/work/spack-user-cache",
]


def volumes(cfg: Config, engine: Engine) -> None:
    """Create the three podman volumes and seed their directory layout."""
    with stage(cfg, "volumes", "create volumes and seed layout") as h:
        for name in (cfg["VOL_CVMFS"], cfg["VOL_CACHE"], cfg["VOL_WORK"]):
            if engine.volume_exists(name):
                print(f"[mspack] volume {name} already exists")
            else:
                engine.volume_create(name)
                print(f"[mspack] created volume {name}")
        seed = "mkdir -p '{root}' {dirs} && ls -la /cvmfs /multispack/cache /multispack/work".format(
            root=cfg.cvmfs_root, dirs=" ".join(_SEED_DIRS))
        engine.run(cfg["BUILDER_BASE"], ["/bin/sh", "-c", seed],
                   mounts=cfg.volume_mounts(ro=False), rm=True,
                   log_path=h.log_path)


def images(cfg: Config, engine: Engine, names: list[str] | None = None) -> None:
    """Build container images from containers/Containerfile.<name>."""
    want = names or (["builder"] + cfg.validators + cfg.devel_validators)
    with stage(cfg, "images", f"build images: {' '.join(want)}") as h:
        for name in want:
            cf = cfg.root / "containers" / f"Containerfile.{name}"
            if not cf.is_file():
                raise FileNotFoundError(f"no such Containerfile: {cf}")
            img = cfg.image(name)
            print(f"[mspack] building image {img} from {cf}")
            engine.build(img, cf, cfg.root, build_args={
                "BUILDER_BASE": cfg["BUILDER_BASE"],
                "CVMFS_HOST": cfg["CVMFS_HOST"],
                "BUILDER_IMG": cfg.builder_img,
            }, log_path=h.log_path)
        # Record image ids: the "build environment captured" artifact.
        detail = {"phase": "images", "images": [
            {"name": n, "image": cfg.image(n), "id": engine.image_id(cfg.image(n))}
            for n in want]}
        (cfg.meta_dir / "images.detail.json").write_text(
            json.dumps(detail, indent=2) + "\n")


def _in_builder(cfg: Config, engine: Engine, phase: str, description: str,
                script: str) -> None:
    """Run a bin/phase-*.sh inside the builder (== in_builder in the shell)."""
    with stage(cfg, phase, description, command=script) as h:
        engine.run(cfg.builder_img, [script],
                   mounts=cfg.volume_mounts(ro=False),
                   env=cfg.container_env(), rm=True, interactive=True,
                   log_path=h.log_path)


def bootstrap(cfg: Config, engine: Engine) -> None:
    """Clone Spack into /cvmfs, install site config, bootstrap clingo."""
    _in_builder(cfg, engine, "bootstrap",
                f"clone Spack {cfg['SPACK_REF']} into {cfg.cvmfs_root}",
                "/opt/multispack/bin/phase-bootstrap.sh")


def compiler(cfg: Config, engine: Engine) -> None:
    """Build the GCC ladder: GCC_SPEC then GCC_TARGET_SPEC (the payload)."""
    _in_builder(cfg, engine, "compiler",
                f"build {cfg['GCC_SPEC']} then {cfg['GCC_TARGET_SPEC']}",
                "/opt/multispack/bin/phase-compiler.sh")
