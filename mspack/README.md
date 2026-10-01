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
  phases.py     # volumes, images, compiler-validate (native); bootstrap,
                #   compiler, makenv (delegate)
  image.py      # image (monolithic OCI runtime image of an env), sif (Apptainer)
  cli.py        # Click CLI: mspack <phase> / mspack makenv / mspack config
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
uv run mspack compiler-validate                      # shipped gcc in validators
uv run mspack makenv ../envs/foss-desktop/spack.yaml # build an arbitrary env
uv run mspack image --env foss-desktop --sif fd.sif  # runtime image + SIF
uv run --extra dev pytest -q                         # unit tests
```

Configuration precedence (lowest to highest): built-in defaults → `multispack.conf`
→ process environment → global CLI options. `mspack` and the shell therefore agree
on every value.

### Driving mspack from another project

```sh
mspack --root ~/dev/multispack --conf ~/proj/multispack.conf config
```

`--conf` gives the project its own volumes (`VOL_*`), images (`IMG_PREFIX`) and
install root (`CVMFS_HOST`).  Phase records and logs go to `META_DIR`: conf, env or
`--meta-dir`, defaulting to `./meta` **in the directory mspack is run from** (not the
multispack root), so each project keeps its own.  `multispack.sh` honors `META_DIR`
too but keeps `<repo>/meta` as its default.

### Local recipe repos: `makenv --repos`

`--repos` is repeatable.  `--repos DIR` mounts DIR read-only at
`$CVMFS_ROOT/repos/<basename DIR>`; `--repos NAME=DIR` at `$CVMFS_ROOT/repos/NAME`.
A `spack.yaml` refers to such a clone by path, e.g.
`$spack/../repos/NAME/spack_repo/<namespace>`.  Prefer git remotes in `repos:` for
reproducibility and use `--repos` as a development override.  The old single-dir
behaviour (DIR mounted *as* `$CVMFS_ROOT/repos`) is `--repos =DIR`, and it cannot be
combined with others.  `mspack image --repos` takes the same values so the clones
ship in the image.

### Runtime images: `mspack image`, `mspack sif`

`mspack image --env NAME [--base alma9-devel] [--tag T] [--closure|--all-store]
[--compilers target|all|none] [--sif OUT]` bakes one built env into a monolithic
OCI image in which Spack still works read-only; `mspack sif IMAGE [OUT]` converts
any podman image to an Apptainer SIF.  See `../docs/deploy.md` (scheme 3).

## Testing

Unit tests here cover config layering, the engine argv, the phase wiring (via a
`dry_run` engine that records commands), the metadata records, and the deploy
registry. The container **acceptance** harnesses under the repo's `tests/` and
`test/` (which validate the built `/cvmfs` artifact) are unchanged and remain the
top of the pyramid.
