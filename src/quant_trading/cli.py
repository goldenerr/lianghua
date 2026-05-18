"""CLI entry point for the Quant Trading System."""

import typer

app = typer.Typer(name="quant-cli", help="Production-grade quantitative trading CLI")


@app.command()
def init(env: str = typer.Option("dev", help="Environment: dev, test, or prod")):
    """Initialize the trading system environment."""
    typer.echo(f"Initializing {env} environment...")
    # TODO: Full init logic in config-001
    typer.echo(f"Environment {env} ready.")


@app.command()
def version():
    """Show system version."""
    from quant_trading import __version__

    typer.echo(f"Quant Trading System v{__version__}")


if __name__ == "__main__":
    app()
