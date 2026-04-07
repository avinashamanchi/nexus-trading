"""
Pure asyncio Raft consensus for a 3-node in-process cluster.

Used by PortfolioSupervisorAgent to require majority agreement before
executing HALT / PAUSE FSM transitions, preventing single-node split-brain.

Simplifications vs. full Raft (appropriate for in-process use):
  - No network I/O — nodes communicate via direct async method calls.
  - Log is in-memory (not persisted across restarts).
  - NODE_COUNT is hardcoded at 3; majority = 2.
  - Leader election uses randomised asyncio.sleep timeouts (150–300 ms).
  - No log compaction / snapshotting.

Public API:
  RaftCluster  — manages 3 RaftNodes, elects a leader, exposes propose().
  RaftPSA      — decorator around PSA; intercepts halt/resume through Raft log.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)


# ─── Data types ───────────────────────────────────────────────────────────────

class RaftState(str, Enum):
    FOLLOWER  = "follower"
    CANDIDATE = "candidate"
    LEADER    = "leader"


@dataclass
class LogEntry:
    term:    int
    index:   int
    command: dict


@dataclass
class RaftMessage:
    type:      str    # "request_vote"|"vote_granted"|"vote_denied"|
                      # "append_entries"|"append_ack"
    term:      int
    from_node: int
    to_node:   int
    payload:   dict = field(default_factory=dict)


# ─── RaftNode ─────────────────────────────────────────────────────────────────

class RaftNode:
    """Single participant in the 3-node Raft cluster."""

    NODE_COUNT = 3

    def __init__(
        self,
        node_id: int,
        peers: list["RaftNode"],
        on_commit: Callable[[LogEntry], Awaitable[None]],
        election_timeout_ms_range: tuple[int, int] = (150, 300),
    ) -> None:
        self._id   = node_id
        self._peers = peers
        self._on_commit = on_commit
        self._timeout_range = election_timeout_ms_range

        # Persistent state (in-memory)
        self._state:        RaftState  = RaftState.FOLLOWER
        self._current_term: int        = 0
        self._voted_for:    int | None = None
        self._log:          list[LogEntry] = []

        # Volatile state
        self._commit_index: int = -1
        self._last_applied: int = -1

        # Leader volatile state
        self._next_index:  dict[int, int] = {}
        self._match_index: dict[int, int] = {}

        # Internal bookkeeping
        self._running               = False
        self._last_heartbeat_mono   = time.monotonic()
        self._election_timer_task:  asyncio.Task | None = None
        self._heartbeat_task:       asyncio.Task | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self._last_heartbeat_mono = time.monotonic()
        self._election_timer_task = asyncio.create_task(
            self._election_timer_loop(),
            name=f"raft_{self._id}_election",
        )

    async def stop(self) -> None:
        self._running = False
        for task in (self._election_timer_task, self._heartbeat_task):
            if task and not task.done():
                task.cancel()

    # ── Properties ─────────────────────────────────────────────────────────────

    @property
    def is_leader(self) -> bool:
        return self._state == RaftState.LEADER

    @property
    def term(self) -> int:
        return self._current_term

    @property
    def log_length(self) -> int:
        return len(self._log)

    @property
    def state(self) -> RaftState:
        return self._state

    # ── Leader: propose ────────────────────────────────────────────────────────

    async def propose(self, command: dict) -> bool:
        """
        Append *command* to the log and replicate to peers.

        Returns True when a majority (≥2/3) has acknowledged the entry and
        it has been committed.  Returns False immediately if this node is not
        the leader.
        """
        if not self.is_leader:
            return False

        entry = LogEntry(
            term=self._current_term,
            index=len(self._log),
            command=command,
        )
        self._log.append(entry)

        # Replicate to peers; count acks (leader itself counts as 1)
        acks = 1
        for peer in self._peers:
            try:
                ok = await peer._handle_append_entries_from(self, entry)
                if ok:
                    acks += 1
            except Exception as exc:
                logger.debug("[Raft %d] Replication to %d failed: %s", self._id, peer._id, exc)

        if acks >= 2:   # majority of 3
            self._commit_index = entry.index
            await self._apply_committed()
            return True

        # Not enough acks — roll back (simplified)
        self._log.pop()
        return False

    # ── Follower: handle incoming entries ──────────────────────────────────────

    async def _handle_append_entries_from(
        self, leader: "RaftNode", entry: LogEntry
    ) -> bool:
        """Accept a log entry from the leader."""
        if entry.term < self._current_term:
            return False
        # Accept the leader's term
        if entry.term > self._current_term:
            self._current_term = entry.term
            self._voted_for = None
        self._state = RaftState.FOLLOWER
        self._last_heartbeat_mono = time.monotonic()
        self._log.append(entry)
        self._commit_index = entry.index
        await self._apply_committed()
        return True

    # ── Commit application ─────────────────────────────────────────────────────

    async def _apply_committed(self) -> None:
        while self._last_applied < self._commit_index:
            self._last_applied += 1
            if self._last_applied < len(self._log):
                try:
                    await self._on_commit(self._log[self._last_applied])
                except Exception as exc:
                    logger.error("[Raft %d] on_commit error: %s", self._id, exc)

    # ── Election ───────────────────────────────────────────────────────────────

    async def _election_timer_loop(self) -> None:
        """Trigger an election if we haven't received a heartbeat in time."""
        while self._running:
            timeout_ms = random.randint(*self._timeout_range)
            await asyncio.sleep(timeout_ms / 1000.0)
            if not self._running:
                break
            if self._state == RaftState.LEADER:
                continue
            elapsed_ms = (time.monotonic() - self._last_heartbeat_mono) * 1000
            if elapsed_ms >= timeout_ms:
                await self._start_election()

    async def _start_election(self) -> None:
        self._current_term += 1
        self._state   = RaftState.CANDIDATE
        self._voted_for = self._id
        votes = 1   # vote for self

        logger.debug(
            "[Raft %d] Election started — term=%d", self._id, self._current_term
        )

        for peer in self._peers:
            try:
                granted = await peer._handle_request_vote(
                    from_node=self._id,
                    term=self._current_term,
                    log_length=len(self._log),
                )
                if granted:
                    votes += 1
            except Exception:
                pass

        if votes >= 2 and self._state == RaftState.CANDIDATE:
            await self._become_leader()

    async def _handle_request_vote(
        self, from_node: int, term: int, log_length: int
    ) -> bool:
        """Respond to a RequestVote RPC."""
        if term < self._current_term:
            return False
        if term > self._current_term:
            self._current_term = term
            self._state        = RaftState.FOLLOWER
            self._voted_for    = None
        # Grant vote if we haven't voted yet (or already voted for this candidate)
        if self._voted_for is None or self._voted_for == from_node:
            self._voted_for = from_node
            self._last_heartbeat_mono = time.monotonic()
            return True
        return False

    async def _become_leader(self) -> None:
        self._state = RaftState.LEADER
        logger.info("[Raft %d] Became LEADER — term=%d", self._id, self._current_term)
        for peer in self._peers:
            self._next_index[peer._id]  = len(self._log)
            self._match_index[peer._id] = -1
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name=f"raft_{self._id}_hb"
        )

    async def _send_heartbeats(self) -> None:
        """Ping followers so they reset their election timers."""
        for peer in self._peers:
            try:
                peer._last_heartbeat_mono = time.monotonic()
                if peer._current_term < self._current_term:
                    peer._current_term = self._current_term
                    peer._state        = RaftState.FOLLOWER
            except Exception:
                pass

    async def _heartbeat_loop(self) -> None:
        while self._running and self._state == RaftState.LEADER:
            await asyncio.sleep(0.05)   # 50 ms heartbeat interval
            await self._send_heartbeats()


