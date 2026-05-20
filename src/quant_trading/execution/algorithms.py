"""
V6 Execution Algorithms — VWAP, TWAP, Almgren-Chriss market impact.
Industry-standard optimal execution for institutional trading.

References:
  - Almgren & Chriss (2000) "Optimal Execution of Portfolio Transactions"
  - Almgren et al. (2005) "Direct Estimation of Equity Market Impact"
  - Kissell & Glantz (2003) "Optimal Trading Strategies"
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import numpy as np


@dataclass
class MarketImpactParams:
    """Almgren-Chriss market impact parameters (calibrated for A-shares)."""
    # Permanent impact (information leakage): γ * σ * √(Q/V)  
    gamma: float = 0.15    # permanent impact coefficient
    # Temporary impact (liquidity demand): η * σ * (Q/(V*T))^β
    eta: float = 0.15       # temporary impact coefficient
    beta: float = 0.6       # temporary impact exponent (0.5-0.7 typical)
    # Participation rate constraint
    max_participation: float = 0.05  # max 5% of volume
    # Spread cost
    half_spread: float = 0.0010  # 0.10% half-spread (typical A-share)


@dataclass
class ExecutionSchedule:
    """Optimal execution schedule."""
    shares_per_slot: np.ndarray  # shares to trade in each time slot
    expected_cost: float         # expected implementation shortfall
    expected_cost_pct: float     # as % of order value
    risk: float                  # execution risk (std of cost)


# ═══════════════════════════════════════════════════════════════
# TWAP (Time-Weighted Average Price)
# ═══════════════════════════════════════════════════════════════

def twap_schedule(total_shares: int, n_slots: int = 10) -> ExecutionSchedule:
    """Simple TWAP: equal shares per slot."""
    shares_per_slot = np.ones(n_slots) * total_shares // n_slots
    shares_per_slot[0] += total_shares - shares_per_slot.sum()  # remainder in first slot
    return ExecutionSchedule(
        shares_per_slot=shares_per_slot,
        expected_cost=0.0,
        expected_cost_pct=0.0,
        risk=0.0,
    )


# ═══════════════════════════════════════════════════════════════
# Almgren-Chriss Optimal Execution
# ═══════════════════════════════════════════════════════════════

def almgren_chriss(
    shares: int,
    daily_volume: int,
    volatility: float,
    price: float,
    risk_aversion: float = 1e-6,
    n_slots: int = 10,
    params: MarketImpactParams = None,
) -> ExecutionSchedule:
    """Almgren-Chriss optimal execution schedule.
    
    Minimizes: temporary_impact + permanent_impact + λ * execution_risk
    
    Args:
        shares: total shares to execute
        daily_volume: average daily volume
        volatility: annualized volatility
        price: current price
        risk_aversion: lambda (risk penalty)
        n_slots: number of time slots
        params: market impact parameters
    """
    if params is None:
        params = MarketImpactParams()
    
    p = params
    T = n_slots
    Q = shares
    V = daily_volume
    sigma = volatility  # annualized
    
    # Per-slot volatility
    sigma_slot = sigma / np.sqrt(252 * T)
    
    # Volume per slot
    volume_per_slot = V / T
    
    # Maximum shares per slot (participation constraint)
    max_per_slot = int(volume_per_slot * p.max_participation)
    
    # Temporary impact: η * σ * (v/V_slot)^β * price  
    # Optimal schedule from Euler-Lagrange
    kappa = p.eta * sigma_slot / (volume_per_slot ** p.beta) * price
    
    # Closed-form for κ = β=0 case (quadratic costs)
    # For general β, solve numerically
    
    def objective(x):
        """Total cost of schedule x (shares per slot)."""
        if np.any(x < 0) or np.any(x > max_per_slot):
            return 1e10
        
        # Temporary impact cost
        temp_cost = np.sum(kappa * (x / volume_per_slot) ** p.beta * x)
        
        # Permanent impact cost
        cum_traded = np.cumsum(x)
        perm_cost = np.sum(p.gamma * sigma_slot * np.sqrt(x / volume_per_slot) * cum_traded * price)
        
        # Risk cost (variance of remaining position)
        remaining = Q - cum_traded
        risk = np.sum(remaining ** 2) * sigma_slot ** 2
        risk_cost = risk_aversion * risk
        
        return temp_cost + perm_cost + risk_cost
    
    # Initial guess: uniform
    x0 = np.ones(T) * Q / T
    
    # Solve via simple gradient-free optimization
    best_x = x0.copy()
    best_cost = objective(best_x)
    
    # Try front-loaded vs back-loaded variants
    for _ in range(50):
        candidate = np.clip(np.random.dirichlet(np.ones(T)) * Q, 0, max_per_slot)
        candidate = candidate * Q / candidate.sum()  # renormalize
        candidate = np.clip(candidate, 0, max_per_slot)
        if np.sum(candidate) < Q * 0.95:
            continue
        
        cost = objective(candidate)
        if cost < best_cost:
            best_cost = cost
            best_x = candidate.copy()
    
    # Normalize
    x = np.round(best_x).astype(int)
    diff = shares - x.sum()
    x[0] += diff  # adjust remainder
    
    # Expected cost estimate
    expected_cost_pct = best_cost / (Q * price) if Q * price > 0 else 0
    execution_risk = sigma_slot * np.sqrt(np.sum((Q - np.cumsum(x)) ** 2))
    
    return ExecutionSchedule(
        shares_per_slot=x,
        expected_cost=float(best_cost),
        expected_cost_pct=float(expected_cost_pct),
        risk=float(execution_risk),
    )


# ═══════════════════════════════════════════════════════════════
# VWAP (Volume-Weighted Average Price)
# ═══════════════════════════════════════════════════════════════

def vwap_schedule(shares: int, volume_profile: np.ndarray, 
                   n_slots: int = 10) -> ExecutionSchedule:
    """VWAP execution following historical volume profile.
    
    Args:
        shares: total shares to execute
        volume_profile: historical volume per slot (length = n_slots)
        n_slots: number of time slots
    """
    if len(volume_profile) == 0:
        return twap_schedule(shares, n_slots)
    
    # Normalize volume profile to probabilities
    profile = np.array(volume_profile[-n_slots:], dtype=float)
    if profile.sum() < 1e-12:
        return twap_schedule(shares, n_slots)
    
    weights = profile / profile.sum()
    shares_per_slot = np.round(weights * shares).astype(int)
    diff = shares - shares_per_slot.sum()
    shares_per_slot[np.argmax(weights)] += diff
    
    return ExecutionSchedule(
        shares_per_slot=shares_per_slot,
        expected_cost=0.0,
        expected_cost_pct=0.0,
        risk=0.0,
    )


# ═══════════════════════════════════════════════════════════════
# Implementation Shortfall Estimation
# ═══════════════════════════════════════════════════════════════

def estimate_implementation_shortfall(
    shares: int,
    price: float,
    daily_volume: int,
    volatility: float,
    side: str = "buy",
    urgency: float = 0.5,  # 0=passive, 1=aggressive
    params: MarketImpactParams = None,
) -> dict:
    """Estimate total implementation shortfall including commissions.
    
    Returns dict with cost breakdown as % of order value.
    """
    if params is None:
        params = MarketImpactParams()
    
    order_value = shares * price
    participation_rate = shares / max(daily_volume, 1)
    
    # 1. Spread cost (half-spread * side)
    spread_cost = params.half_spread
    if side == "buy":
        spread_cost = params.half_spread
    
    # 2. Market impact (Almgren universal model)
    # I = σ * (γ * |Q/V|^α + η * |Q/(V*T)|^β)  * sign(Q)
    daily_vol_usd = volatility * price / np.sqrt(252)
    q_over_v = shares / max(daily_volume, 1)
    
    perm_impact = params.gamma * daily_vol_usd * (q_over_v ** 0.5)
    temp_impact = params.eta * daily_vol_usd * (q_over_v ** params.beta)
    
    # Urgency adjustment
    impact = (perm_impact + temp_impact) * (0.5 + 0.5 * urgency)
    impact_pct = impact / max(price, 0.01)
    
    # 3. Delay cost (risk of adverse selection during execution)
    delay_cost = volatility * np.sqrt(urgency / 252) * 0.5  # half of daily vol
    
    # 4. Commission
    commission = 0.00025  # 0.025%
    stamp_duty = 0.0005 if side == "sell" else 0.0  # 0.05% only on sells
    
    total_pct = spread_cost + impact_pct + delay_cost + commission + stamp_duty
    
    return {
        "total_cost_pct": round(total_pct * 100, 4),
        "spread_bps": round(spread_cost * 10000, 1),
        "impact_bps": round(impact_pct * 10000, 1),
        "delay_bps": round(delay_cost * 10000, 1),
        "commission_bps": round((commission + stamp_duty) * 10000, 1),
        "participation_rate": round(participation_rate * 100, 1),
    }


# ═══════════════════════════════════════════════════════════════
# Smart Order Routing (simplified)
# ═══════════════════════════════════════════════════════════════

def smart_route_order(
    shares: int,
    price: float,
    daily_volume: int,
    volatility: float,
    side: str = "buy",
    max_pct_adv: float = 0.05,
) -> list[ExecutionSchedule]:
    """Smart order routing: split across algorithms based on order size.
    
    Returns list of child schedules.
    """
    pct_adv = shares / max(daily_volume, 1)
    
    params = MarketImpactParams(max_participation=max_pct_adv)
    
    if pct_adv < 0.01:
        # Small order: VWAP over 30min
        return [vwap_schedule(shares, np.ones(6), 6)]
    elif pct_adv < 0.05:
        # Medium order: Almgren-Chriss over 1 day
        return [almgren_chriss(shares, daily_volume, volatility, price,
                               risk_aversion=1e-6, n_slots=10, params=params)]
    else:
        # Large order: split into 3 VWAP slices over 3 days
        slice_size = shares // 3
        schedules = []
        for i in range(3):
            sz = slice_size + (shares - 3 * slice_size if i == 0 else 0)
            schedules.append(vwap_schedule(sz, np.ones(10), 10))
        return schedules
