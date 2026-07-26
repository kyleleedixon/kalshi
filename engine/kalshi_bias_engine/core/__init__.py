"""Core interfaces. Domain-agnostic. Nothing in this package may import
domain-specific modules (crypto, sports, cross-venue)."""

from .contract import Contract, ContractSide, SettlementSource
from .estimate import ProbEstimate, StalenessReason
from .oracle import Oracle
from .bias import BiasFeature, BiasModel, BiasAdjustment
from .execution import ExecutionPolicy, Action, ActionType
from .calibration import CalibrationReport, BandKey, BandStats
from .registry import DomainRegistry, register_domain, get_domain

__all__ = [
    "Contract",
    "ContractSide",
    "SettlementSource",
    "ProbEstimate",
    "StalenessReason",
    "Oracle",
    "BiasFeature",
    "BiasModel",
    "BiasAdjustment",
    "ExecutionPolicy",
    "Action",
    "ActionType",
    "CalibrationReport",
    "BandKey",
    "BandStats",
    "DomainRegistry",
    "register_domain",
    "get_domain",
]
