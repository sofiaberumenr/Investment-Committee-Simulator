from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha256
from math import isfinite
from pathlib import Path
from typing import Any

from .models import Message, MessageType, Severity, Trade


class Blackboard:
    """Append-only shared state; agents communicate through typed messages."""

    ALLOWED_PARTICIPANTS = {
        "macro", "sector", "risk", "compliance", "portfolio_manager",
        "supervisor", "human_chair", "audit",
    }
    SENDER_MESSAGE_TYPES = {
        "macro": {MessageType.OBSERVATION},
        "sector": {MessageType.RECOMMENDATION},
        "risk": {MessageType.OBSERVATION, MessageType.CHALLENGE},
        "compliance": {MessageType.OBSERVATION, MessageType.VETO},
        "portfolio_manager": {MessageType.RECOMMENDATION},
        "supervisor": {MessageType.ESCALATION, MessageType.DECISION},
        "human_chair": {MessageType.APPROVAL},
        "audit": set(),
    }
    EMPTY_AUDIT_HASH = "0" * 64

    def __init__(self, audit_path: Path | None = None) -> None:
        self.messages: list[Message] = []
        self._ids: set[str] = set()
        self._audit_approval_ids: set[str] = set()
        self._last_audit_hash = self.EMPTY_AUDIT_HASH
        self._inboxes: dict[str, list[Message]] = {
            participant: [] for participant in self.ALLOWED_PARTICIPANTS
        }
        self.audit_path = audit_path
        if audit_path:
            audit_path.parent.mkdir(parents=True, exist_ok=True)
            audit_path.touch(exist_ok=True)
            self._load_audit_state(audit_path)

    def publish(self, message: Message) -> None:
        self._validate(message)
        self.messages.append(message)
        self._ids.add(message.id)
        for recipient in message.recipients:
            self._inboxes[recipient].append(message)
        if self.audit_path:
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(self._audit_record(message), sort_keys=True) + "\n")

    @property
    def audit_approval_ids(self) -> set[str]:
        return set(self._audit_approval_ids)

    def _load_audit_state(self, audit_path: Path) -> None:
        for line_number, raw_line in enumerate(audit_path.read_text(encoding="utf-8").splitlines(), 1):
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"malformed audit record on line {line_number}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"audit record on line {line_number} must be an object")

            self._verify_audit_record(record, line_number)
            self._last_audit_hash = record["audit_record_hash"]

            payload = record.get("payload")
            if record.get("message_type") == MessageType.APPROVAL.value and isinstance(payload, dict):
                approval_id = payload.get("approval_id")
                if isinstance(approval_id, str) and approval_id.strip():
                    self._audit_approval_ids.add(approval_id)

    def _audit_record(self, message: Message) -> dict[str, Any]:
        record = message.to_dict()
        previous_hash = self._last_audit_hash
        record["audit_previous_hash"] = previous_hash
        record["audit_record_hash"] = self._hash_record(record)
        self._last_audit_hash = record["audit_record_hash"]
        if message.message_type is MessageType.APPROVAL:
            approval_id = message.payload.get("approval_id")
            if isinstance(approval_id, str) and approval_id.strip():
                self._audit_approval_ids.add(approval_id)
        return record

    def _hash_record(self, record: dict[str, Any]) -> str:
        hash_input = {
            key: value
            for key, value in record.items()
            if key != "audit_record_hash"
        }
        canonical = json.dumps(hash_input, sort_keys=True, separators=(",", ":"))
        return sha256(canonical.encode()).hexdigest()

    def _verify_audit_record(self, record: dict[str, Any], line_number: int) -> None:
        previous_hash = record.get("audit_previous_hash")
        record_hash = record.get("audit_record_hash")
        if not self._is_sha256(previous_hash):
            raise ValueError(f"audit record on line {line_number} missing previous hash")
        if previous_hash != self._last_audit_hash:
            raise ValueError(f"audit hash chain broken on line {line_number}")
        if not self._is_sha256(record_hash):
            raise ValueError(f"audit record on line {line_number} missing record hash")
        if record_hash != self._hash_record(record):
            raise ValueError(f"audit record hash mismatch on line {line_number}")

    def _validate(self, message: Message) -> None:
        if message.id in self._ids:
            raise ValueError(f"duplicate message id: {message.id}")
        if message.sender not in self.ALLOWED_PARTICIPANTS:
            raise ValueError(f"unknown sender: {message.sender}")
        if not isinstance(message.message_type, MessageType):
            raise ValueError("message_type must be a MessageType")
        if message.message_type not in self.SENDER_MESSAGE_TYPES[message.sender]:
            raise ValueError(
                f"sender {message.sender} cannot publish {message.message_type.value}"
            )
        if not isinstance(message.severity, Severity):
            raise ValueError("severity must be a Severity")
        if not isinstance(message.payload, dict):
            raise ValueError("payload must be an object")
        if not message.recipients:
            raise ValueError("message must have at least one recipient")
        unknown = set(message.recipients) - self.ALLOWED_PARTICIPANTS
        if unknown:
            raise ValueError(f"unknown recipients: {sorted(unknown)}")
        if not message.correlation_id.strip():
            raise ValueError("correlation_id is required")
        if not message.subject.strip():
            raise ValueError("subject is required")
        try:
            parsed = datetime.fromisoformat(message.timestamp)
        except ValueError as exc:
            raise ValueError("timestamp must be ISO-8601") from exc
        if parsed.tzinfo is None:
            raise ValueError("timestamp must include a timezone")
        self._validate_payload(message)

    def _validate_payload(self, message: Message) -> None:
        if message.sender == "macro":
            self._require_fields(message.payload, {"inflation", "rate_outlook", "confidence"})
            self._require_number(message.payload["inflation"], "inflation")
            self._require_string(message.payload["rate_outlook"], "rate_outlook")
            self._require_probability(message.payload["confidence"], "confidence")
        elif message.sender in {"sector", "portfolio_manager"}:
            self._validate_trade_payload(message.payload)
            self._require_string(message.payload["thesis"], "thesis")
            self._require_probability(message.payload["confidence"], "confidence")
        elif message.sender == "risk":
            self._require_fields(message.payload, {"breaches", "gross_turnover", "sector_weights"})
            self._require_string_list(message.payload["breaches"], "breaches")
            self._require_probability(message.payload["gross_turnover"], "gross_turnover")
            self._require_weight_mapping(message.payload["sector_weights"], "sector_weights")
        elif message.sender == "compliance":
            self._require_fields(message.payload, {"restricted_assets", "rule_version"})
            self._require_string_list(message.payload["restricted_assets"], "restricted_assets")
            self._require_string(message.payload["rule_version"], "rule_version")
        elif message.sender == "human_chair":
            self._require_fields(
                message.payload,
                {
                    "approval_id",
                    "approver",
                    "approved",
                    "proposal_hash",
                    "rationale",
                    "approval_timestamp",
                    "hash_matches",
                    "fresh",
                    "replayed",
                    "input_snapshot_hash",
                    "input_snapshot_matches",
                },
            )
            self._require_string(message.payload["approval_id"], "approval_id")
            self._require_string(message.payload["approver"], "approver")
            self._require_string(message.payload["rationale"], "rationale")
            self._require_string(message.payload["approval_timestamp"], "approval_timestamp")
            self._require_bool_fields(
                message.payload,
                ("approved", "hash_matches", "fresh", "replayed", "input_snapshot_matches"),
            )
            self._require_hash(message.payload["proposal_hash"], "proposal_hash")
            self._require_hash(message.payload["input_snapshot_hash"], "input_snapshot_hash")
        elif message.sender == "supervisor":
            if message.message_type is MessageType.ESCALATION:
                self._require_fields(message.payload, {"reason", "rollback"})
                self._require_string(message.payload["reason"], "reason")
                if message.payload["rollback"] != "initial_portfolio":
                    raise ValueError("rollback must be initial_portfolio")
                if "error" in message.payload:
                    self._require_string(message.payload["error"], "error")
            elif message.message_type is MessageType.DECISION:
                self._require_fields(
                    message.payload,
                    {"human_approved", "approval_id", "proposal_hash", "input_snapshot_hash", "final_weights"},
                )
                if message.payload["human_approved"] is not None and not isinstance(message.payload["human_approved"], bool):
                    raise ValueError("human_approved must be a boolean or null")
                if message.payload["approval_id"] is not None:
                    self._require_string(message.payload["approval_id"], "approval_id")
                self._require_hash(message.payload["proposal_hash"], "proposal_hash")
                self._require_hash(message.payload["input_snapshot_hash"], "input_snapshot_hash")
                self._require_weight_mapping(message.payload["final_weights"], "final_weights")

    def _validate_trade_payload(self, payload: dict[str, Any]) -> None:
        self._require_fields(payload, {"thesis", "trades", "confidence"})
        trades = payload["trades"]
        if not isinstance(trades, list) or not trades:
            raise ValueError("trades must be a non-empty list")
        for index, trade in enumerate(trades):
            if not isinstance(trade, dict):
                raise ValueError(f"trade {index} must be an object")
            if set(trade) != {"asset", "delta_weight", "sector"}:
                raise ValueError(f"trade {index} must contain asset, delta_weight, and sector")
            Trade(trade["asset"], trade["delta_weight"], trade["sector"])

    def _require_fields(self, payload: dict[str, Any], required: set[str]) -> None:
        missing = required - set(payload)
        if missing:
            raise ValueError(f"payload missing fields: {sorted(missing)}")

    def _require_string(self, value: Any, field_name: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must be a non-empty string")

    def _require_hash(self, value: Any, field_name: str) -> None:
        self._require_string(value, field_name)
        if not self._is_sha256(value):
            raise ValueError(f"{field_name} must be a SHA-256 hex digest")

    def _is_sha256(self, value: Any) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(char in "0123456789abcdef" for char in value)
        )

    def _require_bool_fields(self, payload: dict[str, Any], fields: tuple[str, ...]) -> None:
        for field in fields:
            if not isinstance(payload[field], bool):
                raise ValueError(f"{field} must be a boolean")

    def _require_string_list(self, value: Any, field_name: str) -> None:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"{field_name} must be a list of strings")

    def _require_number(self, value: Any, field_name: str) -> None:
        if not isinstance(value, int | float) or isinstance(value, bool) or not isfinite(value):
            raise ValueError(f"{field_name} must be a finite number")

    def _require_probability(self, value: Any, field_name: str) -> None:
        self._require_number(value, field_name)
        if not 0 <= value <= 1:
            raise ValueError(f"{field_name} must be between 0 and 1")

    def _require_weight_mapping(self, value: Any, field_name: str) -> None:
        if not isinstance(value, dict) or not value:
            raise ValueError(f"{field_name} must be a non-empty mapping")
        for key, weight in value.items():
            self._require_string(key, f"{field_name} key")
            self._require_number(weight, f"{field_name}.{key}")

    def by_correlation(self, correlation_id: str) -> list[Message]:
        return [m for m in self.messages if m.correlation_id == correlation_id]

    def for_recipient(self, recipient: str) -> list[Message]:
        if recipient not in self.ALLOWED_PARTICIPANTS:
            raise ValueError(f"unknown recipient: {recipient}")
        return list(self._inboxes[recipient])
