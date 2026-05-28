# 资金影响评估报告 - config-001 配置快照与原子激活加固

| Field | Value |
| --- | --- |
| Change ID | `config-001` |
| Assessment date | `2026-05-28` |
| Environment scope | Development validation only |
| Approval status | Pending risk review; not approved for production |
| Release posture | Production blocked |

## Change Summary

- 将风险策略因子列表与账户快照转换为不可变元组。
- 将风险策略权重和 API 端点集合转换为真正不可写的只读映射，同时保留 Pydantic JSON 序列化和公共配置哈希计算能力。
- `ConfigLoader` 发布的账户集合现在是不可变快照，避免配置哈希生成后被运行时追加账户。
- 配置加载、批准、发布、成功热重载和拒绝热重载现在通过全局审计事件总线记录非敏感元数据，包括配置哈希与变更前后哈希。
- 市场控制发布采用验证、审计授权、原子发布的顺序；审计总线不可写时拒绝发布候选配置。
- 配置候选在发布前完成日历等外部验证并写入全部必需审计事件，随后仅执行已验证内存快照替换，避免发布后审计失败造成调用态与运行态分裂。
- 同一加载器的后续 `load()` 被视为受审计的热重载并同步缓存；非法初次启动同样记录不含配置内容的拒绝元数据。
- 多账户配置拒绝重复的 `account_id`，避免订单归属、密钥审计或租户限额绑定到歧义身份。
- 生产账户必须绑定到显式配置的交易所 API 端点；生产 REST 端点必须使用 HTTPS，WebSocket 端点必须使用 WSS，且 URL 不得嵌入用户名或密码。
- 生产审批引用必须绑定完整的当前公共配置哈希；空引用、旧引用或截断哈希引用均拒绝启动并记录审计事件。
- 本变更未改变任何仓位、杠杆、损失、VaR、回撤、熔断或流动性阈值。

## Capital Exposure Assessment

| Risk surface | Change effect | Residual requirement |
| --- | --- | --- |
| 风险权重在审批后被原地改写 | Reduced locally: loaded `strategy.weights` is read-only and serializer-safe | Production configuration approval and signed-artifact enforcement remain required |
| API 端点在哈希后被替换 | Reduced locally: loaded endpoint mapping is read-only, including an empty default mapping | Approved production endpoints and network controls remain required |
| 账户列表在加载后被追加 | Reduced locally: account snapshot is a tuple | Production secret resolution and account approval remain required |
| 多个账户配置复用同一身份标识 | Reduced locally: duplicate `account_id` values reject activation and are audited | Approved account registry and entitlement review remain required |
| Vault 密钥被发送到未批准或明文端点 | Reduced locally: prod enabled accounts require configured endpoints, HTTPS/WSS schemes and no embedded URL credentials | Approved network egress policy and endpoint ownership review remain required |
| 配置发布或热重载缺少可信审计记录 | Reduced locally: publish/reload/rejection/approval decisions emit hash-bound audit events without secret material | External immutable audit retention remains required |
| 审计故障期间发布新市场控制 | Reduced locally: candidate publication is blocked until its authorization event is recorded | External bus availability and operational monitoring remain required |
| 发布完成后审计失败导致运行态与缓存态不一致 | Reduced locally: mandatory audit events precede a prevalidated in-memory commit and successful activation always replaces loader cache | Process-wide loader ownership and external audit availability remain required |
| 非法首次启动没有拒绝审计记录 | Reduced locally: initial activation rejection emits non-sensitive error-type metadata when the audit bus is available | External immutable retention remains required |
| 旧审批或截断哈希审批复用于新配置 | Reduced locally: production approval reference must contain the full current `config_hash` | Approved external approval service remains required |
| 风险限制数值变化 | None: no configured threshold value changed | Continue to require risk approval before production |

## Invariant Impact

| Invariant | Assessment |
| --- | --- |
| Position | No order or position transition introduced; immutable account snapshot reduces unapproved routing risk |
| Equity | No PnL, cash or accounting logic changed |
| Order idempotency | No `client_order_id` behavior changed |
| Data consistency | No data processing behavior changed |
| Risk limit | Risk limits are unchanged; runtime mutation of hash-bound strategy weights is now blocked |

## Validation Evidence

- Development configuration load produces a public hash while exposing read-only risk-weight/API-endpoint mappings and a tuple account snapshot.
- Direct attempts to mutate loaded accounts, strategy weights or API endpoints are rejected.
- Configuration publication emits non-secret audit metadata; rejected reload preserves prior market controls and records its error type.
- Failure to persist either mandatory publication audit event prevents a candidate market-control snapshot from becoming active.
- Direct subsequent loading is classified as reload, synchronizes the loader cache with runtime controls and records old/new hashes.
- Invalid initial loading records non-sensitive rejection metadata without publishing controls.
- Production account endpoint binding rejects missing endpoints, plaintext REST/WebSocket schemes and URL-embedded credentials.
- A production approval reference bound only to a hash prefix is rejected; full-current-hash binding is required.
- Focused configuration, market-publication and audit regression: `170 passed`.
- Ruff, Black and strict mypy gates pass locally; lock-file validation reports only Poetry metadata deprecation warnings.
- Smoke backtest consistency: `100.0%`.
- Full regression: `664 passed` with total coverage `85.60%`.

## Approval Record

| Required approver | Status | Reference |
| --- | --- | --- |
| Risk officer | Pending | Not provided |
| Release/operations authorization | Pending | Not provided |

This report records development impact only. It must not be treated as production approval.
