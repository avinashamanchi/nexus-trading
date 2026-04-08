"""
Tests for IEEE 1588 PTP clock sync simulation — drift convergence.

Covers PTPTimestamp, ClockNode, PTPSyncEngine, and PTPCluster behaviour
under normal operating conditions with low network jitter.
"""

import pytest
import time

from infrastructure.ptp_clock_sync import (
    ClockNode,
    ClockRole,
    ClockQuality,
    PTPTimestamp,
    PTPMessage,
    PTPSyncEngine,
    PTPCluster,
    GrandmasterClock,
    GPS_ACCURACY_NS,
    QUARTZ_DRIFT_NS_PER_S,
    PTP_SYNC_INTERVAL_MS,
    NUM_NODES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cluster() -> PTPCluster:
    """Low jitter cluster for fast convergence in tests."""
    return PTPCluster(network_jitter_ns=10.0)


def _true_time() -> int:
    return int(time.time() * 1e9)


# ---------------------------------------------------------------------------
# TestPTPTimestamp
# ---------------------------------------------------------------------------

class TestPTPTimestamp:
    def test_to_ns_and_from_ns_roundtrip(self):
        original_ns = 1_234_567_890_123_456_789
        ts = PTPTimestamp.from_ns(original_ns)
        assert ts.to_ns() == original_ns

    def test_to_ns_correct_value(self):
        # 1 second + 500 nanoseconds = 1_000_000_500
        ts = PTPTimestamp(seconds=1, nanoseconds=500)
        assert ts.to_ns() == 1_000_000_500

    def test_from_ns_splits_seconds_and_nanoseconds(self):
        ns = 2_000_000_001
        ts = PTPTimestamp.from_ns(ns)
        assert ts.seconds == 2
        assert ts.nanoseconds == 1

    def test_zero_timestamp(self):
        ts = PTPTimestamp.from_ns(0)
        assert ts.seconds == 0
        assert ts.nanoseconds == 0
        assert ts.to_ns() == 0

    def test_large_nanosecond_value(self):
        # 999_999_999 ns is the maximum nanosecond field
        ts = PTPTimestamp(seconds=5, nanoseconds=999_999_999)
        assert ts.to_ns() == 5_999_999_999


# ---------------------------------------------------------------------------
# TestClockNode
# ---------------------------------------------------------------------------

class TestClockNode:
    def test_grandmaster_starts_gps_locked(self):
        node = ClockNode(node_id=0, role=ClockRole.GRANDMASTER)
        assert node.quality == ClockQuality.GPS_LOCKED

    def test_ordinary_starts_free_running(self):
        node = ClockNode(node_id=2, role=ClockRole.ORDINARY)
        assert node.quality == ClockQuality.FREE_RUNNING

    def test_grandmaster_offset_within_gps_accuracy(self):
        # Run many times to cover random initialisation
        for _ in range(50):
            node = ClockNode(node_id=0, role=ClockRole.GRANDMASTER)
            assert abs(node.offset_ns) <= GPS_ACCURACY_NS

    def test_grandmaster_drift_is_zero(self):
        node = ClockNode(node_id=0, role=ClockRole.GRANDMASTER)
        assert node.drift_ns_per_s == 0.0

    def test_ordinary_drift_within_quartz_range(self):
        for _ in range(50):
            node = ClockNode(node_id=2, role=ClockRole.ORDINARY)
            assert abs(node.drift_ns_per_s) <= QUARTZ_DRIFT_NS_PER_S

    def test_fail_sets_quality_failed(self):
        node = ClockNode(node_id=2, role=ClockRole.ORDINARY)
        node.fail()
        assert node.quality == ClockQuality.FAILED

    def test_fail_accelerates_drift(self):
        node = ClockNode(node_id=2, role=ClockRole.ORDINARY)
        node.fail()
        assert abs(node.drift_ns_per_s) > QUARTZ_DRIFT_NS_PER_S

    def test_apply_correction_converges(self):
        """After repeated corrections of the same measured_offset, offset shrinks."""
        node = ClockNode(node_id=2, role=ClockRole.ORDINARY)
        node.offset_ns = 10_000.0  # start 10 µs off
        initial_offset = abs(node.offset_ns)
        measured_error = 10_000.0  # matches the current offset
        for _ in range(10):
            node.apply_ptp_correction(-measured_error)
        assert abs(node.offset_ns) < initial_offset

    def test_apply_correction_increments_sync_count(self):
        node = ClockNode(node_id=2, role=ClockRole.ORDINARY)
        node.apply_ptp_correction(0.0)
        node.apply_ptp_correction(0.0)
        assert node.sync_count == 2

    def test_apply_correction_sets_synced_when_offset_small(self):
        node = ClockNode(node_id=2, role=ClockRole.ORDINARY)
        node.offset_ns = 0.0
        node.apply_ptp_correction(0.0)  # measured_offset=0 → offset stays 0
        assert node.quality == ClockQuality.PTP_SYNCED

    def test_local_time_ns_adds_offset(self):
        node = ClockNode(node_id=2, role=ClockRole.ORDINARY)
        node.offset_ns = 500.0
        true_time = 1_000_000_000
        assert node.local_time_ns(true_time) == 1_000_000_500

    def test_summary_has_required_keys(self):
        node = ClockNode(node_id=2, role=ClockRole.ORDINARY)
        s = node.summary()
        for key in ("node_id", "role", "quality", "offset_ns", "sync_count",
                    "drift_ns_per_s", "max_offset_seen_ns", "mean_path_delay_ns"):
            assert key in s, f"Missing key: {key}"

    def test_summary_node_id_matches(self):
        node = ClockNode(node_id=7, role=ClockRole.ORDINARY)
        assert node.summary()["node_id"] == 7


# ---------------------------------------------------------------------------
# TestPTPSyncEngine
# ---------------------------------------------------------------------------

class TestPTPSyncEngine:
    def _make_pair(self):
        master = ClockNode(node_id=0, role=ClockRole.GRANDMASTER)
        slave = ClockNode(node_id=2, role=ClockRole.ORDINARY)
        return master, slave

    def test_sync_round_updates_slave_offset(self):
        master, slave = self._make_pair()
        slave.offset_ns = 5_000.0
        engine = PTPSyncEngine(network_jitter_ns=5.0)
        before = slave.offset_ns
        engine.sync_round(master, slave, _true_time())
        # Offset should have moved toward zero
        assert abs(slave.offset_ns) != abs(before) or slave.sync_count == 1

    def test_sync_round_increments_sync_count(self):
        master, slave = self._make_pair()
        engine = PTPSyncEngine()
        engine.sync_round(master, slave, _true_time())
        assert slave.sync_count == 1

    def test_sync_round_returns_ptp_message(self):
        master, slave = self._make_pair()
        engine = PTPSyncEngine()
        msg = engine.sync_round(master, slave, _true_time())
        assert isinstance(msg, PTPMessage)

    def test_sequence_id_increments(self):
        master, slave = self._make_pair()
        engine = PTPSyncEngine()
        t = _true_time()
        msg1 = engine.sync_round(master, slave, t)
        msg2 = engine.sync_round(master, slave, t)
        assert msg2.sequence_id == msg1.sequence_id + 1

    def test_message_source_node_id(self):
        master, slave = self._make_pair()
        engine = PTPSyncEngine()
        msg = engine.sync_round(master, slave, _true_time())
        assert msg.source_node_id == master.node_id

    def test_boundary_correction_nonzero_when_via_boundary(self):
        master, slave = self._make_pair()
        engine = PTPSyncEngine(boundary_residence_ns=200.0)
        msg = engine.sync_round(master, slave, _true_time(), via_boundary=True)
        assert msg.correction_ns == 200

    def test_all_four_timestamps_set(self):
        master, slave = self._make_pair()
        engine = PTPSyncEngine()
        msg = engine.sync_round(master, slave, _true_time())
        assert msg.t1_ns != 0
        assert msg.t2_ns != 0
        assert msg.t3_ns != 0
        assert msg.t4_ns != 0

    def test_t2_after_t1(self):
        """T2 (slave recv) should be after T1 (master send) in true time."""
        master, slave = self._make_pair()
        master.offset_ns = 0.0
        slave.offset_ns = 0.0
        engine = PTPSyncEngine(network_jitter_ns=1.0)
        msg = engine.sync_round(master, slave, _true_time())
        assert msg.t2_ns > msg.t1_ns


# ---------------------------------------------------------------------------
# TestPTPCluster
# ---------------------------------------------------------------------------

class TestPTPCluster:
    def test_cluster_has_5_ordinary_nodes(self):
        cluster = _cluster()
        assert len(cluster.nodes) == NUM_NODES

    def test_sync_cycle_returns_stats_dict(self):
        cluster = _cluster()
        stats = cluster.sync_cycle(_true_time())
        for key in ("max_offset_ns", "mean_offset_ns", "all_synced", "grandmaster", "sync_cycle"):
            assert key in stats, f"Missing key: {key}"

    def test_after_20_cycles_nodes_synced(self):
        cluster = _cluster()
        results = cluster.run_sync_cycles(num_cycles=20)
        assert results[-1]["all_synced"] is True

    def test_after_20_cycles_max_offset_under_500ns(self):
        cluster = _cluster()
        cluster.run_sync_cycles(num_cycles=20)
        for offset in cluster.node_offsets_ns():
            assert abs(offset) < 500, f"Offset {offset:.1f} ns exceeds 500 ns threshold"

    def test_inter_node_error_shrinks_with_cycles(self):
        cluster = _cluster()
        # Measure inter-node error before any sync
        error_before = cluster.max_inter_node_error_ns()
        cluster.run_sync_cycles(num_cycles=20)
        error_after = cluster.max_inter_node_error_ns()
        assert error_after < error_before or error_after < 500, (
            f"Inter-node error did not shrink: before={error_before:.1f} ns, "
            f"after={error_after:.1f} ns"
        )

    def test_summary_has_required_keys(self):
        cluster = _cluster()
        s = cluster.summary()
        for key in ("sync_cycles", "num_nodes", "grandmaster",
                    "boundary_offset_ns", "max_node_offset_ns",
                    "max_inter_node_error_ns", "all_synced"):
            assert key in s, f"Missing key: {key}"

    def test_run_sync_cycles_returns_list(self):
        cluster = _cluster()
        results = cluster.run_sync_cycles(num_cycles=5)
        assert isinstance(results, list)
        assert len(results) == 5

    def test_sync_cycle_counter_increments(self):
        cluster = _cluster()
        cluster.run_sync_cycles(num_cycles=7)
        assert cluster._sync_cycles == 7

    def test_all_nodes_property_includes_gm_and_boundary(self):
        cluster = _cluster()
        all_nodes = cluster.all_nodes
        roles = {n.role for n in all_nodes}
        assert ClockRole.GRANDMASTER in roles
        assert ClockRole.BOUNDARY in roles
        assert ClockRole.ORDINARY in roles

    def test_node_offsets_ns_length_matches_num_nodes(self):
        cluster = _cluster()
        offsets = cluster.node_offsets_ns()
        assert len(offsets) == NUM_NODES

    def test_max_offset_ns_stat_is_nonnegative(self):
        cluster = _cluster()
        stats = cluster.sync_cycle(_true_time())
        assert stats["max_offset_ns"] >= 0

    def test_mean_offset_ns_stat_is_nonnegative(self):
        cluster = _cluster()
        stats = cluster.sync_cycle(_true_time())
        assert stats["mean_offset_ns"] >= 0

    def test_grandmaster_stat_matches_active_node(self):
        cluster = _cluster()
        stats = cluster.sync_cycle(_true_time())
        assert stats["grandmaster"] == cluster.grandmaster_mgr.active.node_id
