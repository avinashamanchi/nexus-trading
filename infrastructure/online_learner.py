"""
Intraday AI Retraining with RDMA / InfiniBand cluster simulation.

Market regimes shift intraday — a LOB model trained overnight can be
obsolete by 11:00 AM.  This module addresses that with four components:

1. InfiniBandCluster
   Simulates an NVIDIA H100 GPU cluster connected via InfiniBand HDR (200 Gbps).
   RDMA (Remote Direct Memory Access) lets GPUs exchange gradients without
   CPU involvement — key for latency-sensitive retraining loops.

2. OnlineLOBLearner
   Wraps LOBImbalanceModel with a streaming observation ring-buffer and
   incremental SGD updates.  Three retraining triggers:
     (a) New-observation count threshold  (default: 500)
     (b) Fixed wall-clock interval         (default: 15 min)
     (c) Prediction error spike > 3σ baseline

3. Hot-swap
   Model B trains while model A runs live.  When B passes held-out
   validation, coefficient pointer is atomically swapped — zero downtime.

4. RetrainingScheduler
   Asyncio background task; orchestrates periodic retraining calls and
   simulates RDMA gradient transfer overhead.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import math
import time
from dataclasses import dataclass, field
from typing import NamedTuple

import numpy as np

from core.lob_model import LOBFeatures, LOBImbalanceModel, extract_features
from core.models import OrderBook

logger = logging.getLogger(__name__)


# ─── InfiniBand / RDMA Cluster ────────────────────────────────────────────────

@dataclass
class InfiniBandCluster:
    """
    Simulates an NVIDIA H100 GPU cluster connected via InfiniBand HDR.

    RDMA one-sided PUT: GPU → GPU without CPU involvement.
    InfiniBand HDR: 200 Gbps per port, ~1.2 µs base latency.
    Ring-AllReduce: bandwidth-optimal gradient aggregation.
    """
    num_gpus:          int   = 8
    bandwidth_gbps:    float = 200.0
    base_latency_us:   float = 1.2      # RDMA base (no software stack)
    cpu_bypass:        bool  = True

    def transfer_latency_us(self, data_bytes: int) -> float:
        """RDMA one-sided GET/PUT latency: base + data/bandwidth."""
        wire_us = (data_bytes * 8) / (self.bandwidth_gbps * 1e9) * 1e6
        return self.base_latency_us + wire_us

    def all_reduce_latency_us(self, num_params: int, dtype_bytes: int = 4) -> float:
        """
        Ring-AllReduce latency for gradient synchronisation.
        Bandwidth-optimal formula: 2(N−1)/N × data / bandwidth
        """
        data  = num_params * dtype_bytes
        N     = self.num_gpus
        ring  = 2 * (N - 1) / N * data
        wire_us = (ring * 8) / (self.bandwidth_gbps * 1e9) * 1e6
        return self.base_latency_us + wire_us

    async def simulate_training_step(
        self, num_params: int, batch_size: int = 512
    ) -> float:
        """
        Simulate one mini-batch training step.  Returns total time in ms.
        H100 peak: ~312 TFLOPS FP16; gradient sync via InfiniBand AllReduce.
        """
        # Forward + backward pass on each GPU (2× forward FLOPs)
        flops_per_step = 3.0 * num_params * batch_size
        compute_ms = flops_per_step / (312e12 / 1e3)  # 312 TFLOPs
        # Gradient AllReduce
        allreduce_ms = self.all_reduce_latency_us(num_params) / 1_000
        total_ms = compute_ms + allreduce_ms
        await asyncio.sleep(0)   # yield to event loop; actual latency is tiny
        return total_ms

    def summary(self) -> dict:
        return {
            "num_gpus":        self.num_gpus,
            "bandwidth_gbps":  self.bandwidth_gbps,
            "base_latency_us": self.base_latency_us,
            "cpu_bypass":      self.cpu_bypass,
        }


# ─── Online Observation ───────────────────────────────────────────────────────

class Observation(NamedTuple):
    features:         LOBFeatures
    realized_return:  float      # actual Δprice / mid, N ticks later
    timestamp_ns:     int


# ─── Online LOB Learner ───────────────────────────────────────────────────────

class OnlineLOBLearner:
    """
    Streams market observations → incrementally retrains LOBImbalanceModel.

    Architecture:
      – Ring buffer: last `buffer_size` (features, realized_return) pairs
      – Incremental SGD: small LR updates to polynomial coefficients
      – Hot-swap: new coefficients atomically replace old when val improves
    """

    def __init__(
        self,
        model:              LOBImbalanceModel,
        buffer_size:        int   = 1_000,
        retrain_threshold:  int   = 500,       # new obs before retrain
        retrain_interval_s: float = 900.0,     # 15 min periodic trigger
        learning_rate:      float = 1e-4,
        validation_holdout: float = 0.20,      # 20 % of buffer
    ) -> None:
        self.model              = model
        self._buffer_size       = buffer_size
        self._retrain_threshold = retrain_threshold
        self._retrain_interval  = retrain_interval_s
        self._lr                = learning_rate
        self._val_frac          = validation_holdout

        self._buffer: collections.deque[Observation] = collections.deque(
            maxlen=buffer_size
        )
        self._since_retrain   = 0
        self._last_retrain_ns = time.perf_counter_ns()
        self._error_history: collections.deque[float] = collections.deque(maxlen=200)
        self._retrain_count   = 0
        self._swap_count      = 0

    async def observe(self, book: OrderBook, realized_return: float) -> None:
        """Record one market observation; trigger retraining if needed."""
        features = extract_features(book)
        obs = Observation(
            features=features,
            realized_return=realized_return,
            timestamp_ns=time.perf_counter_ns(),
        )
        self._buffer.append(obs)
        self._since_retrain += 1

        # Track running prediction error
        try:
            predicted = self.model.predict_short_term_drift(book)
            self._error_history.append(abs(predicted - realized_return))
        except Exception:
            pass

        if self._should_retrain():
            await self._retrain()

    def _should_retrain(self) -> bool:
        if len(self._buffer) < 20:
            return False
        if self._since_retrain >= self._retrain_threshold:
            return True
        elapsed = (time.perf_counter_ns() - self._last_retrain_ns) / 1e9
        if elapsed >= self._retrain_interval:
            return True
        # Error spike: recent 10-obs mean > baseline + 3σ
        if len(self._error_history) >= 30:
            errs = list(self._error_history)
            mu   = sum(errs) / len(errs)
            std  = math.sqrt(sum((e - mu) ** 2 for e in errs) / len(errs))
            recent = sum(errs[-10:]) / 10
            if std > 0 and (recent - mu) > 3 * std:
                return True
        return False

    async def _retrain(self) -> bool:
        """
        Incremental gradient descent on buffered observations.
        Hot-swaps coefficients if validation error improves.
        Returns True if swap happened.
        """
        self._retrain_count += 1
        self._since_retrain  = 0
        self._last_retrain_ns = time.perf_counter_ns()

        obs = list(self._buffer)
        if len(obs) < 20:
            return False

        n_val   = max(1, int(len(obs) * self._val_frac))
        val_obs = obs[:n_val]
        trn_obs = obs[n_val:]

        new_coef = self._incremental_update(trn_obs)
        if new_coef is None:
            return False

        old_err = self._val_error(self.model.coefficients, val_obs)
        new_err = self._val_error(new_coef, val_obs)

        if new_err < old_err:
            self.model.coefficients = new_coef
            self._swap_count += 1
            logger.info(
                "[OnlineLearner] Hot-swap #%d  val %.4f → %.4f",
                self._swap_count, old_err, new_err,
            )
            return True

        logger.debug(
            "[OnlineLearner] Retrain #%d  no improvement (%.4f vs %.4f)",
            self._retrain_count, new_err, old_err,
        )
        return False

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _feature_vec(self, features: LOBFeatures) -> np.ndarray:
        raw = features.to_array()
        # Normalise with model's stored mean/std (matches LOBImbalanceModel)
        norm = (raw - self.model._mean) / (self.model._std + 1e-8)
        # Same poly expansion as LOBImbalanceModel._poly2_features:
        # [1, x1..x9, x1²..x9²] = 19 features
        return np.concatenate([[1.0], norm, norm ** 2])

    def _incremental_update(
        self, train_obs: list[Observation]
    ) -> np.ndarray | None:
        if not train_obs:
            return None
        coef = self.model.coefficients.copy()
        for obs in train_obs:
            x     = self._feature_vec(obs.features)
            if len(x) != len(coef):
                continue
            pred  = float(np.dot(x, coef))
            error = pred - obs.realized_return
            coef -= self._lr * error * x
        return coef

    def _val_error(
        self, coef: np.ndarray, val_obs: list[Observation]
    ) -> float:
        if not val_obs:
            return float("inf")
        errs = []
        for obs in val_obs:
            x = self._feature_vec(obs.features)
            if len(x) != len(coef):
                continue
            pred = float(np.clip(np.dot(x, coef), -1.0, 1.0))
            errs.append(abs(pred - obs.realized_return))
        return sum(errs) / len(errs) if errs else float("inf")

    def status(self) -> dict:
        return {
            "buffer_size":   len(self._buffer),
            "since_retrain": self._since_retrain,
            "retrain_count": self._retrain_count,
            "swap_count":    self._swap_count,
            "mean_error":    (
                sum(self._error_history) / len(self._error_history)
                if self._error_history else 0.0
            ),
        }


# ─── Retraining Scheduler ─────────────────────────────────────────────────────

class RetrainingScheduler:
    """
    Background asyncio task: fires periodic retraining and simulates RDMA
    gradient transfer overhead via InfiniBandCluster.
    """

    def __init__(
        self,
        learner:  OnlineLOBLearner,
        cluster:  InfiniBandCluster | None = None,
    ) -> None:
        self._learner = learner
        self._cluster = cluster or InfiniBandCluster()
        self._running = False
        self._task:    asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while self._running:
            await asyncio.sleep(self._learner._retrain_interval)
            if len(self._learner._buffer) >= 20:
                n_params = len(self._learner.model.coefficients)
                await self._cluster.simulate_training_step(n_params)
                await self._learner._retrain()

    def status(self) -> dict:
        return {
            "running": self._running,
            "learner": self._learner.status(),
            "cluster": self._cluster.summary(),
        }
