"""
IEEE 1588 Precision Time Protocol (PTP) simulation.

Models a GPS-disciplined Grandmaster Clock syncing 5 server nodes
to sub-nanosecond accuracy — required by MiFID II for trade timestamp
audit trails across distributed exchange co-location infrastructure.

Architecture:
  GPS Grandmaster → Boundary Clock (switch) → 5 Ordinary Clocks (servers)

Real-world numbers:
  GPS disciplined oscillator accuracy: ±20 ns
  PTP hardware timestamp accuracy:     ±1 ns (NIC-level)
  Software timestamp accuracy:         ±1 µs (OS jitter)
  Clock drift without sync:            ~1 µs/s (quartz crystal)
  After PTP sync:                      <100 ns between nodes
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import time
import math
import random
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Core constants
# ---------------------------------------------------------------------------

GPS_ACCURACY_NS = 20                  # GPS grandmaster accuracy ±20 ns
NIC_TIMESTAMP_ACCURACY_NS = 1         # hardware timestamp ±1 ns
SOFTWARE_TIMESTAMP_JITTER_NS = 1000   # OS software jitter ±1 µs
QUARTZ_DRIFT_NS_PER_S = 50            # 50 ns/s drift without sync (modern TCXO)
PTP_SYNC_INTERVAL_MS = 125            # PTP sync message interval (8 Hz = 125 ms)
PTP_CORRECTION_GAIN = 0.125           # PI controller proportional gain
NUM_NODES = 5


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class ClockRole(str, Enum):
    GRANDMASTER = "grandmaster"   # GPS-disciplined primary
    BOUNDARY = "boundary"         # Layer-2 switch with PTP support
    ORDINARY = "ordinary"         # Server NIC hardware clock


class ClockQuality(str, Enum):
    GPS_LOCKED = "gps_locked"          # <20 ns accuracy
    PTP_SYNCED = "ptp_synced"          # <100 ns accuracy
    FREE_RUNNING = "free_running"      # >1 µs accuracy (drifting)
    FAILED = "failed"                  # clock source lost


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PTPTimestamp:
    """96-bit PTP timestamp: seconds + nanoseconds."""
    seconds: int
    nanoseconds: int   # 0–999_999_999

    def to_ns(self) -> int:
        return self.seconds * 1_000_000_000 + self.nanoseconds

    @classmethod
    def from_ns(cls, ns: int) -> "PTPTimestamp":
        return cls(seconds=ns // 1_000_000_000, nanoseconds=ns % 1_000_000_000)


@dataclass
class PTPMessage:
    """PTP Sync/Delay_Req/Delay_Resp message."""
    msg_type: str           # "sync", "follow_up", "delay_req", "delay_resp"
    sequence_id: int
    source_node_id: int
    t1_ns: int = 0          # sync send time (grandmaster)
    t2_ns: int = 0          # sync recv time (slave)
    t3_ns: int = 0          # delay_req send time (slave)
    t4_ns: int = 0          # delay_req recv time (master)
    correction_ns: int = 0  # residence time in boundary clock


# ---------------------------------------------------------------------------
# Clock node
# ---------------------------------------------------------------------------

@dataclass
class ClockNode:
    """
    A single server or switch with a hardware clock.

    Maintains local offset from true time and applies PTP corrections.
    """
    node_id: int
    role: ClockRole
    quality: ClockQuality = ClockQuality.FREE_RUNNING

    # Clock state
    offset_ns: float = 0.0       # current offset from true time (ns)
    drift_ns_per_s: float = 0.0  # current drift rate
    last_sync_ns: int = 0        # wall time of last PTP sync

    # Statistics
    sync_count: int = 0
    max_offset_seen_ns: float = 0.0
    mean_path_delay_ns: float = 0.0

    def __post_init__(self) -> None:
        # Grandmaster has GPS accuracy; others start with quartz drift
        if self.role == ClockRole.GRANDMASTER:
            self.quality = ClockQuality.GPS_LOCKED
            self.offset_ns = random.uniform(-GPS_ACCURACY_NS, GPS_ACCURACY_NS)
            self.drift_ns_per_s = 0.0  # GPS-disciplined
        else:
            self.drift_ns_per_s = random.uniform(
                -QUARTZ_DRIFT_NS_PER_S, QUARTZ_DRIFT_NS_PER_S
            )

    def local_time_ns(self, true_time_ns: int) -> int:
        """Returns this node's local clock reading (true + offset)."""
        return true_time_ns + int(self.offset_ns)

    def apply_ptp_correction(self, measured_offset_ns: float) -> None:
        """
        PI controller: apply fractional correction to avoid stepping the clock.
        offset_ns += PTP_CORRECTION_GAIN * measured_offset_ns
        """
        self.offset_ns += PTP_CORRECTION_GAIN * measured_offset_ns
        self.sync_count += 1
        if abs(self.offset_ns) > self.max_offset_seen_ns:
            self.max_offset_seen_ns = abs(self.offset_ns)
        if abs(self.offset_ns) < 100:
            self.quality = ClockQuality.PTP_SYNCED
        else:
            self.quality = ClockQuality.FREE_RUNNING

    def advance_drift(self, elapsed_s: float) -> None:
        """Simulate quartz crystal drift between sync events."""
        jitter = random.gauss(0, NIC_TIMESTAMP_ACCURACY_NS)
        self.offset_ns += self.drift_ns_per_s * elapsed_s + jitter

    def fail(self) -> None:
        """Mark this clock as failed (hardware fault / GPS signal loss)."""
        self.quality = ClockQuality.FAILED
        self.drift_ns_per_s = QUARTZ_DRIFT_NS_PER_S * 10  # free-running fast

    def summary(self) -> dict:
        return {
            "node_id": self.node_id,
            "role": self.role.value,
            "quality": self.quality.value,
            "offset_ns": round(self.offset_ns, 2),
            "drift_ns_per_s": round(self.drift_ns_per_s, 2),
            "sync_count": self.sync_count,
            "max_offset_seen_ns": round(self.max_offset_seen_ns, 2),
            "mean_path_delay_ns": round(self.mean_path_delay_ns, 2),
        }


