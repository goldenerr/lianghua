# setup-001 verification ledger

Status: NOT COMPLETE / passes=false

Rules:
- No production claim without real evidence.
- No synthetic data.
- No quant-ashare data as lianghua evidence.
- No CSI300 filtering.
- No business-code changes for setup evidence.

| verification_step | current evidence | missing evidence | command/artifact |
| --- | --- | --- | --- |
| directory structure | src/config/data/logs/tests/deployment/scripts exist | fresh committed evidence | see setup-001 plan |
| pyproject/pre-commit | pyproject + .pre-commit-config exist | fresh pre-commit run | see setup-001 plan |
| Docker/compose | deployment/docker/ is the canonical Docker path. Option B selected: do not create root Dockerfile/docker-compose.yml wrappers. Compose safety hardening now removes internal host port publication, loopback-binds public ports, requires Redis/ClickHouse secret env vars, adds resource limits, and removes obsolete top-level version. | runtime up/down evidence still required | data/backtest_results/setup_001_docker_compose_check.txt; data/backtest_results/setup_001_docker_compose_hardening_check.txt |
| CI/CD | .github/workflows/ci.yml exists | fresh validation/CI run | see setup-001 plan |
| init.sh | dev/test/prod and flags exist | fresh init evidence | see setup-001 plan |
| docker compose up | config verified with ephemeral secrets; runtime blocked by docker daemon permission | compose up/ps/down logs from docker-capable environment | data/backtest_results/setup_001_docker_compose_hardening_check.txt |
| smoke + consistency | historical only | fresh full tests and consistency report | see setup-001 plan |
| capital impact + risk approval | not complete | report + approval reference | see setup-001 plan |
| code signing + Kill Switch | not complete | signed verification + Kill Switch evidence | see setup-001 plan |
| core-004 manifest | not freshly verified | manifest generation and verification logs | see setup-001 plan |
| artifact signatures | not complete | signed artifact evidence | see setup-001 plan |

## Docker path decision

Canonical Docker files are under `deployment/docker/`:

- `deployment/docker/Dockerfile`
- `deployment/docker/docker-compose.yml`

Canonical compose config command: `docker compose -f deployment/docker/docker-compose.yml config`.
Canonical compose startup command, when safety blockers are resolved: `docker compose -f deployment/docker/docker-compose.yml up -d`.
Root `Dockerfile` and `docker-compose.yml` are absent as of the setup-001 audit; do not claim literal root-file completion unless wrappers are added and verified.
