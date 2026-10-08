"""Edge-Native Clearing Rail — reference implementation.

A brokerless mutual-credit clearing system. Edge agents propose trades, a
plaintext Solver committee finds cycles, and a deterministic legal utility
handles defaults. There is NO centralized matching engine.
"""
from .types import (
    ZERO,
    Clock,
    CycleCandidate,
    Hop,
    IllegalTransition,
    Initiator,
    ManualClock,
    Node,
    VouchEdge,
)
from .ledger import Ledger
from .vouch import VouchGraph, VouchPolicy
from .amortization import AmortizationEngine, AmortizationPolicy
from .solver import ClearingLoop, ExecutionState, SolverCommittee
from .settlement import (
    CandidateBoard,
    PublishedCandidateBlock,
    SettlementEngine,
    SettlementSubmission,
    SolverRegistry,
)
from .legal import JobContext, Jurisdiction, TxType, evaluate_settlement, generate_payload
from .telemetry import compute as compute_telemetry

__all__ = [
    "ZERO", "Clock", "ManualClock", "Node", "VouchEdge", "Hop", "CycleCandidate",
    "Initiator", "IllegalTransition",
    "Ledger", "VouchGraph", "VouchPolicy",
    "AmortizationEngine", "AmortizationPolicy",
    "ClearingLoop", "ExecutionState", "SolverCommittee",
    "SolverRegistry", "CandidateBoard", "PublishedCandidateBlock",
    "SettlementEngine", "SettlementSubmission",
    "JobContext", "Jurisdiction", "TxType", "generate_payload", "evaluate_settlement",
    "compute_telemetry",
]
__version__ = "0.1.0"
