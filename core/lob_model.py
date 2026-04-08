"""
LOB Imbalance Model — lightweight polynomial regression on order book features.
Uses only numpy; no ONNX required.

LOBFeatures: 9 numerical features extracted from an L2 OrderBook snapshot.
LOBImbalanceModel: degree-2 polynomial regression, output in [-1.0, +1.0].
  Positive = bullish drift (more buying pressure)
  Negative = bearish drift (more selling pressure)

Works with both core.models.OrderBook (L2Level.size) and
core.microstructure.OrderBook (Level.size) — both use .price and .size.
"""
from __future__ import annotations
from dataclasses import dataclass
import numpy as np


@dataclass
class LOBFeatures:
    bid_ask_imbalance: float    # (bid_qty - ask_qty) / (bid_qty + ask_qty) at BBO
    depth_imbalance_5: float    # same but 5 levels
    depth_imbalance_10: float   # same but 10 levels
    queue_pressure_bid: float   # sum(bid_qty * 1/(i+1)) normalized
    queue_pressure_ask: float   # sum(ask_qty * 1/(i+1)) normalized
    weighted_mid: float         # size-weighted mid price
    spread_bps: float           # (ask - bid) / mid * 10000
    bid_slope: float            # linear slope of cumulative bid qty vs price (polyfit)
    ask_slope: float            # linear slope of cumulative ask qty vs price (polyfit)

    def to_array(self) -> np.ndarray:
        return np.array([
            self.bid_ask_imbalance,
            self.depth_imbalance_5,
            self.depth_imbalance_10,
            self.queue_pressure_bid,
            self.queue_pressure_ask,
            self.weighted_mid,
            self.spread_bps,
            self.bid_slope,
            self.ask_slope,
        ], dtype=np.float64)


def _get_size(level) -> float:
    """Get size from any level type (L2Level.size or Level.size)."""
    return float(getattr(level, "size", 0))


def _depth_imbalance(bids, asks, n: int) -> float:
    b = sum(_get_size(lvl) for lvl in bids[:n])
    a = sum(_get_size(lvl) for lvl in asks[:n])
    total = b + a
    return (b - a) / total if total > 0 else 0.0


def _queue_pressure(levels, n: int = 10) -> float:
    total = sum(_get_size(lvl) / (i + 1) for i, lvl in enumerate(levels[:n]))
    norm = sum(1 / (i + 1) for i in range(min(n, len(levels)))) or 1.0
    return total / norm


def _slope(levels) -> float:
    """Linear slope of cumulative qty vs price (polyfit degree 1)."""
    if len(levels) < 2:
        return 0.0
    prices = np.array([lvl.price for lvl in levels], dtype=np.float64)
    qtys = np.cumsum([_get_size(lvl) for lvl in levels], dtype=np.float64)
    try:
        coeffs = np.polyfit(prices, qtys, 1)
        return float(coeffs[0])
    except Exception:
        return 0.0


def _weighted_mid(bids, asks) -> float:
    """Size-weighted mid price (microprice)."""
    if not bids or not asks:
        return 0.0
    bb, ba = bids[0], asks[0]
    bb_size = _get_size(bb)
    ba_size = _get_size(ba)
    total = bb_size + ba_size
    if total == 0.0:
        return (bb.price + ba.price) / 2.0
    return ba.price * (bb_size / total) + bb.price * (ba_size / total)


def extract_features(book) -> LOBFeatures:
    """Extract LOB features from any OrderBook-like object."""
    bids = list(getattr(book, "bids", None) or [])
    asks = list(getattr(book, "asks", None) or [])

    bid_1 = sum(_get_size(lvl) for lvl in bids[:1])
    ask_1 = sum(_get_size(lvl) for lvl in asks[:1])
    bbo_total = bid_1 + ask_1
    bbo_imb = (bid_1 - ask_1) / bbo_total if bbo_total > 0 else 0.0

    wmid = _weighted_mid(bids, asks) if (bids and asks) else 0.0
    if bids and asks:
        best_bid = bids[0].price
        best_ask = asks[0].price
        mid = (best_bid + best_ask) / 2.0
        spread_bps = (best_ask - best_bid) / mid * 10_000 if mid > 0 else 0.0
    else:
        spread_bps = 0.0

    return LOBFeatures(
        bid_ask_imbalance=bbo_imb,
        depth_imbalance_5=_depth_imbalance(bids, asks, 5),
        depth_imbalance_10=_depth_imbalance(bids, asks, 10),
        queue_pressure_bid=_queue_pressure(bids),
        queue_pressure_ask=_queue_pressure(asks),
        weighted_mid=wmid,
        spread_bps=spread_bps,
        bid_slope=_slope(bids),
        ask_slope=_slope(asks),
    )


