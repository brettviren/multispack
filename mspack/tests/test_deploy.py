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


def _conda_fixture(tmp_path, monkeypatch):
    import mspack.deploy.conda as cmod
    from mspack.config import Config
    (tmp_path / "multispack.sh").write_text("#!/bin/sh\n")
    for sub in ("bin", "config", "tests", "containers"):
        (tmp_path / sub).mkdir(exist_ok=True)
    src = tmp_path / "python" / "spaxi"
    src.mkdir(parents=True, exist_ok=True)
    (src / "pyproject.toml").write_text("[project]\n")
    fake_uv = tmp_path / "uv"
    fake_uv.write_text("#!/bin/sh\n")
    fake_uv.chmod(0o755)
    monkeypatch.setattr(cmod, "_uv_bin", lambda: str(fake_uv))
    monkeypatch.setattr(cmod, "_uv_cache", lambda uv: str(tmp_path / "uvcache"))
    return Config.load(root=tmp_path, conf=tmp_path / "none.conf"), src


def test_conda_export_from_podman_image(tmp_path, monkeypatch):
    from mspack.container import Engine
    from mspack.deploy.base import DeployRequest
    cfg, src = _conda_fixture(tmp_path, monkeypatch)
    eng = Engine(dry_run=True)
    req = DeployRequest(dest=str(tmp_path / "out"), env="phaindia",
                        specs=["gcc@15"], options={
                            "jobs": 48, "from_image": "localhost/p/phaindia:0.1",
                            "workdir": str(tmp_path / "wd"),
                            "meta": "phaindia=0.1", "zlevel": 12,
                            "with_compiler": True})
    deploy.get("conda")().run(cfg, eng, req)
    run = [c for c in eng.calls if c[:2] == ["podman", "run"]][-1]
    joined = " ".join(run)
    assert "--read-only" in run
    assert run[run.index("--read-only") + 1] == "localhost/p/phaindia:0.1"
    assert f"{(tmp_path / 'out').resolve()}:/out" in joined
    assert f"{(tmp_path / 'wd').resolve()}:/mspack-work" in joined
    assert f"{src}:/spaxi:ro" in joined
    assert f"{tmp_path / 'uvcache'}:/mspack-uvcache" in joined
    # no build volumes, no build-config env in image mode
    assert "/multispack/cache" not in joined and "CVMFS_ROOT=" not in joined
    assert "CE_MODE=image" in run and "CONDA_ENV=phaindia" in run
    assert "CE_META=phaindia" in run and "CE_META_VERSION=0.1" in run
    assert "CE_ZLEVEL=12" in run and "CE_WITH_COMPILER=1" in run
    assert run[-2:] == ["sh", "gcc@15"]            # extra seeds after the script
    assert run[-5:-3] == ["/bin/bash", "-c"]


def test_conda_export_from_sif(tmp_path, monkeypatch):
    from mspack.container import Engine
    from mspack.deploy.base import DeployRequest
    cfg, src = _conda_fixture(tmp_path, monkeypatch)
    sif = tmp_path / "phaindia.sif"
    sif.write_bytes(b"")
    eng = Engine(dry_run=True)
    req = DeployRequest(dest=str(tmp_path / "out"), env="phaindia", options={
        "jobs": 4, "from_image": str(sif)})
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    deploy.get("conda")().run(cfg, eng, req)
    run = eng.calls[-1]
    assert run[:3] == ["apptainer", "exec", "--containall"]
    assert str(sif) in run
    wd = tmp_path / "tmp" / "mspack-conda-phaindia.sif"
    assert f"{wd}:/mspack-work" in run
    assert f"{src}:/spaxi:ro" in run
    assert "CE_MODE=image" in run and "CE_JOBS=4" in run


def test_conda_export_volume_mode_unchanged(tmp_path, monkeypatch):
    from mspack.container import Engine
    from mspack.deploy.base import DeployRequest
    cfg, _ = _conda_fixture(tmp_path, monkeypatch)
    eng = Engine(dry_run=True)
    deploy.get("conda")().run(cfg, eng, DeployRequest(
        dest=str(tmp_path / "out"), env="e", options={"jobs": 2}))
    run = [c for c in eng.calls if c[:2] == ["podman", "run"]][-1]
    assert "CE_MODE=volume" in run and "--read-only" not in run
    assert "multispack-cvmfs:/cvmfs" in " ".join(run)
    assert run[-4:-2] == ["/bin/bash", "-lc"]


def test_conda_export_option_checks(tmp_path, monkeypatch):
    from mspack.container import Engine
    from mspack.deploy.base import DeployRequest
    cfg, _ = _conda_fixture(tmp_path, monkeypatch)
    m = deploy.get("conda")()
    with pytest.raises(ValueError):
        m.run(cfg, Engine(dry_run=True), DeployRequest(
            dest=str(tmp_path / "o"), options={"image": "b", "from_image": "x:1"}))
    with pytest.raises(ValueError):      # compiler lookup needs an image
        m.run(cfg, Engine(dry_run=True), DeployRequest(
            dest=str(tmp_path / "o"), env="e", options={"with_compiler": True}))
    with pytest.raises(ValueError):      # meta with nothing to pin
        m.run(cfg, Engine(dry_run=True), DeployRequest(
            dest=str(tmp_path / "o"), options={"meta": "phaindia"}))


def test_conda_helpers(tmp_path, monkeypatch):
    from mspack.deploy import conda as cmod
    assert cmod.is_sif("x/y.sif") and not cmod.is_sif("localhost/a/b:1")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    assert cmod.default_workdir("localhost/phaindia/phaindia:0.1-2026") == \
        tmp_path / "mspack-conda-phaindia-0.1-2026"
    assert cmod.parse_meta("phaindia=0.1") == ("phaindia", "0.1")
    name, ver = cmod.parse_meta("phaindia")
    assert name == "phaindia" and ver.count(".") == 2
    assert cmod.parse_meta(None) == ("", "")
    with pytest.raises(ValueError):
        cmod.parse_meta("Bad Name")
