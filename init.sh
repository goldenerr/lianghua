#!/usr/bin/env bash
# =============================================================================
# init.sh — Quant Trading System Environment Initializer
# Usage: ./init.sh [dev|test|prod] [--force-rebuild] [--clean]
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Defaults ────────────────────────────────────────────────────────────────
ENV="${1:-dev}"
FORCE_REBUILD=false
CLEAN=false

# Parse remaining arguments
shift 2>/dev/null || true
for arg in "$@"; do
    case "$arg" in
        --force-rebuild) FORCE_REBUILD=true ;;
        --clean)         CLEAN=true ;;
        *)               echo "Unknown argument: $arg"; exit 1 ;;
    esac
done

# Validate environment
case "$ENV" in
    dev|test|prod) ;;
    *) echo "Error: environment must be dev, test, or prod (got: $ENV)"; exit 1 ;;
esac

echo "========================================"
echo " Quant Trading System — Init"
echo " Environment: $ENV"
echo "========================================"

# ── Clean (if requested) ─────────────────────────────────────────────────────
if $CLEAN; then
    echo "[clean] Removing caches and build artifacts..."
    rm -rf .venv/ .venv-test/ .venv-prod/
    rm -rf .mypy_cache/ .ruff_cache/ .pytest_cache/
    rm -rf dist/ build/ *.egg-info/
    rm -rf htmlcov/ .coverage coverage.xml
    find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    echo "[clean] Done."
fi

# ── Poetry check ─────────────────────────────────────────────────────────────
if ! command -v poetry &>/dev/null; then
    echo "[setup] Installing Poetry..."
    pip3 install poetry --quiet
fi
echo "[setup] Poetry $(poetry --version | awk '{print $3}')"

# ── Virtual environment ──────────────────────────────────────────────────────
VENV_NAME=".venv"
[ "$ENV" != "dev" ] && VENV_NAME=".venv-$ENV"

if [ ! -d "$VENV_NAME" ] || $FORCE_REBUILD; then
    if $FORCE_REBUILD && [ -d "$VENV_NAME" ]; then
        echo "[venv] Force-rebuilding $VENV_NAME..."
        rm -rf "$VENV_NAME"
    fi
    echo "[venv] Creating $VENV_NAME..."
    python3 -m venv "$VENV_NAME"
fi

# Activate
source "$VENV_NAME/bin/activate"
echo "[venv] Activated ($VENV_NAME)"

# ── Install dependencies ─────────────────────────────────────────────────────
echo "[deps] Installing core dependencies..."

if [ "$ENV" = "dev" ]; then
    poetry install --with dev --no-root 2>&1 | tail -3
elif [ "$ENV" = "test" ]; then
    poetry install --with test --no-root 2>&1 | tail -3
else
    # prod: core only, no dev/test groups
    poetry install --only main --no-root 2>&1 | tail -3
fi

# ── Pre-commit hooks (dev only) ──────────────────────────────────────────────
if [ "$ENV" = "dev" ]; then
    echo "[hooks] Installing pre-commit hooks..."
    pre-commit install --install-hooks 2>&1 | tail -3
fi

# ── Config validation ────────────────────────────────────────────────────────
echo "[config] Validating configuration..."
CONFIG_FILE="config/$ENV.yaml"
if [ -f "$CONFIG_FILE" ]; then
    echo "[config] Using $CONFIG_FILE"
else
    echo "[config] Warning: $CONFIG_FILE not found. Run config-001 to create it."
fi

# ── Environment file ─────────────────────────────────────────────────────────
ENV_FILE=".env.$ENV"
if [ ! -f "$ENV_FILE" ] && [ ! -f ".env" ]; then
    echo "[env] Creating $ENV_FILE from example..."
    if [ -f ".env.example" ]; then
        cp .env.example "$ENV_FILE"
    fi
fi

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "========================================"
echo " Environment $ENV ready."
echo ""
echo " Activate with:  source $VENV_NAME/bin/activate"
echo " Run CLI:        quant-cli --help"
echo " Run tests:      pytest"
echo "========================================"
