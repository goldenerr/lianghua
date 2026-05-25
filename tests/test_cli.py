"""Tests for quant CLI."""

from click.testing import CliRunner
from quant_trading.cli import app


def test_cli_init_loads_dev_config():
    runner = CliRunner()
    result = runner.invoke(app, ["init", "--env", "dev"])
    assert result.exit_code == 0
    assert "Environment dev ready." in result.output
    assert "Markets:" in result.output


def test_cli_init_rejects_invalid_env():
    runner = CliRunner()
    result = runner.invoke(app, ["init", "--env", "paper"])
    assert result.exit_code != 0
    assert "Invalid value for '--env'" in result.output


def test_cli_version():
    runner = CliRunner()
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "Quant Trading System v" in result.output
