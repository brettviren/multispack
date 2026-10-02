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
import re
from pathlib import Path

from .config import Config
from .container import Engine
from .meta import stage

# A makenv env name must be a single, simple path component (it becomes a dir
# under /cvmfs/.../env or a managed env name).
_ENV_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

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
            if name.startswith("/"):
                # A host directory bind-mounted in place of a named volume
                # (`-v /abs:/cvmfs`): the store lands directly on the host.
                if not engine.dry_run:
                    Path(name).mkdir(parents=True, exist_ok=True)
                print(f"[mspack] host directory {name} (bind mount, not a volume)")
            elif engine.volume_exists(name):
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
            # The builder is the one image whose Containerfile depends on the
            # base distro's package manager (dnf vs apt); everything else is a
            # fixed Containerfile.<name>.  The tag stays 'builder' either way.
            if name == "builder":
                cf = cfg.builder_containerfile()
            else:
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
                script: str, extra_env: dict | None = None) -> None:
    """Run a bin/phase-*.sh inside the builder (== in_builder in the shell)."""
    env = cfg.container_env()
    if extra_env:
        env.update(extra_env)
    with stage(cfg, phase, description, command=script) as h:
        engine.run(cfg.builder_img, [script],
                   mounts=cfg.volume_mounts(ro=False),
                   env=env, rm=True, interactive=True,
                   log_path=h.log_path)


def bootstrap(cfg: Config, engine: Engine) -> None:
    """Clone Spack into /cvmfs, install site config, bootstrap clingo."""
    _in_builder(cfg, engine, "bootstrap",
                f"clone Spack {cfg['SPACK_REF']} into {cfg.cvmfs_root}",
                "/opt/multispack/bin/phase-bootstrap.sh")


def compiler(cfg: Config, engine: Engine, extra_specs=()) -> None:
    """Build the self-hosted GCC_SPEC base + GCC_TARGET_SPEC, plus any extra
    compiler specs (each built from the base; a spec older than the base is
    best-effort with a warning)."""
    extra = " ".join(extra_specs)
    desc = f"build base {cfg['GCC_SPEC']} + {cfg['GCC_TARGET_SPEC']}"
    if extra:
        desc += f" + extras: {extra}"
    _in_builder(cfg, engine, "compiler", desc,
                "/opt/multispack/bin/phase-compiler.sh",
                extra_env={"EXTRA_GCC_SPECS": extra})


def compiler_validate(cfg: Config, engine: Engine,
                      names: list[str] | None = None) -> list[str]:
    """Run tests/run-compiler-validation.sh in each validator image (== the
    shell's compiler-validate): /cvmfs read-only, nothing from the distro.

    Each distro gets its own ``compiler-validate-<d>`` record; returns the list
    of distros that failed (and raises if any did, after running them all).
    """
    want = names or (cfg.validators + cfg.devel_validators)
    failed: list[str] = []
    for d in want:
        img = cfg.image(d)
        try:
            with stage(cfg, f"compiler-validate-{d}",
                       f"run {cfg['GCC_TARGET_SPEC']} validation on {d}",
                       command="run-compiler-validation.sh") as h:
                if not engine.image_exists(img):
                    raise FileNotFoundError(
                        f"compiler-validate: image not built: {img} "
                        f"(mspack images {d})")
                engine.run(img, ["/bin/sh",
                                 "/opt/multispack/tests/run-compiler-validation.sh"],
                           mounts=cfg.volume_mounts(ro=True),
                           env={"CVMFS_ROOT": cfg.cvmfs_root, "DISTRO": d,
                                "GCC_TARGET_SPEC": cfg["GCC_TARGET_SPEC"]},
                           rm=True, log_path=h.log_path)
        except Exception as err:            # keep going; report at the end
            print(f"[mspack] compiler validation on {d} failed: {err}")
            failed.append(d)
    if failed:
        raise RuntimeError(f"compiler-validate failed on: {' '.join(failed)}")
    return failed


