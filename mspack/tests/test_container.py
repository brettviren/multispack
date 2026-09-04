# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0
"""Engine argv construction (the one seam that shells out)."""

from pathlib import Path

from mspack.container import Engine


def test_run_argv_order_and_content():
    eng = Engine(engine="podman")
    argv = eng.run_argv(
        "img:1", ["/bin/sh", "-c", "echo hi"],
        mounts=["-v", "vol:/cvmfs"], env={"A": "1", "B": "2"},
        rm=True, interactive=True)
    assert argv[:3] == ["podman", "run", "--rm"]
    assert "-i" in argv
    assert "-v" in argv and "vol:/cvmfs" in argv
    # env rendered as -e K=V
    i = argv.index("A=1")
    assert argv[i - 1] == "-e"
    # image precedes its argv, which comes last
    assert argv.index("img:1") < argv.index("/bin/sh")
    assert argv[-3:] == ["/bin/sh", "-c", "echo hi"]


def test_build_argv():
    eng = Engine(engine="podman")
    argv = eng.build_argv("t:1", Path("/c/Containerfile.builder"), Path("/ctx"),
                          {"BUILDER_BASE": "base:8"})
    assert argv[:2] == ["podman", "build"]
    assert "--build-arg" in argv and "BUILDER_BASE=base:8" in argv
    assert argv[-5:] == ["-t", "t:1", "-f", "/c/Containerfile.builder", "/ctx"]


def test_dry_run_records_no_exec():
    eng = Engine(engine="podman", dry_run=True)
    res = eng.run("img:1", ["true"])
    assert res.returncode == 0
    assert eng.calls and eng.calls[-1][0:2] == ["podman", "run"]
