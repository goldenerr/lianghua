import json

import pytest
import yaml
from click.testing import CliRunner
from quant_trading.cli import app
from quant_trading.config.drift_detector import detect_drift


def _write(path, document) -> None:
    path.write_text(yaml.safe_dump(document), encoding="utf-8")


def test_recursive_drift_blocks_nested_risk_change_and_redacts_secret(tmp_path) -> None:
    dev = tmp_path / "dev.yaml"
    prod = tmp_path / "prod.yaml"
    _write(dev, {"risk": {"max_var_pct": 0.05}, "credentials": {"api_key": "dev-key"}})
    _write(prod, {"risk": {"max_var_pct": 0.08}, "credentials": {"api_key": "prod-key"}})

    report = detect_drift(dev, prod)
    assert report["blocked"] is True
    assert "risk.max_var_pct" in report["blocked_keys"]
    assert report["differences"]["credentials.api_key"]["dev"] == "<redacted>"
    assert "dev-key" not in json.dumps(report)


def test_explicit_allowlist_permits_known_environment_endpoint_difference(tmp_path) -> None:
    dev = tmp_path / "dev.yaml"
    prod = tmp_path / "prod.yaml"
    _write(dev, {"api": {"exchanges": {"binance": {"base_url": "https://sandbox"}}}})
    _write(prod, {"api": {"exchanges": {"binance": {"base_url": "https://production"}}}})
    report = detect_drift(dev, prod, ["api.exchanges.*.base_url"])
    assert report["blocked"] is False
    assert report["allowed_keys"] == ["api.exchanges.binance.base_url"]


def test_cli_config_drift_returns_nonzero_for_unapproved_difference(tmp_path) -> None:
    left = tmp_path / "left.yaml"
    right = tmp_path / "right.yaml"
    _write(left, {"risk": {"max_position_pct": 0.20}})
    _write(right, {"risk": {"max_position_pct": 0.30}})
    result = CliRunner().invoke(app, ["config-drift", str(left), str(right)])
    assert result.exit_code != 0
    assert "unapproved configuration drift" in result.output


def test_missing_configuration_is_not_silently_accepted(tmp_path) -> None:
    present = tmp_path / "present.yaml"
    _write(present, {})
    with pytest.raises(FileNotFoundError):
        detect_drift(tmp_path / "missing.yaml", present)
