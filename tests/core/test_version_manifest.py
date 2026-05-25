import json

from click.testing import CliRunner
from quant_trading.cli import app
from quant_trading.core.version import (
    generate_manifest,
    hash_manifest,
    hash_tree,
    load_manifest,
    sign_manifest,
    verify_manifest,
    write_signed_manifest,
)


def _manifest() -> dict:
    return generate_manifest(
        config_hash="config-abc",
        strategy_code_hash="strategy-def",
        data_schema_version="v2026.05",
        component_versions={"redis": "7", "clickhouse": "24"},
        generated_at="2026-05-25T00:00:00+00:00",
    )


def test_signed_manifest_binds_config_code_schema_and_components() -> None:
    manifest = _manifest()
    signed = sign_manifest(manifest, b"artifact-key", "release-key-1")
    assert signed["config_hash"] == "config-abc"
    assert signed["strategy_code_hash"] == "strategy-def"
    assert signed["components"]["clickhouse"] == "24"
    assert len(hash_manifest(manifest)) == 64
    assert verify_manifest(signed, b"artifact-key") is True


def test_manifest_tampering_or_wrong_key_fails_verification() -> None:
    signed = sign_manifest(_manifest(), b"artifact-key", "release-key-1")
    signed["config_hash"] = "tampered"
    assert verify_manifest(signed, b"artifact-key") is False
    assert verify_manifest(sign_manifest(_manifest(), b"artifact-key", "k"), b"wrong-key") is False


def test_tree_hash_binds_relative_path_and_content(tmp_path) -> None:
    tree = tmp_path / "config"
    tree.mkdir()
    (tree / "system.yaml").write_text("env: test\n", encoding="utf-8")
    first = hash_tree(tree, ("*.yaml",))
    (tree / "system.yaml").write_text("env: prod\n", encoding="utf-8")
    assert hash_tree(tree, ("*.yaml",)) != first


def test_write_and_cli_verify_signed_manifest(tmp_path, monkeypatch) -> None:
    path = tmp_path / "manifest.json"
    write_signed_manifest(path, _manifest(), b"artifact-key", "release-key-1")
    assert load_manifest(path)["signature"]["key_id"] == "release-key-1"

    monkeypatch.setenv("TEST_MANIFEST_KEY", "artifact-key")
    result = CliRunner().invoke(
        app, ["manifest", str(path), "--verify", "--key-env", "TEST_MANIFEST_KEY"]
    )
    assert result.exit_code == 0
    assert "Signature: valid" in result.output
    assert json.loads(result.output.split("Signature: valid\n", 1)[1])["manifest_hash"]


def test_cli_build_manifest_requires_key_and_verifies_output(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    source_dir = tmp_path / "source"
    config_dir.mkdir()
    source_dir.mkdir()
    (config_dir / "system.yaml").write_text("env: test\n", encoding="utf-8")
    (source_dir / "strategy.py").write_text("SIGNAL = 1\n", encoding="utf-8")
    output = tmp_path / "manifest.json"
    args = [
        "build-manifest",
        "--output",
        str(output),
        "--key-id",
        "release-key",
        "--schema-version",
        "v1",
        "--config-dir",
        str(config_dir),
        "--strategy-dir",
        str(source_dir),
        "--key-env",
        "TEST_MANIFEST_KEY",
    ]
    failed = CliRunner().invoke(app, args)
    assert failed.exit_code != 0
    monkeypatch.setenv("TEST_MANIFEST_KEY", "signing-secret")
    created = CliRunner().invoke(app, args)
    assert created.exit_code == 0
    assert verify_manifest(load_manifest(output), b"signing-secret") is True
