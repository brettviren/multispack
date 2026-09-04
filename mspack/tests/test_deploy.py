# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0
"""Deploy registry and destination parsing."""

import pytest

from mspack import deploy


def test_builtin_methods_registered():
    names = deploy.available()
    assert "buildcache" in names
    assert "conda" in names


def test_get_unknown_raises():
    with pytest.raises(KeyError):
        deploy.get("nope")


@pytest.mark.parametrize("dest,scp", [
    ("deploy/channel", False),    # ':' absent
    ("/srv/www/spaxi", False),    # local absolute
    ("./rel/path", False),        # local relative
    ("bviren@host:/srv/x", True),
    ("host:/srv/x", True),
    ("host:x", True),
])
def test_is_scp(dest, scp):
    assert deploy.is_scp(dest) is scp


def test_parse_dest_roundtrip():
    d = deploy.parse_dest("bviren@web:/srv/x")
    assert d.is_scp and d.host == "bviren@web" and d.path == "/srv/x"
    d2 = deploy.parse_dest("deploy/channel")
    assert not d2.is_scp and d2.path == "deploy/channel"


def test_method_run_is_stubbed():
    from mspack.deploy.base import DeployRequest
    method = deploy.get("buildcache")()      # buildcache is still a stub
    with pytest.raises(NotImplementedError):
        method.run(None, None, DeployRequest(dest="x"))


def test_conda_export_wires_container(tmp_path, monkeypatch):
    import mspack.deploy.conda as cmod
    from mspack.config import Config
    from mspack.container import Engine
    from mspack.deploy.base import DeployRequest

    (tmp_path / "multispack.sh").write_text("#!/bin/sh\n")
    for sub in ("bin", "config", "tests", "containers"):
        (tmp_path / sub).mkdir()
    src = tmp_path / "python" / "spaxi"
    src.mkdir(parents=True)
    (src / "pyproject.toml").write_text("[project]\n")
    fake_uv = tmp_path / "uv"
    fake_uv.write_text("#!/bin/sh\n")
    fake_uv.chmod(0o755)
    monkeypatch.setattr(cmod, "_uv_bin", lambda: str(fake_uv))
    monkeypatch.setattr(cmod, "_uv_cache", lambda uv: str(tmp_path / "uvcache"))

    cfg = Config.load(root=tmp_path, conf=tmp_path / "none.conf")
    eng = Engine(dry_run=True)
    req = DeployRequest(dest=str(tmp_path / "out"), env="largroups",
                        options={"jobs": 16})
    deploy.get("conda")().run(cfg, eng, req)

    run = [c for c in eng.calls if c[:2] == ["podman", "run"]][-1]
    joined = " ".join(run)
    assert f"{(tmp_path / 'out').resolve()}:/out" in joined
    assert f"{src}:/spaxi:ro" in joined
    assert f"{fake_uv}:/usr/local/bin/uv:ro" in joined
    assert "CONDA_ENV=largroups" in run
    assert "CE_JOBS=16" in run
    assert "CVMFS_ROOT=/cvmfs/multispack.example.org" in run   # container_env passed
    assert "/bin/bash" in run and "spaxi" in joined


def test_conda_export_missing_spaxi_src(tmp_path):
    from mspack.config import Config
    from mspack.container import Engine
    from mspack.deploy.base import DeployRequest
    (tmp_path / "multispack.sh").write_text("#!/bin/sh\n")
    cfg = Config.load(root=tmp_path, conf=tmp_path / "none.conf")
    import pytest
    with pytest.raises(FileNotFoundError):
        deploy.get("conda")().run(cfg, Engine(dry_run=True),
                                  DeployRequest(dest=str(tmp_path / "out")))
