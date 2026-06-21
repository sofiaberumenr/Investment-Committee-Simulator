import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from random import Random

from portfolio_mas.agents import PortfolioManagerAgent
from portfolio_mas.blackboard import Blackboard
from portfolio_mas.models import (
    HumanApproval,
    Mandate,
    Message,
    MessageType,
    Portfolio,
    Trade,
    stable_hash,
)
from portfolio_mas.supervisor import CommitteeSupervisor


def portfolio() -> Portfolio:
    return Portfolio(
        {"TECH_ETF": .20, "BOND_ETF": .30, "ENERGY_ETF": .10, "HEALTH_ETF": .25, "CASH": .15},
        {"TECH_ETF": "Technology", "BOND_ETF": "Fixed Income", "ENERGY_ETF": "Energy", "HEALTH_ETF": "Healthcare", "CASH": "Cash"},
    )


def approve(proposal_hash: str, input_snapshot_hash: str) -> HumanApproval:
    return HumanApproval(
        approver="chair@example.com",
        proposal_hash=proposal_hash,
        input_snapshot_hash=input_snapshot_hash,
        approved=True,
        rationale="Risk and compliance controls cleared.",
    )


class CommitteeSafetyTests(unittest.TestCase):
    def test_compliance_veto_removes_restricted_asset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            audit = Path(directory) / "audit.jsonl"
            supervisor = CommitteeSupervisor(audit)
            result = supervisor.run(portfolio(), Mandate(), approve)
            self.assertEqual(result.status, "APPROVED")
            self.assertNotIn("SANCTIONED_OIL", result.final.weights)
            self.assertTrue(any(m.message_type is MessageType.VETO for m in supervisor.board.messages))
            self.assertEqual(audit.read_text().count("\n"), result.message_count)

    def test_human_gate_blocks_and_rolls_back_without_approval(self) -> None:
        result = CommitteeSupervisor().run(
            portfolio(), Mandate(human_approval_threshold=.05)
        )
        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(result.final.weights, result.initial.weights)

    def test_approval_is_bound_to_exact_proposal_hash(self) -> None:
        def stale_approval(_: str, input_snapshot_hash: str) -> HumanApproval:
            return HumanApproval(
                approver="chair@example.com",
                proposal_hash=stable_hash({"older": "proposal"}),
                input_snapshot_hash=input_snapshot_hash,
                approved=True,
                rationale="Previously approved.",
            )

        supervisor = CommitteeSupervisor()
        result = supervisor.run(portfolio(), Mandate(), stale_approval)
        self.assertEqual(result.status, "BLOCKED")
        approval = next(
            m for m in supervisor.board.messages
            if m.message_type is MessageType.APPROVAL
        )
        self.assertFalse(approval.payload["hash_matches"])

    def test_approval_is_bound_to_input_snapshot_hash(self) -> None:
        def stale_input_approval(proposal_hash: str, _: str) -> HumanApproval:
            return HumanApproval(
                approver="chair@example.com",
                proposal_hash=proposal_hash,
                input_snapshot_hash=stable_hash({"older": "snapshot"}),
                approved=True,
                rationale="Previously approved for different inputs.",
            )

        supervisor = CommitteeSupervisor()
        result = supervisor.run(portfolio(), Mandate(), stale_input_approval)
        self.assertEqual(result.status, "BLOCKED")
        approval = next(
            m for m in supervisor.board.messages
            if m.message_type is MessageType.APPROVAL
        )
        self.assertFalse(approval.payload["input_snapshot_matches"])

    def test_stale_approval_is_rejected(self) -> None:
        def stale_approval(proposal_hash: str, input_snapshot_hash: str) -> HumanApproval:
            return HumanApproval(
                approver="chair@example.com",
                proposal_hash=proposal_hash,
                input_snapshot_hash=input_snapshot_hash,
                approved=True,
                rationale="Approval is too old.",
                timestamp=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
            )

        result = CommitteeSupervisor().run(portfolio(), Mandate(), stale_approval)
        self.assertEqual(result.status, "BLOCKED")

    def test_approval_cannot_be_replayed(self) -> None:
        supervisor = CommitteeSupervisor()
        issued: HumanApproval | None = None

        def reused_approval(proposal_hash: str, input_snapshot_hash: str) -> HumanApproval:
            nonlocal issued
            if issued is None:
                issued = approve(proposal_hash, input_snapshot_hash)
            return issued

        self.assertEqual(
            supervisor.run(portfolio(), Mandate(), reused_approval).status,
            "APPROVED",
        )
        self.assertEqual(
            supervisor.run(portfolio(), Mandate(), reused_approval).status,
            "BLOCKED",
        )

    def test_approval_cannot_be_replayed_after_supervisor_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            audit = Path(directory) / "audit.jsonl"
            issued: HumanApproval | None = None

            def reused_approval(proposal_hash: str, input_snapshot_hash: str) -> HumanApproval:
                nonlocal issued
                if issued is None:
                    issued = approve(proposal_hash, input_snapshot_hash)
                return issued

            self.assertEqual(
                CommitteeSupervisor(audit).run(portfolio(), Mandate(), reused_approval).status,
                "APPROVED",
            )
            self.assertEqual(
                CommitteeSupervisor(audit).run(portfolio(), Mandate(), reused_approval).status,
                "BLOCKED",
            )

    def test_risk_breach_blocks_even_with_human_approval(self) -> None:
        result = CommitteeSupervisor().run(
            portfolio(), Mandate(max_sector_weight=.16), approve
        )
        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(result.final.weights, result.initial.weights)

    def test_weights_remain_balanced(self) -> None:
        result = CommitteeSupervisor().run(portfolio(), Mandate(), approve)
        self.assertEqual(round(sum(result.final.weights.values()), 6), 1.0)

    def test_invalid_recommendation_blocks_without_crashing(self) -> None:
        supervisor = CommitteeSupervisor()

        def malformed_recommendation(correlation_id: str) -> Message:
            return Message(
                sender="sector",
                recipients=("portfolio_manager", "risk", "compliance"),
                message_type=MessageType.RECOMMENDATION,
                subject="Malformed trade list",
                payload={
                    "thesis": "Malformed",
                    "trades": [{"asset": "TECH_ETF", "sector": "Technology"}],
                    "confidence": 0.5,
                },
                correlation_id=correlation_id,
            )

        supervisor.sector.recommend = malformed_recommendation  # type: ignore[method-assign]
        result = supervisor.run(portfolio(), Mandate(), approve)
        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(result.final.weights, result.initial.weights)
        escalation = supervisor.board.messages[-1]
        self.assertEqual(escalation.message_type, MessageType.ESCALATION)
        self.assertEqual(escalation.payload["reason"], "invalid_sector_response")

    def test_unavailable_control_agents_fail_closed(self) -> None:
        failures = ("macro", "sector", "risk", "compliance")
        for agent_name in failures:
            with self.subTest(agent_name=agent_name):
                supervisor = CommitteeSupervisor()

                def unavailable(*_: object) -> Message:
                    raise RuntimeError(f"{agent_name} offline")

                if agent_name == "macro":
                    supervisor.macro.assess = unavailable  # type: ignore[method-assign]
                elif agent_name == "sector":
                    supervisor.sector.recommend = unavailable  # type: ignore[method-assign]
                elif agent_name == "risk":
                    supervisor.risk.review = unavailable  # type: ignore[method-assign]
                else:
                    supervisor.compliance.review = unavailable  # type: ignore[method-assign]

                result = supervisor.run(portfolio(), Mandate(), approve)
                self.assertEqual(result.status, "BLOCKED")
                self.assertEqual(result.final.weights, result.initial.weights)
                self.assertTrue(
                    any(
                        m.message_type is MessageType.ESCALATION
                        and m.payload["reason"] == f"{agent_name}_unavailable"
                        for m in supervisor.board.messages
                    )
                )

    def test_unbalanced_trade_application_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "sum to 1.0"):
            portfolio().apply([Trade("ENERGY_ETF", .01, "Energy")])

    def test_portfolio_requires_sector_for_each_holding(self) -> None:
        with self.assertRaisesRegex(ValueError, "sector is required"):
            Portfolio({"TECH_ETF": 1.0}, {})

    def test_portfolio_manager_rejects_malformed_trade_payload(self) -> None:
        message = Message(
            sender="sector",
            recipients=("portfolio_manager",),
            message_type=MessageType.RECOMMENDATION,
            subject="Bad trade",
            payload={
                "thesis": "Bad trade",
                "trades": [{"asset": "TECH_ETF", "sector": "Technology"}],
                "confidence": 0.5,
            },
            correlation_id="run-1",
        )
        with self.assertRaisesRegex(ValueError, "missing fields"):
            PortfolioManagerAgent().parse_trades(message)

    def test_random_balanced_trades_preserve_portfolio_invariants(self) -> None:
        rng = Random(42)
        assets = ("TECH_ETF", "BOND_ETF", "ENERGY_ETF", "HEALTH_ETF", "CASH")
        sectors = portfolio().sectors
        for _ in range(100):
            current = portfolio()
            trades: list[Trade] = []
            for _ in range(rng.randint(1, 6)):
                source = rng.choice([asset for asset, weight in current.weights.items() if weight > 0.02])
                target = rng.choice([asset for asset in assets if asset != source])
                amount = round(rng.uniform(0.001, min(0.02, current.weights[source])), 6)
                trades.extend([
                    Trade(source, -amount, sectors[source]),
                    Trade(target, amount, sectors[target]),
                ])
                current = current.apply(trades[-2:])

            updated = portfolio().apply(trades)
            self.assertEqual(round(sum(updated.weights.values()), 6), 1.0)
            self.assertTrue(all(weight >= 0 for weight in updated.weights.values()))
            self.assertEqual(set(updated.weights), set(updated.sectors))
            self.assertTrue(all(updated.sectors[asset] == sectors[asset] for asset in updated.weights))


