# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0

"""Scheme 1: export the Spack binary buildcache for HTTP serving.

Port of multispack.sh cache-export.  A full export copies the whole
content-addressed cache; an --env/--spec subset is re-pushed fresh with
``spack buildcache push``.  An scp destination is tar-streamed so no full local
copy is kept.

STATUS: scaffold.  The destination parsing and the plan below are in place; the
container-side copy/push is being ported from the shell (which remains the
working implementation in the meantime -- run ``multispack.sh cache-export``).
"""

from . import register
from .base import DeployMethod, DeployRequest


@register("buildcache")
class BuildcacheDeploy(DeployMethod):
    name = "buildcache"

    def run(self, cfg, engine, request: DeployRequest) -> None:
        raise NotImplementedError(
            "buildcache export is not yet ported to mspack; use "
            "`multispack.sh cache-export` (see docs/deploy.md)")