# ---------------------------------------------------------------------------
# PTP sync engine
# ---------------------------------------------------------------------------

class PTPSyncEngine:
    """
    Simulates the full PTP four-message exchange:
      1. Master sends Sync (T1)
      2. Slave timestamps receipt (T2)
      3. Slave sends Delay_Req (T3)
      4. Master timestamps receipt (T4)

      offset = ((T2 - T1) - (T4 - T3)) / 2
      path_delay = ((T2 - T1) + (T4 - T3)) / 2

    Network jitter is modeled as Gaussian noise on path delay.
    """

    def __init__(
        self,
        network_jitter_ns: float = 50.0,      # Gaussian σ for path delay variation
        boundary_residence_ns: float = 200.0,  # processing delay in boundary clock
    ) -> None:
        self._jitter_ns = network_jitter_ns
        self._boundary_ns = boundary_residence_ns
        self._seq = 0

    def sync_round(
        self,
        master: ClockNode,
        slave: ClockNode,
        true_time_ns: int,
        via_boundary: bool = False,
    ) -> PTPMessage:
        """
        Execute one PTP sync round. Updates slave.offset_ns.
        Returns the PTPMessage with all 4 timestamps.
        """
        # T1: master sends sync (master's local time)
        t1 = master.local_time_ns(true_time_ns)

        # Network delay (one-way path delay with jitter)
        path_delay_ns = abs(random.gauss(500, self._jitter_ns))  # ~500 ns one-way
        boundary_ns = self._boundary_ns if via_boundary else 0.0

        # T2: slave receives sync
        t2 = slave.local_time_ns(true_time_ns) + int(path_delay_ns + boundary_ns)

        # T3: slave sends delay_req
        t3 = slave.local_time_ns(true_time_ns) + int(path_delay_ns + boundary_ns) + 100

        # T4: master receives delay_req
        t4 = master.local_time_ns(true_time_ns) + int(2 * path_delay_ns + boundary_ns) + 100

        # PTP offset calculation (correction field removes boundary residence bias)
        measured_offset = ((t2 - t1) - (t4 - t3)) / 2 - boundary_ns / 2
        measured_delay = ((t2 - t1) + (t4 - t3)) / 2
        slave.mean_path_delay_ns = measured_delay
        slave.apply_ptp_correction(-measured_offset)  # subtract to correct

        msg = PTPMessage(
            msg_type="sync",
            sequence_id=self._seq,
            source_node_id=master.node_id,
            t1_ns=t1,
            t2_ns=t2,
            t3_ns=t3,
            t4_ns=t4,
            correction_ns=int(boundary_ns),
        )
        self._seq += 1
        return msg


# ---------------------------------------------------------------------------
# Grandmaster with failover
# ---------------------------------------------------------------------------