class BlackboardContractTests(unittest.TestCase):
    def message(self, **changes: object) -> Message:
        values = {
            "sender": "macro",
            "recipients": ("portfolio_manager",),
            "message_type": MessageType.OBSERVATION,
            "subject": "Scenario",
            "payload": {"inflation": 0.03, "rate_outlook": "stable", "confidence": 0.6},
            "correlation_id": "run-1",
        }
        values.update(changes)
        return Message(**values)  # type: ignore[arg-type]

    def test_rejects_malformed_or_unauthorized_messages(self) -> None:
        board = Blackboard()
        with self.assertRaisesRegex(ValueError, "correlation_id"):
            board.publish(self.message(correlation_id=""))
        with self.assertRaisesRegex(ValueError, "unknown sender"):
            board.publish(self.message(sender="intruder"))
        with self.assertRaisesRegex(ValueError, "unknown recipients"):
            board.publish(self.message(recipients=("brokerage",)))
        with self.assertRaisesRegex(ValueError, "cannot publish decision"):
            board.publish(self.message(message_type=MessageType.DECISION))

    def test_routes_only_to_declared_recipient_inboxes(self) -> None:
        board = Blackboard()
        message = self.message(recipients=("portfolio_manager", "risk"))
        board.publish(message)
        self.assertEqual(board.for_recipient("portfolio_manager"), [message])
        self.assertEqual(board.for_recipient("risk"), [message])
        self.assertEqual(board.for_recipient("compliance"), [])

    def test_audit_persists_across_blackboard_instances(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            audit = Path(directory) / "audit.jsonl"
            Blackboard(audit).publish(self.message(correlation_id="run-1"))
            Blackboard(audit).publish(self.message(correlation_id="run-2"))
            self.assertEqual(audit.read_text().count("\n"), 2)

    def test_audit_hash_chain_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            audit = Path(directory) / "audit.jsonl"
            Blackboard(audit).publish(self.message(correlation_id="run-1"))
            tampered = audit.read_text().replace("stable", "tampered")
            audit.write_text(tampered, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                Blackboard(audit)

    def test_rejects_malformed_payload_for_key_message_types(self) -> None:
        board = Blackboard()
        with self.assertRaisesRegex(ValueError, "payload missing fields"):
            board.publish(Message(
                sender="risk",
                recipients=("supervisor",),
                message_type=MessageType.OBSERVATION,
                subject="Bad risk",
                payload={"gross_turnover": 0.1},
                correlation_id="run-1",
            ))
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            board.publish(Message(
                sender="supervisor",
                recipients=("human_chair", "audit"),
                message_type=MessageType.DECISION,
                subject="Bad decision",
                payload={
                    "human_approved": True,
                    "approval_id": "approval-1",
                    "proposal_hash": "not-a-hash",
                    "input_snapshot_hash": stable_hash({"input": "ok"}),
                    "final_weights": {"CASH": 1.0},
                },
                correlation_id="run-2",
            ))


if __name__ == "__main__":
    unittest.main()
