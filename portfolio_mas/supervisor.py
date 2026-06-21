from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .agents import ComplianceAgent, MacroAgent, PortfolioManagerAgent, RiskAgent, SectorAgent
from .blackboard import Blackboard
from .models import HumanApproval, Mandate, Message, MessageType, Portfolio, Severity, Trade, stable_hash


ApprovalProvider = Callable[[str, str], HumanApproval | None]


@dataclass
class RunResult:
    status: str
    initial: Portfolio
    proposed: Portfolio
    final: Portfolio
    correlation_id: str
    message_count: int


class CommitteeSupervisor:
    """Orchestrates stages but cannot bypass code-enforced veto and approval gates."""

    def __init__(self, audit_path: Path | None = None) -> None:
        self.board = Blackboard(audit_path)
        self.macro = MacroAgent()
        self.sector = SectorAgent()
        self.risk = RiskAgent()
        self.compliance = ComplianceAgent()
        self.pm = PortfolioManagerAgent()
        self._used_approval_ids: set[str] = set(self.board.audit_approval_ids)

    def _approval_is_fresh(self, approval: HumanApproval) -> bool:
        try:
            timestamp = datetime.fromisoformat(approval.timestamp)
        except (TypeError, ValueError):
            return False
        if timestamp.tzinfo is None:
            return False
        age = datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)
        return timedelta(0) <= age <= timedelta(minutes=15)

    def _blocked_result(
        self,
        cid: str,
        initial: Portfolio,
        proposed: Portfolio | None = None,
    ) -> RunResult:
        return RunResult(
            "BLOCKED",
            initial,
            proposed if proposed is not None else initial,
            initial,
            cid,
            len(self.board.by_correlation(cid)),
        )

    def _escalate(
        self,
        cid: str,
        subject: str,
        reason: str,
        error: str | None = None,
        recipients: tuple[str, ...] = ("human_chair", "audit"),
    ) -> None:
        payload = {"reason": reason, "rollback": "initial_portfolio"}
        if error:
            payload["error"] = error
        self.board.publish(Message(
            sender="supervisor",
            recipients=recipients,
            message_type=MessageType.ESCALATION,
            subject=subject,
            payload=payload,
            severity=Severity.CRITICAL,
            correlation_id=cid,
        ))

    def _input_snapshot_hash(
        self,
        initial: Portfolio,
        mandate: Mandate,
        macro: Message,
        recommendation: Message,
    ) -> str:
        return stable_hash({
            "initial_portfolio": initial.to_dict(),
            "mandate": mandate.to_dict(),
            "market_data_snapshot": {
                "macro": {
                    "payload": macro.payload,
                    "evidence": list(macro.evidence),
                },
                "sector": {
                    "payload": {
                        key: value
                        for key, value in recommendation.payload.items()
                        if key != "trades"
                    },
                    "evidence": list(recommendation.evidence),
                },
            },
        })

    def _publish_agent_response(
        self,
        cid: str,
        expected_sender: str,
        call: Callable[[], Message],
    ) -> Message | None:
        try:
            message = call()
        except Exception as exc:
            self._escalate(
                cid,
                f"{expected_sender} response unavailable",
                f"{expected_sender}_unavailable",
                str(exc),
            )
            return None
        if not isinstance(message, Message):
            self._escalate(
                cid,
                f"{expected_sender} response missing",
                f"{expected_sender}_missing",
            )
            return None
        try:
            self.board.publish(message)
        except ValueError as exc:
            self._escalate(
                cid,
                f"{expected_sender} response rejected",
                f"invalid_{expected_sender}_response",
                str(exc),
            )
            return None
        return message

    def _review_controls(
        self,
        cid: str,
        initial: Portfolio,
        trades: list[Trade],
        mandate: Mandate,
    ) -> tuple[Message | None, Message | None]:
        risk = self._publish_agent_response(
            cid, "risk", lambda: self.risk.review(initial, trades, mandate, cid)
        )
        if risk is None:
            return None, None
        compliance = self._publish_agent_response(
            cid, "compliance", lambda: self.compliance.review(trades, mandate, cid)
        )
        return risk, compliance

    def run(
        self,
        initial: Portfolio,
        mandate: Mandate,
        approval_provider: ApprovalProvider | None = None,
    ) -> RunResult:
        cid = str(uuid4())
        macro = self._publish_agent_response(cid, "macro", lambda: self.macro.assess(cid))
        if macro is None:
            return self._blocked_result(cid, initial)
        recommendation = self._publish_agent_response(cid, "sector", lambda: self.sector.recommend(cid))
        if recommendation is None:
            return self._blocked_result(cid, initial)
        input_snapshot_hash = self._input_snapshot_hash(initial, mandate, macro, recommendation)

        try:
            trades = self.pm.parse_trades(recommendation)
            proposed = initial.apply(trades)
        except ValueError as exc:
            self._escalate(
                cid,
                "Invalid recommendation blocked",
                "invalid_recommendation",
                str(exc),
            )
            return self._blocked_result(cid, initial)

        risk, compliance = self._review_controls(cid, initial, trades, mandate)
        if risk is None or compliance is None:
            return self._blocked_result(cid, initial, proposed)

        if compliance.message_type is MessageType.VETO:
            trades = self.pm.revise_after_veto(trades, compliance)
            self.board.publish(Message(
                sender="portfolio_manager", recipients=("risk", "compliance", "supervisor"),
                message_type=MessageType.RECOMMENDATION, subject="Revised trade list after veto",
                payload={
                    "thesis": "Remove restricted exposure and hold proceeds in cash",
                    "trades": [t.__dict__ for t in trades],
                    "confidence": 0.65,
                }, correlation_id=cid,
            ))
            try:
                initial.apply(trades)
            except ValueError as exc:
                self._escalate(cid, "Invalid revision blocked", "invalid_revision", str(exc))
                return self._blocked_result(cid, initial, proposed)
            risk, compliance = self._review_controls(cid, initial, trades, mandate)
            if risk is None or compliance is None:
                return self._blocked_result(cid, initial, proposed)

        unresolved = risk.payload["breaches"] or compliance.message_type is MessageType.VETO
        turnover = risk.payload["gross_turnover"]
        needs_human = turnover >= mandate.human_approval_threshold
        controlled_proposal = initial.apply(trades)
        proposal_hash = controlled_proposal.proposal_hash()
        approval = approval_provider(proposal_hash, input_snapshot_hash) if needs_human and approval_provider else None
        valid_approval = bool(
            approval
            and approval.approved
            and approval.proposal_hash == proposal_hash
            and approval.input_snapshot_hash == input_snapshot_hash
            and approval.approver.strip()
            and self._approval_is_fresh(approval)
            and approval.id not in self._used_approval_ids
        )
        if approval:
            approval_fresh = self._approval_is_fresh(approval)
            approval_replayed = approval.id in self._used_approval_ids
            input_snapshot_matches = approval.input_snapshot_hash == input_snapshot_hash
            self.board.publish(Message(
                sender="human_chair", recipients=("supervisor", "audit"),
                message_type=MessageType.APPROVAL, subject="Human approval response",
                payload={
                    "approval_id": approval.id,
                    "approver": approval.approver,
                    "approved": approval.approved,
                    "proposal_hash": approval.proposal_hash,
                    "rationale": approval.rationale,
                    "approval_timestamp": approval.timestamp,
                    "hash_matches": approval.proposal_hash == proposal_hash,
                    "fresh": approval_fresh,
                    "replayed": approval_replayed,
                    "input_snapshot_hash": approval.input_snapshot_hash,
                    "input_snapshot_matches": input_snapshot_matches,
                },
                severity=Severity.INFO if valid_approval else Severity.CRITICAL,
                correlation_id=cid,
            ))
            self._used_approval_ids.add(approval.id)

        if unresolved or (needs_human and not valid_approval):
            if unresolved:
                reason = "unresolved_control_breach"
            elif approval:
                reason = "invalid_or_rejected_human_approval"
            else:
                reason = "human_approval_required"
            self._escalate(cid, "Decision blocked", reason, recipients=("human_chair",))
            return self._blocked_result(cid, initial, proposed)

        final = controlled_proposal
        self.board.publish(Message(
            sender="supervisor", recipients=("human_chair", "audit"),
            message_type=MessageType.DECISION, subject="Rebalance approved",
            payload={
                "human_approved": valid_approval if needs_human else None,
                "approval_id": approval.id if approval else None,
                "proposal_hash": proposal_hash,
                "input_snapshot_hash": input_snapshot_hash,
                "final_weights": final.weights,
            },
            correlation_id=cid,
        ))
        return RunResult("APPROVED", initial, proposed, final, cid, len(self.board.by_correlation(cid)))
