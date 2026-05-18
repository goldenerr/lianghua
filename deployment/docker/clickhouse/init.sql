-- Quant Trading System — ClickHouse initialization
CREATE DATABASE IF NOT EXISTS quant;

-- Market data table (timeseries)
CREATE TABLE IF NOT EXISTS quant.market_data (
    symbol String,
    exchange String,
    timestamp DateTime64(3),
    open Float64,
    high Float64,
    low Float64,
    close Float64,
    volume Float64,
    vwap Float64,
    trades UInt32,
    data_source String,
    adjusted UInt8 DEFAULT 0
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (symbol, timestamp)
SETTINGS index_granularity = 8192;

-- Order events table
CREATE TABLE IF NOT EXISTS quant.order_events (
    event_time DateTime64(3),
    client_order_id String,
    exchange_order_id String,
    strategy_id String,
    symbol String,
    order_type String,
    side String,
    quantity Float64,
    price Float64,
    status String,
    filled_qty Float64 DEFAULT 0,
    avg_price Float64 DEFAULT 0,
    commission Float64 DEFAULT 0,
    trace_id String
) ENGINE = MergeTree()
ORDER BY (strategy_id, event_time, symbol);

-- Risk metrics table
CREATE TABLE IF NOT EXISTS quant.risk_metrics (
    timestamp DateTime64(3),
    strategy_id String,
    var_95 Float64,
    sharpe_20d Float64,
    max_drawdown Float64,
    total_exposure Float64,
    leverage Float64,
    daily_pnl Float64,
    total_equity Float64
) ENGINE = MergeTree()
ORDER BY (strategy_id, timestamp);

-- Audit events table (immutable)
CREATE TABLE IF NOT EXISTS quant.audit_events (
    event_time DateTime64(3),
    event_type String,
    source String,
    trace_id String,
    payload String,
    signature String DEFAULT ''
) ENGINE = MergeTree()
ORDER BY (event_type, event_time);
