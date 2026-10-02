# Deploying multispack binaries to end users

The stack is built into a padded, `$ORIGIN`-relocatable `/cvmfs` install tree inside
podman volumes.  Without a Fermilab CVMFS namespace to publish into, `multispack.sh`
offers two local deployment schemes.  Both produce a plain directory you serve over
HTTP; neither requires the end user to touch the podman volumes.

| scheme | producer | consumer | user sees |
|--------|----------|----------|-----------|
| 1. Spack buildcache | `cache-export` | `cache-client` + Spack | Spack |
| 2. conda channel    | `conda-export` / `mspack export-conda [--from-image]` (spaxi) | `pixi`, `micromamba`, `conda` | conda env (no Spack) |
| 3. monolithic image | `mspack image` (+ `mspack sif`) | podman / Apptainer | the env at its `/cvmfs` path, Spack optional |

Artifacts land under `deploy/` by default (git-ignored); override with `DEPLOY_DIR`.

---

## Scheme 1 — Spack buildcache over HTTP

Ship the relocatable binary tarballs; the user's own Spack pulls and relocates them.

### Provider

```sh
# whole cache -> a local dir, then serve it
./multispack.sh cache-export deploy/buildcache
(cd deploy/buildcache && python3 -m http.server 8080)

# just one environment's packages
./multispack.sh cache-export --env largroups deploy/lg

# only the concrete closures of some seed specs (needs --env for the recipe repos)
./multispack.sh cache-export --env largroups --spec larwirecell --spec wire-cell-toolkit deploy/wc
```

`cache-export` copies the whole cache with a fast content-addressed copy (`cp -an`
for immutable blobs, plus a refreshed `v3/` index).  A `--env`/`--spec` **subset** is
re-pushed fresh from the store with `spack buildcache push`, so it contains exactly
the requested closures.

**To another host, without a local copy.**  Give an `scp` target `[user@]host:path`
instead of a directory.  The full cache is **tar-streamed through ssh** — it never
lands on a local filesystem:

```sh
./multispack.sh cache-export deploy@web:/srv/multispack/buildcache          # whole cache, streamed
./multispack.sh cache-export --env largroups --spec larwirecell deploy@web:/srv/wc   # subset, streamed
```

(You need working ssh to `host`.  A `--env`/`--spec` subset is staged bounded in the
work volume, then streamed; the whole cache streams straight from the volume.)

### Consumer

```sh
./multispack.sh cache-client http://web:8080 ~/mspack-client
# get a Spack matching the ref this prints (SPACK_REF), put `spack` on PATH, then:
source ~/mspack-client/setup.sh
spack mirror list                              # 'multispack' -> http://web:8080
spack install --no-check-signature larwirecell # downloads prebuilt binaries
```

`cache-client` writes a **self-contained** bundle: `mirrors.yaml` (the HTTP cache) plus
the build's own `config.yaml`/`packages.yaml`/`concretizer.yaml` (so the client
concretizes to the same hashes the cache holds).  `setup.sh` redirects
`SPACK_USER_CONFIG_PATH`, `SPACK_SYSTEM_CONFIG_PATH` and `SPACK_USER_CACHE_PATH` into
the bundle, so **nothing is written to `~/.spack`**.

Caveats: the site config scope carries no `repos.yaml`, so for guaranteed cache hits
the client also needs the same recipe repos this stack used (the git ones resolve; a
locally-patched repo like `largroups`' `fnal_art` would not — deploy such an env via
Scheme 2 instead).  Binaries relocate out of `/cvmfs` into the client's store via
their padded `$ORIGIN` rpaths.

---

## Scheme 2 — conda channel for pixi

Convert installed Spack packages into conda packages so the end user drives everyday
tooling (`pixi`) and never sees Spack.  Uses your `spaxi` (`$SPAXI_SRC`, default
`python/spaxi` — clone it there, it is git-ignored — run via `uv` inside the builder).

### Provider

