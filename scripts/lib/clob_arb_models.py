from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class Leg:
    market_id: str
    question: str
    label: str
    token_id: str
    simmer_market_id: str = ""
    side: str = "yes"
    condition_id: str = ""


@dataclass
class EventBasket:
    key: str
    title: str
    legs: List[Leg]
    strategy: str = "buckets"
    market_id: str = ""
    event_id: str = ""
    event_slug: str = ""
    liquidity_num: float = 0.0
    volume24hr: float = 0.0
    spread: float = 0.0
    one_day_price_change: float = 0.0
    end_ms: Optional[int] = None
    min_order_size: float = 0.0
    price_tick_size: float = 0.0
    score: float = 0.0
    wallet_signal_score: float = 0.0
    wallet_signal_confidence: float = 0.0
    combined_score: float = 0.0
    sports_market_type: str = ""
    last_alert_ts: float = 0.0
    last_exec_ts: float = 0.0
    last_eval_ts: float = 0.0
    last_signature: str = ""
    exec_edge_neg_streak: int = 0
    exec_edge_filter_until_ts: float = 0.0


@dataclass
class LocalBook:
    asks: List[dict] = field(default_factory=list)
    bids: List[dict] = field(default_factory=list)
    best_ask: Optional[float] = None
    best_bid: Optional[float] = None
    asks_synthetic: bool = False
    bids_synthetic: bool = False
    updated_at: float = 0.0


@dataclass
class Candidate:
    strategy: str
    event_key: str
    title: str
    shares_per_leg: float
    basket_cost: float
    payout_after_fee: float
    fixed_cost: float
    net_edge: float
    edge_pct: float
    leg_costs: List[Tuple[Leg, float]]


@dataclass
class RuntimeState:
    day: str
    executions_today: int = 0
    notional_today: float = 0.0
    consecutive_failures: int = 0
    halted: bool = False
    halt_reason: str = ""
    start_pnl_total: Optional[float] = None
    last_pnl_total: Optional[float] = None
    last_pnl_check_ts: float = 0.0


@dataclass
class RunStats:
    candidates_total: int = 0
    candidates_window: int = 0
    best_all: Optional[Candidate] = None
    best_window: Optional[Candidate] = None
    window_started_at: float = field(default_factory=lambda: time.time())
    last_summary_ts: float = field(default_factory=lambda: time.time())

