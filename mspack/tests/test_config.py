# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0
"""Config: conf parsing, layering precedence, and container wiring."""

from mspack.config import Config, parse_conf


def test_parse_conf_subset():
    text = """
# a comment
ENGINE=podman
BUILD_JOBS=30
VALIDATORS='alma8 alma9 debian12'
GCC_TARGET_VARIANTS="+binutils"
CVMFS_ROOT=/cvmfs/${CVMFS_HOST}
IGNORED_JUNK=whatever   # trailing comment
"""
    got = parse_conf(text)
    assert got["ENGINE"] == "podman"
    assert got["BUILD_JOBS"] == "30"
    assert got["VALIDATORS"] == "alma8 alma9 debian12"
    assert got["GCC_TARGET_VARIANTS"] == "+binutils"
    # ${CVMFS_HOST} expands from the default
    assert got["CVMFS_ROOT"] == "/cvmfs/multispack.example.org"


def _root(tmp_path):
    (tmp_path / "multispack.sh").write_text("#!/bin/sh\n")
    return tmp_path


def test_defaults_and_derived(tmp_path):
    cfg = Config.load(root=_root(tmp_path), conf=tmp_path / "none.conf")
    assert cfg.engine == "podman"
    assert cfg.cvmfs_root == "/cvmfs/multispack.example.org"
    assert cfg.builder_img == "localhost/multispack/builder:1"
    assert cfg.image("alma8") == "localhost/multispack/alma8:1"
    assert cfg.get("DEPLOY_DIR") == str(tmp_path / "deploy")
    assert cfg.get("SPAXI_SRC") == str(tmp_path / "python" / "spaxi")


def test_conf_then_env_precedence(tmp_path, monkeypatch):
    root = _root(tmp_path)
    (root / "multispack.conf").write_text("BUILD_JOBS=30\nGCC_SPEC=gcc@14\n")
    monkeypatch.setenv("GCC_SPEC", "gcc@13")   # env wins over conf
    cfg = Config.load(root=root)
    assert cfg["BUILD_JOBS"] == "30"           # from conf
    assert cfg["GCC_SPEC"] == "gcc@13"         # env overrides conf


def test_jobs_zero_is_cpu_count(tmp_path):
    cfg = Config.load(root=_root(tmp_path), conf=tmp_path / "none.conf",
                      overrides={"BUILD_JOBS": "0"})
    assert cfg.jobs >= 1
    cfg2 = Config.load(root=_root(tmp_path), conf=tmp_path / "none.conf",
                       overrides={"BUILD_JOBS": "7"})
    assert cfg2.jobs == 7


def test_container_env_matches_env_args(tmp_path):
    cfg = Config.load(root=_root(tmp_path), conf=tmp_path / "none.conf")
    env = cfg.container_env()
    # The microarch is exported as MULTISPACK_TARGET, never a bare TARGET.
    assert env["MULTISPACK_TARGET"] == "x86_64_v3"
    assert "TARGET" not in env
    assert env["SPACK_JOBS"] == str(cfg.jobs)
    assert env["CVMFS_ROOT"] == "/cvmfs/multispack.example.org"
    assert set(env) >= {"GCC_SPEC", "GCC_TARGET_SPEC", "ROOT_VARIANTS",
                        "PADDED_LENGTH", "BUILDER_BASE"}


def test_volume_mounts_rw_and_ro(tmp_path):
    cfg = Config.load(root=_root(tmp_path), conf=tmp_path / "none.conf")
    rw = cfg.volume_mounts(ro=False)
    joined = " ".join(rw)
    assert "multispack-cvmfs:/cvmfs" in joined
    assert "multispack-cache:/multispack/cache" in joined
    assert "multispack-work:/multispack/work" in joined
    assert "/opt/multispack/bin:ro" in joined       # DEV_MOUNTS=1 default

    ro = cfg.volume_mounts(ro=True)
    joined = " ".join(ro)
    assert "multispack-cvmfs:/cvmfs:ro" in joined
    assert "/multispack/cache" not in joined         # only cvmfs, read-only