def parse_repos(specs) -> list[tuple[str, Path]]:
    """Resolve ``makenv --repos`` values to ``(name, abs_dir)`` pairs.

    Each value is ``DIR`` (mounted at ``$CVMFS_ROOT/repos/<basename DIR>``) or
    ``NAME=DIR`` (mounted at ``$CVMFS_ROOT/repos/NAME``).  The legacy whole-tree
    form ``=DIR`` (empty NAME) mounts DIR *as* ``$CVMFS_ROOT/repos``; it must be
    the only value, because nothing can be bind-mounted inside a read-only bind.
    """
    out: list[tuple[str, Path]] = []
    for raw in specs or ():
        if "=" in raw:
            name, d = raw.split("=", 1)
        else:
            name, d = None, raw
        path = Path(d).expanduser().resolve()
        if not path.is_dir():
            raise FileNotFoundError(f"makenv: --repos is not a directory: {d}")
        if name is None:
            name = path.name
        if name and not _ENV_NAME_RE.match(name):
            raise ValueError(
                f"makenv: --repos NAME must be a simple path component: '{name}'")
        out.append((name, path))
    names = [n for n, _ in out]
    if "" in names and len(out) > 1:
        raise ValueError("makenv: --repos =DIR (whole repos tree) cannot be "
                         "combined with other --repos values")
    dups = sorted({n for n in names if names.count(n) > 1})
    if dups:
        raise ValueError(f"makenv: duplicate --repos mount name(s): {', '.join(dups)} "
                         f"(use NAME=DIR to disambiguate)")
    return out


def repo_mounts(cfg: Config, repos) -> tuple[list[str], str]:
    """``-v`` args for parsed ``repos`` plus the MAKENV_REPOS record string."""
    sel = cfg.sel
    mounts: list[str] = []
    rec: list[str] = []
    for name, path in repos:
        tgt = f"{cfg.cvmfs_root}/repos" + (f"/{name}" if name else "")
        mounts += ["-v", f"{path}:{tgt}:ro{sel}"]
        rec.append(f"{name}={path}")
        print(f"[mspack] makenv: mounting repos {path} -> {tgt}")
    return mounts, " ".join(rec)


def makenv(cfg: Config, engine: Engine, yaml, *, image: str | None = None,
           name: str | None = None, repos=None,
           managed: bool | None = None, nocheck: bool = False) -> None:
    """Concretize + install an arbitrary Spack environment into the shared store.

    Delegates the build to bin/phase-makenv.sh in the build image.  The whole
    env DIRECTORY (not just the yaml) is mounted at /multispack/input so relative
    ``include:``/``repos:`` files come along; MAKENV_YAML names the manifest.
    ``repos`` (a list, or one string) are local recipe-repo clones, each bound
    read-only at ``$CVMFS_ROOT/repos/<name>`` (see :func:`parse_repos`) -- a dev
    override for git ``repos:``.  Not a fixed-order phase, so it writes no
    numbered meta record (the phase script still writes
    meta/makenv-<name>.detail.json); output streams live.
    """
    image = image or cfg.get("MAKENV_IMAGE", "builder")
    yaml_path = Path(yaml).resolve()
    if not yaml_path.is_file():
        raise FileNotFoundError(f"makenv: no such file: {yaml}")

    if name is None:                         # default: the yaml's parent dir name
        name = yaml_path.parent.name or "custom"
    if not _ENV_NAME_RE.match(name):
        raise ValueError(
            f"makenv: --name must be a simple path component: '{name}'")
    if isinstance(repos, str):
        repos = [repos]
    parsed = parse_repos(repos)

    img = cfg.image(image)
    if not engine.image_exists(img):
        raise FileNotFoundError(
            f"makenv: image not built: {img} (build it: mspack images {image})")

    if managed is None:
        managed = cfg.get("MAKENV_MANAGED", "0") == "1"

    sel = cfg.sel
    mounts = cfg.volume_mounts(ro=False)
    rmounts, repos_rec = repo_mounts(cfg, parsed)
    mounts += rmounts
    mounts += ["-v", f"{yaml_path.parent}:/multispack/input:ro{sel}"]

    env = dict(cfg.container_env())
    env.update({
        "MAKENV_YAML": yaml_path.name,
        "MAKENV_NAME": name,
        "MAKENV_NOCHECK": "1" if nocheck else "0",
        "MAKENV_IMAGE": image,
        "MAKENV_REPOS": repos_rec,
        "MAKENV_MANAGED": "1" if managed else "0",
    })
    print(f"[mspack] makenv: build env '{name}' from {yaml_path} in image '{image}'")
    engine.run(img, ["/opt/multispack/bin/phase-makenv.sh"],
               mounts=mounts, env=env, rm=True, interactive=True)
