# multispack builder -- the captured build environment for Strategy B.
#
# The base image defines the ENTIRE portability contract of the resulting
# binaries: its glibc is the oldest glibc the output will run against.
# AlmaLinux 8 ships glibc 2.28, which is exactly the manylinux_2_28 baseline.
#
# PIN THIS BY DIGEST for a reproducible rebuild, e.g.
#   BUILDER_BASE=docker.io/library/almalinux@sha256:...
# and record the digest in multispack.conf.
ARG BUILDER_BASE=docker.io/library/almalinux:8
FROM ${BUILDER_BASE}

LABEL org.opencontainers.image.title="multispack builder" \
      org.opencontainers.image.description="Strategy B portable Spack build environment (glibc 2.28 floor)"

# Only what Spack itself needs to run and to bootstrap its first compiler.
# Deliberately minimal: everything above libc is built by Spack, so the
# distribution contributes nothing to the shipped artifacts.
RUN set -eux; \
    dnf -y --setopt=install_weak_deps=False --setopt=tsflags=nodocs install \
        gcc gcc-c++ gcc-gfortran \
        make patch file findutils diffutils which hostname procps-ng \
        git curl tar gzip bzip2 xz zstd libzstd-devel unzip \
        gawk sed grep \
        perl perl-Data-Dumper perl-Thread-Queue \
        binutils ca-certificates \
        python3.11 python3.11-setuptools ; \
    dnf clean all; rm -rf /var/cache/dnf
RUN ln -sf /usr/bin/python3.11 /usr/local/bin/python3 && python3 --version

# GNU make >= 4.4 into /usr/local/bin (first on PATH), shadowing AlmaLinux 8's
# make 4.2.1.  GCC 12+'s LTO (-flto=auto) makes lto-wrapper spawn `make` with a
# named-pipe ("fifo:") jobserver; make 4.2.1 cannot parse it and dies with
# "invalid --jobserver-auth string 'fifo:...'", breaking any package built with
# LTO (py-matplotlib was the first to bite).  make is the one bootstrap tool the
# compiler reaches for directly -- cmake and the rest come from Spack build deps
# -- so modernizing it here fixes LTO for every package.  Built with the system
# make; a source build keeps us off the distro's ancient version.
ARG MAKE_VERSION=4.4.1
RUN set -eux; cd /tmp; \
    curl -fsSL "https://ftp.gnu.org/gnu/make/make-${MAKE_VERSION}.tar.gz" | tar xz; \
    cd "make-${MAKE_VERSION}"; \
    ./configure --prefix=/usr/local >/dev/null; \
    make >/dev/null; make install >/dev/null; \
    cd /; rm -rf "/tmp/make-${MAKE_VERSION}"; \
    hash -r; make --version | head -1

# A modern, zstd-capable binutils >= 2.40 into /usr/local/bin (first on PATH),
# shadowing AlmaLinux 8's binutils 2.30.  A modern GCC compresses libgcc.a's
# .debug_info with zstd, but ld 2.30 predates zstd section support (added in
# binutils 2.40) and dies with "unable to initialize decompress status for
# section .debug_info ... File format not recognized" the moment a link pulls one
# of those compressed libgcc objects (e.g. _muldi3.o / _divdi3.o for __int128).
# Our own gcc uses its bundled binutils (via -B) so gcc links fine -- but a
# freshly-built clang (llvm's runtimes phase links libc++ against gcc's libgcc.a)
# resolves the bare `ld` from PATH, which is the distro's 2.30.  Shipping a
# zstd-capable ld here fixes every PATH-resolved linker.  --with-zstd needs the
# libzstd headers (installed above); the ldd check asserts zstd is actually wired
# in, so a silently non-zstd build fails the image here rather than mid-stack.
ARG BINUTILS_VERSION=2.43.1
RUN set -eux; cd /tmp; \
    curl -fsSL "https://ftp.gnu.org/gnu/binutils/binutils-${BINUTILS_VERSION}.tar.xz" | tar xJ; \
    cd "binutils-${BINUTILS_VERSION}"; \
    ./configure --prefix=/usr/local --with-zstd --enable-64-bit-bfd \
        --disable-nls --disable-werror >/dev/null; \
    make MAKEINFO=true -j"$(nproc)" >/dev/null; \
    make MAKEINFO=true install >/dev/null; \
    cd /; rm -rf "/tmp/binutils-${BINUTILS_VERSION}"; \
    hash -r; ld --version | head -1; \
    ldd "$(command -v ld)" | grep -q libzstd || { echo "built ld lacks zstd support"; exit 1; }

# Spack clones live on the /cvmfs volume and are owned by whoever runs the
# container; do not let git refuse to operate on them.
RUN git config --system --add safe.directory '*'

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin \
    SPACK_DISABLE_LOCAL_CONFIG=1 \
    SPACK_USER_CACHE_PATH=/multispack/work/spack-user-cache \
    TMPDIR=/multispack/work/tmp

# Baked-in recipe.  multispack.sh bind-mounts the host copies over these when
# DEV_MOUNTS=1 so scripts can be edited without rebuilding the image.
COPY bin    /opt/multispack/bin
COPY config /opt/multispack/config
COPY tests  /opt/multispack/tests
RUN chmod +x /opt/multispack/bin/*.sh /opt/multispack/bin/*.py || true

WORKDIR /multispack
CMD ["/bin/bash", "-l"]
