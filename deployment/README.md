# Deployment Gate: `deploy-001`

`deploy-001` is not approved for production rollout until every gate below is
recorded and reviewed. The application must never use a rollout event to
enable live trading automatically.

## Canary Policy

- Deploy only 5% of instances at the first canary stage.
- Observe each stage for at least 24 hours.
- Advance stages in order: 5%, 10%, 50%, 100%.
- Roll back immediately if error rate exceeds 5%, P99 latency exceeds 200 ms,
  or strategy Sharpe declines by more than 30%.
- Publish every decision to the audit event bus.

## Blocking Gates

- Verify the signed `core-004` system version manifest in every artifact.
- Run full tests, smoke tests, backtest-to-paper consistency checks, and a DR drill.
- Generate the capital impact assessment and obtain risk approval.
- Configure production secrets through Vault or an approved secret manager.
- Confirm monitoring, alert escalation, and rollback controls in the target environment.

The Docker assets provide a development/deployment baseline only. They do not
constitute production approval.

The main-branch image workflow must generate the manifest from the exact
configuration and source tree copied into the image, then verify it before
calling `docker build`:

```bash
export QUANT_MANIFEST_SIGNING_KEY='<secret-manager-injected-key>'
quant-cli build-manifest \
  --key-id github-actions-release \
  --schema-version v2026.05 \
  --output deployment/artifact-manifest.json
quant-cli manifest deployment/artifact-manifest.json --verify
```

The signing key is never stored in the repository or image. The generated
manifest carries hashes and a signature only, and is part of the constructed
artifact.
