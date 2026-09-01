#!/usr/bin/env python3
"""Merge meta/*.json into one self-contained HTML report.

Reads two kinds of file:
  <order>-<phase>.json         written by multispack.sh: timing and status
  <phase>.detail.json          written by the phase itself: what it found

Stdlib only, no external assets, no JavaScript beyond <details> toggling.
"""
import argparse, glob, html, json, os, time

CSS = """
:root{--bg:#fff;--fg:#16181d;--mut:#5b6270;--line:#e3e6ec;--card:#f7f8fa;
--ok:#0f7a3d;--okbg:#e6f4ec;--bad:#b3261e;--badbg:#fbeae9;
--warn:#8a5a00;--warnbg:#fdf1dd;--info:#1c4f8f;--infobg:#e7eefa;--mono:#f2f3f6}
@media(prefers-color-scheme:dark){:root{--bg:#12141a;--fg:#e6e8ee;--mut:#98a0b0;
--line:#2a2f3a;--card:#191c24;--ok:#5ed48f;--okbg:#12301f;--bad:#ff8b82;--badbg:#3a1a17;
--warn:#e8b465;--warnbg:#33260f;--info:#8ab4f8;--infobg:#16243b;--mono:#1c1f28}}
*{box-sizing:border-box}
body{margin:0;padding:2rem 1.5rem 5rem;background:var(--bg);color:var(--fg);
font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
max-width:1100px;margin-inline:auto}
h1{font-size:1.6rem;margin:0 0 .25rem}
h2{font-size:1.15rem;margin:2.5rem 0 .75rem;padding-bottom:.35rem;border-bottom:1px solid var(--line)}
h3{font-size:.95rem;margin:1.4rem 0 .5rem;color:var(--mut);font-weight:600}
.sub{color:var(--mut);margin:0 0 1.5rem}
table{border-collapse:collapse;width:100%;margin:.5rem 0 1rem;font-size:.9rem}
th,td{text-align:left;padding:.45rem .6rem;border-bottom:1px solid var(--line);vertical-align:top}
th{font-weight:600;color:var(--mut);font-size:.78rem;text-transform:uppercase;letter-spacing:.04em}
td.num{text-align:right;font-variant-numeric:tabular-nums}
code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.86em}
code{background:var(--mono);padding:.1rem .3rem;border-radius:3px}
.pill{display:inline-block;padding:.1rem .5rem;border-radius:99px;font-size:.75rem;
font-weight:600;letter-spacing:.02em;white-space:nowrap}
.ok{background:var(--okbg);color:var(--ok)} .bad{background:var(--badbg);color:var(--bad)}
.warn{background:var(--warnbg);color:var(--warn)} .info{background:var(--infobg);color:var(--info)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:.75rem;margin:1rem 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:.75rem .9rem}
.card .k{font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:var(--mut)}
.card .v{font-size:1.25rem;font-weight:600;margin-top:.15rem;word-break:break-word}
.card .v.sm{font-size:.9rem;font-weight:500}
details{margin:.4rem 0;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:.5rem .75rem}
summary{cursor:pointer;font-size:.85rem;color:var(--mut);font-weight:600}
pre{overflow-x:auto;font-size:.8rem;background:var(--mono);padding:.6rem;border-radius:6px;margin:.5rem 0 0}
.note{background:var(--infobg);border-left:3px solid var(--info);padding:.6rem .85rem;
border-radius:0 6px 6px 0;font-size:.88rem;margin:1rem 0}
ul.pk{columns:4;column-gap:1.5rem;font-size:.82rem;list-style:none;padding:0;margin:.3rem 0}
"""

PILL = {"ok": "ok", "pass": "ok", "clean": "ok", "libc": "ok",
        "xfail": "info", "info": "info", "host-injected": "info",
        "fail": "bad", "bad": "bad", "unexpected": "bad",
        "xpass": "warn", "review": "warn", "skip": "warn", "grey": "warn",
        "missing": "warn"}


def e(x):
    return html.escape(str(x), quote=True)


def pill(status):
    return '<span class="pill %s">%s</span>' % (PILL.get(str(status).lower(), "info"), e(status))


