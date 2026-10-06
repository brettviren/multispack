# Deriving Spack `group:`/`needs:` from declared views

A plan for a tool that consumes several experiments' partly-specified
`spack.yaml` seed sets, takes a **declared partition of seeds into views**
(deployment units), and mechanically emits a single merged `spack.yaml` whose
`group:`/`needs:` structure maximizes cross-view build reuse and minimizes
incremental solve time.

## Scope and premises

- Each experiment supplies seed specs (versions/variants partly given; the rest
  free for Spack to choose). Each experiment's set is assumed to concretize
  correctly in isolation.
- We take the seeds **at face value**: whatever is underspecified is Spack's to
  choose. We do *not* attempt cross-experiment version consensus — genuine
  constraint disagreement is an input, not a problem the tool solves.
- The **one** irreducible human input is a partition of seeds into **views**:
  sets of specs that must be co-usable in a single flat, collision-free
  directory (i.e. `unify:true` over the set). A view is a *deployment intent*,
  not a graph property, so it cannot be derived — only declared, validated, and
  informed. Everything downstream is mechanical.

## Key facts this design rests on

Verified against Spack 1.2.2 in `extern/spack` (see
`lib/spack/spack/environment/environment.py` and `.../solver/asp.py`).

1. **Groups = per-group clingo solves.** The environment concretizer runs one
   solve per `group:` in `needs:` topological order (`_order_groups`,
   `_concretize_single_group`). `unify:` applies *within* a group, not across.
2. **`needs:` forces reuse.** Specs from a `needs:` group are injected into the
   downstream solve as *must-reuse*, regardless of `concretizer:reuse`
   (`ReusableSpecsFactory.__call__`, environment.py). This is how a base group's
   concrete nodes get *frozen* for dependents.
3. **Determinism.** Concretization is deterministic given fixed inputs (repo
   state, `packages:` config, reuse set, clingo version). Identical
   underspecified seeds concretize to identical nodes. All probes must run under
   one shared config scope or this guarantee breaks.
4. **Merkle hashing.** A `dag_hash` is the content hash of a node's whole
   closure: same hash ⟺ identical subgraph. "Find common subgraphs" therefore
   reduces to **intersecting `dag_hash` sets** — no subgraph isomorphism.
5. **Support monotonicity.** Along a dependency edge X→Y, anyone containing X
   contains Y, so `support(Y) ⊇ support(X)`. Deep/most-depended-upon nodes have
   the *largest* support (→ base groups at the bottom); view-specific nodes have
   the smallest support (→ leaf groups needing the bases).
6. **A view demands `unify:true`.** Two prefixes projecting to one flat path
   collide, so a view is effectively one-version-per-package. Hence each
   declared view-unit is concretized with `unify:true`, and we probe at the
   same unification level we deploy at (no optimistic-probe gap).
7. **Multiple named views select groups.** An environment's `view:` may hold
   several named views, each selecting a set of groups (as the repo's
   `extern/envs/gcc15/spack.yaml` does with `view: default: group: [main, apps]`).

## The algorithm

Atoms are **view-units** (declared bags of seeds), not individual seeds.

1. **Ingest & merge.** Parse each input `spack.yaml`; collect seeds with
   provenance (experiment + declared view-unit label). Reconcile environment
   globals (`repos:`, `packages:`, `config:`, `concretizer:`) into one scope;
   flag any hard global conflicts (e.g. incompatible `packages:require:`,
   `providers:`, `target`) as human touchpoints — they are not resolvable here.
2. **Per-unit concretize (`unify:true`).** Concretize each view-unit
   independently under the shared config. Result: one consistent DAG per unit.
   These runs are independent → parallelizable.
3. **Shared-subgraph detection.** For every concrete node, compute its
   **support set** = the set of view-units whose closure contains that
   `dag_hash`. Intersection is plain set intersection on hashes.
4. **Support-set lattice → groups.** Partition nodes by support set.
   - Nodes with support = *all units* → deepest common **base group**.
   - Each distinct multi-unit support class (e.g. `{FD,ND}`) → a base group that
     `needs:` the larger-support classes it depends on.
   - Nodes with singleton support `{U}` → view-unit `U`'s own leaf group.
   - `needs:` edges follow the support-*superset* relation (larger support is
     more-depended-upon → sits below).
   - Optionally coalesce tiny classes to limit group count (a solve-time vs.
     clarity knob; see Objectives).
5. **Emit frozen bases.** Write each base group's specs as the
   **fully-concretized specs (or `/hash` references)** from step 2 — not
   abstract specs — so the base group deterministically rebuilds exactly those
   nodes and `needs:` freezes them for dependents.
