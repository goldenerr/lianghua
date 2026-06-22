# 量化交易系统

[![CI](https://github.com/goldenerr/quant-trading-system/actions/workflows/ci.yml/badge.svg)](https://github.com/goldenerr/quant-trading-system/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

这是一个面向生产级可靠性、资金安全和可复现性的多市场量化交易系统。

生产部署在签名发布证据、风控审批、模拟盘/小资金实盘 gate 完成前一律阻塞。详见 `ARCHITECTURE.md` 和 `RISK_POLICY.md`。

## 功能概览

- **多市场支持**：A 股、期货、加密货币，并支持继续扩展。
- **高性能回测**：向量化回测引擎，支持 Walk-Forward 验证。
- **风险优先架构**：多层风控、核心不变性校验和 Safe Mode。
- **事件驱动**：实时事件总线和可审计事件链。
- **配置安全**：基于 Pydantic v2 和 SecretStr，禁止硬编码密钥。
- **可观测性**：结构化日志、Grafana 看板和告警。

## 快速开始

```bash
# 初始化开发环境
./init.sh dev

# 可选：预下载本地 pre-commit hook 环境
pre-commit install-hooks

# 激活虚拟环境
source .venv/bin/activate

# 查看 CLI 帮助
quant-cli --help

# 运行测试
pytest

# 运行强制快速安全路径
pytest -q tests/integration/test_smoke.py

# 启动 Docker 开发基线
docker compose -f deployment/docker/docker-compose.yml up -d
```

## 目录结构

```text
src/quant_trading/
├── core/        # 事件引擎、状态机、审计总线
├── data/        # 多源数据管道（yfinance、akshare、ccxt）
├── strategy/    # 可插拔策略框架和 A/B 测试
├── risk/        # 多层风险管理
├── execution/   # 订单管理和交易所适配
├── portfolio/   # 组合优化和再平衡
├── monitor/     # 实时监控和告警
├── ml/          # 机器学习模型
├── compliance/  # 合规报告
└── utils/       # 通用工具
```

## 开发规范

开发流程和规范见 `AGENTS.md`。
功能跟踪见 `feature_list.json`。

## 许可证

MIT