# Default pretrained coefficients (fit on synthetic balanced data → near-zero drift)
_DEFAULT_LINEAR_WEIGHTS = np.array([
    0.35,   # bid_ask_imbalance — strongest signal
    0.20,   # depth_imbalance_5
    0.15,   # depth_imbalance_10
    0.05,   # queue_pressure_bid
   -0.05,   # queue_pressure_ask
    0.00,   # weighted_mid (not directional)
   -0.10,   # spread_bps (wider spread = uncertainty)
    0.08,   # bid_slope
   -0.08,   # ask_slope
], dtype=np.float64)

_DEFAULT_MEAN = np.zeros(9, dtype=np.float64)
_DEFAULT_STD = np.ones(9, dtype=np.float64)


def _poly2_features(x: np.ndarray) -> np.ndarray:
    """Degree-2 polynomial feature expansion (no cross terms for simplicity).

    Result: [1, x1, ..., x9, x1^2, ..., x9^2] = 19 features
    """
    bias = np.array([1.0])
    squared = x ** 2
    return np.concatenate([bias, x, squared])


class LOBImbalanceModel:
    N_FEATURES = 9
    N_POLY = 1 + N_FEATURES + N_FEATURES  # 19: bias + linear + squared

    def __init__(self, coefficients: np.ndarray | None = None) -> None:
        if coefficients is not None:
            assert len(coefficients) == self.N_POLY, \
                f"Expected {self.N_POLY} coefficients, got {len(coefficients)}"
            self._coeffs = coefficients.astype(np.float64)
        else:
            # Build default from linear weights padded with zeros for squared terms
            default = np.concatenate([
                [0.0],                                  # bias
                _DEFAULT_LINEAR_WEIGHTS,                # 9 linear
                np.zeros(self.N_FEATURES),              # 9 squared (zero)
            ])
            self._coeffs = default
        self._mean = _DEFAULT_MEAN.copy()
        self._std = _DEFAULT_STD.copy()

    @property
    def coefficients(self) -> np.ndarray:
        return self._coeffs

    @coefficients.setter
    def coefficients(self, value: np.ndarray) -> None:
        self._coeffs = value.astype(np.float64)

    def predict_short_term_drift(self, book) -> float:
        """Return predicted short-term price drift in [-1.0, +1.0]."""
        features = extract_features(book)
        x = features.to_array()
        # Normalize
        x_norm = (x - self._mean) / (self._std + 1e-8)
        # Polynomial expansion
        x_poly = _poly2_features(x_norm)
        # Linear prediction
        drift = float(np.dot(x_poly, self._coeffs))
        # Clip to [-1, 1]
        return max(-1.0, min(1.0, drift))

    @classmethod
    def fit(cls, snapshots: list, targets: list[float]) -> "LOBImbalanceModel":
        """Fit model via numpy least-squares."""
        X_raw = np.array([extract_features(s).to_array() for s in snapshots])
        y = np.array(targets, dtype=np.float64)
        mean = X_raw.mean(axis=0)
        std = X_raw.std(axis=0)
        X_norm = (X_raw - mean) / (std + 1e-8)
        X_poly = np.array([_poly2_features(row) for row in X_norm])
        # Least squares
        coeffs, _, _, _ = np.linalg.lstsq(X_poly, y, rcond=None)
        model = cls(coefficients=coeffs)
        model._mean = mean
        model._std = std
        return model
