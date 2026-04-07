"""
Tests for:
  - RaftNode     (single Raft participant)
  - RaftCluster  (3-node in-process cluster)
  - RaftPSA      (Raft-wrapped PortfolioSupervisorAgent)
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from infrastructure.raft import (
    LogEntry,
    RaftCluster,
    RaftNode,
    RaftPSA,
    RaftState,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _noop_commit(entry: LogEntry) -> None:
    pass


def _make_node(node_id: int = 0) -> RaftNode:
    """Create an isolated RaftNode with no peers."""
    return RaftNode(node_id=node_id, peers=[], on_commit=_noop_commit)


def _make_cluster_of_3(on_commit=None) -> list[RaftNode]:
    """Return 3 RaftNodes wired as peers (not started)."""
    cb = on_commit or _noop_commit
    nodes = [RaftNode(i, [], cb) for i in range(3)]
    for n in nodes:
        n._peers = [m for m in nodes if m._id != n._id]
    return nodes


# ═══════════════════════════════════════════════════════════════════════════════
#  TestRaftNode — basic state
# ═══════════════════════════════════════════════════════════════════════════════

class TestRaftNode:

    def test_initial_state_is_follower(self):
        node = _make_node()
        assert node.state == RaftState.FOLLOWER

    def test_initial_term_is_zero(self):
        node = _make_node()
        assert node.term == 0

    def test_is_leader_false_initially(self):
        node = _make_node()
        assert node.is_leader is False

    def test_log_length_zero_initially(self):
        node = _make_node()
        assert node.log_length == 0

    @pytest.mark.asyncio
    async def test_start_creates_election_timer_task(self):
        node = _make_node()
        await node.start()
        assert node._election_timer_task is not None
        await node.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_tasks(self):
        node = _make_node()
        await node.start()
        await node.stop()
        assert not node._running

    @pytest.mark.asyncio
    async def test_handle_request_vote_grants_when_eligible(self):
        node = _make_node()
        granted = await node._handle_request_vote(from_node=1, term=1, log_length=0)
        assert granted is True
        assert node._voted_for == 1

    @pytest.mark.asyncio
    async def test_handle_request_vote_denies_lower_term(self):
        node = _make_node()
        node._current_term = 5
        granted = await node._handle_request_vote(from_node=1, term=3, log_length=0)
        assert granted is False

    @pytest.mark.asyncio
    async def test_handle_request_vote_denies_already_voted(self):
        node = _make_node()
        node._current_term = 1
        node._voted_for = 2   # already voted for node 2
        granted = await node._handle_request_vote(from_node=1, term=1, log_length=0)
        assert granted is False

    @pytest.mark.asyncio
    async def test_election_with_majority_makes_leader(self):
        """With 2 peers granting votes, node becomes LEADER."""
        nodes = _make_cluster_of_3()
        for n in nodes:
            await n.start()
        await nodes[0]._start_election()
        assert nodes[0].is_leader
        for n in nodes:
            await n.stop()


# ═══════════════════════════════════════════════════════════════════════════════
#  TestRaftLog — propose and commit
# ═══════════════════════════════════════════════════════════════════════════════

class TestRaftLog:

    @pytest.mark.asyncio
    async def test_propose_returns_false_when_not_leader(self):
        node = _make_node()
        result = await node.propose({"action": "halt"})
        assert result is False

    @pytest.mark.asyncio
    async def test_propose_appends_to_log(self):
        nodes = _make_cluster_of_3()
        for n in nodes:
            await n.start()
        await nodes[0]._start_election()
        assert nodes[0].is_leader
        result = await nodes[0].propose({"x": 1})
        assert result is True
        # Leader log should have the entry
        assert nodes[0].log_length >= 1
        for n in nodes:
            await n.stop()

    @pytest.mark.asyncio
    async def test_propose_returns_true_with_majority(self):
        nodes = _make_cluster_of_3()
        for n in nodes:
            await n.start()
        await nodes[0]._start_election()
        result = await nodes[0].propose({"cmd": "test"})
        assert result is True
        for n in nodes:
            await n.stop()

    @pytest.mark.asyncio
    async def test_on_commit_callback_called(self):
        committed = []

        async def cb(entry: LogEntry) -> None:
            committed.append(entry)

        nodes = _make_cluster_of_3(on_commit=cb)
        for n in nodes:
            await n.start()
        await nodes[0]._start_election()
        await nodes[0].propose({"op": "test"})
        # on_commit should have been called on the leader (and possibly followers)
        assert len(committed) >= 1
        for n in nodes:
            await n.stop()

    @pytest.mark.asyncio
    async def test_log_entry_has_correct_fields(self):
        nodes = _make_cluster_of_3()
        for n in nodes:
            await n.start()
        await nodes[0]._start_election()
        cmd = {"action": "halt", "reason": "test"}
        await nodes[0].propose(cmd)
        entry = nodes[0]._log[0]
        assert entry.index == 0
        assert entry.term == nodes[0].term
        assert entry.command == cmd
        for n in nodes:
            await n.stop()

    @pytest.mark.asyncio
    async def test_multiple_proposes_increase_log_length(self):
        nodes = _make_cluster_of_3()
        for n in nodes:
            await n.start()
        await nodes[0]._start_election()
        await nodes[0].propose({"a": 1})
        await nodes[0].propose({"b": 2})
        assert nodes[0].log_length >= 2
        for n in nodes:
            await n.stop()

    @pytest.mark.asyncio
    async def test_follower_receives_log_entry(self):
        nodes = _make_cluster_of_3()
        for n in nodes:
            await n.start()
        await nodes[0]._start_election()
        await nodes[0].propose({"forwarded": True})
        # At least one follower should have the entry
        followers = [n for n in nodes if not n.is_leader]
        received = any(n.log_length >= 1 for n in followers)
        assert received
        for n in nodes:
            await n.stop()

    @pytest.mark.asyncio
    async def test_non_leader_propose_returns_false(self):
        nodes = _make_cluster_of_3()
        for n in nodes:
            await n.start()
        await nodes[0]._start_election()
        follower = next(n for n in nodes if not n.is_leader)
        result = await follower.propose({"x": 1})
        assert result is False
        for n in nodes:
            await n.stop()


# ═══════════════════════════════════════════════════════════════════════════════
#  TestRaftCluster — 3-node cluster
# ═══════════════════════════════════════════════════════════════════════════════

class TestRaftCluster:

    @pytest.mark.asyncio
    async def test_cluster_elects_leader_within_2s(self):
        cluster = RaftCluster()
        await cluster.start()
        leader = cluster.get_leader()
        assert leader is not None
        assert leader.is_leader
        await cluster.stop()

    @pytest.mark.asyncio
    async def test_get_leader_returns_leader_node(self):
        cluster = RaftCluster()
        await cluster.start()
        leader = cluster.get_leader()
        assert isinstance(leader, RaftNode)
        assert leader.is_leader
        await cluster.stop()

    @pytest.mark.asyncio
    async def test_propose_via_cluster_commits(self):
        cluster = RaftCluster()
        await cluster.start()
        result = await cluster.propose({"cmd": "halt", "reason": "test"})
        assert result is True
        await cluster.stop()

    @pytest.mark.asyncio
    async def test_cluster_propose_routes_to_leader(self):
        committed = []

        async def cb(entry: LogEntry) -> None:
            committed.append(entry)

        cluster = RaftCluster(on_commit=cb)
        await cluster.start()
        await cluster.propose({"x": 42})
        assert any(e.command.get("x") == 42 for e in committed)
        await cluster.stop()

    @pytest.mark.asyncio
    async def test_no_leader_propose_returns_false(self):
        """propose() with no leader returns False after retries."""
        cluster = RaftCluster()
        # Don't call start() — no nodes, no leader
        result = await cluster.propose({"x": 1})
        assert result is False


# ═══════════════════════════════════════════════════════════════════════════════
#  TestRaftPSA — consensus decorator around PSA
# ═══════════════════════════════════════════════════════════════════════════════

class TestRaftPSA:

    def _mock_psa(self):
        psa = MagicMock()
        psa._halt = AsyncMock()
        psa._resume = AsyncMock()
        return psa

    @pytest.mark.asyncio
    async def test_propose_halt_calls_psa_halt_after_commit(self):
        psa = self._mock_psa()
        cluster = RaftCluster()
        await cluster.start()
        raft_psa = RaftPSA(psa, cluster)
        await raft_psa.propose_halt("loss_cap", "daily_loss")
        psa._halt.assert_called_once_with("loss_cap", "daily_loss")
        await cluster.stop()

    @pytest.mark.asyncio
    async def test_propose_halt_no_majority_psa_not_called(self):
        """Without a cluster leader, propose_halt should NOT call psa._halt."""
        psa = self._mock_psa()
        cluster = RaftCluster()   # not started → no leader
        raft_psa = RaftPSA(psa, cluster)
        await raft_psa.propose_halt("test", "test")
        psa._halt.assert_not_called()

    @pytest.mark.asyncio
    async def test_propose_resume_calls_psa_resume(self):
        psa = self._mock_psa()
        cluster = RaftCluster()
        await cluster.start()
        raft_psa = RaftPSA(psa, cluster)
        await raft_psa.propose_resume("ops_team")
        psa._resume.assert_called_once()
        await cluster.stop()

    @pytest.mark.asyncio
    async def test_on_commit_dispatches_halt(self):
        psa = self._mock_psa()
        cluster = RaftCluster()
        raft_psa = RaftPSA(psa, cluster)
        entry = LogEntry(term=1, index=0, command={"action": "halt",
                                                    "reason": "dd",
                                                    "breach_type": "drawdown"})
        await raft_psa._on_commit(entry)
        psa._halt.assert_called_once_with("dd", "drawdown")

    @pytest.mark.asyncio
    async def test_on_commit_dispatches_resume(self):
        psa = self._mock_psa()
        cluster = RaftCluster()
        raft_psa = RaftPSA(psa, cluster)
        entry = LogEntry(term=1, index=0, command={"action": "resume",
                                                    "approved_by": "trader"})
        await raft_psa._on_commit(entry)
        psa._resume.assert_called_once_with("trader")
