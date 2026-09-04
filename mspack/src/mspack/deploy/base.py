# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0

"""The DeployMethod interface."""

from dataclasses import dataclass, field


@dataclass
class DeployRequest:
    """What to deploy and where.

    ``env``/``specs`` scope the export (empty means "everything installed");
    ``dest`` is a local dir or scp host:path; ``options`` carries method-specific
    knobs (jobs, channel, log level, ...).
    """

    dest: str
    env: str | None = None
    specs: list[str] = field(default_factory=list)
    options: dict = field(default_factory=dict)


class DeployMethod:
    """Base class for a deployment method.

    Subclasses set ``name`` and implement ``run``.  ``run`` receives the resolved
    Config and a ready Engine so the method can drive containers through the same
    single seam the phases use.
    """

    name: str = "base"

    def run(self, cfg, engine, request: DeployRequest) -> None:  # pragma: no cover
        raise NotImplementedError(f"deploy method '{self.name}' not implemented")
