# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0
"""Phases drive the engine with the right image, mounts, env and script."""

import json

from mspack.config import Config
from mspack.container import Engine
from mspack import phases


def _repo(tmp_path):
    (tmp_path / "multispack.sh").write_text("#!/bin/sh\n")
    for sub in ("bin", "config", "tests"):
        (tmp_path / sub).mkdir()
    cdir = tmp_path / "containers"
    cdir.mkdir()
    for name in ("builder", "alma8"):
        (cdir / f"Containerfile.{name}").write_text("FROM scratch\n")
    return Config.load(root=tmp_path, conf=tmp_path / "none.conf")


def test_volumes_seeds_with_builder_base(tmp_path):
    cfg = _repo(tmp_path)
    eng = Engine(dry_run=True)
    phases.volumes(cfg, eng)
    run = [c for c in eng.calls if c[:2] == ["podman", "run"]][-1]
    assert cfg["BUILDER_BASE"] in run
    assert "multispack-cvmfs:/cvmfs" in " ".join(run)
    assert any("mkdir -p" in a for a in run)
    assert (cfg.meta_dir / "10-volumes.json").is_file()


def test_images_builds_each_and_writes_detail(tmp_path):
    cfg = _repo(tmp_path)
    eng = Engine(dry_run=True)
    phases.images(cfg, eng, ["builder", "alma8"])
    builds = [c for c in eng.calls if c[:2] == ["podman", "build"]]
    assert len(builds) == 2
    tags = [b[b.index("-t") + 1] for b in builds]
    assert cfg.image("builder") in tags and cfg.image("alma8") in tags
    assert "BUILDER_BASE=" + cfg["BUILDER_BASE"] in " ".join(builds[0])
    detail = json.loads((cfg.meta_dir / "images.detail.json").read_text())
    assert {i["name"] for i in detail["images"]} == {"builder", "alma8"}


def test_images_unknown_containerfile_raises(tmp_path):
    cfg = _repo(tmp_path)
    eng = Engine(dry_run=True)
    try:
        phases.images(cfg, eng, ["nope"])
        assert False, "expected FileNotFoundError"
    except FileNotFoundError:
        pass


def test_bootstrap_delegates_to_phase_script(tmp_path):
    cfg = _repo(tmp_path)
    eng = Engine(dry_run=True)
    phases.bootstrap(cfg, eng)
    run = [c for c in eng.calls if c[:2] == ["podman", "run"]][-1]
    assert cfg.builder_img in run
    assert "/opt/multispack/bin/phase-bootstrap.sh" in run
    # build environment is passed through
    assert "-e" in run and any(a.startswith("MULTISPACK_TARGET=") for a in run)
    assert "/opt/multispack/bin:ro" in " ".join(run)   # dev mount present
    assert (cfg.meta_dir / "30-bootstrap.json").is_file()


def test_compiler_delegates_to_phase_script(tmp_path):
    cfg = _repo(tmp_path)
    eng = Engine(dry_run=True)
    phases.compiler(cfg, eng)
    run = [c for c in eng.calls if c[:2] == ["podman", "run"]][-1]
    assert "/opt/multispack/bin/phase-compiler.sh" in run
    assert any(a.startswith("GCC_TARGET_SPEC=") for a in run)


def _env_yaml(tmp_path, dirname="myenv"):
    d = tmp_path / dirname
    d.mkdir()
    y = d / "spack.yaml"
    y.write_text("spack:\n  specs: [emacs]\n")
    return y


def test_makenv_wires_input_mount_and_env(tmp_path):
    cfg = _repo(tmp_path)
    yaml = _env_yaml(tmp_path)
    eng = Engine(dry_run=True)
    phases.makenv(cfg, eng, str(yaml))
    run = [c for c in eng.calls if c[:2] == ["podman", "run"]][-1]
    joined = " ".join(run)
    assert cfg.builder_img in run
    assert "/opt/multispack/bin/phase-makenv.sh" in run
    assert f"{yaml.parent}:/multispack/input:ro" in joined
    assert "MAKENV_YAML=spack.yaml" in run
    assert "MAKENV_NAME=myenv" in run            # defaults to parent dir name
    assert "MAKENV_MANAGED=0" in run
    assert "MAKENV_NOCHECK=0" in run
    assert "-i" in run                           # interactive
    # the standard build env is present too
    assert any(a.startswith("MULTISPACK_TARGET=") for a in run)


def test_makenv_explicit_name_repos_managed(tmp_path):
    cfg = _repo(tmp_path)
    yaml = _env_yaml(tmp_path)
    repos = tmp_path / "recipes"
    repos.mkdir()
    eng = Engine(dry_run=True)
    phases.makenv(cfg, eng, str(yaml), name="rel-2026", repos=str(repos),
                  managed=True, nocheck=True)
    run = [c for c in eng.calls if c[:2] == ["podman", "run"]][-1]
    joined = " ".join(run)
    assert "MAKENV_NAME=rel-2026" in run
    assert "MAKENV_MANAGED=1" in run
    assert "MAKENV_NOCHECK=1" in run
    assert f"{repos}:{cfg.cvmfs_root}/repos:ro" in joined
    assert f"MAKENV_REPOS={repos}" in run


def test_makenv_bad_name_rejected(tmp_path):
    cfg = _repo(tmp_path)
    yaml = _env_yaml(tmp_path)
    eng = Engine(dry_run=True)
    try:
        phases.makenv(cfg, eng, str(yaml), name="bad/name")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_makenv_missing_yaml(tmp_path):
    cfg = _repo(tmp_path)
    eng = Engine(dry_run=True)
    try:
        phases.makenv(cfg, eng, str(tmp_path / "nope.yaml"))
        assert False, "expected FileNotFoundError"
    except FileNotFoundError:
        pass
