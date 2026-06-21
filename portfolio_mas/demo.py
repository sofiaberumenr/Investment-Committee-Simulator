from pathlib import Path

from .models import HumanApproval, Mandate, Portfolio
from .supervisor import CommitteeSupervisor


def main() -> None:
    initial = Portfolio(
        weights={"TECH_ETF": 0.20, "BOND_ETF": 0.30, "ENERGY_ETF": 0.10, "HEALTH_ETF": 0.25, "CASH": 0.15},
        sectors={"TECH_ETF": "Technology", "BOND_ETF": "Fixed Income", "ENERGY_ETF": "Energy", "HEALTH_ETF": "Healthcare", "CASH": "Cash"},
    )
    audit = Path("runs/latest-audit.jsonl")

    def chair_approval(proposal_hash: str, input_snapshot_hash: str) -> HumanApproval:
        return HumanApproval(
            approver="committee-chair@example.com",
            proposal_hash=proposal_hash,
            input_snapshot_hash=input_snapshot_hash,
            approved=True,
            rationale="Controls cleared; rebalance is within the approved risk budget.",
        )

    try:
        supervisor = CommitteeSupervisor(audit)
    except ValueError as exc:
        audit = Path("runs/latest-audit-hash-chained.jsonl")
        print(f"Existing audit log is not chain-valid ({exc}); writing demo audit to {audit}")
        supervisor = CommitteeSupervisor(audit)

    result = supervisor.run(initial, Mandate(), chair_approval)
    print(f"Status: {result.status}")
    print(f"Initial: {result.initial.weights}")
    print(f"Unsafe proposal: {result.proposed.weights}")
    print(f"Final after veto/review: {result.final.weights}")
    print(f"Messages: {result.message_count}; audit: {audit}")


if __name__ == "__main__":
    main()
