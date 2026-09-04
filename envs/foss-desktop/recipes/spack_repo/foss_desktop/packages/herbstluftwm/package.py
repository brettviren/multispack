# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0
#
# herbstluftwm is not in the Spack builtin repo; this local recipe lets the
# foss-desktop environment build it.  First-cut -- refine variants/deps as real
# builds surface anything missing.

from spack_repo.builtin.build_systems.cmake import CMakePackage

from spack.package import *


class Herbstluftwm(CMakePackage):
    """herbstluftwm is a manual tiling window manager for X11 using Xlib and
    Glib. Its config lives in a shell script that calls herbstclient to talk to
    the running window manager over an IPC interface."""

    homepage = "https://herbstluftwm.org/"
    url = "https://herbstluftwm.org/tarballs/herbstluftwm-0.9.5.tar.gz"

    maintainers("brettviren")

    license("BSD-2-Clause")

    version("0.9.5", sha256="b2d4600909e5bece5ad63818dfb30bb19fd2ac9f52847b1a7a74ad4040718105")
    version("0.9.4", sha256="eef8eed076af33af2a75911c0fb1215fdb3427606a034ea8b44fe76872cb03cc")

    depends_on("cxx", type="build")  # C++ sources
    depends_on("cmake@3.5:", type="build")
    depends_on("pkgconfig", type="build")

    depends_on("libx11")
    depends_on("libxext")
    depends_on("libxinerama")
    depends_on("libxrandr")
    depends_on("glib")

    def cmake_args(self):
        # Man/HTML docs need asciidoc/xmlto; skip them so the WM itself is the
        # only build product and the closure stays small.
        return [
            self.define("WITH_DOCUMENTATION", False),
        ]