# ─── RaftCluster ──────────────────────────────────────────────────────────────

class RaftCluster:
    """
    Manages a 3-node in-process Raft cluster.

    After start(), the cluster elects a leader within ~500 ms.
    propose() routes commands to the leader and waits for majority commit.
    """

    def __init__(
        self,
        on_commit: Callable[[LogEntry], Awaitable[None]] | None = None,
    ) -> None:
        self._on_commit = on_commit or self._noop_commit
        self._nodes: list[RaftNode] = []

    @staticmethod
    async def _noop_commit(entry: LogEntry) -> None:
        pass

    async def start(self) -> None:
        nodes = [RaftNode(i, [], self._on_commit) for i in range(3)]
        # Wire peers (every node knows the other two)
        for node in nodes:
            node._peers = [n for n in nodes if n._id != node._id]
        self._nodes = nodes
        for node in nodes:
            await node.start()
        # Wait up to 2 s for a leader to be elected
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if any(n.is_leader for n in self._nodes):
                return
            await asyncio.sleep(0.05)

    async def stop(self) -> None:
        for node in self._nodes:
            await node.stop()

    def get_leader(self) -> RaftNode | None:
        for node in self._nodes:
            if node.is_leader:
                return node
        return None

    async def propose(self, command: dict) -> bool:
        """Route to leader and retry up to 3× on transient failures."""
        for _ in range(3):
            leader = self.get_leader()
            if leader:
                result = await leader.propose(command)
                if result:
                    return True
            await asyncio.sleep(0.1)
        return False


# ─── RaftPSA ──────────────────────────────────────────────────────────────────

class RaftPSA:
    """
    Decorator around PortfolioSupervisorAgent that intercepts halt/resume
    transitions through the Raft log.  A transition only executes after a
    majority of nodes have committed the entry.

    Usage::

        cluster = RaftCluster(on_commit=raft_psa._on_commit)
        raft_psa = RaftPSA(psa, cluster)
        # PSA now uses Raft for _halt() / _pause() consensus
    """

    def __init__(self, psa, cluster: RaftCluster) -> None:
        self._psa     = psa
        self._cluster = cluster

    async def propose_halt(self, reason: str, breach_type: str) -> None:
        """Propose a HALT through Raft; only executes after majority commit."""
        command = {"action": "halt", "reason": reason, "breach_type": breach_type}
        committed = await self._cluster.propose(command)
        if committed:
            await self._psa._halt(reason, breach_type)

    async def propose_resume(self, approved_by: str) -> None:
        """Propose a RESUME through Raft; only executes after majority commit."""
        command = {"action": "resume", "approved_by": approved_by}
        committed = await self._cluster.propose(command)
        if committed:
            resume_fn = getattr(self._psa, "_resume", None) or getattr(
                self._psa, "_unpause", None
            )
            if resume_fn:
                await resume_fn(approved_by)

    async def _on_commit(self, entry: LogEntry) -> None:
        """Called by Raft nodes when an entry reaches majority commit."""
        action = entry.command.get("action")
        if action == "halt":
            await self._psa._halt(
                entry.command.get("reason", "raft_consensus"),
                entry.command.get("breach_type", "consensus"),
            )
        elif action == "resume":
            approved_by = entry.command.get("approved_by", "consensus")
            resume_fn = getattr(self._psa, "_resume", None) or getattr(
                self._psa, "_unpause", None
            )
            if resume_fn:
                await resume_fn(approved_by)
