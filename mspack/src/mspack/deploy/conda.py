# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0

"""Scheme 2: convert the installed store into a conda channel for pixi.

Port of multispack.sh conda-export.  It runs the `spaxi` tool (via uv, from
SPAXI_SRC) inside the builder against the store, handing all specs to one
`spaxi conda --specs-from -` pass; an scp destination is drained during
conversion.  spaxi itself already parallelizes the batch over one pool.

STATUS: scaffold.  Registered so the plugin seam is exercised; the container
invocation is being ported from the shell (which remains the working
implementation -- run ``multispack.sh conda-export``).
"""

from . import register
from .base import DeployMethod, DeployRequest


@register("conda")
class CondaDeploy(DeployMethod):
    name = "conda"

    def run(self, cfg, engine, request: DeployRequest) -> None:
        raise NotImplementedError(
            "conda export is not yet ported to mspack; use "
            "`multispack.sh conda-export` (see docs/deploy.md)")
