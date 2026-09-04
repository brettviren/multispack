# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0

"""Phase timing + provenance records, byte-compatible with multispack.sh.

Each phase writes ``meta/<order>-<phase>.json`` (and a ``.log``) with the same
schema ``stage_run`` writes, so the existing ``bin/report.py`` reads mspack runs
and shell runs interchangeably.
"""

import json
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# Fixed phase ordering (== phase_order in multispack.sh) so a report sorts.
_ORDER = {
    "volumes": 10, "images": 20, "bootstrap": 30, "compiler": 40,
    "compiler-validate": 45, "concretize": 50, "stack": 60, "originize": 70,
    "audit": 80, "buildcache": 90, "deploy": 95, "report": 99,
}


def phase_order(phase: str) -> int:
    if phase in _ORDER:
        return _ORDER[phase]
    if phase.startswith("compiler-validate-"):
        return 46
    if phase.startswith("validate-"):
        return 96
    return 50


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class StageError(RuntimeError):
    """A staged phase exited non-zero."""


@contextmanager
def stage(cfg, phase: str, description: str, command: str = ""):
    """Time a phase, then write meta/<order>-<phase>.json.

    Yields a small object with ``.log_path`` so the body can tee container
    output to the phase log.  On exception the record is still written with
    status=fail and the exception re-raised.
    """
    order = phase_order(phase)
    meta = cfg.meta_dir
    meta.mkdir(parents=True, exist_ok=True)
    log_rel = f"{order}-{phase}.log"

    class _Handle:
        log_path = meta / log_rel
        returncode = 0

    handle = _Handle()
    t0 = time.time()
    started = _iso()
    status = "ok"
    exc: Exception | None = None
    try:
        yield handle
    except Exception as e:  # record the failure, then re-raise
        exc = e
        status = "fail"
        if handle.returncode == 0:
            handle.returncode = 1
    finally:
        finished = _iso()
        record = {
            "order": order,
            "phase": phase,
            "description": description,
            "status": status,
            "returncode": handle.returncode,
            "started": started,
            "finished": finished,
            "duration_s": int(time.time() - t0),
            "command": command,
            "log": log_rel,
            "config": {
                "cvmfs_root": cfg["CVMFS_ROOT"],
                "spack_ref": cfg["SPACK_REF"],
                "target": cfg["TARGET"],
                "gcc_spec": cfg["GCC_SPEC"],
                "gcc_target_spec": cfg["GCC_TARGET_SPEC"],
                "builder_base": cfg["BUILDER_BASE"],
                "padded_length": int(cfg["PADDED_LENGTH"]),
                "cxxstd_list": cfg["CXXSTD_LIST"],
                "base_cxxstd": cfg["BASE_CXXSTD"],
                "build_jobs": cfg.jobs,
            },
        }
        (meta / f"{order}-{phase}.json").write_text(
            json.dumps(record, indent=2) + "\n")
    if exc is not None:
        raise exc
