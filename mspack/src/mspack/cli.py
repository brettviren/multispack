# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0

"""The ``mspack`` command line interface.

Parses options, resolves configuration and delegates to the phase / deploy
modules.  It carries no build logic itself.
"""

import sys
from pathlib import Path

import click

from . import deploy, phases
from . import survey as survey_mod
from .config import Config
from .container import ContainerError, Engine
from .deploy.base import DeployRequest

CONTEXT_SETTINGS = dict(help_option_names=["-h", "--help"])


class App:
    """Shared state: resolved Config plus a ready Engine."""

    def __init__(self, root, conf, engine, dry_run, base_distro=None,
                 meta_dir=None):
        overrides = {}
        if base_distro:
            overrides["BASE_DISTRO"] = base_distro
        if meta_dir:
            overrides["META_DIR"] = meta_dir
        self.cfg = Config.load(root=Path(root) if root else None,
                               conf=Path(conf) if conf else None,
                               overrides=overrides or None)
        eng = engine or self.cfg.engine
        self.engine = Engine(engine=eng, dry_run=dry_run)


@click.group(context_settings=CONTEXT_SETTINGS)
@click.option("--root", type=click.Path(file_okay=False), default=None,
              help="multispack repo root (default: found by walking up, or "
                   "$MULTISPACK_ROOT).")
@click.option("--conf", type=click.Path(dir_okay=False), default=None,
              help="Path to multispack.conf (default: <root>/multispack.conf).")
@click.option("--engine", default=None,
              help="Container engine override (default: config ENGINE).")
@click.option("--base-distro", default=None,
              help="Base distro key from config/distros.yaml (sets the glibc "
                   "floor + builder); default: config BASE_DISTRO. See "
                   "`mspack survey table`.")
@click.option("--meta-dir", default=None, type=click.Path(file_okay=False),
              help="Directory for phase records/logs, mounted at "
                   "/multispack/meta (default: config META_DIR, else "
                   "./meta in the current directory).")
@click.option("--dry-run", is_flag=True,
              help="Print the container commands that would run; execute none.")
@click.pass_context
def cli(ctx, root, conf, engine, base_distro, meta_dir, dry_run):
    """mspack: build and deploy the multispack Strategy B Spack stack."""
    ctx.obj = App(root, conf, engine, dry_run, base_distro=base_distro,
                  meta_dir=meta_dir)


def _fail(msg: str):
    click.secho(f"mspack: {msg}", fg="red", err=True)
    sys.exit(1)


