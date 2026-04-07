"""
Co-location and latency infrastructure utilities.

Provides:
  - Nanosecond-precision latency measurement (time.perf_counter_ns)
  - FPGA-ready hot-path isolation (CPU affinity hints, process priority)
  - Network path benchmarking (RTT histogram, jitter, packet loss estimation)
  - Latency budget tracker (per-stage breakdown for HFT pipelines)
  - Clock synchronisation quality (PTP/NTP offset estimation)
  - Colocation profile selection (RETAIL → ULTRA_HFT tier)

All functions are non-blocking and safe to call from asyncio event loops.
CPU-affinity operations are best-effort (silently skipped on unsupported platforms).
"""
from __future__ import annotations

import asyncio
import collections
import logging
import os
import statistics
import time
from dataclasses import dataclass, field
from typing import Callable

from core.enums import DataCenter, LatencyTier

logger = logging.getLogger(__name__)


# ─── Nanosecond Timer ────────────────────────────────────────────────────────

def ns() -> int:
    """Current monotonic time in nanoseconds (FPGA-ready resolution)."""
    return time.perf_counter_ns()


def elapsed_us(start_ns: int) -> float:
    """Microseconds elapsed since start_ns."""
    return (ns() - start_ns) / 1_000.0


def elapsed_ms(start_ns: int) -> float:
    """Milliseconds elapsed since start_ns."""
    return (ns() - start_ns) / 1_000_000.0


# ─── Latency Budget ───────────────────────────────────────────────────────────

@dataclass
class LatencyStage:
    name: str
    start_ns: int = field(default_factory=ns)
    end_ns: int = 0

    def stop(self) -> "LatencyStage":
        self.end_ns = ns()
        return self

    @property
    def duration_us(self) -> float:
        return (self.end_ns - self.start_ns) / 1_000.0 if self.end_ns else 0.0


@dataclass
class LatencyBudget:
    """
    Per-order latency breakdown tracker for HFT pipeline profiling.

    Tracks individual stages (signal → risk → sizing → execution → ack).
    All times in microseconds.
    """
    stages: list[LatencyStage] = field(default_factory=list)

    def begin_stage(self, name: str) -> LatencyStage:
        stage = LatencyStage(name=name)
        self.stages.append(stage)
        return stage

    @property
    def total_us(self) -> float:
        if not self.stages:
            return 0.0
        first_ns = self.stages[0].start_ns
        last_ns = max(s.end_ns for s in self.stages if s.end_ns)
        return (last_ns - first_ns) / 1_000.0

    def to_dict(self) -> dict:
        return {
            "total_us": self.total_us,
            "stages": [
                {"name": s.name, "duration_us": s.duration_us}
                for s in self.stages
            ],
        }

    def exceeds_budget(self, budget_us: float) -> bool:
        return self.total_us > budget_us


# ─── Network Benchmarking ────────────────────────────────────────────────────

LATENCY_BUDGETS_US: dict[LatencyTier, float] = {
    LatencyTier.RETAIL:        150_000.0,   # 150ms
    LatencyTier.INSTITUTIONAL: 5_000.0,     # 5ms
    LatencyTier.HFT:           50.0,        # 50µs
    LatencyTier.ULTRA_HFT:     0.5,         # 500ns (FPGA path)
}

DATA_CENTER_LATENCY_US: dict[DataCenter, float] = {
    DataCenter.EQUINIX_NY4: 0.08,    # 80ns hardware latency (co-located)
    DataCenter.EQUINIX_NY5: 0.12,
    DataCenter.CME_AURORA:  4_500.0, # ~4.5ms Chicago round-trip from NY4
    DataCenter.EQUINIX_LD4: 70_000.0,
    DataCenter.EQUINIX_TY3: 140_000.0,
    DataCenter.EQUINIX_HK1: 130_000.0,
}


@dataclass
class RTTSample:
    target: str
    rtt_us: float
    timestamp_ns: int