```sh
# a whole environment's packages -> a conda channel, then serve it
./multispack.sh conda-export --env largroups deploy/channel
(cd deploy/channel && python3 -m http.server 8080)

# explicit specs (qualify with /hash if ambiguous)
./multispack.sh conda-export --spec wire-cell-toolkit --spec larwirecell deploy/channel

# EVERYTHING installed (the whole store) -- all specs are handed to ONE spaxi
# process (via --specs-from -); per-spec failures are reported, not fatal.
./multispack.sh conda-export --jobs 0 deploy/channel

# straight to another host -- packages are drained to the remote *during*
# conversion (rsync), so no full local copy is kept
./multispack.sh conda-export --env largroups bviren@web:/srv/www/spaxi

# turn up parallelism and spaxi's logging (a file sink is a container path;
# /out is the channel dir, so this log lands beside it on the host)
./multispack.sh conda-export --jobs 0 -L debug -l /out/convert.log deploy/channel
```

`--jobs`/`-j` is spaxi's parallelism budget: the whole batch of specs (deduped by
hash across all their closures) converts through **one** pool of that many workers,
so a single `conda-export` saturates the machine rather than converting spec by
spec.  A package that fails to convert is reported and the batch continues.  `-l`/`-L`
forward spaxi's `--log-sink`/`--log-level`.

`DEST` is a local dir (default `deploy/channel`) or an `scp` `host:path`.  With
neither `--env` nor `--spec`, `conda-export` converts **everything installed**.

**scp target — no full local copy.**  When `DEST` is `host:path`, a background
**drain** `rsync`s each *settled* `.conda` (built more than a minute ago, so
spaxi's post-build digest read is long finished) to the remote and deletes it
locally *while conversion continues*, so local disk only ever holds the last
interval's packages plus the growing `repodata.json`.  The channel index stays
local (authoritative) until a **final sync** ships `repodata.json` and any
stragglers; the local staging dir is then removed entirely.  This needs `rsync`
on both ends — without it, `conda-export` falls back to staging the whole channel
locally and `tar`-streaming it at the end (local staging is kept in that case).

`spaxi conda --deps --origin-rpaths` writes `<arch>/<pkg>-<ver>-<hash>.conda` files and
`repodata.json` into the channel, self-contained (no Spack store needed to link).  The
first run downloads a Python and spaxi's dependencies (network); later runs reuse the
mounted uv cache.

### Provider, from a runtime image (hybrid: no build volumes needed)

`mspack export-conda` can also read the store **baked into a runtime image**
made by `mspack image` (Scheme 3), so a machine that only holds the image can
produce the channel:

```sh
# a podman image ref ...
mspack export-conda --from-image localhost/me/NAME:1.0 --env NAME \
    --with-compiler --meta NAME=1.0 -j 48 /srv/channel
# ... or an Apptainer SIF (run with apptainer exec --containall)
mspack export-conda --from-image NAME.sif --env NAME --meta NAME=1.0 /srv/channel
```

The conversion script runs in the image itself (`podman run --read-only`, or
`apptainer exec --containall`), against the image's read-only
`$CVMFS_ROOT`.  Bound in are only: `DEST` at `/out`, a host scratch dir
(`--workdir`, default `$TMPDIR/mspack-conda-<image>`, kept for re-runs) at
`/mspack-work` for the Spack user cache, `TMPDIR`, a uv-managed Python and the
spaxi venv, `SPAXI_SRC` read-only at `/spaxi`, and the host uv cache.  The host
`uv` binary is copied into the scratch dir.  The image's `ENV` (view on `PATH`,
`PYTHONPATH`, `LD_LIBRARY_PATH`) is cleared before spaxi and Spack run; Spack
uses the image's runtime site config (`locks: false`, caches under
`SPACK_USER_CACHE_PATH`), so nothing writes into `/cvmfs`.  Scratch use peaks at
roughly the size of the largest packages in flight (spaxi tars each payload
before compressing it).

Differences from the volume mode:

- `--env NAME` converts the env's **roots** (`spack find -x`) plus every
  `--spec`, and their runtime closure.  (The volume mode converts every spec
  `spack -e NAME find` lists.)
