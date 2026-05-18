# Quant Trading System

[![CI](https://github.com/goldenerr/quant-trading-system/actions/workflows/ci.yml/badge.svg)](https://github.com/goldenerr/quant-trading-system/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.9%20%7C%203.10%20%7C%203.11-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Production-grade multi-market quantitative trading system. Built for reliability, safety, and reproducibility.

## Features

- **Multi-market**: A-shares, futures, cryptocurrencies (extensible)
- **High-performance backtesting**: Vectorized engine with walk-forward validation
- **Risk-first architecture**: Multi-layered risk management with invariants enforcement
- **Event-driven**: Real-time event bus with audit trail
- **Configuration safety**: Pydantic v2 + SecretStr, no hardcoded secrets
- **Observability**: Structured logging, Grafana dashboards, alerts

## Quick Start

```bash
# Initialize development environment
./init.sh dev

# Activate virtual environment
source .venv/bin/activate

# Run CLI
quant-cli --help

# Run tests
pytest

# Docker
docker compose -f deployment/docker/docker-compose.yml up -d
```

## Architecture

```
src/quant_trading/
├── core/        # Event engine, state machine, audit bus
├── data/        # Multi-source data pipeline (yfinance, akshare, ccxt)
├── strategy/    # Pluggable strategy framework with A/B testing
├── risk/        # Multi-layered risk management
├── execution/   # Order management, exchange adapters
├── portfolio/   # Portfolio optimization, rebalancing
├── monitor/     # Real-time monitoring, alerts
├── ml/          # Machine learning models
├── compliance/  # Regulatory reporting
└── utils/       # Shared utilities
```

## Development

See `AGENTS.md` for development workflow and standards.  
See `feature_list.json` for feature tracking.

## License

MIT