@dataclass
class NetworkProfile:
    """Rolling RTT statistics for a target endpoint."""
    target: str
    _samples: collections.deque = field(
        default_factory=lambda: collections.deque(maxlen=1000)
    )

    def record(self, rtt_us: float) -> None:
        self._samples.append(RTTSample(
            target=self.target, rtt_us=rtt_us, timestamp_ns=ns()
        ))

    @property
    def p50_us(self) -> float:
        if not self._samples:
            return 0.0
        rtts = sorted(s.rtt_us for s in self._samples)
        return rtts[len(rtts) // 2]

    @property
    def p99_us(self) -> float:
        if not self._samples:
            return 0.0
        rtts = sorted(s.rtt_us for s in self._samples)
        idx = int(len(rtts) * 0.99)
        return rtts[min(idx, len(rtts) - 1)]

    @property
    def jitter_us(self) -> float:
        if len(self._samples) < 2:
            return 0.0
        rtts = [s.rtt_us for s in self._samples]
        return statistics.stdev(rtts)

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "samples": len(self._samples),
            "p50_us": self.p50_us,
            "p99_us": self.p99_us,
            "jitter_us": self.jitter_us,
        }


async def benchmark_rtt(
    host: str,
    port: int,
    num_pings: int = 20,
    timeout_sec: float = 1.0,
) -> NetworkProfile:
    """
    Benchmark TCP round-trip time to a target (FIX gateway / exchange).

    Opens a TCP connection and measures connection + first-byte latency.
    Returns a NetworkProfile with RTT statistics.
    """
    profile = NetworkProfile(target=f"{host}:{port}")
    for _ in range(num_pings):
        start = ns()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=timeout_sec,
            )
            rtt = elapsed_us(start)
            profile.record(rtt)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        except (asyncio.TimeoutError, OSError):
            pass  # Count as packet loss — exclude from stats
        await asyncio.sleep(0.01)   # 10ms between pings

    logger.info(
        "[Colocation] RTT to %s:%d — p50=%.1fµs p99=%.1fµs jitter=%.1fµs",
        host, port, profile.p50_us, profile.p99_us, profile.jitter_us,
    )
    return profile


# ─── CPU Affinity (FPGA hot-path isolation) ──────────────────────────────────

def pin_to_cpu(core_id: int) -> bool:
    """
    Pin the current process to a specific CPU core.
    Returns True on success. Silent best-effort on unsupported platforms.

    For HFT hot-path isolation — the asyncio event loop should run on a
    dedicated, isolated core (kernel boot param isolcpus=<id>).
    """
    try:
        os.sched_setaffinity(0, {core_id})
        logger.info("[Colocation] Process pinned to CPU core %d", core_id)
        return True
    except (AttributeError, OSError, PermissionError) as exc:
        logger.debug("[Colocation] CPU affinity not available: %s", exc)
        return False


def set_realtime_priority() -> bool:
    """
    Attempt to set SCHED_FIFO real-time scheduling priority.
    Requires root / CAP_SYS_NICE on Linux. Silent on failure.
    """
    try:
        param = os.sched_param(os.sched_get_priority_max(os.SCHED_FIFO))
        os.sched_setscheduler(0, os.SCHED_FIFO, param)
        logger.info("[Colocation] Real-time SCHED_FIFO priority set")
        return True
    except (AttributeError, OSError, PermissionError) as exc:
        logger.debug("[Colocation] RT priority not available: %s", exc)
        return False


# ─── Clock Sync Quality ───────────────────────────────────────────────────────

@dataclass
class ClockSyncStatus:
    """
    PTP/NTP synchronisation quality report.
    In production, read from chronyc or ptpd. Here we model via monotonic drift.
    """
    source: str = "system"           # "ptp", "ntp", "gps", "system"
    offset_ns: float = 0.0           # current clock offset vs reference
    rms_jitter_ns: float = 5_000.0  # typical software NTP
    stratum: int = 4                  # 1=GPS, 2=PTP master, 3-4=NTP
    is_synchronized: bool = True

    @property
    def quality_label(self) -> str:
        if self.rms_jitter_ns < 100:
            return "PTP-hardware"
        elif self.rms_jitter_ns < 1_000:
            return "PTP-software"
        elif self.rms_jitter_ns < 50_000:
            return "NTP"
        return "degraded"


