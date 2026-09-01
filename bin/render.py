#!/usr/bin/env python3
"""Substitute @NAME@ placeholders from the environment.  Usage: render.py IN OUT"""
import os, re, sys

src, dst = sys.argv[1], sys.argv[2]
text = open(src).read()

def sub(m):
    key = m.group(1)
    val = os.environ.get(key)
    if val is None:
        raise SystemExit("render.py: no environment value for @%s@ (in %s)" % (key, src))
    return val

open(dst, "w").write(re.sub(r"@([A-Z_][A-Z0-9_]*)@", sub, text))
print("rendered %s -> %s" % (src, dst))
