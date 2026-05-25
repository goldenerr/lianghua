"""CLI entry point for the Quant Trading System."""

import json
import os
from pathlib import Path

import click

from quant_trading.config.loader import ConfigLoader
from quant_trading.config.settings import Environment


@click.group()
def app() -> None:
    """Production-grade quantitative trading CLI."""


@app.command("init")
@click.option(
    "--env",
    type=click.Choice([environment.value for environment in Environment]),
    default=Environment.DEV.value,
    show_default=True,
    help="Environment to initialize.",
)
def initialize(env: str) -> None:
    """Load and validate configuration for the target environment."""
    target_env = Environment(env)
    click.echo(f"Initializing {target_env.value} environment...")
    settings = ConfigLoader(env=target_env).load()
    markets = ", ".join(market.value for market in settings.system.primary_markets)
    click.echo(f"Markets: {markets}")
    click.echo(f"Accounts: {len(settings.accounts)}")
    click.echo(f"Environment {settings.system.env.value} ready.")


@app.command()
def version() -> None:
    """Show system version."""
    from quant_trading import __version__

    click.echo(f"Quant Trading System v{__version__}")


@app.command()
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--verify", is_flag=True, help="Verify the manifest signature before displaying it.")
@click.option("--key-env", default="QUANT_MANIFEST_SIGNING_KEY", show_default=True)
def manifest(path: Path, verify: bool, key_env: str) -> None:
    """Display or verify a signed core-004 artifact manifest."""
    from quant_trading.core.version import load_manifest, verify_manifest

    document = load_manifest(path)
    if verify:
        secret = os.getenv(key_env)
        if not secret:
            raise click.ClickException(
                f"required signing key environment variable is missing: {key_env}"
            )
        if not verify_manifest(document, secret.encode("utf-8")):
            raise click.ClickException("manifest signature verification failed")
        click.echo("Signature: valid")
    click.echo(json.dumps(document, indent=2, sort_keys=True))


@app.command("build-manifest")
@click.option(
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path("deployment/artifact-manifest.json"),
    show_default=True,
)
@click.option("--key-env", default="QUANT_MANIFEST_SIGNING_KEY", show_default=True)
@click.option("--key-id", required=True, help="Secret-manager key identifier, never key material.")
@click.option(
    "--config-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("config"),
)
@click.option(
    "--strategy-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("src/quant_trading"),
)
@click.option("--schema-version", required=True, help="Approved data schema release identifier.")
def build_manifest(
    output: Path,
    key_env: str,
    key_id: str,
    config_dir: Path,
    strategy_dir: Path,
    schema_version: str,
) -> None:
    """Build a signed release manifest tied to configuration and source content."""
    from quant_trading.core.version import generate_manifest, hash_tree, write_signed_manifest

    secret = os.getenv(key_env)
    if not secret:
        raise click.ClickException(
            f"required signing key environment variable is missing: {key_env}"
        )
    document = generate_manifest(
        config_hash=hash_tree(config_dir, ("*.yaml", "*.yml")),
        strategy_code_hash=hash_tree(strategy_dir, ("*.py",)),
        data_schema_version=schema_version,
    )
    signed = write_signed_manifest(output, document, secret.encode("utf-8"), key_id)
    click.echo(f"Signed manifest written: {output}")
    click.echo(f"Manifest hash: {signed['manifest_hash']}")


@app.command("config-drift")
@click.argument("baseline", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument("candidate", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--allow", "allowed_paths", multiple=True, help="Allowed dotted path pattern.")
def config_drift(baseline: Path, candidate: Path, allowed_paths: tuple[str, ...]) -> None:
    """Fail when unapproved core configuration differences are detected."""
    from quant_trading.config.drift_detector import detect_drift

    report = detect_drift(baseline, candidate, allowed_paths)
    click.echo(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    if report["blocked"]:
        raise click.ClickException("unapproved configuration drift detected")


if __name__ == "__main__":
    app()