def card(k, v, small=False):
    return '<div class="card"><div class="k">%s</div><div class="v%s">%s</div></div>' % (
        e(k), " sm" if small else "", e(v))


def dur(s):
    try:
        s = int(s)
    except (TypeError, ValueError):
        return "-"
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm %02ds" % (s // 60, s % 60)
    return "%dh %02dm" % (s // 3600, (s % 3600) // 60)


def load(metadir):
    phases, details = [], {}
    for path in sorted(glob.glob(os.path.join(metadir, "*.json"))):
        try:
            data = json.load(open(path))
        except Exception as exc:
            print("  skipping %s: %s" % (path, exc))
            continue
        if path.endswith(".detail.json"):
            details[data.get("phase", os.path.basename(path))] = data
        elif "order" in data:
            phases.append(data)
    phases.sort(key=lambda d: (d.get("order", 50), d.get("phase", "")))
    return phases, details


def sec_pipeline(phases):
    if not phases:
        return "<p>No phases have run yet.</p>"
    rows = []
    total = 0
    for p in phases:
        total += p.get("duration_s") or 0
        rows.append(
            "<tr><td><code>%s</code></td><td>%s</td><td class='num'>%s</td>"
            "<td>%s</td><td>%s</td></tr>" % (
                e(p.get("phase")), pill(p.get("status")), dur(p.get("duration_s")),
                e(p.get("finished", "")), e(p.get("description", ""))))
    return ("<table><tr><th>Phase</th><th>Status</th><th>Duration</th>"
            "<th>Finished (UTC)</th><th>What it did</th></tr>%s"
            "<tr><td><b>total</b></td><td></td><td class='num'><b>%s</b></td>"
            "<td></td><td></td></tr></table>" % ("".join(rows), dur(total)))


def sec_environment(phases, details):
    cfg = next((p.get("config", {}) for p in phases if p.get("config")), {})
    boot = details.get("bootstrap", {})
    comp = details.get("compiler", {})
    stack = details.get("stack", {})
    cards = [
        card("Base image (glibc floor)", cfg.get("builder_base", "?"), True),
        card("Target ISA", cfg.get("target", "?")),
        card("Spack", "%s" % (boot.get("spack_version") or cfg.get("spack_ref", "?")), True),
        card("Spack commit", (boot.get("spack_commit") or "?")[:12], True),
        card("Stack compiler", "gcc %s" % comp.get("spack_gcc_version", "?"), True),
        card("Target compiler (payload)", "gcc %s" % comp.get("target_gcc_version", "?"), True),
        card("Build kernel", boot.get("build_host_kernel", "?"), True),
        card("Build host libc", boot.get("build_host_libc", "?"), True),
        card("Install tree", "%s GiB / %s files" % (
            stack.get("install_tree_gib", "?"), stack.get("install_tree_files", "?")), True),
    ]
    out = ['<div class="cards">%s</div>' % "".join(cards)]
    out.append('<div class="note"><b>The build environment is the contract.</b> '
               "Everything above <code>libc</code> was compiled by the Spack GCC "
               "shown here, inside the pinned base image. The distribution "
               "contributed the bootstrap compiler and nothing else. Pin the base "
               "image by digest in <code>multispack.conf</code> to make this "
               "rebuildable.</div>")
    return "".join(out)


def sec_sharing(details):
    c = details.get("concretize")
    if not c:
        return "<p>Not run.</p>"
    cards = [card("Total nodes", c.get("total_nodes", "?")),
             card("Shared by all flavours", c.get("shared_nodes", "?")),
             card("Shared fraction", "%.0f%%" % (100 * (c.get("shared_fraction") or 0)))]
    for k, v in sorted((c.get("closure_sizes") or {}).items()):
        cards.append(card("closure %s" % k, v))
    out = ['<div class="cards">%s</div>' % "".join(cards)]
    uns = c.get("unshared_by_flavour") or {}
    if uns:
        rows = "".join("<tr><td><code>%s</code></td><td class='num'>%d</td><td>%s</td></tr>"
                       % (e(k), len(v), ", ".join("<code>%s</code>" % e(x) for x in v) or "&mdash;")
                       for k, v in sorted(uns.items()))
        out.append("<h3>Packages NOT shared between flavours</h3>")
        out.append("<table><tr><th>Flavour</th><th>Count</th><th>Packages</th></tr>%s</table>" % rows)
    shared = c.get("shared_packages") or []
    if shared:
        out.append("<details><summary>%d shared packages</summary><ul class='pk'>%s</ul></details>"
                   % (len(shared), "".join("<li><code>%s</code></li>" % e(x) for x in shared)))
    out.append('<div class="note">Dependencies are pinned to <code>cxxstd=17</code> '
               "via <code>packages:all:variants</code> so the C++17 and C++23 ROOT "
               "builds can share them. Anything listed as not shared either carries a "
               "<code>cxxstd</code> variant that ROOT propagates, or differs for an "
               "unrelated reason worth checking.</div>")
    return "".join(out)


def sec_relocation(details):
    o = details.get("originize")
    a = details.get("audit")
    out = []
    if o:
        s = o.get("stats", {})
        out.append('<div class="cards">%s</div>' % "".join([
            card("ELF files scanned", s.get("scanned", "?")),
            card("Objects with rpath", s.get("with_rpath", "?")),
            card("Rpaths rewritten", s.get("rewritten", "?")),
            card("Too long to rewrite", s.get("skipped_too_long", "?")),
        ]))
        if s.get("skipped_too_long"):
            rows = "".join("<tr><td class='mono'>%s</td><td class='mono'>%s</td></tr>"
                           % (e(x["file"]), e(x["new"])) for x in o.get("skipped_too_long_examples", []))
            out.append("<h3>Entries left absolute (new rpath longer than old)</h3>")
            out.append("<table><tr><th>File</th><th>Wanted</th></tr>%s</table>" % rows)
            out.append('<div class="note">In-place ELF string edits can only shrink. '
                       "Raise <code>PADDED_LENGTH</code> so the build prefix is longer "
                       "than the <code>$ORIGIN/../../</code> replacement, then rebuild.</div>")
    if not a:
        return "".join(out) or "<p>Not run.</p>"
    verdict = a.get("verdict", "?")
    out.append('<div class="cards">%s</div>' % "".join([
        card("glibc floor", a.get("glibc_floor", "?")),
        card("ELF objects", a.get("elf_objects", "?")),
        card("Audit verdict", verdict),
    ]))
    rk = a.get("rpath_components") or {}
    out.append("<h3>Rpath composition</h3><table><tr><th>Kind</th><th>Count</th></tr>%s</table>"
               % "".join("<tr><td><code>%s</code></td><td class='num'>%d</td></tr>" % (e(k), v)
                         for k, v in sorted(rk.items())))
    out.append("<h3>Program interpreters</h3><table><tr><th>PT_INTERP</th><th>Objects</th></tr>%s</table>"
               % "".join("<tr><td class='mono'>%s</td><td class='num'>%d</td></tr>" % (e(k), v)
                         for k, v in sorted((a.get("program_interpreters") or {}).items())))
    ext = a.get("external_libraries") or []
    rows = "".join(
        "<tr><td class='mono'>%s</td><td>%s</td><td class='num'>%d</td></tr>"
        % (e(x["library"]), pill(x["tier"]), x["referencing_objects"]) for x in ext)
    out.append("<h3>Libraries the host must provide</h3>")
    out.append("<table><tr><th>Library</th><th>Tier</th><th>Referencing objects</th></tr>%s</table>" % rows)
    out.append('<div class="note"><b>Reading this table.</b> '
               "<code>libc</code> is the Strategy B contract and is expected. "
               "<code>host-injected</code> is kernel- or site-bound (libcuda, verbs, PMI) "
               "and can only ever come from the host. <code>grey</code> is present on most "
               "glibc distributions but is not part of glibc &mdash; each entry silently "
               "narrows portability and should be traced to a package and eliminated. "
               "<code>unexpected</code> is a defect.</div>")
    return "".join(out)


def sec_validation(phases, details):
    vals = [d for k, d in sorted(details.items()) if k.startswith("validate-")]
    if not vals:
        return "<p>No validation has run yet.</p>"
    names = []
    for v in vals:
        for t in v.get("tests", []):
            if t["name"] not in names:
                names.append(t["name"])
    head = "".join("<th>%s</th>" % e(n) for n in names)
    rows = []
    for v in vals:
        by = {t["name"]: t for t in v.get("tests", [])}
        cells = "".join("<td>%s</td>" % (pill(by[n]["status"]) if n in by else "&mdash;")
                        for n in names)
        rows.append(
            "<tr><td><b>%s</b><br><span class='mono' style='font-size:.75rem;color:var(--mut)'>%s</span></td>"
            "<td>%s</td>%s</tr>" % (e(v.get("distro")), e(v.get("os_release", "")),
                                    pill(v.get("verdict")), cells))
    out = ["<table><tr><th>Distribution</th><th>Verdict</th>%s</tr>%s</table>" % (head, "".join(rows))]
    rows = "".join(
        "<tr><td><b>%s</b></td><td class='mono'>%s</td><td class='mono'>%s</td>"
        "<td>%s</td><td>%s</td></tr>" % (
            e(v.get("distro")), e(v.get("kernel", "")), e(v.get("host_libc", ""))[:48],
            "yes" if v.get("loader_present") else "<b>no</b>", pill(v.get("expectation")))
        for v in vals)
    out.append("<h3>Host facts</h3><table><tr><th>Distribution</th><th>Kernel</th>"
               "<th>Host libc</th><th>glibc loader</th><th>Expectation</th></tr>%s</table>" % rows)
    out.append('<div class="note"><b>No <code>LD_LIBRARY_PATH</code> was set in any run.</b> '
               "Every validation container is bare &mdash; nothing was installed from the "
               "distribution &mdash; and <code>/cvmfs</code> was mounted read-only. If ROOT "
               "starts, the <code>$ORIGIN</code> rpaths are doing all the work.<br><br>"
               "<b>Alpine is an expected failure (<code>xfail</code>).</b> Strategy B ships no "
               "loader and no libc, so <code>PT_INTERP</code> points at "
               "<code>/lib64/ld-linux-x86-64.so.2</code>, which does not exist on a musl "
               "system. That row is the boundary of Strategy B, not a bug: it is exactly "
               "the row Strategy A (own glibc, own loader) would turn green.</div>")
    return "".join(out)


def sec_raw(phases, details):
    out = []
    for d in phases:
        out.append("<details><summary>%s &mdash; phase record</summary><pre>%s</pre></details>"
                   % (e(d.get("phase")), e(json.dumps(d, indent=2))))
    for k in sorted(details):
        out.append("<details><summary>%s &mdash; detail</summary><pre>%s</pre></details>"
                   % (e(k), e(json.dumps(details[k], indent=2))))
    return "".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--meta", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    phases, details = load(args.meta)
    cfg = next((p.get("config", {}) for p in phases if p.get("config")), {})
    now = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())

    doc = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>multispack &mdash; Strategy B build and validation report</title>
<style>%s</style></head><body>
<h1>multispack &mdash; distribution-independent Spack install area</h1>
<p class="sub">Strategy B (host glibc, everything above it built by Spack) &middot;
<code>%s</code> &middot; generated %s</p>

<h2>1. Pipeline</h2>%s
<h2>2. Build environment</h2>%s
<h2>3. Dependency sharing between C++17 and C++23</h2>%s
<h2>4. Relocation and portability audit</h2>%s
<h2>5. Cross-distribution validation</h2>%s
<h2>6. Raw records</h2>%s
</body></html>""" % (
        CSS, e(cfg.get("cvmfs_root", "")), now,
        sec_pipeline(phases), sec_environment(phases, details),
        sec_sharing(details), sec_relocation(details),
        sec_validation(phases, details), sec_raw(phases, details))

    with open(args.out, "w") as fp:
        fp.write(doc)
    print("wrote %s (%d phases, %d detail records)" % (args.out, len(phases), len(details)))


if __name__ == "__main__":
    main()
