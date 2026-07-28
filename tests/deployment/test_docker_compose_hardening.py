from pathlib import Path

import yaml

COMPOSE_PATH = Path("deployment/docker/docker-compose.yml")


def load_compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text())


def test_compose_does_not_publish_internal_data_services_to_host() -> None:
    compose = load_compose()

    for service_name in ("redis", "clickhouse"):
        service = compose["services"][service_name]
        assert "ports" not in service, f"{service_name} must stay internal-only"


def test_compose_external_ports_are_loopback_bound_only() -> None:
    compose = load_compose()

    for service_name in ("app", "nginx"):
        for port in compose["services"][service_name].get("ports", []):
            assert isinstance(port, str)
            assert port.startswith(
                "127.0.0.1:"
            ), f"{service_name} port is not loopback-bound: {port}"


def test_compose_requires_redis_and_clickhouse_passwords() -> None:
    compose = load_compose()
    services = compose["services"]

    redis_command = services["redis"].get("command", "")
    assert "--requirepass" in redis_command
    assert "${REDIS_PASSWORD:?" in redis_command

    redis_healthcheck = " ".join(services["redis"]["healthcheck"]["test"])
    assert "-a" in redis_healthcheck
    assert "${REDIS_PASSWORD:?" in redis_healthcheck

    clickhouse_env = services["clickhouse"]["environment"]
    assert "CLICKHOUSE_PASSWORD=${CLICKHOUSE_PASSWORD:?Set CLICKHOUSE_PASSWORD}" in clickhouse_env

    clickhouse_healthcheck = " ".join(services["clickhouse"]["healthcheck"]["test"])
    assert "--password" in clickhouse_healthcheck
    assert "${CLICKHOUSE_PASSWORD:?" in clickhouse_healthcheck

    app_env = services["app"]["environment"]
    assert any("REDIS_PASSWORD" in item and "REDIS_URL" in item for item in app_env)
    assert any("CLICKHOUSE_PASSWORD" in item and "CLICKHOUSE_URL" in item for item in app_env)


def test_compose_services_have_local_resource_limits() -> None:
    compose = load_compose()

    for service_name, service in compose["services"].items():
        assert "mem_limit" in service, f"{service_name} missing mem_limit"
        assert "cpus" in service, f"{service_name} missing cpus"
        assert "pids_limit" in service, f"{service_name} missing pids_limit"


def test_compose_omits_obsolete_version_key() -> None:
    compose = load_compose()

    assert "version" not in compose