def _run(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except (ContainerError, FileNotFoundError, ValueError, OSError) as err:
        _fail(str(err))


@cli.command()
@click.pass_context
def volumes(ctx):
    """Create the three podman volumes and seed their layout."""
    _run(phases.volumes, ctx.obj.cfg, ctx.obj.engine)


@cli.command()
@click.argument("names", nargs=-1)
@click.pass_context
def images(ctx, names):
    """Build container images (default: builder + all validators)."""
    _run(phases.images, ctx.obj.cfg, ctx.obj.engine, list(names) or None)


@cli.command()
@click.pass_context
def bootstrap(ctx):
    """Clone Spack into /cvmfs, install site config, bootstrap clingo."""
    _run(phases.bootstrap, ctx.obj.cfg, ctx.obj.engine)


@cli.command()
@click.argument("specs", nargs=-1)
@click.pass_context
def compiler(ctx, specs):
    """Build the self-hosted GCC_SPEC base + payload, plus any extra SPECS.

    Each extra SPEC (e.g. gcc@14) is built from the GCC_SPEC base; a spec older
    than the base is built best-effort with a warning.
    """
    _run(phases.compiler, ctx.obj.cfg, ctx.obj.engine, list(specs))


@cli.command()
@click.argument("spack_yaml", type=click.Path(dir_okay=False))
@click.option("-i", "--image", default=None,
              help="build image (default: MAKENV_IMAGE, i.e. builder).")
@click.option("-n", "--name", default=None,
              help="env name under /cvmfs/env (default: the yaml's parent dir).")
@click.option("-r", "--repos", "repos", multiple=True, metavar="[NAME=]DIR",
              help="local recipe-repo clone, mounted read-only at "
                   "$CVMFS_ROOT/repos/NAME (NAME defaults to DIR's basename). "
                   "Repeatable.  '=DIR' mounts DIR as $CVMFS_ROOT/repos itself "
                   "(legacy, alone only).")
@click.option("--managed/--directory", "managed", default=None,
              help="managed named env vs by-path directory env "
                   "(default: MAKENV_MANAGED).")
@click.option("--no-check", "nocheck", is_flag=True,
              help="skip the pre-install container capability gates.")
@click.pass_context
def makenv(ctx, spack_yaml, image, name, repos, managed, nocheck):
    """Concretize + install an arbitrary Spack env (SPACK_YAML) into the store."""
    _run(phases.makenv, ctx.obj.cfg, ctx.obj.engine, spack_yaml,
         image=image, name=name, repos=list(repos), managed=managed, nocheck=nocheck)


@cli.command("export-conda")
@click.argument("dest", required=False)
@click.option("--env", "env", default=None,
              help="convert an env's roots+deps (default: everything installed).")
@click.option("--spec", "specs", multiple=True,
              help="convert these specs' closures (repeatable).")
@click.option("-j", "--jobs", type=int, default=0, show_default=True,
              help="spaxi conversion parallelism (0 = one per CPU).")
@click.option("-i", "--image", default=None,
              help="build image to run spaxi in (default: MAKENV_IMAGE).")
@click.option("-l", "--log-sink", default=None,
              help="forward spaxi --log-sink (stderr|stdout|container PATH).")
@click.option("-L", "--log-level", default=None,
              help="forward spaxi --log-level (debug|info|warning|error).")
@click.pass_context
def export_conda(ctx, dest, env, specs, jobs, image, log_sink, log_level):
    """Convert installed Spack packages into a conda channel DEST (for pixi).

    DEST is a local directory (default: DEPLOY_DIR).  With --env, converts that
    env's roots and their runtime deps; with --spec, those specs' closures; with
    neither, everything installed.
    """
    cfg = ctx.obj.cfg
    dest = dest or cfg.get("DEPLOY_DIR")
    req = DeployRequest(dest=dest, env=env, specs=list(specs), options={
        "jobs": jobs, "image": image, "log_sink": log_sink,
        "log_level": log_level})
    _run(deploy.get("conda")().run, cfg, ctx.obj.engine, req)


@cli.group(invoke_without_command=True)
@click.option("--json", "as_json", is_flag=True, help="emit JSON, not text.")
@click.pass_context
def survey(ctx, as_json):
    """Survey Linux distros: glibc/gcc coverage from config/distros.yaml.

    With no subcommand, prints the glibc-coverage report (which distro releases
    a stack built on each glibc floor can run on).
    """
    if ctx.invoked_subcommand is None:
        catalog = survey_mod.load_catalog(ctx.obj.cfg.root)
        click.echo(survey_mod.report(catalog, as_json=as_json))


@survey.command("table")
@click.option("--json", "as_json", is_flag=True, help="emit JSON, not text.")
@click.pass_context
def survey_table(ctx, as_json):
    """Print the raw distro catalog (glibc, default gcc, image, EOL, ...)."""
    catalog = survey_mod.load_catalog(ctx.obj.cfg.root)
    click.echo(survey_mod.table(catalog, as_json=as_json))


@survey.command("probe")
@click.argument("names", nargs=-1)
@click.pass_context
def survey_probe(ctx, names):
    """Refresh glibc/gcc by pulling each distro image and reading it.

    NAMES limits the probe to those catalog keys (default: all).  Rewrites the
    probed fields into config/distros.yaml, preserving its comments.
    """
    def _do():
        results = survey_mod.probe(ctx.obj.cfg.root, ctx.obj.engine,
                                   list(names) or None)
        for r in results:
            click.echo(f"  {r['key']:<12} {r.get('status', '')}")
    _run(_do)


@cli.command("config")
@click.argument("key", required=False)
@click.pass_context
def config_cmd(ctx, key):
    """Show resolved configuration (all values, or one KEY)."""
    cfg = ctx.obj.cfg
    if key:
        click.echo(cfg.get(key))
        return
    click.echo(f"# root: {cfg.root}")
    for k in sorted(cfg.values):
        click.echo(f"{k}={cfg.values[k]}")


def main():
    cli(prog_name="mspack")


if __name__ == "__main__":
    main()