class GrandmasterClock:
    """
    Primary GPS-disciplined grandmaster with automatic failover to backup.

    Failover: if primary fails, secondary (boundary clock with GPS receiver)
    takes over within 2 PTP sync intervals = 250 ms.
    """

    def __init__(self) -> None:
        self.primary = ClockNode(node_id=0, role=ClockRole.GRANDMASTER)
        self.secondary = ClockNode(node_id=1, role=ClockRole.GRANDMASTER)  # hot-standby
        self.secondary.offset_ns = random.uniform(-GPS_ACCURACY_NS, GPS_ACCURACY_NS)
        self._active = self.primary
        self._failover_count = 0
        self._failover_ns: int = 0  # wall time of last failover

    @property
    def active(self) -> ClockNode:
        return self._active

    @property
    def failover_count(self) -> int:
        return self._failover_count

    def trigger_failover(self, true_time_ns: int) -> ClockNode:
        """
        Simulate GPS signal loss / hardware fault on primary.
        Secondary becomes active grandmaster.
        Returns the new active node.
        """
        self.primary.fail()
        self._active = self.secondary
        self._failover_count += 1
        self._failover_ns = true_time_ns
        return self._active

    def restore_primary(self, true_time_ns: int) -> ClockNode:
        """Re-qualify primary after GPS lock re-established."""
        self.primary.quality = ClockQuality.GPS_LOCKED
        self.primary.drift_ns_per_s = 0.0
        self._active = self.primary
        return self._active

    def summary(self) -> dict:
        return {
            "active_node_id": self._active.node_id,
            "active_quality": self._active.quality.value,
            "failover_count": self._failover_count,
            "primary_ok": self.primary.quality != ClockQuality.FAILED,
            "secondary_ok": self.secondary.quality != ClockQuality.FAILED,
        }


# ---------------------------------------------------------------------------
# PTP cluster
# ---------------------------------------------------------------------------

class PTPCluster:
    """
    5-node PTP cluster: 1 Grandmaster + 1 Boundary Clock + 4 Ordinary Clocks.

    Topology:
      GM(0) → Boundary(1) → Ordinary(2,3,4,5)

    Each sync cycle:
      1. GM syncs Boundary (via direct link)
      2. Boundary syncs Ordinary nodes (via switch)
      3. All nodes apply drift between sync events
    """

    def __init__(self, network_jitter_ns: float = 50.0) -> None:
        self.grandmaster_mgr = GrandmasterClock()
        self.boundary = ClockNode(node_id=1, role=ClockRole.BOUNDARY)
        self.nodes = [
            ClockNode(node_id=i, role=ClockRole.ORDINARY)
            for i in range(2, 2 + NUM_NODES)
        ]
        self._engine = PTPSyncEngine(network_jitter_ns=network_jitter_ns)
        self._sync_cycles = 0

    @property
    def all_nodes(self) -> list:
        return [self.grandmaster_mgr.active, self.boundary] + self.nodes

    def sync_cycle(self, true_time_ns: int, elapsed_s: float = 0.125) -> dict:
        """
        Run one full PTP sync cycle (called every PTP_SYNC_INTERVAL_MS ms).
        1. Drift all slave nodes
        2. GM → Boundary sync
        3. Boundary → each Ordinary sync
        Returns stats dict.
        """
        gm = self.grandmaster_mgr.active

        # 1. Apply drift since last sync
        for node in [self.boundary] + self.nodes:
            node.advance_drift(elapsed_s)

        # 2. Sync Boundary from Grandmaster
        self._engine.sync_round(gm, self.boundary, true_time_ns, via_boundary=False)

        # 3. Sync each Ordinary node through Boundary
        for node in self.nodes:
            self._engine.sync_round(self.boundary, node, true_time_ns, via_boundary=True)

        self._sync_cycles += 1

        offsets = [abs(n.offset_ns) for n in self.nodes]
        return {
            "sync_cycle": self._sync_cycles,
            "max_offset_ns": max(offsets),
            "mean_offset_ns": sum(offsets) / len(offsets),
            "all_synced": all(n.quality == ClockQuality.PTP_SYNCED for n in self.nodes),
            "grandmaster": gm.node_id,
        }

    def run_sync_cycles(self, num_cycles: int, true_time_ns: int | None = None) -> list:
        """Run N sync cycles, advancing true_time by PTP_SYNC_INTERVAL_MS each."""
        if true_time_ns is None:
            true_time_ns = int(time.time() * 1e9)
        results = []
        interval_ns = int(PTP_SYNC_INTERVAL_MS * 1e6)
        for _ in range(num_cycles):
            stats = self.sync_cycle(true_time_ns, elapsed_s=PTP_SYNC_INTERVAL_MS / 1000)
            results.append(stats)
            true_time_ns += interval_ns
        return results

    def node_offsets_ns(self) -> list:
        """Return current offset_ns for all ordinary nodes."""
        return [n.offset_ns for n in self.nodes]

    def max_inter_node_error_ns(self) -> float:
        """Maximum offset difference between any two ordinary nodes."""
        offsets = self.node_offsets_ns()
        return max(offsets) - min(offsets)

    def summary(self) -> dict:
        offsets = self.node_offsets_ns()
        return {
            "sync_cycles": self._sync_cycles,
            "num_nodes": len(self.nodes),
            "grandmaster": self.grandmaster_mgr.summary(),
            "boundary_offset_ns": round(self.boundary.offset_ns, 2),
            "max_node_offset_ns": round(max(abs(o) for o in offsets), 2),
            "max_inter_node_error_ns": round(self.max_inter_node_error_ns(), 2),
            "all_synced": all(n.quality == ClockQuality.PTP_SYNCED for n in self.nodes),
        }
