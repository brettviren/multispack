# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0
"""mspack image / sif: wiring and the docker-archive assembly."""

import hashlib
import io
import json
import tarfile

import pytest

from mspack import image as image_mod
from mspack.config import Config
from mspack.container import Engine


def _cfg(tmp_path, **ov):
    (tmp_path / "multispack.sh").write_text("#!/bin/sh\n")
    for sub in ("bin", "config", "tests"):
        (tmp_path / sub).mkdir(exist_ok=True)
    return Config.load(root=tmp_path, conf=tmp_path / "none.conf", overrides=ov)


def test_resolve_base_and_tag(tmp_path):
    cfg = _cfg(tmp_path, BASE_DISTRO="alma9", IMG_PREFIX="localhost/phaindia")
    assert image_mod.resolve_base(cfg, None) == "localhost/phaindia/alma9-devel:1"
    assert image_mod.resolve_base(cfg, "debian12-devel") == \
        "localhost/phaindia/debian12-devel:1"
    assert image_mod.resolve_base(cfg, "docker.io/library/almalinux:9") == \
        "docker.io/library/almalinux:9"
    assert image_mod.default_tag(cfg, "smoke").startswith("localhost/phaindia/smoke:")


def test_image_dry_run_wiring(tmp_path):
    cfg = _cfg(tmp_path)
    repo = tmp_path / "xerosere-spack"
    repo.mkdir()
    eng = Engine(dry_run=True)
    image_mod.image(cfg, eng, "smoke", repos=[str(repo)],
                    workdir=str(tmp_path / "wd"))
    runs = [c for c in eng.calls if c[:2] == ["podman", "run"]]
    assert len(runs) == 3
    probe, boot, plan = runs
    builds = [c for c in eng.calls if c[:2] == ["podman", "build"]]
    assert builds and "BASE_IMG=" + cfg.image("alma8-devel") in builds[0]
    assert cfg.image("alma8-devel-spack") in probe   # BASE_DISTRO default alma8
    assert cfg.builder_img in boot                   # bootstrap needs git
    assert any("bootstrap now" in a for a in boot)
    j = " ".join(plan)
    assert cfg.builder_img in plan
    assert f"{tmp_path / 'wd'}:/out" in j
    assert f"{repo}:{cfg.cvmfs_root}/repos/xerosere-spack:ro" in j
    assert "IMG_ENV=smoke" in plan and "IMG_SCOPE=closure" in plan
    assert (cfg.meta_dir / "50-image-smoke.json").is_file()


def test_image_rejects_bad_args(tmp_path):
    cfg = _cfg(tmp_path)
    eng = Engine(dry_run=True)
    with pytest.raises(ValueError):
        image_mod.image(cfg, eng, "a/b")
    with pytest.raises(ValueError):
        image_mod.image(cfg, eng, "ok", scope="most")
    with pytest.raises(ValueError):
        image_mod.image(cfg, eng, "ok", compilers="some")


def _tar_bytes(members: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, data in members.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def test_load_with_layer_appends_one_layer(tmp_path):
    base_layer = _tar_bytes({"etc/os-release": b"ID=test\n"})
    base_cfg = {"config": {"Env": ["PATH=/usr/bin", "LANG=C"], "Cmd": ["sh"]},
                "rootfs": {"type": "layers",
                           "diff_ids": ["sha256:" + hashlib.sha256(base_layer).hexdigest()]},
                "history": [{"created_by": "base"}]}
    base_tar = tmp_path / "base.tar"
    base_tar.write_bytes(_tar_bytes({
        "aaa.tar": base_layer,
        "cfg.json": json.dumps(base_cfg).encode(),
        "manifest.json": json.dumps([{"Config": "cfg.json", "RepoTags": ["b:1"],
                                      "Layers": ["aaa.tar"]}]).encode()}))
    payload = tmp_path / "payload.tar"
    payload.write_bytes(_tar_bytes({"cvmfs/x/spack/README": b"hi\n"}))
    sha = hashlib.sha256(payload.read_bytes()).hexdigest()

    sink = tmp_path / "loaded.tar"
    fake = tmp_path / "fakepodman"
    fake.write_text(f"#!/bin/sh\n[ \"$1\" = load ] && cat > {sink}\n")
    fake.chmod(0o755)
    eng = Engine(engine=str(fake))
    image_mod._load_with_layer(eng, base_tar, payload, sha, "localhost/t/smoke:1",
                               env_vars={"PATH": "/view/bin:/usr/bin", "SPACK_ROOT": "/s"},
                               labels={"io.mspack.env": "smoke"})
    with tarfile.open(sink) as t:
        man = json.load(t.extractfile("manifest.json"))[0]
        assert man["RepoTags"] == ["localhost/t/smoke:1"]
        assert man["Layers"] == ["aaa.tar", f"{sha}/layer.tar"]
        assert t.extractfile(man["Layers"][1]).read() == payload.read_bytes()
        cfgdata = t.extractfile(man["Config"]).read()
        assert man["Config"] == hashlib.sha256(cfgdata).hexdigest() + ".json"
        c = json.loads(cfgdata)
    assert c["rootfs"]["diff_ids"][-1] == f"sha256:{sha}"
    assert len(c["rootfs"]["diff_ids"]) == 2
    env = c["config"]["Env"]
    assert "PATH=/view/bin:/usr/bin" in env and "LANG=C" in env and "SPACK_ROOT=/s" in env
    assert c["config"]["Labels"]["io.mspack.env"] == "smoke"
    assert c["config"]["WorkingDir"] == "/work"
    assert len(c["history"]) == 2


def test_sif_dry_run(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    eng = Engine(dry_run=True)
    out = image_mod.sif(cfg, eng, "localhost/t/smoke:1", str(tmp_path / "s.sif"),
                        workdir=str(tmp_path))
    assert out == tmp_path / "s.sif"
    saves = [c for c in eng.calls if c[:2] == ["podman", "save"]]
    assert saves and "oci-archive" in saves[0]
    builds = [c for c in eng.calls if c[:2] == ["apptainer", "build"]]
    assert builds and builds[0][-1].startswith("oci-archive://")