- `--with-compiler` adds the image's shipped compiler (`$MSPACK_GCC_PREFIX`).
  The env DB records that gcc as an *external* living in the store.  spaxi
  converts the real install behind it.  Its bootstrap closure (built by gcc@12:
  `gcc-runtime` 12, binutils, gmp, ...) gets `>=version` pins instead of exact
  builds, so it installs next to the stack's `gcc-runtime` 15 in one
  one-build-per-name conda env.

Options for both modes:

- `--meta NAME[=VERSION]` runs `spaxi meta`.  The meta-package NAME pins the
  exact builds of the env roots, every `--spec` and the compiler, and declares
  `__glibc >=<floor>`.  It ships:
  - `etc/conda/{activate,deactivate}.d/NAME.sh`: the env's
    `spack env activate` settings with the view re-rooted on `$CONDA_PREFIX`,
    minus `PATH`, `SPACK_*` and `CC`/`CXX`/...;
  - a `site-packages/NAME.pth` with the extra `PYTHONPATH` dirs, so Jupyter
    kernels import them without activation;
  - a `lib64 -> lib` link (spaxi folds `lib64` onto `lib`).

  A root that failed to convert is left out of the meta-package and reported.
- `-z N` sets the zstd level (spaxi's default is 19).

Then a whole stack installs with one command:

```sh
micromamba create -p ./env -c file:///srv/channel --override-channels NAME
```

Relocation notes (spaxi branch `phaindia`):
- ELF RPATHs become `$ORIGIN`-relative, and text and ELF files get a conda
  prefix placeholder.  Absolute symlinks into other converted prefixes become
  relative.
- `__pycache__/*.pyc` is dropped.  Non-ELF binaries (`.pcm`, `.a`, caches)
  stay byte-identical: conda's NUL-padded rewrite would corrupt their
  length-prefixed strings.
- Package fixups (`spaxi/fixups.py`) cover what bytes cannot.  For ROOT,
  whose install prefix is compiled into code immediates: `ROOTIGNOREPREFIX=1`
  plus layout links.  For gcc on Debian-family hosts: the multiarch include
  and lib dirs.
- A worked example, with the failures found and the consumer tests, is in
  phaindia's `docs/conda-channel.md`.

### Host directories instead of named volumes

`VOL_CVMFS`, `VOL_CACHE` and `VOL_WORK` may be **absolute host paths** instead
of podman volume names.  `-v /abs/path:/cvmfs` is then a bind mount, so the
store (and caches) land directly on the host filesystem.  `mspack volumes`
creates the directories instead of calling `podman volume create` (mspack only: `multispack.sh volumes` still expects volume names).  Rootless
podman maps container root to the invoking user, so the tree is owned by you.
On an SELinux-enforcing host, set `SEL=:z`.

### User

```sh
pixi init myproj && cd myproj
# add the channel (http://web:8080) to pixi.toml, set the glibc virtual to match, then:
pixi add wire-cell-toolkit        # or: spaxi add-spec 'wire-cell-toolkit@0.37.1'
```

See the spaxi docs (`python/spaxi/README.org`) for the pixi-side details (channel URL,
the `__glibc` virtual, disambiguation).

---

## Scheme 3 — a monolithic runtime image (OCI and SIF)

One built Spack environment, baked with its install closure into a container image
at the very same absolute `/cvmfs/<host>` paths it was built at.  Best for
tutorials and classrooms: every user sees identical paths and nothing needs to be
installed.  Spack itself keeps working inside the image, read-only.

### Provider

```sh
mspack image --env NAME                          # -> IMG_PREFIX/NAME:<YYYYMMDD>
mspack image --env NAME --tag ghcr.io/me/NAME:1.0 --sif NAME.sif
mspack image --env NAME --all-store              # ship the whole store, not the closure
mspack image --env NAME --repos ~/src/my-recipes # same dev clones as makenv --repos
mspack sif localhost/me/NAME:1.0 NAME.sif        # any podman image -> SIF
```

What goes in, as **one** layer over the runtime base:

| path | content |
|------|---------|
| `$CVMFS_ROOT/opt/...` | the link/run closure of the env's roots plus the shipped compiler (`--compilers target`, default: `GCC_TARGET_SPEC`; or `all`/`none`); `--all-store` ships every installed spec.  The store DB is pruned to match, so `spack find` shows exactly what ships. |
| `$CVMFS_ROOT/env/NAME` | the env dir: `spack.yaml`, `spack.lock`, the view, plus `mspack-image.json` (what was shipped) |
| `$CVMFS_ROOT/env/gcc` | `env.sh` of the shipped compiler |
| `$CVMFS_ROOT/spack` | the Spack clone with a runtime site config: caches under the user's dir, `locks: false`, no buildcache mirror, unshipped compiler externals removed |
| `$CVMFS_ROOT/repos/<name>` | git recipe repos (incl. `builtin`), copied from the work volume's clones and found through a site `destination:` override, so the env's `spack.yaml` is shipped verbatim |
| `$CVMFS_ROOT/bootstrap/store` | clingo, bootstrapped for the **base image's** python |
| `/etc/profile.d/mspack-NAME.sh`, image `ENV` | what `spack env activate` sets (view on `PATH`, `CMAKE_PREFIX_PATH`, ...), the compiler's `bin`, `SPACK_ROOT`, `SPACK_ENV`; the profile script also defines the `spack` shell function |
| `/usr/local/bin/spack` | wrapper that seeds the per-user bootstrap root, then runs Spack |
| `/work` | empty, mode 1777: a bind target for tutorial files |

Not shipped: build/test stage, source and misc caches, the buildcache, `tmp`, the
Spack user cache, failure markers, and anything not installed (the throwaway
stage-1 bootstrap gcc is already uninstalled by the `compiler` phase).

The base is `--base NAME` (an mspack image name, default `<BASE_DISTRO>-devel`, so
the shipped gcc can compile) or a full image reference.  `mspack image` first
derives `<base>-spack` from it (`containers/Containerfile.spack-runtime`: git,
python3, unpackers), because Spack refuses to start without git once a recipe repo
is git-backed.  `--no-spack-deps` skips that; `--no-bootstrap` skips clingo (no
concretizing in the image).

Labels: `io.mspack.env`, `io.mspack.spack-lock.sha256`, `io.mspack.spack.commit`,
`io.mspack.multispack.commit`, `io.mspack.cvmfs-root`, `io.mspack.view`,
`org.opencontainers.image.{created,revision,base.name,title}`.  A record also goes
to `META_DIR/image-NAME.detail.json`.

How it is built (all rootless): a builder container runs `bin/image-plan.py`
(`spack -e ENV python`) and `bin/image-payload.sh` (one `tar`, owned root:root,
repo paths renamed on the fly); the host appends that tar to the base image's
`podman save` docker-archive and streams the result into `podman load`.  It
avoids `podman build` on purpose: podman 4.3 stages a build's layers in
`/var/tmp` whatever `TMPDIR` says, and a many-GB payload does not fit there.
Disk use is about 3x the payload: the tar in `--workdir` (default
`$TMPDIR/mspack-image-NAME`, removed unless `--keep`), podman's load copy, and the
image itself.  `mspack sif` adds an oci-archive and Apptainer's scratch copy, both
temporary under `$TMPDIR`.

### User

```sh
podman run --rm -it -v $PWD:/work localhost/me/NAME:1.0          # login shell, env active
apptainer exec --bind $PWD:/work NAME.sif bash -l                # same with Apptainer
apptainer exec NAME.sif g++ --version                            # ENV applies without a login shell
```

Inside: `spack find`, `spack load PKG`, `spack env activate $MSPACK_ENV_DIR`, and
`spack spec`/`concretize` all work with `/cvmfs` read-only and offline.  Per-user
Spack state (caches, the clingo bootstrap root) goes to
`$HOME/.mspack/<host>`, apart from a host Spack's `~/.spack`.  Installing is
not possible into the read-only store; a developer who wants to add packages
uses the image's store as a Spack upstream for their own install tree.

---

## Notes

- `cache-export` (full) is append-only — it never prunes specs removed upstream;
  re-mirror into a fresh directory if you need to drop old binaries.
- Deployment is intentionally **not** part of `multispack.sh all`; run these steps
  when you actually want to publish.
- Everything is served as static files: any HTTP server (nginx, `python3 -m
  http.server`, a CDN) works for the buildcache dir or the conda channel dir.
