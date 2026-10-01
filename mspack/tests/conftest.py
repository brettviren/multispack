# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    """META_DIR defaults to ./meta of the invocation dir: keep tests out of the
    real working directory (and drop any META_DIR from the caller's env)."""
    monkeypatch.delenv("META_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
