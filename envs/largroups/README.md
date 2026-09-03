# largroups — a factored, grouped LArSoft stack

A hand-curated, **native-Spack** grouped environment capturing the intent of
`reference/larsoft-release-configs/unified-spack.yaml` (the art + nusofthep +
larsoft layers), structured so `spack -e . concretize && spack -e . install`
builds the whole stack. No custom tooling is needed to concretize this — it is
plain Spack `group:`/`needs:`.

## Layout & why it is split this way

Spack's `include:` merges **config sections** (`definitions:`, `packages:`,
`repos:`, `concretizer:`, …) but **not** the `specs:` list. So the group *DAG*
must live in the main file, while everything else factors out:

| file | holds | role |
|---|---|---|
| `spack.yaml` | `specs:` `group:`/`needs:` DAG, `config`, `toolchains`, `concretizer`, `include:` | **structure** |
| `groups/*.yaml` | `definitions:` — each group's member spec list | **membership** |
| `packages.yaml` | `packages:` — all version/variant pins | **versions** |
| `repos.yaml` | `repos:` by upstream git URL | recipes (default) |
| `repos-dev.yaml` | `repos:` by local clone path | recipes (hacking) |

This is the decoupling: version churn touches only `packages.yaml`; adding/removing
a package touches only a `groups/*.yaml`; the DAG in `spack.yaml` stays put.

## The group DAG

```
base ── wire-cell-toolkit ┐
  │                        ├─ larsoft
  └─ art ── nusoft ────────┘
```

- `base` — `python root boost` built once with `%build_compiler`; every group
  `needs: [base]`.
- `art` (was `st_larsoft_0`), `nusoft` (`st_larsoft_1`), `larsoft`
  (`st_larsoft_2`), `wirecell` — the deployment layers.
- `larsoft needs: [nusoft, wirecell]` (larwirecell → wire-cell-toolkit).

`%build_compiler` (gcc@12) on every group member makes the **whole** DAG one
compiler — wire-cell-toolkit, boost, root included — so the layers actually share.

## Two hard lessons baked into `packages.yaml`

1. **A `packages: require` pins in all cases; a base group does not.**
   `needs:` reuse is *opportunistic* — `base` built `boost@1.89` (newest) but the
   art/lar stack requires `boost@1.82`, so it ignored base and forked. Only a
   `packages: boost: require: '@=1.82.0'` (and the same for `eigen`) collapses the
   fork. **To force one version of a shared lib, pin it in `packages:`** — the base
   group only guarantees a single *build* of whatever version already agrees.

2. **`python` must be pinned to a series, not a range, and then conflicts must be
   fixed.** The reference used `@:3.11` (a *range* admitting 3.10); pinning
   `@3.11` forced the runtime python to 3.11.14 and, as a side effect, pulled a
   3.11-capable `py-tensorflow@2.19` instead of the `@2.10` (python ≤3.10) that
   the DNN lar packages otherwise drag in. Remaining `python@3.10.19` is **build
   only** (for `llvm`/`ninja`) with zero link/run parents — it never enters a
   view, so it is harmless; chase it only if you want a single python in the store.

Verified: after the pins, every **link/run** (deployable) version is single —
`python 3.11.14`, `boost 1.82.0`, `eigen 3.4.1`, `root 6.28.12` — across all 402
concrete nodes. `concretize` returns 0.

## Build

```
./multispack.sh makenv envs/largroups/spack.yaml   # concretize + install (long)
./multispack.sh runenv largroups                   # activate + interactive shell
```

`runenv` only *activates* the env; since it is `view: false`, run
`spack load larwirecell` (etc.) to put `lar`/`wire-cell` on `PATH`. On a minimal
distro, build a `-run` image first (bare distro + python3 + git):
`./multispack.sh images debian13-run && ./multispack.sh runenv --image debian13-run largroups`.

Built successfully (362 specs) with these recipe/config resolutions baked in:

- **nusofthep** → `repos.yaml` points at the `brettviren/nusofthep-spack-recipes`
  fork, branch `fix-libxml2-in-spack` (upstream dk2nugenie mis-sets `LIBXML2_FQ_DIR`).
- **triton** → `repos.yaml`'s `fnal_art` points at a LOCAL clone under `recipes/`
  (git-ignored) carrying `depends_on("zlib")` (upstream cc-clients can't find ZLIB).
  Re-create it before building on a fresh checkout, or swap to a fork once the PR lands:
  `git clone -b develop https://github.com/FNALssi/fnal_art recipes/fnal_art` then
  add the zlib dep to `recipes/fnal_art/spack_repo/fnal_art/packages/triton/package.py`.
- **artg4tk** → `packages.yaml` pins `@=13.00.00` (the pinned `13.0.1` has no recipe).
- **larrecodnn (+ the `larsoft` meta) DROPPED** — see `groups/larsoft.yaml`.
  larrecodnn 10.05.01 hard-requires TensorFlow (`find_package(TensorFlow REQUIRED)`
  + its C++ uses it), and `py-tensorflow@2.19` needs gcc≥13 (`-mavxvnniint8`), so it
  cannot build in this gcc@12 stack. Restore both once a gcc@12-buildable TensorFlow
  (or a larrecodnn that makes TF optional) exists.

## Still loose / open

- larrecodnn / the `larsoft` umbrella (see above) — pending a TensorFlow story.
- Build-only tool forks (python@3.10 for llvm/ninja, py-cython) — benign; pin their
  build deps only if a single-python *store* matters.
## Analyze / check

`viewgroups` is now a read-only analyzer over this env (needs-DAG sanity, effective
`concretizer:reuse`, multi-version link/run-vs-build-only forks, unpinned shared
libs, per-view collisions, cross-group sharing):

```
./multispack.sh viewgroups --concretize --report --require-single python envs/largroups
```

`--concretize` re-solves (else it reuses `spack.lock`); `--require-single PKG` and
`--strict-single` make it exit non-zero on a deployable multi-version fork. It
reports the *effective* reuse, confirming the env's `reuse:false` overrides the
site scope's `reuse:true`. Verified on this env: reuse=False, single link/run
python 3.11.14 / boost 1.82.0 / eigen 3.4.1, no link/run forks.
