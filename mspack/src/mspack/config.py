# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0

"""Configuration for the multispack build.

This mirrors the ``: "${VAR:=default}"`` block at the top of ``multispack.sh``
so that ``mspack`` and the shell agree on every value.  Precedence, lowest to
highest: built-in defaults -> ``multispack.conf`` (the same simple shell
KEY=VALUE file the script sources) -> process environment.

Only the simple ``KEY=VALUE`` / ``KEY='VALUE'`` subset of shell is understood in
the conf file (with ``${VAR}`` expansion against already-resolved values); that
covers the real ``multispack.conf``.  Anything fancier belongs in the
environment or is ignored -- mspack never runs a shell to read it.
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# name -> default, in the order multispack.sh defines them.  Defaults that are
# derived from other values (CVMFS_ROOT, DEPLOY_DIR, ...) are computed in
# Config.load after the flat layer merge, so they are None here.
DEFAULTS: dict[str, str | None] = {
    "ENGINE": "podman",
    "CVMFS_HOST": "multispack.example.org",
    "CVMFS_ROOT": None,                      # /cvmfs/${CVMFS_HOST}
    "SPACK_GIT": "https://github.com/spack/spack.git",
    "SPACK_REF": "v1.2.2",
    "SPACK_PACKAGES_GIT": "https://github.com/spack/spack-packages.git",
    "SPACK_PACKAGES_REF": "v1.2.2",
    "TARGET": "x86_64_v3",
    "BUILDER_BASE": "docker.io/library/almalinux:8",
    "GCC_SPEC": "gcc@14",
    "GCC_LANGS": "c,c++,fortran",
    "GCC_TARGET_SPEC": "gcc@15",
    "GCC_TARGET_VARIANTS": "+binutils",
    "ROOT_PKG": "root",
    "ROOT_VARIANTS": "~x ~opengl ~examples ~tmva",
    "CXXSTD_LIST": "17 23",
    "BASE_CXXSTD": "17",
    "PADDED_LENGTH": "128",
    "ORIGINIZE_RUNPATH": "0",
    "BUILD_JOBS": "0",
    "IMG_PREFIX": "localhost/multispack",
    "IMG_TAG": "1",
    "VOL_CVMFS": "multispack-cvmfs",
    "VOL_CACHE": "multispack-cache",
    "VOL_WORK": "multispack-work",
    "VALIDATORS": "alma8 alma9 debian12 debian13 sles15 alpine",
    "DEVEL_VALIDATORS": "alma8-devel alma9-devel debian12-devel debian13-devel sles15-devel",
    "MAKENV_IMAGE": "builder",
    "DEPLOY_DIR": None,                      # $HERE/deploy
    "SPAXI_SRC": None,                       # $HERE/python/spaxi
    "MAKENV_MANAGED": "0",
    "DEV_MOUNTS": "1",
    "SEL": "",
}

_CONF_LINE = re.compile(r"""^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$""")
_VAR = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


def _unquote(val: str) -> str:
    """Strip a single layer of matching quotes and a trailing ``# comment``."""
    if val[:1] in "\"'" and val[-1:] == val[:1]:
        return val[1:-1]
    # An unquoted value ends at the first ' #' comment.
    hasrun = val.split(" #", 1)[0].rstrip()
    return hasrun


