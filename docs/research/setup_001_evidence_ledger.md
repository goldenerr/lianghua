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
| Docker/compose | deployment/docker files exist | root wrappers or canonical path decision + up/down evidence | see setup-001 plan |
| CI/CD | .github/workflows/ci.yml exists | fresh validation/CI run | see setup-001 plan |
| init.sh | dev/test/prod and flags exist | fresh init evidence | see setup-001 plan |
| docker compose up | not freshly verified | compose config/up/down logs | see setup-001 plan |
| smoke + consistency | historical only | fresh full tests and consistency report | see setup-001 plan |
| capital impact + risk approval | not complete | report + approval reference | see setup-001 plan |
| code signing + Kill Switch | not complete | signed verification + Kill Switch evidence | see setup-001 plan |
| core-004 manifest | not freshly verified | manifest generation and verification logs | see setup-001 plan |
| artifact signatures | not complete | signed artifact evidence | see setup-001 plan |
