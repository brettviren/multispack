# pthread / glibc-2.34 symbol-version portability

This is the first of a set of **criticism-response** areas: small, self-contained,
reproducible demonstrations that test a specific objection raised against the
multispack (Strategy B) approach and show what is actually true.

## The criticism

> "I know for a fact that binaries built on AL9 do not necessarily work on AL10,
> because of differences in the exported symbol set for pthreads."
> — a DUNE collaborator

The concern is real and important, but **the direction is inverted**. The failure
is *newer → older* (a binary built on a newer glibc fails on an older one), not
*AL9 → AL10*. And the fix for it is exactly what multispack already does: build on
the **oldest** supported glibc.

## Background: the glibc 2.34 pthread merge

glibc uses **symbol versioning**. Every exported symbol carries a version node
like `GLIBC_2.28`, and glibc never removes old version nodes. This gives
**forward compatibility**: a binary linked against symbols up to `GLIBC_2.X`
runs on any glibc `>= 2.X`. It gives **no backward compatibility**: a binary that
needs `GLIBC_2.Y` will not load on a glibc `< 2.Y` (the loader reports
`version 'GLIBC_2.Y' not found`).

In **glibc 2.34** (2021) the threading library `libpthread` was **merged into
libc**. The pthread functions (`pthread_create`, `pthread_join`, …) that had
lived in `libpthread.so.0` since forever — versioned `GLIBC_2.2.5` — were given a
**new default version node `GLIBC_2.34`** inside `libc.so.6`. Consequences:

- A program **newly compiled on glibc >= 2.34** binds the pthread symbols at
  `@GLIBC_2.34` and therefore **requires glibc >= 2.34 to run**.
- A program **compiled on glibc < 2.34** binds them at `@GLIBC_2.2.5`, which is
  present on *every* glibc — so it runs on old and new systems alike.
- On glibc >= 2.34, `libpthread.so.0` remains as an empty forwarding stub, so old
  binaries that still `DT_NEEDED` it keep working.

Relevant distro glibc versions:

| Distro | glibc | note |
|---|---|---|
| AlmaLinux 8 | 2.28 | pre-merge; multispack's build floor (= `manylinux_2_28`) |
| AlmaLinux 9 | 2.34 | **the merge release** |
| AlmaLinux 10 | 2.39 | post-merge |

## The tests

- `test.c` — a tiny program calling `pthread_create/join/key_create/once`.
- `Containerfile.al8`, `Containerfile.al9`, `Containerfile.al10` — each compiles
  `test.c` against that distro's glibc (with `-pthread`, the same build-time flag
  multispack had to inject for HDF5 on the 2.28 floor).
- `run.sh` — builds the three binaries, prints the versioned pthread symbols each
  one requires, then **runs every binary under every distro's glibc** and checks
  the result against the forward-compatibility rule (*runs iff host glibc >=
  build glibc*). It exits non-zero if any cell violates the rule.

Run it:

```sh
cd test/pthread
./run.sh            # needs podman (or ENGINE=docker) + network to pull almalinux:{8,9,10}
```

## Results

**pthread symbols required by each binary** (`readelf --dyn-syms`):