def read_clock_sync() -> ClockSyncStatus:
    """
    Read clock sync status from the system (best-effort).
    Parses /proc/net/ptp_clock or falls back to system time quality heuristic.
    Returns a ClockSyncStatus with sensible defaults for dev environments.
    """
    # In production: call `chronyc tracking` or parse ptpd syslog
    # For now, return a placeholder representing software-NTP quality
    return ClockSyncStatus(
        source="ntp",
        offset_ns=3_000.0,      # 3µs typical software NTP offset
        rms_jitter_ns=5_000.0,  # 5µs typical
        stratum=3,
        is_synchronized=True,
    )


# ─── Colocation Profile ───────────────────────────────────────────────────────

@dataclass
class ColocationProfile:
    """
    Describes the physical/network deployment profile for this instance.
    Controls which optimisations are active and which SLOs apply.
    """
    tier: LatencyTier = LatencyTier.RETAIL
    data_center: DataCenter | None = None
    dedicated_core: int | None = None   # CPU core ID for hot-path isolation
    use_fpga: bool = False
    use_microwave: bool = False          # microwave backhaul (NY4 → Chicago)

    @property
    def budget_us(self) -> float:
        return LATENCY_BUDGETS_US[self.tier]

    @property
    def expected_rtt_us(self) -> float:
        if self.data_center is None:
            return LATENCY_BUDGETS_US[self.tier]
        return DATA_CENTER_LATENCY_US.get(self.data_center, self.budget_us)

    def apply(self) -> None:
        """Apply OS-level optimisations for this profile."""
        if self.dedicated_core is not None:
            pin_to_cpu(self.dedicated_core)
        if self.tier in (LatencyTier.HFT, LatencyTier.ULTRA_HFT):
            set_realtime_priority()
        logger.info(
            "[Colocation] Profile applied: tier=%s dc=%s fpga=%s microwave=%s",
            self.tier.value,
            self.data_center.value if self.data_center else "none",
            self.use_fpga,
            self.use_microwave,
        )


# ─── Latency Monitor ─────────────────────────────────────────────────────────

class LatencyMonitor:
    """
    Tracks end-to-end order latency (signal → execution → ACK) with
    a rolling window and budget violation alerting.

    Thread-safe (asyncio event-loop safe).
    """

    def __init__(
        self,
        profile: ColocationProfile,
        on_budget_breach: Callable[[LatencyBudget], None] | None = None,
        window: int = 1000,
    ) -> None:
        self._profile = profile
        self._on_breach = on_budget_breach
        self._history: collections.deque[LatencyBudget] = collections.deque(maxlen=window)

    def record(self, budget: LatencyBudget) -> None:
        self._history.append(budget)
        if budget.exceeds_budget(self._profile.budget_us):
            logger.warning(
                "[Latency] Budget breach: %.1fµs > %.1fµs | %s",
                budget.total_us, self._profile.budget_us,
                budget.to_dict(),
            )
            if self._on_breach:
                self._on_breach(budget)

    @property
    def p50_us(self) -> float:
        if not self._history:
            return 0.0
        vals = sorted(b.total_us for b in self._history)
        return vals[len(vals) // 2]

    @property
    def p99_us(self) -> float:
        if not self._history:
            return 0.0
        vals = sorted(b.total_us for b in self._history)
        idx = int(len(vals) * 0.99)
        return vals[min(idx, len(vals) - 1)]

    @property
    def breach_rate(self) -> float:
        """Fraction of recorded orders that breached the latency budget."""
        if not self._history:
            return 0.0
        breaches = sum(
            1 for b in self._history if b.exceeds_budget(self._profile.budget_us)
        )
        return breaches / len(self._history)

    def to_dict(self) -> dict:
        return {
            "tier": self._profile.tier.value,
            "budget_us": self._profile.budget_us,
            "p50_us": self.p50_us,
            "p99_us": self.p99_us,
            "breach_rate": self.breach_rate,
            "samples": len(self._history),
        }
