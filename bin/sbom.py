#!/usr/bin/env python3
"""Generate a Software Bill of Materials from a Spack environment's spack.lock.

The lockfile records the exact name, version and hash of every concrete package
in a build, so it IS an SBOM in all but format.  This converts it to the formats
vulnerability scanners consume:

  cyclonedx  CycloneDX 1.5 JSON   (Grype/Trivy/osv-scanner friendly; the default)
  spdx       SPDX 2.3 JSON
  text       a plain human listing

Stdlib only.  Usage: sbom.py [--format F] [--name NAME] [-o FILE] <spack.lock>
"""
import argparse, datetime, json, sys, uuid


def load(lockpath):
    lock = json.load(open(lockpath))
    nodes = lock.get("concrete_specs", {})
    comps, seen = [], set()
    for h, n in nodes.items():
        key = (n.get("name"), str(n.get("version", "")), h)
        if key in seen:
            continue
        seen.add(key)
        external = bool(n.get("external_path")) or isinstance(n.get("external"), dict)
        comps.append({
            "name": n.get("name"),
            "version": str(n.get("version", "")),
            "hash": h,
            "external": external,
        })
    comps.sort(key=lambda c: (c["name"] or "", c["version"]))
    return comps


def purl(c):
    return "pkg:generic/%s@%s" % (c["name"], c["version"])


def cpe(c):
    # Best-effort CPE 2.3.  Correct for packages whose CPE vendor==product==name
    # (e.g. openssl, curl, expat, sqlite); a heuristic otherwise.  Scanners also
    # match on name+version, so this only helps, never hurts.
    return "cpe:2.3:a:%s:%s:%s:*:*:*:*:*:*:*" % (c["name"], c["name"], c["version"])


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cyclonedx(name, comps):
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": "urn:uuid:%s" % uuid.uuid4(),
        "version": 1,
        "metadata": {
            "timestamp": now(),
            "tools": [{"vendor": "multispack", "name": "sbom.py"}],
            "component": {"type": "application", "name": name, "bom-ref": "root:" + name},
        },
        "components": [{
            "type": "library",
            "name": c["name"],
            "version": c["version"],
            "bom-ref": c["hash"],
            "purl": purl(c),
            "cpe": cpe(c),
            "properties": [
                {"name": "spack:hash", "value": c["hash"]},
                {"name": "spack:external", "value": str(c["external"]).lower()},
            ],
        } for c in comps],
    }


def spdx(name, comps):
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": name,
        "documentNamespace": "https://multispack.invalid/%s/%s" % (name, uuid.uuid4()),
        "creationInfo": {"created": now(), "creators": ["Tool: multispack-sbom"]},
        "packages": [{
            "name": c["name"],
            "versionInfo": c["version"],
            "SPDXID": "SPDXRef-Package-%s" % c["hash"],
            "downloadLocation": "NOASSERTION",
            "licenseConcluded": "NOASSERTION",
            "licenseDeclared": "NOASSERTION",
            "copyrightText": "NOASSERTION",
            "externalRefs": [{
                "referenceCategory": "PACKAGE-MANAGER",
                "referenceType": "purl",
                "referenceLocator": purl(c),
            }],
        } for c in comps],
    }


def text(name, comps):
    out = ["# SBOM for environment: %s  (%d components)" % (name, len(comps))]
    for c in comps:
        out.append("%-32s %-18s %s%s" % (
            c["name"], c["version"], c["hash"][:12],
            "  (external/host-provided)" if c["external"] else ""))
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("lock", help="path to a spack.lock")
    ap.add_argument("--format", choices=["cyclonedx", "spdx", "text"], default="cyclonedx")
    ap.add_argument("--name", default="spack-env")
    ap.add_argument("-o", "--output")
    args = ap.parse_args()

    comps = load(args.lock)
    if args.format == "text":
        s = text(args.name, comps)
    else:
        doc = cyclonedx(args.name, comps) if args.format == "cyclonedx" else spdx(args.name, comps)
        s = json.dumps(doc, indent=2) + "\n"

    if args.output:
        with open(args.output, "w") as fp:
            fp.write(s)
        print("sbom.py: wrote %d components to %s (%s)" % (len(comps), args.output, args.format),
              file=sys.stderr)
    else:
        sys.stdout.write(s)


if __name__ == "__main__":
    main()
