"""
FPGA/ASIC silicon-level execution simulation.

Models a 5-stage hardware pipeline executing at 250 MHz (4 ns/cycle).
Unlike software which executes instructions sequentially, FPGA pipeline
stages run in parallel on different orders — one frame exits every clock
cycle once the pipeline is full.

Real-world analogue: Xilinx Virtex UltraScale+ VU9P on an Alveo U250 card,
running tick-to-trade at 50–100 ns (vs 1–5 µs for optimised C++ software).

Pipeline stages:
  0 – DECODE:   SBE market-data decode → extract bid/ask/last fields
  1 – SIGNAL:   Pattern / momentum check via DSP slices
  2 – RISK:     BRAM-resident pre-loaded limit lookup (1 clock = 4 ns)
  3 – ENCODE:   SBE/OUCH binary order construction
  4 – TRANSMIT: Push to NIC TX ring buffer

Classes:
  FPGAFrame       – 32-byte aligned input/output struct
  FPGASignalUnit  – Agent 4 (MiSA) signal logic in synthesised gates
  FPGARiskUnit    – Hardware pre-loaded risk limits (BRAM lookup)
  FPGAOrderUnit   – Agent 8 (EA) order encoding in FPGA fabric
  FPGAPipeline    – Orchestrates all 5 stages; exposes simulated_latency_ns
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import IntEnum

CLOCK_FREQ_HZ     = 250_000_000      # 250 MHz Xilinx Virtex UltraScale+
CLOCK_PERIOD_NS   = 4                 # 4 ns per clock cycle
PIPELINE_STAGES   = 5                 # decode, signal, risk, encode, transmit
NIC_SERIAL_NS     = 60                # ~60 ns NIC serialisation (64-byte frame @10G)
FPGA_LATENCY_NS   = PIPELINE_STAGES * CLOCK_PERIOD_NS + NIC_SERIAL_NS  # 80 ns


class PipelineStage(IntEnum):
    DECODE   = 0
    SIGNAL   = 1
    RISK     = 2
    ENCODE   = 3
    TRANSMIT = 4


# ─── Frame ────────────────────────────────────────────────────────────────────

@dataclass
class FPGAFrame:
    """
    One market tick flowing through the pipeline.
    Caller populates market-data fields; pipeline writes signal/order fields.
    """
    sequence:         int          = 0
    symbol:           bytes        = b""
    bid:              float        = 0.0
    ask:              float        = 0.0
    last:             float        = 0.0
    volume:           int          = 0
    timestamp_ns:     int          = 0
    stage:            PipelineStage = PipelineStage.DECODE

    # Written by FPGASignalUnit
    signal_strength:  int   = 0    # 0-255  (8-bit quantised momentum)
    signal_direction: int   = 0    # +1 buy, -1 sell, 0 no signal

    # Written by FPGARiskUnit
    risk_approved:    bool  = False

    # Written by FPGAOrderUnit
    order_qty:        int   = 0
    order_price:      float = 0.0


# ─── Pipeline Stats ───────────────────────────────────────────────────────────

@dataclass
class PipelineStats:
    frames_processed:  int   = 0
    signals_generated: int   = 0
    risk_rejections:   int   = 0
    total_latency_ns:  int   = 0

    @property
    def mean_latency_ns(self) -> float:
        if self.frames_processed == 0:
            return 0.0
        return self.total_latency_ns / self.frames_processed

    @property
    def throughput_mps(self) -> float:
        """Theoretical: one frame per clock cycle (pipeline full)."""
        return CLOCK_FREQ_HZ / 1_000_000


# ─── Stage 1: Signal Unit ─────────────────────────────────────────────────────

class FPGASignalUnit:
    """
    Models Agent 4 (MiSA) micro-signal detection in synthesised FPGA gates.

    In hardware:
      – Spread threshold comparison → 1 LUT lookup  (~4 ns)
      – Momentum slope via 2 DSP slices (multiply-accumulate) (~4 ns)
      – Output: 8-bit signal_strength, 2-bit signal_direction

    Python simulation executes identical logic; latency is MODELLED, not real.
    """

    def __init__(
        self,
        spread_threshold_bps: float = 5.0,
        momentum_window:      int   = 8,
        signal_threshold:     float = 0.3,   # momentum bps to fire a signal
    ) -> None:
        self._spread_bps   = spread_threshold_bps
        self._window       = momentum_window
        self._threshold    = signal_threshold
        self._price_buffer: list[float] = []

    def process(self, frame: FPGAFrame) -> FPGAFrame:
        """Single clock cycle: compute signal direction and strength."""
        frame.stage = PipelineStage.SIGNAL

        if frame.ask <= 0 or frame.bid <= 0:
            return frame

        spread_bps = (frame.ask - frame.bid) / frame.bid * 10_000
        if spread_bps > self._spread_bps:
            # Wide spread — no signal (1 LUT comparison)
            return frame

        # Rolling price buffer (FPGA BRAM ring)
        self._price_buffer.append(frame.last)
        if len(self._price_buffer) > self._window:
            self._price_buffer.pop(0)

        if len(self._price_buffer) < 3:
            return frame

        mid = len(self._price_buffer) // 2
        recent = sum(self._price_buffer[mid:]) / (len(self._price_buffer) - mid)
        older  = sum(self._price_buffer[:mid]) / mid
        if older <= 0:
            return frame

        momentum = (recent - older) / older * 10_000  # in bps
        frame.signal_strength = min(255, int(abs(momentum) * 10))
        if abs(momentum) >= self._threshold:
            frame.signal_direction = 1 if momentum > 0 else -1

        return frame


# ─── Stage 2: Risk Unit ───────────────────────────────────────────────────────

class FPGARiskUnit:
    """
    Models hardware pre-loaded risk limits stored in FPGA BRAM.

    BRAM lookup latency: 1 clock cycle (4 ns) regardless of table size.
    Limits are loaded at session start via a software management plane
    and never change during the trading session.
    """

    def __init__(
        self,
        max_order_size:   int   = 5_000,
        max_notional_usd: float = 500_000.0,
        price_band_bps:   float = 50.0,
    ) -> None:
        self._max_size     = max_order_size
        self._max_notional = max_notional_usd
        self._price_band   = price_band_bps

    def approve(self, frame: FPGAFrame) -> FPGAFrame:
        """Single-cycle BRAM lookup: approve or reject provisional order."""
        frame.stage = PipelineStage.RISK

        if frame.signal_direction == 0:
            return frame

        mid = (frame.bid + frame.ask) / 2
        notional = frame.order_qty * mid

        if frame.order_qty > self._max_size:
            frame.risk_approved = False
            return frame
        if notional > self._max_notional:
            frame.risk_approved = False
            return frame
        if mid > 0 and frame.order_price > 0:
            deviation_bps = abs(frame.order_price - mid) / mid * 10_000
            if deviation_bps > self._price_band:
                frame.risk_approved = False
                return frame

        frame.risk_approved = True
        return frame


# ─── Stage 3: Order Unit ─────────────────────────────────────────────────────

class FPGAOrderUnit:
    """
    Models Agent 8 (EA) OUCH order encoding in FPGA fabric.

    In hardware:
      – Field assignments → parallel register writes (1 cycle)
      – CRC/checksum → XOR reduction tree (1 cycle)
      Result: 44-byte OUCH binary order in 4 ns.
    """

    def __init__(self, default_qty: int = 100) -> None:
        self._default_qty = default_qty
        self._order_seq   = 0

    def encode_order(self, frame: FPGAFrame) -> FPGAFrame:
        """Single clock cycle: encode order from signal + risk approval."""
        frame.stage = PipelineStage.ENCODE

        if frame.signal_direction == 0 or not frame.risk_approved:
            return frame

        self._order_seq += 1
        # Aggressive: cross the spread to ensure immediate fill
        frame.order_price = frame.ask if frame.signal_direction > 0 else frame.bid
        frame.order_qty   = self._default_qty
        return frame


# ─── Pipeline Orchestrator ────────────────────────────────────────────────────

class FPGAPipeline:
    """
    5-stage deterministic hardware execution pipeline.

    Simulated latency:
      5 stages × 4 ns/cycle + 60 ns NIC serialisation = 80 ns

    In software, each 'stage' is a Python function call.
    `simulated_latency_ns` reports the MODELLED hardware latency.
    `stats.mean_latency_ns` also returns the modelled value (useful for tests).
    """

    def __init__(
        self,
        signal_unit: FPGASignalUnit | None = None,
        risk_unit:   FPGARiskUnit   | None = None,
        order_unit:  FPGAOrderUnit  | None = None,
    ) -> None:
        self._signal = signal_unit or FPGASignalUnit()
        self._risk   = risk_unit   or FPGARiskUnit()
        self._order  = order_unit  or FPGAOrderUnit()
        self.stats   = PipelineStats()

    def process(self, frame: FPGAFrame) -> FPGAFrame:
        """
        Run frame through all 5 pipeline stages.
        Returns frame with signal / risk / order fields populated.
        """
        # Stage 0: Decode (pre-populated by caller)
        frame.stage = PipelineStage.DECODE

        # Stage 1: Signal
        frame = self._signal.process(frame)

        # Stage 2: Set provisional order fields before risk check
        if frame.signal_direction != 0:
            frame.order_qty   = self._order._default_qty
            frame.order_price = frame.ask if frame.signal_direction > 0 else frame.bid
        frame = self._risk.approve(frame)

        # Stage 3: Encode order
        frame = self._order.encode_order(frame)

        # Stage 4: Transmit (wire-out — no-op in simulation)
        frame.stage = PipelineStage.TRANSMIT

        # Accumulate stats
        self.stats.frames_processed  += 1
        self.stats.total_latency_ns  += FPGA_LATENCY_NS
        if frame.signal_direction != 0:
            self.stats.signals_generated += 1
        if frame.signal_direction != 0 and not frame.risk_approved:
            self.stats.risk_rejections += 1

        return frame

    async def process_async(self, frame: FPGAFrame) -> FPGAFrame:
        """Async wrapper — yields once to the event loop between frames."""
        result = self.process(frame)
        await asyncio.sleep(0)
        return result

    @property
    def simulated_latency_ns(self) -> int:
        """Modelled end-to-end pipeline latency (80 ns nominal)."""
        return FPGA_LATENCY_NS
