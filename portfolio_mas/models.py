from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
from math import isfinite
from typing import Any
from uuid import uuid4


class MessageType(str, Enum):
    OBSERVATION = "observation"
    RECOMMENDATION = "recommendation"
    CHALLENGE = "challenge"
    VETO = "veto"
    ESCALATION = "escalation"
    APPROVAL = "approval"
    DECISION = "decision"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True)
class Message:
    sender: str
    recipients: tuple[str, ...]
    message_type: MessageType
    subject: str
    payload: dict[str, Any]
    evidence: tuple[str, ...] = ()
    severity: Severity = Severity.INFO
    correlation_id: str = ""
    id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["message_type"] = self.message_type.value
        result["severity"] = self.severity.value
        return result


@dataclass(frozen=True)
class Trade:
    asset: str
    delta_weight: float
    sector: str

    def __post_init__(self) -> None:
        if not isinstance(self.asset, str) or not self.asset.strip():
            raise ValueError("trade asset is required")
        if not isinstance(self.sector, str) or not self.sector.strip():
            raise ValueError("trade sector is required")
        if (
            not isinstance(self.delta_weight, int | float)
            or isinstance(self.delta_weight, bool)
            or not isfinite(self.delta_weight)
        ):
            raise ValueError("trade delta_weight must be a finite number")


@dataclass
class Portfolio:
    weights: dict[str, float]
    sectors: dict[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.weights, dict) or not self.weights:
            raise ValueError("portfolio weights must be a non-empty mapping")
        if not isinstance(self.sectors, dict):
            raise ValueError("portfolio sectors must be a mapping")
        for asset, weight in self.weights.items():
            if not isinstance(asset, str) or not asset.strip():
                raise ValueError("portfolio asset names must be non-empty strings")
            if (
                not isinstance(weight, int | float)
                or isinstance(weight, bool)
                or not isfinite(weight)
                or weight < 0
            ):
                raise ValueError(
                    f"portfolio weight for {asset} must be a non-negative finite number"
                )
            sector = self.sectors.get(asset)
            if not isinstance(sector, str) or not sector.strip():
                raise ValueError(f"portfolio sector is required for {asset}")
        total = round(sum(self.weights.values()), 6)
        if total != 1:
            raise ValueError(f"portfolio weights must sum to 1.0, got {total}")

    def apply(self, trades: list[Trade]) -> "Portfolio":
        weights = self.weights.copy()
        sectors = self.sectors.copy()
        for trade in trades:
            if not isinstance(trade, Trade):
                raise ValueError("portfolio can only apply Trade objects")
            weights[trade.asset] = round(weights.get(trade.asset, 0) + trade.delta_weight, 6)
            if weights[trade.asset] < -0.000001:
                raise ValueError(f"trade overdraws asset weight: {trade.asset}")
            sectors[trade.asset] = trade.sector
        weights = {asset: weight for asset, weight in weights.items() if weight > 0.000001}
        sectors = {asset: sectors[asset] for asset in weights}
        return Portfolio(weights, sectors)

    def sector_weights(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for asset, weight in self.weights.items():
            sector = self.sectors[asset]
            totals[sector] = round(totals.get(sector, 0) + weight, 6)
        return totals

    def to_dict(self) -> dict[str, Any]:
        return {"weights": self.weights, "sectors": self.sectors}

    def proposal_hash(self) -> str:
        """Return a stable digest that binds approval to exact portfolio contents."""
        return stable_hash(self.to_dict())


@dataclass(frozen=True)
class Mandate:
    max_asset_weight: float = 0.30
    max_sector_weight: float = 0.35
    min_cash_weight: float = 0.05
    restricted_assets: tuple[str, ...] = ("SANCTIONED_OIL",)
    human_approval_threshold: float = 0.10

    def __post_init__(self) -> None:
        limits = {
            "max_asset_weight": self.max_asset_weight,
            "max_sector_weight": self.max_sector_weight,
            "min_cash_weight": self.min_cash_weight,
            "human_approval_threshold": self.human_approval_threshold,
        }
        for name, value in limits.items():
            if (
                not isinstance(value, int | float)
                or isinstance(value, bool)
                or not isfinite(value)
                or not 0 <= value <= 1
            ):
                raise ValueError(f"{name} must be a finite number between 0 and 1")
        if not all(
            isinstance(asset, str) and asset.strip()
            for asset in self.restricted_assets
        ):
            raise ValueError("restricted_assets must contain non-empty asset names")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_asset_weight": self.max_asset_weight,
            "max_sector_weight": self.max_sector_weight,
            "min_cash_weight": self.min_cash_weight,
            "restricted_assets": list(self.restricted_assets),
            "human_approval_threshold": self.human_approval_threshold,
        }


@dataclass(frozen=True)
class HumanApproval:
    approver: str
    proposal_hash: str
    input_snapshot_hash: str
    approved: bool
    rationale: str
    id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


def stable_hash(value: dict[str, Any]) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode()).hexdigest()
