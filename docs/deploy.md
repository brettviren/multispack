# Deploying multispack binaries to end users

The stack is built into a padded, `$ORIGIN`-relocatable `/cvmfs` install tree inside
podman volumes.  Without a Fermilab CVMFS namespace to publish into, `multispack.sh`
offers two local deployment schemes.  Both produce a plain directory you serve over
HTTP; neither requires the end user to touch the podman volumes.

| scheme | producer | consumer | user sees |
|--------|----------|----------|-----------|
| 1. Spack buildcache | `cache-export` | `cache-client` + Spack | Spack |
| 2. conda channel    | `conda-export` (spaxi) | `pixi` | pixi (no Spack) |

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
tooling (`pixi`) and never sees Spack.  Uses your `spaxi` (`~/dev/spaxi` by default,
run via `uv` inside the builder).

### Provider

```sh
# a whole environment's packages -> a conda channel, then serve it
./multispack.sh conda-export --env largroups deploy/channel
(cd deploy/channel && python3 -m http.server 8080)

# explicit specs (qualify with /hash if ambiguous)
./multispack.sh conda-export --spec wire-cell-toolkit --spec larwirecell deploy/channel

# EVERYTHING installed (the whole store) -- slow; -j0 uses one worker per CPU
./multispack.sh conda-export --jobs 0 deploy/channel

# straight to another host (staged locally, then tar-streamed over ssh)
./multispack.sh conda-export --env largroups bviren@web:/srv/www/spaxi
```

`DEST` is a local dir (default `deploy/channel`) or an `scp` `host:path`.  With
neither `--env` nor `--spec`, `conda-export` converts **everything installed**.

`spaxi conda --deps --origin-rpaths` writes `<arch>/<pkg>-<ver>-<hash>.conda` files and
`repodata.json` into the channel, self-contained (no Spack store needed to link).  The
first run downloads a Python and spaxi's dependencies (network); later runs reuse the
mounted uv cache.

### User

```sh
pixi init myproj && cd myproj
# add the channel (http://web:8080) to pixi.toml, set the glibc virtual to match, then:
pixi add wire-cell-toolkit        # or: spaxi add-spec 'wire-cell-toolkit@0.37.1'
```

See the spaxi docs (`~/dev/spaxi/README.org`) for the pixi-side details (channel URL,
the `__glibc` virtual, disambiguation).

---

## Notes

- `cache-export` (full) is append-only — it never prunes specs removed upstream;
  re-mirror into a fresh directory if you need to drop old binaries.
- Deployment is intentionally **not** part of `multispack.sh all`; run these steps
  when you actually want to publish.
- Everything is served as static files: any HTTP server (nginx, `python3 -m
  http.server`, a CDN) works for the buildcache dir or the conda channel dir.
