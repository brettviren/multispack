# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0

"""Deployment methods, discovered through a small registry.

A deployment method turns the built store into something an end user consumes:
a Spack buildcache over HTTP, a conda channel for pixi, and (future) others.
Methods register here by name; third parties can add their own via the
``mspack.deploy`` entry-point group, which is how "empower individuals to offer
their work to other multispack users" lands for deployment.

This module is the seam and the plumbing (destination parsing, registry).  The
heavy container work currently lives in multispack.sh's cache-export /
conda-export / cache-client; those are ported behind this interface method by
method.  See docs/deploy.md.
"""

from dataclasses import dataclass
from importlib import metadata as _metadata

_REGISTRY: dict[str, type] = {}


def register(name: str):
    """Class decorator: register a DeployMethod under ``name``."""
    def deco(cls):
        _REGISTRY[name] = cls
        return cls
    return deco


def available() -> list[str]:
    """Registered method names (built-in plus any entry-point plugins)."""
    _load_entry_points()
    return sorted(_REGISTRY)


def get(name: str) -> type:
    _load_entry_points()
    if name not in _REGISTRY:
        raise KeyError(f"no deploy method '{name}'; have: {', '.join(available())}")
    return _REGISTRY[name]


_ep_loaded = False


def _load_entry_points() -> None:
    global _ep_loaded
    if _ep_loaded:
        return
    _ep_loaded = True
    try:
        eps = _metadata.entry_points(group="mspack.deploy")
    except TypeError:  # py<3.10 selection API
        eps = _metadata.entry_points().get("mspack.deploy", [])
    for ep in eps:
        try:
            _REGISTRY.setdefault(ep.name, ep.load())
        except Exception:  # a broken plugin must not kill the CLI
            pass


@dataclass
class Destination:
    """A deploy target: either a local directory or an ``scp`` host:path."""

    raw: str
    host: str | None = None      # set when scp
    path: str = ""               # remote path (scp) or local dir

    @property
    def is_scp(self) -> bool:
        return self.host is not None


def is_scp(dest: str) -> bool:
    """True if ``dest`` looks like ``[user@]host:path`` and not a local path.

    Mirrors _is_scp in multispack.sh: a ':' before any '/', and the part before
    it is a hostname (no '/'), so absolute/relative local paths never match, but
    a Windows-style drive is not a concern here.
    """
    if ":" not in dest:
        return False
    head = dest.split(":", 1)[0]
    if not head or "/" in head:
        return False
    return True


def parse_dest(dest: str) -> Destination:
    if is_scp(dest):
        host, path = dest.split(":", 1)
        return Destination(raw=dest, host=host, path=path)
    return Destination(raw=dest, host=None, path=dest)


# Import built-in methods so their @register runs.
from . import buildcache as _buildcache  # noqa: E402,F401
from . import conda as _conda  # noqa: E402,F401
