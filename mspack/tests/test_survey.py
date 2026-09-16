# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0
"""Survey: catalog collation, rendering, and mechanical probe argv."""

import json

from mspack import survey
from mspack.container import Engine

_CATALOG = {
    "alma8": {"distro": "AlmaLinux", "release": "8", "glibc": "2.28",
              "gcc": "8.5", "family": "rpm", "pkg_manager": "dnf",
              "image": "docker.io/library/almalinux:8", "eol": "2029-05"},
    "debian12": {"distro": "Debian", "release": "12", "glibc": "2.36",
                 "gcc": "12", "family": "deb", "pkg_manager": "apt",
                 "image": "docker.io/library/debian:12", "eol": "2026-07"},
    "sles15sp6": {"distro": "SLES", "release": "15-SP6", "glibc": "2.38",
                  "gcc": "7.5", "family": "rpm", "pkg_manager": "zypper",
                  "image": "registry.suse.com/bci/bci-base:15.6"},
}

_YAML = """schema: 1
updated: "2026-09"
distros:
  alma8:
    distro: AlmaLinux
    release: "8"
    glibc: "2.28"
    gcc: "8.5"
    family: rpm
    pkg_manager: dnf
    image: docker.io/library/almalinux:8
  debian12:
    distro: Debian
    release: "12"
    glibc: "2.36"
    gcc: "12"
    family: deb
    pkg_manager: apt
    image: docker.io/library/debian:12
"""


def _catalog_root(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "distros.yaml").write_text(_YAML)
    return tmp_path


def test_glibc_key_orders_numerically():
    assert survey.glibc_key("2.9") < survey.glibc_key("2.28")
    assert survey.glibc_key("2.28") < survey.glibc_key("2.36")


def test_collate_support_math():
    rows = survey.collate(_CATALOG)
    floors = [r["glibc"] for r in rows]
    assert floors == ["2.28", "2.36", "2.38"]           # ascending

    row28 = rows[0]
    # A 2.28 floor runs everywhere (all glibc >= 2.28), sorted by glibc.
    assert row28["runs_on"] == ["alma8", "debian12", "sles15sp6"]
    assert row28["provided_by"] == ["alma8"]
    assert row28["bootstrappable"] is True               # alma8 = dnf

    # A 2.38 floor (SLES) runs only on itself; SLES is not bootstrappable.
    row38 = rows[-1]
    assert row38["runs_on"] == ["sles15sp6"]
    assert row38["bootstrappable"] is False              # zypper only


def test_report_text_and_json():
    text = survey.report(_CATALOG)
    assert "glibc 2.28" in text and "AlmaLinux 8" in text
    assert "⚑" in text                                    # bootstrap marker

    rows = json.loads(survey.report(_CATALOG, as_json=True))
    assert rows[0]["glibc"] == "2.28"


def test_table_text_and_json():
    text = survey.table(_CATALOG)
    assert "alma8" in text and "docker.io/library/debian:12" in text
    assert "*alma8" in text                               # base marker

    data = json.loads(survey.table(_CATALOG, as_json=True))
    assert data["debian12"]["glibc"] == "2.36"


def test_load_catalog_missing_is_empty(tmp_path):
    assert survey.load_catalog(tmp_path) == {}


def test_load_catalog_reads_file(tmp_path):
    cat = survey.load_catalog(_catalog_root(tmp_path))
    assert set(cat) == {"alma8", "debian12"}
    assert cat["alma8"]["glibc"] == "2.28"


def test_probe_builds_run_argv_and_no_mutation_on_dry_run(tmp_path):
    root = _catalog_root(tmp_path)
    before = (root / "config" / "distros.yaml").read_text()
    eng = Engine(dry_run=True)
    results = survey.probe(root, eng, ["alma8", "debian12"])

    # A run per named distro, against its image, with getconf in the script.
    run_calls = [c for c in eng.calls if len(c) > 1 and c[1] == "run"]
    assert len(run_calls) == 2
    joined = " ".join(run_calls[0])
    assert "docker.io/library/almalinux:8" in joined
    assert "GNU_LIBC_VERSION" in joined

    # dry-run yields no output, so nothing is "changed" and the file is intact.
    assert all(r["status"] == "up to date" for r in results)
    assert (root / "config" / "distros.yaml").read_text() == before


def test_probe_writes_changed_fields(tmp_path, monkeypatch):
    """A probe that returns new values rewrites them, keeping YAML comments."""
    root = _catalog_root(tmp_path)

    class FakeEngine:
        def run(self, image, argv, **kw):
            from mspack.container import RunResult
            # Pretend the image reports a different glibc than the catalog.
            return RunResult(["run"], 0, "glibc=2.99\ngcc=42\n")

    results = survey.probe(root, FakeEngine(), ["alma8"])
    assert any("2.28 -> 2.99" in r["status"] for r in results)
    text = (root / "config" / "distros.yaml").read_text()
    assert "2.99" in text
    assert "schema:" in text                              # header preserved
