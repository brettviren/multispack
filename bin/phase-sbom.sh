#!/usr/bin/env bash
# Generate an SBOM for an environment and flag the security-sensitive leaves.
#
# Turns the "we ship our own libraries, outside OS CVE procedures" concern into a
# concrete, scannable artifact: the spack.lock IS a bill of materials, so we emit
# it as CycloneDX/SPDX (feed to Grype/Trivy/osv-scanner) and print the versions of
# the packages most worth watching for CVEs.
. /opt/multispack/bin/common.sh
use_spack 2>/dev/null || true      # only needed to resolve a MANAGED env by name

: "${SBOM_ENV:?}"
SBOM_FORMAT="${SBOM_FORMAT:=cyclonedx}"

# Resolve the env reference (a spack.lock path, an env dir, a dir under
# $CVMFS_ROOT/env, or a managed env NAME) to a lockfile.
if   [ -f "$SBOM_ENV" ];                              then LOCK="$SBOM_ENV"
elif [ -f "$SBOM_ENV/spack.lock" ];                   then LOCK="$SBOM_ENV/spack.lock"
elif [ -f "${CVMFS_ROOT}/env/${SBOM_ENV}/spack.lock" ]; then LOCK="${CVMFS_ROOT}/env/${SBOM_ENV}/spack.lock"
else
    d="$(spack location -e "$SBOM_ENV" 2>/dev/null || true)"
    [ -n "$d" ] && [ -f "$d/spack.lock" ] && LOCK="$d/spack.lock"
fi
[ -n "${LOCK:-}" ] || { echo "sbom: cannot find a spack.lock for '${SBOM_ENV}'" >&2; exit 1; }

NAME="$(basename "$(dirname "$LOCK")")"
case "$SBOM_FORMAT" in
    cyclonedx) EXT="cdx.json" ;;
    spdx)      EXT="spdx.json" ;;
    text)      EXT="txt" ;;
    *) echo "sbom: unknown --format '${SBOM_FORMAT}'" >&2; exit 2 ;;
esac
OUT="${META}/sbom-${NAME}.${EXT}"

say "generating ${SBOM_FORMAT} SBOM for '${NAME}' from ${LOCK}"
python3 "${MSBIN}/sbom.py" --format "$SBOM_FORMAT" --name "$NAME" -o "$OUT" "$LOCK"
say "wrote ${OUT}"

# ---- highlight the CVE-prone leaves ------------------------------------------
# A curated watchlist of network/parsing/crypto libraries most worth tracking.
WATCH="openssl libressl gnutls nss krb5 openldap cyrus-sasl libssh2 curl \
nghttp2 nghttp3 c-ares libxml2 libxslt expat libyaml json-c jansson zlib \
zlib-ng bzip2 xz zstd lz4 libpng libjpeg-turbo libtiff freetype sqlite pcre2 \
pcre libgcrypt gmp libarchive libtasn1 p11-kit glib dbus python perl git \
boost protobuf grpc"
say "security-sensitive packages present (watch these for CVEs):"
python3 "${MSBIN}/sbom.py" --format text --name "$NAME" "$LOCK" \
    | awk -v w=" $WATCH " 'NR>1 && index(w, " " $1 " ") { printf "    %-28s %s\n", $1, $2 }' \
    | sort -u \
    || say "  (none from the watchlist found)"

say "scan it, e.g.:  grype sbom:${OUT}    # or  trivy sbom ${OUT}"