| built on | glibc | requires |
|---|---|---|
| AL8  | 2.28 | `pthread_create@GLIBC_2.2.5`, `pthread_join@GLIBC_2.2.5`, `pthread_key_create@GLIBC_2.2.5`, `pthread_once@GLIBC_2.2.5` |
| AL9  | 2.34 | `pthread_*@GLIBC_2.34` |
| AL10 | 2.39 | `pthread_*@GLIBC_2.34` (note: **2.34, not 2.39** — pthread's node froze at the merge) |

**Cross-run matrix** — does the binary built on `<row>` run under `<col>`'s glibc?

| built-on ↓ / run-on → | AL8 (2.28) | AL9 (2.34) | AL10 (2.39) |
|---|---|---|---|
| **AL8 (2.28)** | RUN | RUN | RUN |
| **AL9 (2.34)** | FAIL `GLIBC_2.34` | RUN | RUN |
| **AL10 (2.39)** | FAIL `GLIBC_2.34` | RUN | RUN |

## How the results support the conclusions

1. **The criticism as stated is false.** The AL9-built binary **runs on AL10**
   (row AL9, col AL10 = RUN). glibc never drops version nodes, so `2.34 ⊆ 2.39`.
   "Built on AL9 doesn't run on AL10" does not happen.

2. **There is a real cliff — in the opposite direction.** AL9- and AL10-built
   binaries **fail on AL8** (`GLIBC_2.34 not found`). The break is *newer → older*
   (build on `>= 2.34`, run on `< 2.34`: AL8, EL7, older SLES). The collaborator
   saw this real effect and mislabeled which side is which.

3. **Building on the oldest glibc is the mitigation — and it is what multispack
   does.** The AL8 binary requires only `pthread_*@GLIBC_2.2.5` and **runs on AL8,
   AL9 and AL10** (top row all RUN). multispack builds on AlmaLinux 8 / glibc 2.28
   (the `manylinux_2_28` baseline), so its binaries sit *below* the 2.34 cliff.
   A native **AL9** build — e.g. the standard DUNE build — is the one exposed to
   this problem: it won't run on AL8/EL7. So, correctly understood, the criticism
   is an argument **for** multispack's floor, not against it.

4. **The cliff is at 2.34 specifically, not "every release."** The AL10 binary
   requires only `GLIBC_2.34` for pthread and still runs on AL9. The general rule
   remains: build against the *oldest* glibc you must support and rely on forward
   compatibility.

## The `-pthread` we had to inject

On glibc < 2.34, pthread is a *separate* `libpthread`, so any link that references
`pthread_*` must pass `-pthread`/`-lpthread` or it fails to link — this is why the
multispack build needed it for HDF5's `mirror_vfd` on the AL8 floor. It is a
**build-time linking** requirement, unrelated to runtime symbol-version
portability. The binary it produces (`pthread_*@GLIBC_2.2.5` + a `DT_NEEDED` on
the `libpthread.so.0` stub) is the *maximally* forward-portable form — which is
exactly why the AL8 binary still runs on AL9 and AL10.

## Honest limits (so this isn't oversold)

- Forward, not backward: an **AL10**-built binary can still fail on AL9 for
  *non-pthread* symbols introduced in glibc 2.35–2.39. That is why you build on
  the oldest target, never the newest.
- glibc has, very rarely, *removed* deprecated symbols (e.g. `gets`; `res_*`
  moved to libc in 2.34). Building old also avoids acquiring those.
- This is a **glibc** story only. It says nothing about **musl** (Alpine), which
  is the deliberate `xfail` in the main validation matrix.

## If you genuinely saw an AL9 → AL10 failure

This test uses a trivial binary that needs only `libc.so.6` and public
`pthread_*@GLIBC_2.34` symbols, so it *cannot* reproduce every real-world
failure. A **pure public-symbol** forward break from AL9 to AL10 is essentially
impossible (glibc 2.34 ⊆ 2.39), so if you saw a real failure it was most likely
one of these — none of which this toy captures, and most of which multispack's
design specifically avoids:

1. **Direction was actually reversed** — built on AL10 (or a newer container/
   toolchain) and run on AL9, or built on AL9 and run on EL8/EL7. That *is* the
   cliff this test shows. (Most common.)
2. **A non-glibc dependency changed.** EL10 is a major rebase: a system library
   the binary links may have a bumped SONAME or be dropped, giving
   `libfoo.so.N: cannot open`. Unrelated to pthreads, easy to misattribute.
   **multispack ships its own libraries and depends on nothing but glibc, so this
   class cannot affect it** — the `audit` phase (elfaudit) verifies exactly that.
3. **`GLIBC_PRIVATE` references.** glibc's *internal* symbols are not stable
   across versions; a low-level or old proprietary library that links glibc
   internals can fail with a glibc/pthread symbol error. Normal code (and this
   test) uses none; multispack does not ship glibc, so it is largely immune.
4. **Static-TLS-surplus exhaustion** (`dlopen: cannot allocate memory in static
   TLS block`). Plugin-heavy apps (ROOT!) with initial-exec TLS can exceed
   glibc's reserved static-TLS block, and the surplus computation changed across
   glibc versions. This is threading-adjacent and a *genuine residual risk* that
   this test does **not** cover — a candidate for its own `test/` area.
5. **`SIGSTKSZ`/`MINSIGSTKSZ` became runtime values in glibc 2.34.** Code that
   baked the old compile-time constant can undersize signal stacks and crash.

**To pin it down, get the collaborator's actual evidence:** the exact error
text, and `ldd` + `readelf -V` (or `-d`) of the *failing binary* on AL10. That
distinguishes a missing glibc version (1) from a missing/renamed dependency (2),
a `GLIBC_PRIVATE` mismatch (3), or a runtime TLS error (4) in one look.