def parse_conf(text: str) -> dict[str, str]:
    """Parse the simple shell KEY=VALUE subset of a multispack.conf."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = _CONF_LINE.match(line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2)
        val = _unquote(raw)
        # Expand ${VAR} against values seen so far (then env, then defaults).
        def sub(mo: "re.Match[str]") -> str:
            name = mo.group(1)
            return out.get(name) or os.environ.get(name) or (DEFAULTS.get(name) or "")
        out[key] = _VAR.sub(sub, val)
    return out


def find_root(start: Path | None = None) -> Path:
    """Locate the multispack repo root (dir holding multispack.sh).

    Honors ``$MULTISPACK_ROOT``; otherwise walks up from ``start`` (cwd).
    """
    env = os.environ.get("MULTISPACK_ROOT")
    if env:
        return Path(env).resolve()
    cur = (start or Path.cwd()).resolve()
    for cand in (cur, *cur.parents):
        if (cand / "multispack.sh").is_file():
            return cand
    return cur


@dataclass
class Config:
    """Resolved multispack configuration (values keyed by their shell names)."""

    root: Path
    values: dict[str, str] = field(default_factory=dict)

    # -- construction -------------------------------------------------------
    @classmethod
    def load(cls, root: Path | None = None, conf: Path | None = None,
             overrides: dict[str, str] | None = None) -> "Config":
        root = (root or find_root()).resolve()
        vals: dict[str, str] = {k: (v if v is not None else "")
                                for k, v in DEFAULTS.items()}
        conf_path = conf if conf is not None else root / "multispack.conf"
        if conf_path and Path(conf_path).is_file():
            for k, v in parse_conf(Path(conf_path).read_text()).items():
                if k in DEFAULTS:
                    vals[k] = v
        for k in DEFAULTS:                       # environment wins
            if k in os.environ:
                vals[k] = os.environ[k]
        if overrides:
            vals.update({k: v for k, v in overrides.items() if k in DEFAULTS})

        # Derived defaults (only when not explicitly set by conf/env/override).
        if not vals.get("CVMFS_ROOT"):
            vals["CVMFS_ROOT"] = f"/cvmfs/{vals['CVMFS_HOST']}"
        if not vals.get("DEPLOY_DIR"):
            vals["DEPLOY_DIR"] = str(root / "deploy")
        if not vals.get("SPAXI_SRC"):
            vals["SPAXI_SRC"] = str(root / "python" / "spaxi")
        return cls(root=root, values=vals)

    # -- typed accessors ----------------------------------------------------
    def __getitem__(self, key: str) -> str:
        return self.values[key]

    def get(self, key: str, default: str = "") -> str:
        return self.values.get(key, default)

    @property
    def engine(self) -> str:
        return self.values["ENGINE"]

    @property
    def cvmfs_root(self) -> str:
        return self.values["CVMFS_ROOT"]

    @property
    def meta_dir(self) -> Path:
        return self.root / "meta"

    @property
    def builder_img(self) -> str:
        return f"{self.values['IMG_PREFIX']}/builder:{self.values['IMG_TAG']}"

    def image(self, name: str) -> str:
        return f"{self.values['IMG_PREFIX']}/{name}:{self.values['IMG_TAG']}"

    @property
    def jobs(self) -> int:
        """Concurrent build jobs: BUILD_JOBS, or this host's CPU count if 0."""
        try:
            n = int(self.values["BUILD_JOBS"])
        except ValueError:
            n = 0
        return n if n > 0 else (os.cpu_count() or 1)

    @property
    def validators(self) -> list[str]:
        return self.values["VALIDATORS"].split()

    @property
    def devel_validators(self) -> list[str]:
        return self.values["DEVEL_VALIDATORS"].split()

    @property
    def dev_mounts(self) -> bool:
        return self.values["DEV_MOUNTS"] == "1"

    @property
    def sel(self) -> str:
        return self.values["SEL"]

    # -- container wiring (mirror of vol_args / env_args in multispack.sh) ---
    def container_env(self) -> dict[str, str]:
        """The environment passed into every build container (== env_args)."""
        v = self.values
        return {
            "CVMFS_HOST": v["CVMFS_HOST"],
            "CVMFS_ROOT": v["CVMFS_ROOT"],
            "SPACK_GIT": v["SPACK_GIT"],
            "SPACK_REF": v["SPACK_REF"],
            "SPACK_PACKAGES_GIT": v["SPACK_PACKAGES_GIT"],
            "SPACK_PACKAGES_REF": v["SPACK_PACKAGES_REF"],
            "MULTISPACK_TARGET": v["TARGET"],
            "GCC_SPEC": v["GCC_SPEC"],
            "GCC_LANGS": v["GCC_LANGS"],
            "GCC_TARGET_SPEC": v["GCC_TARGET_SPEC"],
            "GCC_TARGET_VARIANTS": v["GCC_TARGET_VARIANTS"],
            "ROOT_PKG": v["ROOT_PKG"],
            "ROOT_VARIANTS": v["ROOT_VARIANTS"],
            "CXXSTD_LIST": v["CXXSTD_LIST"],
            "BASE_CXXSTD": v["BASE_CXXSTD"],
            "PADDED_LENGTH": v["PADDED_LENGTH"],
            "ORIGINIZE_RUNPATH": v["ORIGINIZE_RUNPATH"],
            "SPACK_JOBS": str(self.jobs),
            "BUILDER_BASE": v["BUILDER_BASE"],
        }

    def volume_mounts(self, ro: bool = False) -> list[str]:
        """The -v arguments shared by build/validation containers (== vol_args).

        ``ro`` mounts only /cvmfs read-only (the validation contract); otherwise
        the cvmfs, cache and work volumes mount read-write.
        """
        v = self.values
        sel = self.sel
        a: list[str] = []
        if ro:
            a += ["-v", f"{v['VOL_CVMFS']}:/cvmfs:ro{sel}"]
        else:
            a += ["-v", f"{v['VOL_CVMFS']}:/cvmfs{sel}"]
            a += ["-v", f"{v['VOL_CACHE']}:/multispack/cache{sel}"]
            a += ["-v", f"{v['VOL_WORK']}:/multispack/work{sel}"]
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        a += ["-v", f"{self.meta_dir}:/multispack/meta{sel}"]
        if self.dev_mounts:
            a += ["-v", f"{self.root}/bin:/opt/multispack/bin:ro{sel}"]
            a += ["-v", f"{self.root}/config:/opt/multispack/config:ro{sel}"]
            a += ["-v", f"{self.root}/tests:/opt/multispack/tests:ro{sel}"]
        return a
