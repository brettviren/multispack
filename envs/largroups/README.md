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
spack -e envs/largroups concretize -f      # succeeds as-is (repos.yaml, git recipes)
spack -e envs/largroups install            # needs the nusofthep patch below
```

`install` requires the dk2nugenie fix (upstream nusofthep's genie CMake uses an
unset `LIBXML2_FQ_DIR`). Clone + patch nusofthep-spack-recipes under `recipes/`
and switch the `spack.yaml` include from `repos.yaml` to `repos-dev.yaml`.

## Still loose / open

- Everything is inside a group; the only remaining multi-version items are
  build-only tool forks (python@3.10 for llvm/ninja, py-cython) — pin their build
  deps if a single-python *store* matters.
- `viewgroups` is being refocused into an **analyzer/checker** that reads this raw
  yaml + the `spack.lock` and reports exactly these findings (per-view multi-version
  collisions, build-only forks, unpinned shared libs, needs-DAG sanity).
