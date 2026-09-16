# Strategy A vs Strategy B — erasing the build distro from the output

multispack's core goal is to make a Spack-built software stack whose binaries do
**not** depend on the Linux distribution they were built on. Two strategies were
considered for that. multispack implements **Strategy B**; this note records what
**Strategy A** was, how it would have worked, and the trade-offs that led to
choosing B.

The one-line difference: **how much of the C library stack you ship.**

- **Strategy B** — use the *host's* `glibc` and dynamic loader; build and ship
  everything *above* libc; target the oldest glibc you must support.
- **Strategy A** — build and ship your *own* `glibc` **and** dynamic loader, so
  the host provides only the kernel.

Both ship everything above libc from Spack, and both must still take a handful of
kernel/driver-bound libraries from the host (`libcuda`, `libGL`, verbs, PMI) —
those can never be shipped by anyone. They differ only at the libc/loader layer.

---

## Strategy A — bring your own glibc and loader

### The idea

Build `glibc` (and its dynamic loader / program interpreter, `ld-linux-x86-64.so.2`)
from source as part of the stack, and make every shipped executable resolve its
libc — and *itself* start up — through that shipped loader rather than the host's.
The host then contributes only the **kernel**: the syscall ABI, which is stable
and forward-compatible. This is "maximum erasure" of the build distribution.

This is essentially **EESSI's compatibility layer** (a Gentoo-Prefix userspace
mounted at a fixed CVMFS path) and is closely related to how **Nix** and
**conda** relocate binaries onto their own loader.

### The runtime tricks that make the host accept it

An ELF executable names its dynamic loader in a header field, `PT_INTERP` — an
**absolute path string** baked into the binary (e.g. `/lib64/ld-linux-x86-64.so.2`).
The kernel reads that path and hands control to that loader, which then resolves
the rest of the libraries. Strategy A has to redirect this to the shipped loader:

1. **Repoint `PT_INTERP`** of every executable to the shipped
   `.../lib/ld-linux-x86-64.so.2` (e.g. `patchelf --set-interpreter`), *or* launch
   each program by invoking the loader explicitly
   (`.../ld-linux.so --library-path ... ./prog`) via wrapper scripts.
2. **Point `RPATH`/`RUNPATH`** at the shipped `lib` dirs so the shipped `glibc`,
   `libstdc++`, etc. are found ahead of anything on the host.
3. Because `PT_INTERP` is an **absolute path** and cannot be made
   `$ORIGIN`-relative, the install tree must live at a **fixed absolute prefix**.
   In practice that means delivering it at a stable mount point (CVMFS, a
   squashfs image, a container) rather than as a tarball a user unpacks anywhere.
4. **Host injection** for the driver/kernel-bound libraries (`libcuda`, `libGL`,
   verbs, PMI) — same requirement as Strategy B — via a `host_injections`-style
   symlink or `LD_LIBRARY_PATH`.

### What that buys and what it costs

Because the program never touches the host `glibc`, Strategy A runs on **any**
Linux kernel regardless of the host's C library: ancient glibc (EL7/SL6), future
distros, **and even musl hosts** (Alpine). In multispack's validation matrix, the
`alpine` (musl) row is a deliberate *expected failure* that marks the Strategy B
boundary — **Strategy A is exactly what would turn that row green.** There is also
no glibc "floor" and therefore none of the symbol-version cliffs (e.g. the
[glibc 2.34 pthread merge](base-glibc-floor.md)) apply.

The costs are what ruled it out here:

- **You must build glibc + loader from source.** Spack has no well-trodden
  glibc-from-source path; this is a large, ongoing complexity and maintenance
  burden.
- **You lose relocatability.** The absolute `PT_INTERP` forecloses the
  `$ORIGIN`-relative, unpack-anywhere tarball that Strategy B delivers — and with
  it the Spack buildcache-over-HTTP and the conda/pixi export paths, which all
  assume a relocatable tree. Deployment narrows to fixed-mount delivery.
