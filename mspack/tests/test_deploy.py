# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0
"""Deploy registry and destination parsing."""

import pytest

from mspack import deploy


def test_builtin_methods_registered():
    names = deploy.available()
    assert "buildcache" in names
    assert "conda" in names


def test_get_unknown_raises():
    with pytest.raises(KeyError):
        deploy.get("nope")


@pytest.mark.parametrize("dest,scp", [
    ("deploy/channel", False),    # ':' absent
    ("/srv/www/spaxi", False),    # local absolute
    ("./rel/path", False),        # local relative
    ("bviren@host:/srv/x", True),
    ("host:/srv/x", True),
    ("host:x", True),
])
def test_is_scp(dest, scp):
    assert deploy.is_scp(dest) is scp


def test_parse_dest_roundtrip():
    d = deploy.parse_dest("bviren@web:/srv/x")
    assert d.is_scp and d.host == "bviren@web" and d.path == "/srv/x"
    d2 = deploy.parse_dest("deploy/channel")
    assert not d2.is_scp and d2.path == "deploy/channel"


def test_method_run_is_stubbed():
    from mspack.deploy.base import DeployRequest
    method = deploy.get("conda")()
    with pytest.raises(NotImplementedError):
        method.run(None, None, DeployRequest(dest="x"))
