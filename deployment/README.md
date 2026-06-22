# 部署 Gate：`deploy-001`

在以下每个 gate 都完成记录和复核之前，`deploy-001` 不允许进入生产发布。应用程序绝不能通过一次发布事件自动开启实盘交易。

## Canary 策略

- 首个 canary 阶段只部署 5% 实例。
- 每个阶段至少观察 24 小时。
- 按顺序推进阶段：5%、10%、50%、100%。
- 如果错误率超过 5%、P99 延迟超过 200 ms，或策略 Sharpe 下降超过 30%，必须立即回滚。
- 每一次发布决策都必须写入审计事件总线。

## 阻塞 Gate

- 在每个 artifact 中校验已签名的 `core-004` 系统版本清单。
- 运行全量测试、smoke test、回测到模拟盘一致性检查和灾备演练。
- 生成资金影响评估，并取得风控审批。
- 通过 Vault 或已批准的 Secret Manager 配置生产密钥。
- 在目标环境确认监控、告警升级和回滚控制。

Docker 资产只提供开发/部署基线，不构成生产批准。

主分支镜像工作流必须基于复制进镜像的精确配置和源码树生成 manifest，并在调用 `docker build` 前完成校验：

```bash
export QUANT_MANIFEST_SIGNING_KEY='<secret-manager-injected-key>'
quant-cli build-manifest \
  --key-id github-actions-release \
  --schema-version v2026.05 \
  --output deployment/artifact-manifest.json
quant-cli manifest deployment/artifact-manifest.json --verify
```

签名密钥绝不能存储在仓库或镜像中。生成的 manifest 只携带 hash 和签名，并作为构建 artifact 的一部分。
