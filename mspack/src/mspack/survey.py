# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0

"""The Linux distribution survey.

Reads the curated catalog at ``config/distros.yaml`` (glibc, default gcc,
package family/manager, official image, EOL per distro release) and turns it
into two things:

* the *coverage report* -- for each glibc version present, which distro releases
  a binary built against that floor can run on (glibc keeps forward
  compatibility via symbol versioning, so a build against glibc X runs on any
  distro whose glibc >= X);
* the raw *catalog table*.

It also drives ``mspack survey probe``: pull each distro's official image and
read glibc (and, best-effort, the default gcc) straight from it, then write the
refreshed values back into ``distros.yaml`` while preserving its comments.

The same catalog is the lookup table behind ``BASE_DISTRO`` in
:mod:`mspack.config`, so this module deliberately imports nothing from there.
"""

import json
from pathlib import Path

from ruamel.yaml import YAML

from .container import Engine

_CATALOG_REL = ("config", "distros.yaml")


# -- loading ------------------------------------------------------------------
def catalog_path(root: Path) -> Path:
    return root.joinpath(*_CATALOG_REL)


def load_doc(root: Path):
    """Round-trip-load the whole catalog document (comments preserved).

    Returns ``None`` if the file is missing or unparseable -- callers that only
    need the data use :func:`load_catalog`; the round-trip doc is for ``probe``,
    which writes it back.
    """
    path = catalog_path(root)
    if not path.is_file():
        return None
    try:
        return YAML().load(path.read_text())
    except Exception:
        return None


def load_catalog(root: Path) -> dict:
    """The ``distros:`` map as a plain dict (empty if the catalog is absent)."""
    doc = load_doc(root)
    if not doc:
        return {}
    distros = doc.get("distros") or {}
    # Normalize to plain dicts so downstream code never sees ruamel nodes.
    return {k: dict(v) for k, v in distros.items()}


# -- helpers ------------------------------------------------------------------
def glibc_key(ver: str) -> tuple:
    """A sortable/comparable key for a glibc version string like '2.28'."""
    out = []
    for part in str(ver).split("."):
        try:
            out.append(int(part))
        except ValueError:
            out.append(0)
    return tuple(out)


def label(key: str, entry: dict) -> str:
    """Human label for a catalog entry, e.g. 'AlmaLinux 8'."""
    name = entry.get("distro", key)
    rel = entry.get("release", "")
    return f"{name} {rel}".strip()


def is_bootstrappable(entry: dict) -> bool:
    """True if a builder Containerfile exists for this distro's pkg manager."""
    return entry.get("pkg_manager") in ("dnf", "apt")


# -- collation ----------------------------------------------------------------
def collate(catalog: dict) -> list:
    """For each distinct glibc floor, the releases that can run its binaries.

    Returns a list (ascending glibc) of dicts:
      glibc          the floor version string
      provided_by    keys whose native glibc == this floor
      runs_on        keys whose glibc >= this floor (sorted glibc then key)
      bootstrappable any ``provided_by`` distro can seed the ladder (dnf/apt)
    """
    entries = [(k, v) for k, v in catalog.items() if v.get("glibc")]
    floors = sorted({v["glibc"] for _, v in entries}, key=glibc_key)

    def sort_keys(keys):
        return sorted(keys, key=lambda k: (glibc_key(catalog[k]["glibc"]), k))

    rows = []
    for floor in floors:
        fk = glibc_key(floor)
        provided_by = [k for k, v in entries if v["glibc"] == floor]
        runs_on = [k for k, v in entries if glibc_key(v["glibc"]) >= fk]
        rows.append({
            "glibc": floor,
            "provided_by": sort_keys(provided_by),
            "runs_on": sort_keys(runs_on),
            "bootstrappable": any(is_bootstrappable(catalog[k])
                                  for k in provided_by),
        })
    return rows


# -- rendering ----------------------------------------------------------------
def report(catalog: dict, as_json: bool = False) -> str:
    """The glibc-coverage report (or its JSON)."""
    rows = collate(catalog)
    if as_json:
        return json.dumps(rows, indent=2)
    if not rows:
        return ("no distro catalog found (config/distros.yaml missing); "
                "run `mspack survey probe` or restore the file")
    out = [
        "glibc coverage -- a stack built on a given glibc floor runs on every",
        "distro release at or above it (⚑ = usable as a multispack base):",
        "",
    ]
    for r in rows:
        mark = "⚑" if r["bootstrappable"] else " "
        src = ", ".join(label(k, catalog[k]) for k in r["provided_by"])
        out.append(f"{mark} glibc {r['glibc']:<6} ({src})")
        runs = ", ".join(label(k, catalog[k]) for k in r["runs_on"])
        out.append(f"    runs on: {runs}")
        out.append("")
    out.append("⚑ bootstrap base available (dnf -> Containerfile.builder, "
               "apt -> Containerfile.builder-debian).")
    return "\n".join(out)


