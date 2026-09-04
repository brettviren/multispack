# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0

"""A thin, single-seam wrapper over the container engine (podman/docker).

This is the one place that shells out to the engine, replacing the 17 scattered
``"$ENGINE" run`` call sites in multispack.sh.  It builds argv only -- the
mounts and environment come from :class:`mspack.config.Config` -- so it is
trivially unit-testable by capturing the argv a run *would* execute.
"""

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


class ContainerError(RuntimeError):
    """A container command exited non-zero (or the engine is missing)."""


@dataclass
class RunResult:
    argv: list[str]
    returncode: int
    output: str | None = None  # combined stdout+stderr when captured/teed


@dataclass
class Engine:
    """Build and (optionally) execute engine commands.

    With ``dry_run`` the argv is recorded on :attr:`calls` and nothing runs --
    the hook the tests use to assert on mounts/env without a real podman.
    """

    engine: str = "podman"
    dry_run: bool = False
    calls: list[list[str]] = field(default_factory=list)

    # -- argv construction --------------------------------------------------
    def run_argv(self, image: str, argv: list[str] | None = None, *,
                 mounts: list[str] | None = None,
                 env: dict[str, str] | None = None,
                 rm: bool = True, interactive: bool = False,
                 tty: bool = False, extra: list[str] | None = None) -> list[str]:
        cmd = [self.engine, "run"]
        if rm:
            cmd.append("--rm")
        if interactive:
            cmd.append("-i")
        if tty:
            cmd.append("-t")
        cmd += list(mounts or [])
        for k, v in (env or {}).items():
            cmd += ["-e", f"{k}={v}"]
        cmd += list(extra or [])
        cmd.append(image)
        cmd += list(argv or [])
        return cmd

    def build_argv(self, tag: str, containerfile: Path, context: Path,
                   build_args: dict[str, str] | None = None) -> list[str]:
        cmd = [self.engine, "build"]
        for k, v in (build_args or {}).items():
            cmd += ["--build-arg", f"{k}={v}"]
        cmd += ["-t", tag, "-f", str(containerfile), str(context)]
        return cmd

    # -- execution ----------------------------------------------------------
    def _exec(self, argv: list[str], *, log_path: Path | None = None,
              capture: bool = False, check: bool = True) -> RunResult:
        self.calls.append(argv)
        if self.dry_run:
            print("[dry-run] " + " ".join(argv), file=sys.stderr)
            return RunResult(argv, 0, "" if (capture or log_path) else None)
        if not (capture or log_path):
            proc = subprocess.run(argv)
            res = RunResult(argv, proc.returncode)
        else:
            res = _run_teed(argv, log_path)
        if check and res.returncode != 0:
            raise ContainerError(
                f"command failed (rc={res.returncode}): {' '.join(argv)}")
        return res

    def run(self, image: str, argv: list[str] | None = None, *,
            mounts: list[str] | None = None, env: dict[str, str] | None = None,
            rm: bool = True, interactive: bool = False, tty: bool = False,
            extra: list[str] | None = None, log_path: Path | None = None,
            capture: bool = False, check: bool = True) -> RunResult:
        cmd = self.run_argv(image, argv, mounts=mounts, env=env, rm=rm,
                            interactive=interactive, tty=tty, extra=extra)
        return self._exec(cmd, log_path=log_path, capture=capture, check=check)

    def build(self, tag: str, containerfile: Path, context: Path,
              build_args: dict[str, str] | None = None,
              log_path: Path | None = None, check: bool = True) -> RunResult:
        cmd = self.build_argv(tag, containerfile, context, build_args)
        return self._exec(cmd, log_path=log_path, check=check)

    # -- simple queries -----------------------------------------------------
    def _capture(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        if self.dry_run:
            return 0, ""
        proc = subprocess.run(argv, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True)
        return proc.returncode, proc.stdout.strip()

    def volume_exists(self, name: str) -> bool:
        rc, _ = self._capture([self.engine, "volume", "exists", name])
        return rc == 0

    def image_exists(self, image: str) -> bool:
        rc, _ = self._capture([self.engine, "image", "exists", image])
        return rc == 0

    def volume_create(self, name: str) -> None:
        self._exec([self.engine, "volume", "create", name])

    def image_id(self, image: str) -> str:
        rc, out = self._capture(
            [self.engine, "image", "inspect", "--format", "{{.Id}}", image])
        return out if rc == 0 and out else "unknown"


def _run_teed(argv: list[str], log_path: Path | None) -> RunResult:
    """Run argv, streaming combined output to our stdout and (optionally) a log."""
    buf: list[str] = []
    fh = None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(log_path, "w")
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            buf.append(line)
            if fh:
                fh.write(line)
        proc.wait()
    finally:
        if fh:
            fh.close()
    return RunResult(argv, proc.returncode, "".join(buf))
