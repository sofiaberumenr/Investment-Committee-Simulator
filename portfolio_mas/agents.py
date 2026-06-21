from __future__ import annotations

from typing import Any

from .models import Mandate, Message, MessageType, Portfolio, Severity, Trade


class MacroAgent:
    name = "macro"

    def assess(self, correlation_id: str) -> Message:
        return Message(
            sender=self.name,
            recipients=("portfolio_manager", "risk", "sector"),
            message_type=MessageType.OBSERVATION,
            subject="Inflation shock scenario",
            payload={"inflation": 0.041, "rate_outlook": "higher_for_longer", "confidence": 0.72},
            evidence=("mock://central_bank/cpi_2026_06", "mock://yield_curve/2026_06_20"),
            severity=Severity.WARNING,
            correlation_id=correlation_id,
        )


class SectorAgent:
    name = "sector"

    def recommend(self, correlation_id: str) -> Message:
        return Message(
            sender=self.name,
            recipients=("portfolio_manager", "risk", "compliance"),
            message_type=MessageType.RECOMMENDATION,
            subject="Inflation-hedge tilt",
            payload={
                "thesis": "Tilt toward energy while preserving liquidity",
                "trades": [
                    {"asset": "TECH_ETF", "delta_weight": -0.08, "sector": "Technology"},
                    {"asset": "CASH", "delta_weight": -0.04, "sector": "Cash"},
                    {"asset": "ENERGY_ETF", "delta_weight": 0.07, "sector": "Energy"},
                    {"asset": "SANCTIONED_OIL", "delta_weight": 0.05, "sector": "Energy"},
                ],
                "confidence": 0.68,
            },
            evidence=("mock://sector/energy_revision",),
            correlation_id=correlation_id,
        )


class RiskAgent:
    name = "risk"

    def review(self, portfolio: Portfolio, trades: list[Trade], mandate: Mandate, correlation_id: str) -> Message:
        proposed = portfolio.apply(trades)
        breaches = []
        for asset, weight in proposed.weights.items():
            if weight > mandate.max_asset_weight:
                breaches.append(f"asset_concentration:{asset}:{weight:.2f}")
        for sector, weight in proposed.sector_weights().items():
            if weight > mandate.max_sector_weight:
                breaches.append(f"sector_concentration:{sector}:{weight:.2f}")
        if proposed.weights.get("CASH", 0) < mandate.min_cash_weight:
            breaches.append(f"minimum_cash:{proposed.weights.get('CASH', 0):.2f}")
        gross_turnover = round(sum(abs(t.delta_weight) for t in trades) / 2, 4)
        return Message(
            sender=self.name,
            recipients=("portfolio_manager", "supervisor"),
            message_type=MessageType.CHALLENGE if breaches else MessageType.OBSERVATION,
            subject="Pre-trade risk review",
            payload={"breaches": breaches, "gross_turnover": gross_turnover, "sector_weights": proposed.sector_weights()},
            severity=Severity.CRITICAL if breaches else Severity.INFO,
            correlation_id=correlation_id,
        )


class ComplianceAgent:
    name = "compliance"

    def review(self, trades: list[Trade], mandate: Mandate, correlation_id: str) -> Message:
        restricted = sorted({t.asset for t in trades if t.asset in mandate.restricted_assets and t.delta_weight > 0})
        return Message(
            sender=self.name,
            recipients=("portfolio_manager", "supervisor"),
            message_type=MessageType.VETO if restricted else MessageType.OBSERVATION,
            subject="Mandate compliance review",
            payload={"restricted_assets": restricted, "rule_version": "mandate-v1"},
            severity=Severity.CRITICAL if restricted else Severity.INFO,
            correlation_id=correlation_id,
        )


class PortfolioManagerAgent:
    name = "portfolio_manager"

    def parse_trades(self, recommendation: Message) -> list[Trade]:
        raw_trades = recommendation.payload.get("trades")
        if not isinstance(raw_trades, list) or not raw_trades:
            raise ValueError("recommendation payload must include a non-empty trades list")

        trades: list[Trade] = []
        for index, raw_trade in enumerate(raw_trades):
            if not isinstance(raw_trade, dict):
                raise ValueError(f"trade {index} must be an object")
            trades.append(self._parse_trade(raw_trade, index))
        return trades

    def _parse_trade(self, raw_trade: dict[str, Any], index: int) -> Trade:
        allowed = {"asset", "delta_weight", "sector"}
        extra = set(raw_trade) - allowed
        missing = allowed - set(raw_trade)
        if missing:
            raise ValueError(f"trade {index} missing fields: {sorted(missing)}")
        if extra:
            raise ValueError(f"trade {index} has unknown fields: {sorted(extra)}")
        try:
            return Trade(
                asset=raw_trade["asset"],
                delta_weight=raw_trade["delta_weight"],
                sector=raw_trade["sector"],
            )
        except ValueError as exc:
            raise ValueError(f"trade {index} is invalid: {exc}") from exc

    def revise_after_veto(self, trades: list[Trade], veto: Message) -> list[Trade]:
        restricted = set(veto.payload["restricted_assets"])
        revised = [t for t in trades if t.asset not in restricted]
        removed = sum(t.delta_weight for t in trades if t.asset in restricted)
        if removed:
            revised.append(Trade("CASH", removed, "Cash"))
        return revised
