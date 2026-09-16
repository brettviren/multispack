# Choosing the base distro: the glibc floor (Alma 8 vs Alma 9)

multispack builds against a **floor glibc**: the base container's glibc is the
oldest glibc the resulting binaries can run on. The default base is AlmaLinux 8
(glibc 2.28 = the `manylinux_2_28` baseline). Now that the base is selectable
(`BASE_DISTRO` / `mspack --base-distro`, see `mspack survey`), the natural
question is what actually changes if you move the floor **up** — for example to
AlmaLinux 9 (glibc 2.34).

The obvious cost is "you drop support for older glibcs." This note records the
*non-obvious* implications, because AlmaLinux 9 sits on a real fault line in
glibc history: the **glibc 2.34 pthread merge**. Alma 8 is pre-merge (2.28),
Alma 9 *is* the merge release (2.34). See the dedicated criticism-response areas
[`test/pthread/`](../test/pthread/README.md) and
[`test/tls-surplus/`](../test/tls-surplus/README.md) for the reproducible
experiments behind the claims below.

## Background: the glibc 2.34 pthread merge

glibc uses **symbol versioning** and never removes old version nodes, giving
*forward* compatibility (a binary needing `GLIBC_2.X` runs on any glibc `>= 2.X`)
and no *backward* compatibility. In **glibc 2.34** (2021) the threading and
related libraries — `libpthread`, `libdl`, `librt`, `libutil`, `libanl` — were
**merged into `libc` itself**. The pthread functions that had lived in
`libpthread.so.0` at `@GLIBC_2.2.5` got a **new default version node
`@GLIBC_2.34`** inside `libc.so.6`; the old separate libraries remain only as
empty forwarding stubs.

| Base distro | glibc | relation to the merge |
|---|---|---|
| AlmaLinux 8 | 2.28 | **pre-merge** — multispack's default floor (`manylinux_2_28`) |
| AlmaLinux 9 | 2.34 | **the merge release** (`manylinux_2_34`) |
| AlmaLinux 10 | 2.39 | post-merge (pthread node still frozen at 2.34) |

---

## Implications of moving the floor to Alma 9

### 1. The build gets *simpler*, not harder

This is the implication tied directly to the pthread line. On glibc **< 2.34**,
`pthread`/`dl`/`rt`/`util` are **separate** libraries, so any link referencing
`pthread_*`, `dlopen`, `clock_*`, … must explicitly pass `-pthread`/`-ldl`/`-lrt`
or it fails to link. multispack had to inject `-pthread` for HDF5's `mirror_vfd`
precisely because of the 2.28 floor.

On glibc **>= 2.34** those symbols live in `libc`, so that class of "missing
`-lpthread`" link failures and the flag-injection workarounds largely disappear.
Moving the floor up *removes* build friction rather than adding it.

### 2. The floor is genuinely 2.34 for the whole stack — not only pthreads

Everything you ship can bind symbol nodes up to `GLIBC_2.34` (any non-pthread
symbol added in 2.29–2.34 becomes required too). An Alma 9-based build is, in
effect, a **native AL9 build** — the very thing Strategy B exists to differ from.

Concrete deployment delta (generate the exact lists with
`mspack survey` / `mspack survey table`):

- **Dropped** (glibc < 2.34): AlmaLinux/RHEL/Rocky **8**, **Debian 11**,
  **Ubuntu 20.04**, **SLES 15-SP5** and **openSUSE Leap 15.5** (all 2.31).
- **Kept** (glibc >= 2.34): EL9/EL10, Debian 12/13, Ubuntu 22.04/24.04,
  **SLES 15-SP6/SP7** (2.38), Fedora, Tumbleweed.

HPC note: the current `sles15` validator is `bci-base:15.6` (glibc 2.38), which
**survives** a 2.34 floor — but SLES 15 **SP5** and earlier do not. And you must
**drop `alma8` from `VALIDATORS`**: you cannot validate below your own floor, so
the alma8 row would fail with `GLIBC_2.34 not found` — exactly the cliff
[`test/pthread/`](../test/pthread/README.md) demonstrates.

### 3. The manylinux baseline shifts 2_28 → 2_34

Alma 8/2.28 = `manylinux_2_28`; Alma 9/2.34 = `manylinux_2_34`. This matters for
the conda/pixi export and Python-wheel interop story: your artifacts then
correspond to the narrower `manylinux_2_34` compatibility set (fewer old
consumers, though 2_34 wheels are increasingly common).

### 4. You *gain* a runtime backstop for static-TLS-surplus `dlopen` failures

A genuine upside for plugin-heavy stacks (ROOT, Wire-Cell, Phlex). The
`dlopen(...): cannot allocate memory in static TLS block` failure
([`test/tls-surplus/`](../test/tls-surplus/README.md)) is a **runtime** property
that building-on-old does *not* fix. glibc **>= 2.34** exposes the tunable
`GLIBC_TUNABLES=glibc.rtld.optional_static_tls=<bytes>` to enlarge the surplus —
and that escape hatch **does not exist on Alma 8's 2.28**. The surplus size
itself (~2 KB) is effectively constant across 2.28–2.39, so the *threshold*
doesn't move; only the availability of the runtime knob does.

### 5. Minor 2.34 semantic changes (low practical impact)

- `SIGSTKSZ`/`MINSIGSTKSZ` became **runtime** values (no longer compile-time
  constants) in 2.34; code that baked the old constant can undersize signal
  stacks. Rare, but 2.34-specific.
- A few deprecated interfaces moved/were removed (e.g. `res_*` moved into
  `libc`). Negligible for modern code.

### 6. Things that do *not* change

- The **bootstrap compiler** goes 8.5 → 11.5, but the ladder throws stage-1 away
  and rebuilds its own gcc (`gcc@12` base) regardless — **no shipped-artifact
  effect** from the newer host gcc.
- Microarch target (`x86_64_v3`), rpath/relocation, and the
  audit/buildcache/deploy pipeline are all unchanged. multispack still ships
  everything above libc and depends on nothing but the host glibc.

---

## Bottom line

Moving to Alma 9 buys a slightly simpler build (the pthread/dl/rt merge removes
link-flag workarounds) and a runtime TLS tunable you lack on 2.28, at the cost of
pushing the **entire** stack above the 2.34 cliff — dropping EL8, Debian 11,
Ubuntu 20.04 and SLES 15-SP5, and requiring `alma8` to leave the validation
matrix. Because the widest-reach floor is the whole point of Strategy B,
**Alma 8 remains the better default.** Alma 9 makes sense only when you have
deliberately decided that pre-2.34 systems (notably older SLES/EL8 HPC login
nodes) are out of scope — in which case the merge makes the build marginally
cleaner and hands you the static-TLS runtime knob for plugin-heavy hosts.

See also: [`test/pthread/`](../test/pthread/README.md) (the symbol-version
cliff, with the AL8/9/10 cross-run matrix) and
[`test/tls-surplus/`](../test/tls-surplus/README.md) (the `dlopen` TLS-surplus
failure and its mitigations).
