#!/bin/sh
# Compile the dlopen probe and N shared libraries in BOTH TLS models:
#   ie-libNNN.so : initial-exec  (consumes the fixed static-TLS surplus)
#   gd-libNNN.so : global-dynamic (allocated dynamically; no fixed limit)
# Usage: build.sh <srcdir> <outdir> [N] [TLS_BYTES]
set -eu
SRC="${1:?srcdir}"; OUT="${2:?outdir}"; N="${3:-64}"; TB="${4:-512}"
CC="${CC:-cc}"
mkdir -p "$OUT"

"$CC" -O2 -o "$OUT/probe" "$SRC/probe.c" -ldl

i=0
while [ "$i" -lt "$N" ]; do
    p=$(printf '%03d' "$i")
    "$CC" -O2 -fPIC -shared -DTLS_BYTES="$TB" -DIE_TLS "$SRC/tlslib.c" -o "$OUT/ie-lib$p.so"
    "$CC" -O2 -fPIC -shared -DTLS_BYTES="$TB"          "$SRC/tlslib.c" -o "$OUT/gd-lib$p.so"
    i=$((i + 1))
done
echo "built: probe + $N ie-lib*.so + $N gd-lib*.so  (TLS_BYTES=$TB) in $OUT"
