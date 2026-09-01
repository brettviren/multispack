# static-TLS-surplus `dlopen` failures

> **TLS here = Thread-Local Storage** (the `__thread` / C++ `thread_local`
> storage class — a variable with one instance per thread). This has **nothing
> to do with Transport Layer Security / SSL.** Unfortunate acronym collision.

This area exists because the [`pthread/`](../pthread/) test's "honest limits"
flagged the **static-TLS surplus** as a real, threading-related `dlopen` failure
mode that the symbol-version story does *not* cover — and the most likely thing a
plugin-heavy host actually hits. Both **Wire-Cell Toolkit** and especially
**Phlex** (one component per shared library, so *many* `dlopen`s) are exactly the
shape of program that can trip it.

## Background: TLS access models and the static TLS block

A thread-local variable is accessed under one of three models:

- **global-dynamic** — the default for shared libraries. TLS is found through
  `__tls_get_addr` and allocated **lazily/dynamically** per (module, thread). Any
  number of libraries can be `dlopen`ed.
- **initial-exec** — faster: the variable lives at a **fixed offset in the
  per-thread *static TLS block*** that is laid out at program/thread start. A
  library using initial-exec TLS must be given a slot in that static block.
- **local-exec** — for the main executable only.

The static TLS block is a fixed size per thread. For libraries `dlopen`ed
*after* startup, glibc can only place their initial-exec TLS in a small reserved
**"static TLS surplus"** (glibc ≥ 2.34 default `glibc.rtld.optional_static_tls`
≈ **2048 bytes**). A `dlopen`ed library whose initial-exec TLS exceeds what
remains fails outright:

```
dlopen(...): cannot allocate memory in static TLS block
```

Where does initial-exec TLS come from in real stacks? Explicit
`-ftls-model=initial-exec` / `__attribute__((tls_model("initial-exec")))`, some
performance-sensitive libraries, and — commonly — **OpenMP** (`libgomp`) and some
threading runtimes. A plugin that pulls one of those in can carry initial-exec
TLS without anyone intending it.

## The tests

- `tlslib.c` — a library with a `__thread` block of `TLS_BYTES`, built two ways:
  `-DIE_TLS` (initial-exec) and default (global-dynamic).
- `probe.c` — `dlopen`s `<prefix>NNN.so` until one fails; prints how many opened.
- `build.sh` / `Containerfile.build` — compile the probe + N libraries of each
  model, once, on AlmaLinux 8 (so the artifacts run on AL8/9/10).
- `run.sh` — runs the probe under AL8/AL9/AL10 glibc for both models and
  **self-validates** (global-dynamic must open all N; initial-exec must hit the
  cap).

Run it:

```sh
cd test/tls-surplus
./run.sh                       # podman (or ENGINE=docker) + network for almalinux:{8,9,10}
TLS_BYTES=1024 ./run.sh        # the other regime: small IE blocks all load
```

## Results

With `TLS_BYTES=4096` (a 4 KB initial-exec block, above the ~2 KB surplus):

| distro (glibc) | initial-exec | global-dynamic |
|---|---|---|
| AL8 (2.28)  | **0** of 32 | 32 of 32 |
| AL9 (2.34)  | **0** of 32 | 32 of 32 |
| AL10 (2.39) | **0** of 32 | 32 of 32 |

```
dlopen #0 (ie-lib000.so) failed: cannot allocate memory in static TLS block
```

Sweeping `TLS_BYTES` puts the threshold right at the surplus: at **≤ 1 KB** the
initial-exec libraries all load; at **≥ 2 KB** the very first `dlopen` fails.

## How the results support the conclusions

1. **The failure mode is real and is about initial-exec TLS in `dlopen`ed libs.**
   The identical library differs only in TLS model: initial-exec cannot be
   `dlopen`ed once its block exceeds the surplus; global-dynamic always can. A
   Phlex-style host (many `dlopen`s) is precisely where cumulative initial-exec
   TLS can run the surplus out.
2. **The fix is global-dynamic TLS** (the compiler default). All 32 global-dynamic
   libraries `dlopen` on every distro. Building plugin/component libraries with
   global-dynamic TLS — i.e. *not* passing `-ftls-model=initial-exec`, and being
   careful about statically-linked OpenMP/threading runtimes that use it —
   removes the ceiling. Because multispack controls the whole build, it can
   enforce this across the stack.
3. **A runtime backstop exists** on glibc ≥ 2.34: enlarge the surplus with
   `GLIBC_TUNABLES=glibc.rtld.optional_static_tls=<bytes>` (e.g. set it in the
   deployed `env.sh`). Not available on AL8's glibc 2.28.

## Honest limits

- **This run does NOT reproduce version-dependence.** AL8, AL9 and AL10 behave
  identically (surplus ≈ 2 KB on all three), so by itself it does *not* explain
  "works on AL9, fails on AL10." The surplus *has* changed across glibc history,
  so a workload crossing the threshold between very different glibcs (e.g. EL7's
  2.17, or via the tunable existing on 2.34+ but not 2.28) is possible — but
  between 2.28 and 2.39 it is effectively constant. If your collaborator's case
  was version-specific, get the exact error: a `static TLS block` message points
  here; a `GLIBC_2.x not found` points at [`pthread/`](../pthread/)'s cliff; a
  `libX.so.N: cannot open` points at a dependency rebase.
- **It is a per-module-size demonstration.** Real failures are often *cumulative*
  across many small initial-exec plugins; glibc's per-`dlopen` growth absorbs
  some of that, which makes the cumulative case harder to synthesize cleanly than
  the single-oversized-module case shown here.
- This is a **runtime** property of the glibc the program runs under, so unlike
  the symbol-version cliff, building on the oldest glibc does **not** by itself
  avoid it. The mitigation is the *build model* of the plugins (global-dynamic)
  plus, optionally, the runtime tunable — both of which multispack can own.