6. **Emit views.** One named `view:` per declared view-unit, selecting
   `{base groups it needs} ∪ {its own leaf group}`.
7. **Verify (mandatory).** Re-concretize the generated grouped environment.
   Confirm: (a) each group solves against its frozen `needs:` bases; (b) each
   view is collision-free; (c) the realized shared `dag_hash`es match the
   prediction from step 3. On any mismatch, demote the offending view-unit to a
   self-contained group (drop it from the conflicting base) and re-emit.

## Checking the view partition

The partition is declared, but the tool validates and informs it (decision
support, not derivation):

- **Feasibility:** each declared unit must concretize collision-free under
  `unify:true`. If not, it cannot be one view — report the colliding package(s).
- **Forced splits (derivable):** compute the conflict structure and report
  "needing both X@v1 and X@v2 forces ≥2 views." The *must*-split is derivable
  even though the *want*-split is not.
- **Redundancy:** if two declared-separate units concretize identically, flag
  them as mergeable (they would share a view for free).
- **Bounds:** compute the coarsest feasible partition (merge everything that
  unifies) and the finest (every seed alone); report reuse/solve cost at each so
  the human can place their intent between them.

## Objectives (what "optimal" means here)

- **Build reuse** ≈ minimize distinct concrete nodes = maximize shared bases.
  Already handled by the intersection; groups never *improve* reuse beyond a
  single unified solve — they can only cap it — so the base extraction is the
  reuse lever.
- **Incremental solve time** ← freeze large stable bases (`needs:`) so
  re-concretizing a view only re-solves its leaves; and keep each group's free
  node count small. Pushes toward *more, smaller* groups.
- **Tension:** more groups speed re-solves but risk forced build forks; fewer
  groups share more builds but grow per-solve size. The coalescing step (4) is
  where this trade-off is exposed; emit a small Pareto set with predicted
  {#distinct-builds, est. build cost, #groups, #solve-rounds} rather than a
  single answer.

## Implementation phases

**Phase 0 — probe harness (foundation).**
Drive `spack.concretize` (or `spack concretize`/lockfile output) per unit under
a shared config scope; extract concrete DAGs and `dag_hash`es. Deliverable: a
function `unit -> {dag_hash: concrete_spec}` and closure edges.

**Phase 1 — view checker.**
Feasibility, forced-split, redundancy, and coarsest/finest bounds reporting over
the Phase 0 probe. Standalone value on existing envs; no emission yet.

**Phase 2 — support lattice & group synthesis.**
Support sets, partition, `needs:` DAG, coalescing knob. Deliverable: an
in-memory group model + predicted reuse/solve stats.

**Phase 3 — emitter.**
Serialize the group model to `spack.yaml`: frozen base groups (concrete
specs/`/hash`), leaf groups, `needs:`, and one named `view:` per unit. Merge
reconciled globals.

**Phase 4 — verify & repair.**
Re-concretize the emitted env; check group solvability, view collision-freedom,
and predicted-vs-realized shared hashes; auto-demote conflicting units and
re-emit; report residual divergence honestly.

## Data model (sketch)

```
Seed        { spec: str, experiment: str, unit: str }
ViewUnit    { label: str, seeds: [Seed], config_scope }
UnitSolve   { unit: str, nodes: {dag_hash: ConcreteSpec}, edges }
Node        { dag_hash, name, version, support: set[unit] }
Group       { name, specs: [concrete|abstract], needs: [group], frozen: bool }
Plan        { groups: [Group], views: {name: [group]}, stats, warnings }
```

## Open questions / risks

- **Global-config reconciliation** across experiments (`packages:require:`,
  `providers:`, `target`) can be irreconcilable; this is a human touchpoint the
  tool can only detect, not resolve.
- **Probe reuse config:** decide whether the probe runs with
  `concretizer:reuse` on (reflects an existing store, biases toward real
  sharing) or off (context-free preferred nodes). Must be *uniform* across
  units regardless.
- **Expressing frozen bases:** confirm the cleanest way to pin a base group to
  exact nodes in Spack 1.2.x — concrete spec strings vs `/hash` references vs an
  included concrete environment — and that `needs:` reliably reuses them.
- **Coalescing policy:** the group-count knob is the one remaining tunable; its
  default (e.g. "one group per distinct support set, merge classes below N
  nodes") needs empirical calibration against real experiment stacks.

## Non-goals

- Cross-experiment version consensus / renegotiating seed constraints.
- Choosing the view partition itself (declared, not derived).
- Picking the point on the reuse/solve Pareto frontier (a policy choice; the
  tool presents, the human selects).
