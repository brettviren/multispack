# mspack

Structured Python orchestration for the multispack Strategy B Spack stack — the
"real package" companion to `multispack.sh`.

`mspack` owns configuration, a single container-engine seam, and the CLI. For the
build phases it follows a **strangler-fig** strategy: `volumes` and `images` are
implemented natively; `bootstrap` and `compiler` **delegate** to the existing,
validated `bin/phase-*.sh` (run in the builder image with exactly the mounts and
environment `multispack.sh` uses), so there is one source of build truth during
the transition. The new domain — config evolution, environment merging, Fermilab
ingest, deployment plugins — grows here in Python.

## Layout

```
src/mspack/
  config.py     # defaults mirroring multispack.sh; reads multispack.conf + env
  container.py  # the one podman seam (argv construction + tee'd exec)
  meta.py       # phase records, stage_run-compatible (bin/report.py reads them)
  phases.py     # volumes, images (native); bootstrap, compiler (delegate)
  cli.py        # Click CLI: mspack <phase> / mspack config
  deploy/       # deployment methods behind a plugin registry (entry points)
```

## Use

```sh
uv sync --extra dev
uv run mspack --root /path/to/multispack config      # show resolved config
uv run mspack --dry-run bootstrap                    # print the podman command
uv run mspack volumes                                # create + seed volumes
uv run mspack images builder                         # build the builder image
uv run mspack bootstrap                              # clone Spack, site config
uv run mspack compiler                               # build the GCC ladder
uv run --extra dev pytest -q                         # unit tests
```

Configuration precedence (lowest to highest): built-in defaults → `multispack.conf`
→ process environment. `mspack` and the shell therefore agree on every value.

## Testing

Unit tests here cover config layering, the engine argv, the phase wiring (via a
`dry_run` engine that records commands), the metadata records, and the deploy
registry. The container **acceptance** harnesses under the repo's `tests/` and
`test/` (which validate the built `/cvmfs` artifact) are unchanged and remain the
top of the pyramid.
