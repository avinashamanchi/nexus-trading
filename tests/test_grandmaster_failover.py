"""
Tests for IEEE 1588 PTP Grandmaster failover behaviour.

Covers GrandmasterClock state transitions and PTPCluster resilience when
the primary GPS-disciplined grandmaster suffers GPS signal loss or hardware
fault, triggering automatic failover to the hot-standby secondary.
"""

import pytest
import time

from infrastructure.ptp_clock_sync import (
    ClockNode,
    ClockRole,
    ClockQuality,
    GrandmasterClock,
    PTPCluster,
    GPS_ACCURACY_NS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _true_time() -> int:
    return int(time.time() * 1e9)


def _gm() -> GrandmasterClock:
    return GrandmasterClock()


# ---------------------------------------------------------------------------
# TestGrandmasterClock
# ---------------------------------------------------------------------------

class TestGrandmasterClock:
    def test_starts_with_primary_active(self):
        gm = _gm()
        assert gm.active is gm.primary

    def test_primary_starts_gps_locked(self):
        gm = _gm()
        assert gm.primary.quality == ClockQuality.GPS_LOCKED

    def test_secondary_starts_gps_locked(self):
        gm = _gm()
        assert gm.secondary.quality == ClockQuality.GPS_LOCKED

    def test_failover_count_starts_zero(self):
        gm = _gm()
        assert gm.failover_count == 0

    def test_failover_marks_primary_failed(self):
        gm = _gm()
        gm.trigger_failover(_true_time())
        assert gm.primary.quality == ClockQuality.FAILED

    def test_failover_activates_secondary(self):
        gm = _gm()
        new_active = gm.trigger_failover(_true_time())
        assert gm.active is gm.secondary
        assert new_active is gm.secondary

    def test_failover_count_increments(self):
        gm = _gm()
        gm.trigger_failover(_true_time())
        assert gm.failover_count == 1

    def test_failover_count_increments_multiple_times(self):
        gm = _gm()
        # Restore and fail again to simulate repeated events
        t = _true_time()
        gm.trigger_failover(t)
        gm.restore_primary(t)
        gm.trigger_failover(t)
        assert gm.failover_count == 2

    def test_restore_primary_returns_primary(self):
        gm = _gm()
        t = _true_time()
        gm.trigger_failover(t)
        restored = gm.restore_primary(t)
        assert restored is gm.primary
        assert gm.active is gm.primary

    def test_restore_primary_gps_locked_again(self):
        gm = _gm()
        t = _true_time()
        gm.trigger_failover(t)
        gm.restore_primary(t)
        assert gm.primary.quality == ClockQuality.GPS_LOCKED

    def test_restore_primary_resets_drift(self):
        gm = _gm()
        t = _true_time()
        gm.trigger_failover(t)
        # Primary drift is elevated after failure
        assert abs(gm.primary.drift_ns_per_s) > 0
        gm.restore_primary(t)
        assert gm.primary.drift_ns_per_s == 0.0

    def test_summary_has_required_keys(self):
        gm = _gm()
        s = gm.summary()
        for key in ("active_node_id", "active_quality", "failover_count",
                    "primary_ok", "secondary_ok"):
            assert key in s, f"Missing key: {key}"

    def test_summary_primary_ok_false_after_failover(self):
        gm = _gm()
        gm.trigger_failover(_true_time())
        s = gm.summary()
        assert s["primary_ok"] is False

    def test_summary_secondary_ok_true_after_failover(self):
        gm = _gm()
        gm.trigger_failover(_true_time())
        s = gm.summary()
        assert s["secondary_ok"] is True

    def test_summary_active_node_id_changes_after_failover(self):
        gm = _gm()
        primary_id = gm.summary()["active_node_id"]
        gm.trigger_failover(_true_time())
        assert gm.summary()["active_node_id"] != primary_id

    def test_secondary_offset_within_gps_accuracy(self):
        for _ in range(30):
            gm = _gm()
            assert abs(gm.secondary.offset_ns) <= GPS_ACCURACY_NS


# ---------------------------------------------------------------------------
# TestFailoverIntegration
# ---------------------------------------------------------------------------

class TestFailoverIntegration:
    def test_cluster_continues_syncing_after_failover(self):
        cluster = PTPCluster(network_jitter_ns=10.0)
        t_ns = _true_time()
        # Pre-sync cluster
        cluster.run_sync_cycles(10, t_ns)
        # Trigger grandmaster failover
        cluster.grandmaster_mgr.trigger_failover(t_ns)
        # Continue syncing with secondary as active grandmaster
        results = cluster.run_sync_cycles(5, t_ns + int(10 * 125e6))
        assert len(results) == 5
        assert all("max_offset_ns" in r for r in results)

    def test_nodes_remain_synced_after_failover(self):
        """After failover, running 10 more cycles should keep nodes synced."""
        cluster = PTPCluster(network_jitter_ns=10.0)
        t_ns = _true_time()
        # Pre-sync to convergence
        cluster.run_sync_cycles(20, t_ns)
        assert all(
            n.quality == ClockQuality.PTP_SYNCED for n in cluster.nodes
        ), "Nodes should be synced before failover"
        # Trigger failover
        cluster.grandmaster_mgr.trigger_failover(t_ns)
        # Run more sync cycles
        cluster.run_sync_cycles(10, t_ns + int(20 * 125e6))
        # Nodes should still be synced to sub-500 ns
        for offset in cluster.node_offsets_ns():
            assert abs(offset) < 500, (
                f"Node offset {offset:.1f} ns exceeds 500 ns after failover recovery"
            )

    def test_failover_does_not_crash_sync_cycle(self):
        cluster = PTPCluster(network_jitter_ns=10.0)
        t_ns = _true_time()
        cluster.grandmaster_mgr.trigger_failover(t_ns)
        # This must not raise
        stats = cluster.sync_cycle(t_ns)
        assert "max_offset_ns" in stats

    def test_active_grandmaster_changes_after_failover(self):
        cluster = PTPCluster(network_jitter_ns=10.0)
        t_ns = _true_time()
        primary_id = cluster.grandmaster_mgr.active.node_id
        cluster.grandmaster_mgr.trigger_failover(t_ns)
        assert cluster.grandmaster_mgr.active.node_id != primary_id

    def test_summary_reports_failover_count(self):
        cluster = PTPCluster(network_jitter_ns=10.0)
        t_ns = _true_time()
        cluster.grandmaster_mgr.trigger_failover(t_ns)
        s = cluster.summary()
        assert s["grandmaster"]["failover_count"] == 1

    def test_restore_after_failover_uses_primary_again(self):
        cluster = PTPCluster(network_jitter_ns=10.0)
        t_ns = _true_time()
        primary_id = cluster.grandmaster_mgr.primary.node_id
        cluster.grandmaster_mgr.trigger_failover(t_ns)
        cluster.grandmaster_mgr.restore_primary(t_ns)
        assert cluster.grandmaster_mgr.active.node_id == primary_id