- **You now own glibc.** Its CVEs and its minimum-kernel requirement become your
  responsibility to track and rebuild, instead of the host distro patching libc
  underneath you.

---

## Strategy B — use the host glibc (implemented here)

Ship everything above libc; use the host's `glibc`/`ld.so`; set the floor to the
oldest glibc you must support. The full contract:

| Layer | Provided by | Notes |
|---|---|---|
| kernel | host | syscall ABI is stable forward |
| `glibc` / `ld.so` | **host** | floor set by the build base image |
| kernel/driver-bound libs | host | `libcuda`, `libGL`, verbs, PMI — never shippable |
| everything else | **Spack** | compiler, `libstdc++`, python, ROOT, all of it |

It works because glibc guarantees **forward** compatibility through symbol
versioning: a binary linked against glibc 2.28 runs on 2.34, 2.36, 2.41, … It does
**not** run below its floor, and it does **not** run on musl. Picking glibc 2.28
(AlmaLinux 8) makes the floor the `manylinux_2_28` baseline — "the same
portability guarantee as a pip wheel." See [base-glibc-floor.md](base-glibc-floor.md)
for the implications of moving that floor up, and `PLAN.md` §1 for the full
rationale.

---

## Pros / cons at a glance

| Dimension | Strategy A (own glibc+loader) | Strategy B (host glibc) — chosen |
|---|---|---|
| Host dependency | kernel only | kernel + glibc (host) |
| Runs on musl / Alpine | **yes** | no (deliberate `xfail`) |
| Runs below the build glibc (EL7/SL6, old SLES) | **yes** | no — bounded by the floor |
| glibc symbol-version cliffs (e.g. 2.34 pthreads) | none | present (mitigated by an old floor) |
| Relocatable (unpack-anywhere tarball, `$ORIGIN` rpath) | **no** — fixed absolute prefix | **yes** |
| Deployment options | fixed mount (CVMFS/squashfs/container) | fixed mount **and** relocatable tarball, buildcache-over-HTTP, conda/pixi |
| Build complexity | high — glibc+loader from source, no Spack path | low — Spack builds above libc |
| Who patches glibc CVEs | **you** | the host distro |
| "manylinux == pip wheel" pitch | n/a | **yes** |
| Prior art | EESSI compat layer; Nix; conda | manylinux; standard portable-binary practice |

---

## Why B, and when A would be worth it

**Strategy B was chosen** because it is dramatically simpler (no glibc from
source), it keeps binaries **relocatable** — which is what enables the tarball,
buildcache, and conda/pixi deployments — and it comes with a one-sentence,
ecosystem-backed portability pitch (`manylinux_2_28`). The only thing it gives up
is running on hosts *older than the floor* or on *musl*, both of which are out of
scope for the target audience (Alma/Debian/Ubuntu/SLES HPC and desktop nodes with
glibc ≥ 2.28). The Alpine/musl `xfail` in the validation matrix marks that
boundary precisely and on purpose — do not "fix" it.

**Strategy A becomes worth its cost** only if the requirements change to include:

- must run on **musl** hosts (Alpine) or on hosts with glibc **older** than any
  acceptable floor;
- a delivery model that is already a **fixed mount** (CVMFS/squashfs), so losing
  tarball relocatability costs nothing;
- willingness to own a glibc-from-source build and its security lifecycle.

At that point the design converges on the EESSI pattern (a compat-layer userspace
at a fixed CVMFS prefix, with a `v2`/microarch tree selected at runtime by
`archspec`). Absent those requirements, B is the better fit.

See also: `PLAN.md` (§1 "Strategy B in one page", §7 "Deliberately not done"),
[base-glibc-floor.md](base-glibc-floor.md), and the criticism-response
experiments in [`../test/pthread/`](../test/pthread/README.md) and
[`../test/tls-surplus/`](../test/tls-surplus/README.md).
