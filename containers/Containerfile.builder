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
        git curl tar gzip bzip2 xz zstd unzip \
        gawk sed grep \
        perl perl-Data-Dumper perl-Thread-Queue \
        binutils ca-certificates \
        python3.11 python3.11-setuptools ; \
    dnf clean all; rm -rf /var/cache/dnf
RUN ln -sf /usr/bin/python3.11 /usr/local/bin/python3 && python3 --version

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
