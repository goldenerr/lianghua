"""
V6 Machine Learning Factor Model — IC prediction & factor timing with XGBoost/LightGBM.
Industry-standard approach used by top quant funds (WorldQuant, Two Sigma, 幻方).

Capabilities:
  1. Rolling IC prediction per factor (which factors will work next period?)
  2. Dynamic factor weights based on predicted IC
  3. Non-linear factor interaction modeling
  4. Factor decay / crowding detection
  5. Regime-switching factor model
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import numpy as np
import warnings
warnings.filterwarnings("ignore")


@dataclass
class MLCConfig:
    """ML factor model configuration."""
    # Training
    lookback_periods: int = 252        # days of history for training
    retrain_frequency: int = 21        # retrain every N days
    min_training_samples: int = 100
    
    # XGBoost parameters
    xgb_params: dict = field(default_factory=lambda: {
        'n_estimators': 100,
        'max_depth': 3,
        'learning_rate': 0.05,
        'subsample': 0.8,
        'colsample_bytree': 0.8,
        'reg_alpha': 0.1,
        'reg_lambda': 1.0,
        'random_state': 42,
    })
    
    # IC prediction features
    use_factor_autocorr: bool = True   # factor IC autocorrelation
    use_cross_factor_corr: bool = True # cross-factor correlation
    use_market_regime: bool = True     # market regime features
    use_factor_decay: bool = True      # IC decay features
    use_crowding: bool = True          # factor crowding detection


# ═══════════════════════════════════════════════════════════════
# IC (Information Coefficient) Computation
# ═══════════════════════════════════════════════════════════════

def compute_rolling_ic(
    factor_scores: np.ndarray,  # (n_stocks, n_days) factor values
    forward_returns: np.ndarray,  # (n_stocks, n_days) forward returns
    method: str = "spearman",
    window: int = 21,
) -> np.ndarray:
    """Compute rolling cross-sectional IC (rank correlation).
    
    IC_t = corr(factor_scores_t, forward_returns_t)
    """
    n_stocks, n_days = factor_scores.shape
    if n_days < window:
        return np.full(n_days - 1, np.nan)
    
    ic_series = np.full(n_days - 1, np.nan)
    
    for t in range(n_days - 1):
        if t < 10:  # need min stocks
            continue
        valid = ~(np.isnan(factor_scores[:, t]) | np.isnan(forward_returns[:, t]))
        if np.sum(valid) < 20:
            continue
        
        x = factor_scores[valid, t]
        y = forward_returns[valid, t]
        
        if method == "spearman":
            from scipy.stats import spearmanr
            ic, _ = spearmanr(x, y)
        elif method == "pearson":
            ic = np.corrcoef(x, y)[0, 1]
        else:
            ic = 0.0
        
        ic_series[t] = ic
    
    return ic_series


def compute_factor_ic_matrix(
    factor_scores_dict: dict[str, np.ndarray],  # {name: (n_stocks, n_days)}
    forward_returns: np.ndarray,
) -> dict[str, np.ndarray]:
    """Compute rolling IC for all factors."""
    return {name: compute_rolling_ic(scores, forward_returns)
            for name, scores in factor_scores_dict.items()}


# ═══════════════════════════════════════════════════════════════
# Market Regime Detection
# ═══════════════════════════════════════════════════════════════

def detect_market_regime(
    market_returns: np.ndarray,  # (n_days,) market return series
    vol_window: int = 21,
    trend_window: int = 60,
) -> np.ndarray:
    """Classify each day into market regimes.
    
    Returns: array of regime labels
      0 = low vol, mean-reverting
      1 = low vol, trending up  
      2 = low vol, trending down
      3 = high vol, any direction
    """
    n_days = len(market_returns)
    regimes = np.full(n_days, -1, dtype=int)
    
    for t in range(max(vol_window, trend_window), n_days):
        vol = np.std(market_returns[t-vol_window:t], ddof=1)
        trend = np.mean(market_returns[t-trend_window:t])
        
        # Normalize vol relative to historical
        hist_vol = np.std(market_returns[:t], ddof=1) if t > 100 else vol
        vol_ratio = vol / max(hist_vol, 1e-8)
        
        if vol_ratio > 1.5:
            regimes[t] = 3  # high vol
        elif trend > 0.001:
            regimes[t] = 1  # trending up
        elif trend < -0.001:
            regimes[t] = 2  # trending down
        else:
            regimes[t] = 0  # mean-reverting
    
    return regimes


# ═══════════════════════════════════════════════════════════════
# IC Prediction Features
# ═══════════════════════════════════════════════════════════════

def build_ic_prediction_features(
    ic_history: np.ndarray,
    cross_ic_matrix: dict[str, np.ndarray],
    factor_names: list[str],
    market_regimes: np.ndarray = None,
    lookback: int = 60,
) -> tuple[np.ndarray, np.ndarray]:
    """Build features for predicting next-period IC.
    
    Features per factor:
      - IC lag 1, 5, 21
      - IC moving average (5, 21)
      - IC volatility (21)
      - IC momentum (21d change)
      - Cross-factor average IC
      - Market regime (one-hot if available)
    
    Target: next-day IC
    """
    n_factors = len(factor_names)
    n_days = len(ic_history)
    
    features = []
    targets = []
    
    for t in range(lookback, n_days - 1):
        if np.isnan(ic_history[t]):
            continue
        
        feat_vec = []
        
        for i, name in enumerate(factor_names):
            ic_hist = ic_history[:t+1]  # up to time t
            
            if len(ic_hist) < lookback:
                continue
            
            # IC lags
            ic_lag1 = ic_hist[-1] if len(ic_hist) >= 1 else 0
            ic_lag5 = ic_hist[-5] if len(ic_hist) >= 5 else 0
            ic_lag21 = ic_hist[-21] if len(ic_hist) >= 21 else 0
            
            # IC moving averages
            ic_ma5 = np.nanmean(ic_hist[-5:]) if len(ic_hist) >= 5 else 0
            ic_ma21 = np.nanmean(ic_hist[-21:]) if len(ic_hist) >= 21 else 0
            
            # IC volatility
            ic_vol = np.nanstd(ic_hist[-21:]) if len(ic_hist) >= 21 else 0
            
            # IC momentum
            ic_mom = ic_ma5 - ic_ma21
            
            feat_vec.extend([ic_lag1, ic_lag5, ic_lag21, ic_ma5, ic_ma21, ic_vol, ic_mom])
            
            # Cross-factor IC
            cross_ic_mean = 0
            for other_name in factor_names:
                if other_name != name and other_name in cross_ic_matrix:
                    other_ic = cross_ic_matrix[other_name][:t+1]
                    cross_ic_mean += np.nanmean(other_ic[-21:]) if len(other_ic) >= 21 else 0
            cross_ic_mean /= max(len(factor_names) - 1, 1)
            feat_vec.append(cross_ic_mean)
        
        # Factor decay features
        for i, name in enumerate(factor_names):
            ic_hist = ic_history[:t+1]
            if len(ic_hist) >= 126:
                # IC trend over 6 months
                ic_first_half = np.nanmean(ic_hist[-126:-63])
                ic_second_half = np.nanmean(ic_hist[-63:])
                ic_decay = ic_second_half - ic_first_half
                feat_vec.append(ic_decay)
            else:
                feat_vec.append(0.0)
        
        # Market regime (one-hot)
        if market_regimes is not None and t < len(market_regimes):
            regime = market_regimes[t]
            one_hot = [0, 0, 0, 0]
            if 0 <= regime < 4:
                one_hot[regime] = 1
            feat_vec.extend(one_hot)
        
        target = ic_history[t + 1]  # next day IC
        
        features.append(feat_vec)
        targets.append(target)
    
    return np.array(features), np.array(targets)


# ═══════════════════════════════════════════════════════════════
# Factor Timing Model (XGBoost)
# ═══════════════════════════════════════════════════════════════

class FactorTimingModel:
    """XGBoost-based factor timing model.
    
    Predicts next-period IC for each factor based on:
      - Historical IC patterns
      - Cross-factor interactions
      - Market regime
    """
    
    def __init__(self, config: MLCConfig = None):
        self.config = config or MLCConfig()
        self.models = {}  # {factor_name: xgb_model}
        self.last_train_day = -1
        self._xgb = None
    
    def _get_xgb(self):
        if self._xgb is None:
            try:
                import xgboost as xgb
                self._xgb = xgb
            except ImportError:
                return None
        return self._xgb
    
    def predict_ic(self, features: np.ndarray, factor_name: str = None) -> np.ndarray:
        """Predict next-period IC."""
        if factor_name and factor_name in self.models:
            model = self.models[factor_name]
        elif 'default' in self.models:
            model = self.models['default']
        else:
            return np.zeros(len(features))
        
        xgb = self._get_xgb()
        if xgb is None:
            return np.zeros(len(features))
        
        return model.predict(features)
    
    def train(self, features: np.ndarray, targets: np.ndarray, 
              factor_name: str = 'default'):
        """Train XGBoost model for IC prediction."""
        xgb = self._get_xgb()
        if xgb is None:
            # Fallback: simple linear regression
            self.models[factor_name] = _LinearFallback()
            self.models[factor_name].fit(features, targets)
            return
        
        if len(features) < self.config.min_training_samples:
            return
        
        # Train/val split
        n_train = int(len(features) * 0.8)
        X_train, y_train = features[:n_train], targets[:n_train]
        X_val, y_val = features[n_train:], targets[n_train:]
        
        if len(X_train) < 50:
            return
        
        params = self.config.xgb_params.copy()
        dtrain = xgb.DMatrix(X_train, label=y_train)
        dval = xgb.DMatrix(X_val, label=y_val)
        
        model = xgb.train(
            params,
            dtrain,
            num_boost_round=params.get('n_estimators', 100),
            evals=[(dval, 'val')],
            verbose_eval=False,
        )
        
        self.models[factor_name] = model
        self.last_train_day = 0  # placeholder
    
    def compute_dynamic_weights(
        self,
        features_dict: dict[str, np.ndarray],
        factor_names: list[str],
        base_weights: dict[str, float],
        temperature: float = 2.0,
    ) -> dict[str, float]:
        """Compute dynamic factor weights based on IC predictions.
        
        w_i = base_w_i * exp(predicted_IC_i / temperature)
        Then normalized to sum to 1.
        """
        ic_predictions = {}
        for name in factor_names:
            if name not in features_dict:
                ic_predictions[name] = 0.0
                continue
            
            features = features_dict[name]
            if len(features.shape) == 1:
                features = features.reshape(1, -1)
            
            preds = self.predict_ic(features, name)
            ic_predictions[name] = float(np.mean(preds[-5:])) if len(preds) >= 5 else float(preds[-1]) if len(preds) > 0 else 0.0
        
        # Compute tilt weights
        tilted = {}
        total = 0.0
        for name in factor_names:
            base_w = base_weights.get(name, 1.0 / len(factor_names))
            ic_pred = ic_predictions.get(name, 0.0)
            tilted[name] = base_w * np.exp(ic_pred / temperature)
            total += tilted[name]
        
        if total < 1e-12:
            return {name: base_weights.get(name, 1.0 / len(factor_names)) for name in factor_names}
        
        return {name: w / total for name, w in tilted.items()}


class _LinearFallback:
    """Linear regression fallback when XGBoost is unavailable."""
    def __init__(self):
        self.coef_ = None
        self.intercept_ = 0.0
    
    def fit(self, X, y):
        try:
            X_with_intercept = np.column_stack([np.ones(len(X)), X])
            coef = np.linalg.lstsq(X_with_intercept, y, rcond=None)[0]
            self.intercept_ = coef[0]
            self.coef_ = coef[1:]
        except np.linalg.LinAlgError:
            self.coef_ = np.zeros(X.shape[1])
    
    def predict(self, X):
        if self.coef_ is None:
            return np.zeros(len(X))
        return X @ self.coef_ + self.intercept_


# ═══════════════════════════════════════════════════════════════
# Factor Crowding Detection
# ═══════════════════════════════════════════════════════════════

def detect_factor_crowding(
    factor_scores: dict[str, np.ndarray],
    lookback: int = 63,
) -> dict[str, float]:
    """Detect factor crowding via correlation structure.
    
    Crowding signals:
      1. Rising cross-sectional correlation among top-ranked stocks
      2. Factor return autocorrelation increase
      3. Increasing factor volatility
    """
    crowding_signals = {}
    
    for name, scores in factor_scores.items():
        n_stocks, n_days = scores.shape
        
        if n_days < lookback or n_stocks < 20:
            crowding_signals[name] = 0.0
            continue
        
        # 1. Pairwise correlation among top decile
        top_n = max(20, n_stocks // 10)
        recent_scores = scores[:, -lookback:]
        
        # Correlation of top stocks' scores
        top_corr = 0.0
        count = 0
        for t in range(recent_scores.shape[1]):
            valid = ~np.isnan(recent_scores[:, t])
            if np.sum(valid) < top_n: continue
            top_idx = np.argsort(recent_scores[valid, t])[-top_n:]
            top_scores = recent_scores[valid, t][top_idx]
            if len(top_scores) > 5:
                corr_mat = np.corrcoef(top_scores.reshape(1, -1))
                if corr_mat.size > 1:
                    top_corr += abs(corr_mat[0, 0])
                    count += 1
        
        # 2. IC volatility increase
        ic_vol_first = np.nanstd(scores[:, :n_days//2], axis=1).mean() if n_days > 20 else 0
        ic_vol_second = np.nanstd(scores[:, n_days//2:], axis=1).mean() if n_days > 20 else 0
        vol_increase = ic_vol_second / max(ic_vol_first, 1e-12) - 1.0
        
        # Combined crowding signal
        signal = 0.5 * (top_corr / max(count, 1)) + 0.5 * np.clip(vol_increase, 0, 1)
        crowding_signals[name] = float(np.clip(signal, 0, 1))
    
    return crowding_signals
