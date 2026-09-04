# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0
"""mspack: structured orchestration for the multispack Strategy B Spack stack."""

from .config import Config
from .container import Engine

__all__ = ["Config", "Engine", "__version__"]
__version__ = "0.1.0"