def table(catalog: dict, as_json: bool = False) -> str:
    """The raw catalog as a table (or its JSON)."""
    if as_json:
        return json.dumps(catalog, indent=2)
    if not catalog:
        return "no distro catalog found (config/distros.yaml missing)"
    cols = ("key", "distro", "release", "glibc", "gcc", "pkg", "eol", "image")
    rows = []
    for key in sorted(catalog, key=lambda k: (glibc_key(catalog[k].get("glibc", "0")), k)):
        e = catalog[key]
        base = "*" if is_bootstrappable(e) else " "
        rows.append((
            f"{base}{key}", str(e.get("distro", "")), str(e.get("release", "")),
            str(e.get("glibc", "")), str(e.get("gcc", "")),
            str(e.get("pkg_manager", "")), str(e.get("eol", "")),
            str(e.get("image", "")),
        ))
    widths = [max(len(cols[i]), *(len(r[i]) for r in rows)) for i in range(len(cols))]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*cols), fmt.format(*("-" * w for w in widths))]
    lines += [fmt.format(*r) for r in rows]
    lines.append("")
    lines.append("* = usable as a multispack base (BASE_DISTRO=<key>).")
    return "\n".join(lines)


# -- probing ------------------------------------------------------------------
# One shell snippet per package manager that prints the DEFAULT gcc version
# without installing it (network + repo metadata required; best-effort).
_GCC_PROBE = {
    "dnf": r"dnf -q info gcc 2>/dev/null | awk '/^Version/{print $3; exit}'",
    "apt": (r"apt-get update -qq >/dev/null 2>&1; "
            r"apt-cache depends gcc 2>/dev/null | "
            r"awk -F'gcc-' '/Depends: gcc-/{print $2; exit}'"),
    "zypper": r"zypper -n info gcc 2>/dev/null | awk '/^Version/{print $3; exit}'",
}


def _probe_script(pkg_manager: str) -> str:
    glibc = ("GLIBC=$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}'); "
             "[ -n \"$GLIBC\" ] || GLIBC=$(ldd --version 2>/dev/null | "
             "head -1 | awk '{print $NF}'); echo \"glibc=$GLIBC\"")
    gcc_cmd = _GCC_PROBE.get(pkg_manager, "true")
    return f"{glibc}; GCC=$({gcc_cmd}); echo \"gcc=$GCC\""


def _parse_probe(text: str) -> dict:
    got = {}
    for line in (text or "").splitlines():
        if line.startswith("glibc=") and line[6:].strip():
            got["glibc"] = line[6:].strip()
        elif line.startswith("gcc=") and line[4:].strip():
            # apt returns "12"/"12\n"; dnf/zypper "8.5.0" -> keep major.minor.
            v = line[4:].strip()
            got["gcc"] = v
    return got


def probe(root: Path, engine: Engine, names: list | None = None) -> list:
    """Refresh glibc (and best-effort gcc) by reading each distro's image.

    Rewrites the probed fields into ``config/distros.yaml`` in place, preserving
    comments.  Returns a list of per-distro result dicts for reporting.
    """
    doc = load_doc(root)
    if not doc or not doc.get("distros"):
        raise FileNotFoundError(
            f"no catalog to probe at {catalog_path(root)}")
    distros = doc["distros"]
    keys = list(names) if names else list(distros.keys())
    results = []
    changed = False
    for key in keys:
        entry = distros.get(key)
        if entry is None:
            results.append({"key": key, "status": "unknown key"})
            continue
        image = entry.get("image")
        if not image:
            results.append({"key": key, "status": "no image"})
            continue
        pm = entry.get("pkg_manager", "")
        script = _probe_script(pm)
        res = engine.run(image, ["/bin/sh", "-c", script],
                         rm=True, capture=True, check=False)
        got = _parse_probe(res.output or "")
        note = []
        for fld in ("glibc", "gcc"):
            new = got.get(fld)
            if new and str(entry.get(fld)) != new:
                note.append(f"{fld} {entry.get(fld)} -> {new}")
                entry[fld] = new
                changed = True
        results.append({
            "key": key,
            "status": "; ".join(note) if note else "up to date",
            "glibc": got.get("glibc", entry.get("glibc")),
            "gcc": got.get("gcc", entry.get("gcc")),
            "probed": bool(got),
        })
    if changed:
        _stamp_updated(doc)
        yaml = YAML()
        with catalog_path(root).open("w") as fh:
            yaml.dump(doc, fh)
    return results


def _stamp_updated(doc) -> None:
    """Bump the catalog's ``updated`` marker to note a probe ran.

    ``Date.now`` is avoided in this codebase's cron/workflow contexts; here we
    simply append a probe marker so it is obvious the file was machine-touched.
    """
    prev = str(doc.get("updated", ""))
    if "+probe" not in prev:
        doc["updated"] = f"{prev}+probe" if prev else "probe"
