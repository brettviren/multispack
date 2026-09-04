# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0
"""Phase metadata records are stage_run-compatible."""

import json

import pytest

from mspack.config import Config
from mspack.meta import phase_order, stage


def _cfg(tmp_path):
    (tmp_path / "multispack.sh").write_text("#!/bin/sh\n")
    return Config.load(root=tmp_path, conf=tmp_path / "none.conf")


def test_phase_order_matches_shell():
    assert phase_order("volumes") == 10
    assert phase_order("compiler") == 40
    assert phase_order("compiler-validate-alma8") == 46
    assert phase_order("validate-debian12") == 96
    assert phase_order("something-else") == 50


def test_stage_writes_ok_record(tmp_path):
    cfg = _cfg(tmp_path)
    with stage(cfg, "volumes", "seed") as h:
        h.returncode = 0
    rec = json.loads((cfg.meta_dir / "10-volumes.json").read_text())
    assert rec["phase"] == "volumes" and rec["status"] == "ok"
    assert rec["order"] == 10
    assert rec["config"]["target"] == "x86_64_v3"
    assert rec["config"]["padded_length"] == 128


def test_stage_records_failure_and_reraises(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(RuntimeError):
        with stage(cfg, "compiler", "build"):
            raise RuntimeError("boom")
    rec = json.loads((cfg.meta_dir / "40-compiler.json").read_text())
    assert rec["status"] == "fail"
    assert rec["returncode"] != 0
