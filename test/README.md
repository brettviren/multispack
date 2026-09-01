# `test/` — criticism-response demonstrations

This directory is **not** the validation suite (that is `tests/`, the POSIX-sh
scripts run inside the validator containers). It is a growing set of small,
self-contained, **reproducible experiments**, each of which takes a *specific
objection* raised against the multispack / Strategy B approach and shows, with
evidence, what is actually true.

The audience is a skeptic: the goal of each area is a document and a script you
can hand to a collaborator so they can re-run it themselves and see the result.

## Convention

One subdirectory per criticism, named for its topic. Each contains:

| File | Purpose |
|---|---|
| `README.md` | The criticism (quoted), the technical background, what the tests do, the results, **how the results support the conclusion, and honest limits** of what the test does *not* cover. |
| `Containerfile.*` | The reproducible build/runtime environments the test needs. |
| `run.sh` | Builds everything and runs the experiment. It **self-validates**: it encodes the expected outcome as a rule and exits non-zero if any result violates it, so it doubles as a regression test. |
| `test.c` / other sources | Whatever the experiment compiles or runs. |

Guidelines:

- **State the limits.** Every area's README must include an "honest limits"
  section. A demonstration that overclaims is worse than none — a skeptic will
  find the gap. Prefer "here is exactly what this proves, and here is what it
  does not" over a clean but brittle victory.
- **Self-validating `run.sh`.** Derive the expected result from the underlying
  rule (e.g. glibc forward-compatibility) and check the empirical result against
  it, rather than hard-coding a pass/fail table. That way the script keeps its
  value if the toolchain or distro versions move.
- **Standalone.** An area should run from stock images (or the multispack
  builder) without needing a completed multispack build, so a collaborator can
  clone and run it in isolation.

## Areas

| Area | Criticism it addresses | Verdict |
|---|---|---|
| [`pthread/`](pthread/) | "binaries built on AL9 don't necessarily run on AL10 because of pthread exported-symbol differences" | Directionally inverted: the real cliff (glibc 2.34 pthread merge) is *newer→older*; building on the oldest glibc — as multispack does — is the mitigation. |
| [`tls-surplus/`](tls-surplus/) | follow-up to `pthread/`: a real threading-related `dlopen` failure the symbol-version test misses | Confirms the static-TLS-surplus failure mode (a Phlex/WCT risk) and its fix (global-dynamic TLS); honestly, it is *not* version-dependent across AL8/9/10, so it does not by itself explain "AL9 vs AL10". |

## Adding an area

1. `mkdir test/<topic>` and add `README.md`, `run.sh`, and the Containerfiles /
   sources it needs, following the convention above.
2. Add a row to the **Areas** table here.
3. Keep `run.sh` self-validating so it can be wired into CI later.
