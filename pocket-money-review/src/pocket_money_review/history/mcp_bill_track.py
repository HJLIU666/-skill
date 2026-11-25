"""A dependency-free local history store for pocket-money reviews.

The module exposes two layers:

* ``McpBillTrackStore`` performs local JSON reads and writes.
* ``McpBillTrackClient`` provides the async tool-shaped interface used by the
  review orchestrator.

No bank, payment account, network, or remote history service is accessed.
Amounts are serialized as strings and calculated with ``Decimal``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, Mapping

HistoryOperationStatus = Literal[
    "has-records",
    "no-records",
    "unavailable",
    "query-failed",
    "write-succeeded",
    "write-rejected",
    "write-failed",
]
TransactionType = Literal["income", "expense"]
_SENSITIVE_TEXT = re.compile(
    r"(?:密码|验证码|银行卡密码|账户令牌|password|passcode|verification\s*code|token|otp)",
    re.IGNORECASE,
)


class BillTrackError(Exception):
    """Base error for local bill-track operations."""


class BillTrackFormatError(BillTrackError):
    """The local JSON file does not have the expected structure."""


class BillTrackValidationError(BillTrackError):
    """A caller supplied a value that cannot be stored safely."""


def _decimal(value: Decimal | int | float | str) -> Decimal:
    """Convert a value to a finite Decimal without binary-float arithmetic."""

    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise BillTrackValidationError(f"invalid decimal value: {value!r}") from exc
    if not result.is_finite():
        raise BillTrackValidationError("amount must be finite")
    return result


def _date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise BillTrackValidationError(f"invalid ISO date: {value!r}") from exc


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _period_mapping(value: Mapping[str, Any]) -> "ReviewPeriod":
    start = value.get("start")
    end = value.get("end")
    if start is None or end is None:
        raise BillTrackValidationError("review period requires start and end")
    return ReviewPeriod(_date(start), _date(end))


@dataclass(frozen=True)
class ReviewPeriod:
    """An explicitly selected review date range."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise BillTrackValidationError("review period start must not exceed end")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ReviewPeriod":
        return _period_mapping(value)

    def to_dict(self) -> dict[str, str]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}


@dataclass(frozen=True)
class HistoryRecord:
    """A confirmed period summary, never an individual expense record."""

    history_id: str
    review_period: ReviewPeriod
    currency: str
    total_spending: Decimal
    category_amounts: dict[str, Decimal]
    category_rule_version: str
    confirmation_state: Literal["confirmed"] = "confirmed"
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.history_id,
            "period": self.review_period.to_dict(),
            "currency": self.currency,
            "total_spending": str(self.total_spending),
            "categories": {
                name: str(amount) for name, amount in self.category_amounts.items()
            },
            "category_rule_version": self.category_rule_version,
            "confirmation_state": self.confirmation_state,
            "created_at": self.created_at or _now_iso(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HistoryRecord":
        categories = value.get("categories", {})
        if not isinstance(categories, Mapping):
            raise BillTrackFormatError("history categories must be an object")
        confirmation_state = value.get("confirmation_state")
        if confirmation_state != "confirmed":
            raise BillTrackFormatError("only confirmed history summaries are readable")
        return cls(
            history_id=str(value["id"]),
            review_period=ReviewPeriod.from_mapping(value["period"]),
            currency=str(value["currency"]),
            total_spending=_decimal(value["total_spending"]),
            category_amounts={
                str(name): _decimal(amount) for name, amount in categories.items()
            },
            category_rule_version=str(value.get("category_rule_version", "unknown")),
            confirmation_state="confirmed",
            created_at=str(value.get("created_at", "")),
        )

    def source(self) -> dict[str, Any]:
        return {
            "local_json_record_id": self.history_id,
            "generated_period": self.review_period.to_dict(),
            "data_confirmation_state": self.confirmation_state,
        }


@dataclass(frozen=True)
class HistoryQueryResult:
    status: HistoryOperationStatus
    records: tuple[HistoryRecord, ...] = ()
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "records": [
                {
                    **record.to_dict(),
                    "history_data_source": record.source(),
                    "history_operation_status": self.status,
                }
                for record in self.records
            ],
            "message": self.message,
        }


@dataclass(frozen=True)
class OperationResult:
    status: HistoryOperationStatus
    message: str
    record_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "record_id": self.record_id,
        }


class McpBillTrackStore:
    """Store transactions and review summaries in one local JSON file."""

    def __init__(self, path: str | Path | None = None) -> None:
        configured_path = path or os.environ.get(
            "POCKET_MONEY_BILL_TRACK_FILE", "data/bill_track.json"
        )
        self.path = Path(configured_path).expanduser()
        self._lock = threading.RLock()

    def _empty_database(self) -> dict[str, Any]:
        return {"version": 1, "transactions": [], "reviews": []}

    def _load_database(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty_database()
        try:
            parsed = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BillTrackFormatError(f"cannot read local JSON: {self.path}") from exc
        if not isinstance(parsed, dict):
            raise BillTrackFormatError("local JSON root must be an object")
        if not isinstance(parsed.get("transactions", []), list):
            raise BillTrackFormatError("transactions must be an array")
        if not isinstance(parsed.get("reviews", []), list):
            raise BillTrackFormatError("reviews must be an array")
        parsed.setdefault("version", 1)
        return parsed

    def _save_database(self, database: Mapping[str, Any]) -> None:
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = temporary.name
                json.dump(database, temporary, ensure_ascii=False, indent=2)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
        except OSError as exc:
            raise BillTrackError(f"cannot write local JSON: {self.path}") from exc
        finally:
            if temporary_path:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass

    def query_history(
        self,
        period: ReviewPeriod,
        currency: str,
    ) -> HistoryQueryResult:
        """Return earlier summaries with the same currency.

        The current period is not included as a historical comparison. The
        caller still decides whether a returned record is comparable after
        showing its source and asking for history-use confirmation.
        """

        try:
            with self._lock:
                database = self._load_database()
                records: list[HistoryRecord] = []
                for raw in database["reviews"]:
                    try:
                        record = HistoryRecord.from_dict(raw)
                    except (KeyError, TypeError, ValueError, BillTrackError):
                        continue
                    if (
                        record.currency == currency
                        and record.review_period.end < period.start
                    ):
                        records.append(record)
                records.sort(key=lambda item: item.review_period.end, reverse=True)
                if not records:
                    return HistoryQueryResult("no-records", message="no matching history")
                return HistoryQueryResult("has-records", tuple(records))
        except BillTrackFormatError as exc:
            return HistoryQueryResult("query-failed", message=str(exc))
        except OSError as exc:
            return HistoryQueryResult("unavailable", message=str(exc))

    def record_transaction(
        self,
        *,
        amount: Decimal | int | float | str,
        transaction_type: TransactionType,
        category: str,
        occurred_on: date | str,
        description: str = "",
        confirmation_state: str = "confirmed",
    ) -> OperationResult:
        """Record one confirmed income or expense transaction."""

        if confirmation_state != "confirmed":
            return OperationResult("write-rejected", "transaction is not confirmed")
        value = _decimal(amount)
        if value <= 0:
            raise BillTrackValidationError("transaction amount must be positive")
        if transaction_type not in ("income", "expense"):
            raise BillTrackValidationError("transaction_type must be income or expense")
        if not category.strip():
            raise BillTrackValidationError("transaction category must not be empty")
        if _SENSITIVE_TEXT.search(description) or _SENSITIVE_TEXT.search(category):
            return OperationResult(
                "write-rejected",
                "sensitive credential text was not stored",
            )
        record_id = f"tx-{uuid.uuid4().hex}"
        record = {
            "id": record_id,
            "amount": str(value),
            "type": transaction_type,
            "category": category.strip(),
            "occurred_on": _date(occurred_on).isoformat(),
            "description": description,
            "confirmation_state": "confirmed",
        }
        with self._lock:
            database = self._load_database()
            database["transactions"].append(record)
            self._save_database(database)
        return OperationResult("write-succeeded", "transaction saved locally", record_id)

    def record_review_summary(
        self,
        *,
        period: ReviewPeriod,
        currency: str,
        total_spending: Decimal | int | float | str,
        category_amounts: Mapping[str, Decimal | int | float | str],
        category_rule_version: str,
        history_write_confirmation: bool,
        confirmation_state: str = "confirmed",
    ) -> OperationResult:
        """Save only the confirmed summary used for future comparisons."""

        if not history_write_confirmation:
            return OperationResult("write-rejected", "history write was not confirmed")
        if confirmation_state != "confirmed":
            return OperationResult("write-rejected", "review summary is not confirmed")
        if not currency.strip():
            raise BillTrackValidationError("currency must not be empty")
        if not category_rule_version.strip():
            raise BillTrackValidationError("category rule version must not be empty")
        total = _decimal(total_spending)
        categories = {
            str(name): _decimal(amount) for name, amount in category_amounts.items()
        }
        if any(not name.strip() for name in categories):
            raise BillTrackValidationError("category names must not be empty")
        if sum(categories.values(), Decimal("0")) != total:
            raise BillTrackValidationError(
                "category amounts must equal total_spending before saving"
            )
        record = HistoryRecord(
            history_id=f"review-{uuid.uuid4().hex}",
            review_period=period,
            currency=currency.strip(),
            total_spending=total,
            category_amounts=categories,
            category_rule_version=category_rule_version.strip(),
            created_at=_now_iso(),
        )
        with self._lock:
            database = self._load_database()
            database["reviews"].append(record.to_dict())
            self._save_database(database)
        return OperationResult("write-succeeded", "review summary saved locally", record.history_id)

    def get_balance(
        self,
        *,
        period: ReviewPeriod | None = None,
    ) -> Decimal:
        """Calculate income minus expense from confirmed transactions."""

        with self._lock:
            database = self._load_database()
        balance = Decimal("0")
        for raw in database["transactions"]:
            if raw.get("confirmation_state") != "confirmed":
                continue
            occurred_on = _date(raw["occurred_on"])
            if period and not (period.start <= occurred_on <= period.end):
                continue
            amount = _decimal(raw["amount"])
            balance += amount if raw.get("type") == "income" else -amount
        return balance

    def format_report(self, *, period: ReviewPeriod | None = None) -> str:
        """Return a small local report without exposing raw credentials."""

        with self._lock:
            database = self._load_database()
        transactions = []
        for raw in database["transactions"]:
            if raw.get("confirmation_state") != "confirmed":
                continue
            occurred_on = _date(raw["occurred_on"])
            if period and not (period.start <= occurred_on <= period.end):
                continue
            transactions.append(raw)
        income = sum(
            (_decimal(item["amount"]) for item in transactions if item["type"] == "income"),
            Decimal("0"),
        )
        expenses = sum(
            (_decimal(item["amount"]) for item in transactions if item["type"] == "expense"),
            Decimal("0"),
        )
        lines = [
            "# Bill Track Report",
            "",
            f"- Transactions: {len(transactions)}",
            f"- Income: {income}",
            f"- Expenses: {expenses}",
            f"- Balance: {income - expenses}",
        ]
        return "\n".join(lines)


class McpBillTrackClient:
    """Async tool-shaped facade used by ``PocketMoneyReviewSystem``."""

    def __init__(self, store: McpBillTrackStore) -> None:
        self.store = store

    async def query_history(self, query: Mapping[str, Any]) -> dict[str, Any]:
        privacy_scope = query.get("privacy_scope", query.get("privacyScope"))
        if privacy_scope not in (None, "local-json-only"):
            return HistoryQueryResult(
                "unavailable", message="only local-json-only scope is supported"
            ).to_dict()
        raw_period = query.get("review_period", query.get("reviewPeriod"))
        if not isinstance(raw_period, Mapping):
            return HistoryQueryResult(
                "query-failed", message="review_period is required"
            ).to_dict()
        try:
            result = self.store.query_history(
                ReviewPeriod.from_mapping(raw_period),
                str(query["currency"]),
            )
            return result.to_dict()
        except (KeyError, TypeError, BillTrackError) as exc:
            return HistoryQueryResult("query-failed", message=str(exc)).to_dict()

    async def record_transaction(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Handle both raw transactions and confirmed review-summary writes."""

        privacy_scope = payload.get("privacy_scope", payload.get("privacyScope"))
        if privacy_scope not in (None, "local-json-only"):
            return OperationResult(
                "write-rejected", "only local-json-only scope is supported"
            ).to_dict()
        try:
            if payload.get("record_type") == "review_summary":
                raw_period = payload.get("review_period", payload.get("reviewPeriod"))
                categories = payload.get("category_amounts", payload.get("categories", {}))
                if not isinstance(raw_period, Mapping) or not isinstance(categories, Mapping):
                    raise BillTrackValidationError("review summary payload is incomplete")
                result = self.store.record_review_summary(
                    period=ReviewPeriod.from_mapping(raw_period),
                    currency=str(payload["currency"]),
                    total_spending=payload["total_spending"],
                    category_amounts=categories,
                    category_rule_version=str(
                        payload.get("category_rule_version", "unknown")
                    ),
                    history_write_confirmation=bool(
                        payload.get(
                            "history_write_confirmation",
                            payload.get("historyWriteConfirmation", False),
                        )
                    ),
                    confirmation_state=str(payload.get("confirmation_state", "confirmed")),
                )
            else:
                result = self.store.record_transaction(
                    amount=payload["amount"],
                    transaction_type=str(payload["transaction_type"]),
                    category=str(payload["category"]),
                    occurred_on=payload["occurred_on"],
                    description=str(payload.get("description", "")),
                    confirmation_state=str(payload.get("confirmation_state", "confirmed")),
                )
            return result.to_dict()
        except (KeyError, TypeError, BillTrackError) as exc:
            return OperationResult("write-failed", str(exc)).to_dict()

    async def get_balance(self, period: Mapping[str, Any] | None = None) -> str:
        parsed_period = ReviewPeriod.from_mapping(period) if period else None
        return str(self.store.get_balance(period=parsed_period))

    async def format_report(self, period: Mapping[str, Any] | None = None) -> str:
        parsed_period = ReviewPeriod.from_mapping(period) if period else None
        return self.store.format_report(period=parsed_period)


def _categories(value: list[str]) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    for item in value:
        if "=" not in item:
            raise argparse.ArgumentTypeError("category must use NAME=AMOUNT")
        name, amount = item.split("=", 1)
        result[name.strip()] = _decimal(amount)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local JSON pocket-money bill tracker")
    parser.add_argument("--file", type=Path, default=None, help="local JSON path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    record = subparsers.add_parser("record", help="record one income or expense")
    record.add_argument("--amount", required=True)
    record.add_argument("--type", choices=("income", "expense"), required=True)
    record.add_argument("--category", required=True)
    record.add_argument("--date", required=True)
    record.add_argument("--description", default="")

    summary = subparsers.add_parser("summary", help="save a confirmed review summary")
    summary.add_argument("--start", required=True)
    summary.add_argument("--end", required=True)
    summary.add_argument("--currency", required=True)
    summary.add_argument("--total", required=True)
    summary.add_argument("--category", action="append", default=[])
    summary.add_argument("--category-rule", required=True)
    summary.add_argument("--confirm", action="store_true")

    balance = subparsers.add_parser("balance", help="show transaction balance")
    balance.add_argument("--start")
    balance.add_argument("--end")

    report = subparsers.add_parser("report", help="format a local transaction report")
    report.add_argument("--start")
    report.add_argument("--end")

    args = parser.parse_args(argv)
    store = McpBillTrackStore(args.file)
    if args.command == "record":
        result = store.record_transaction(
            amount=args.amount,
            transaction_type=args.type,
            category=args.category,
            occurred_on=args.date,
            description=args.description,
        )
        print(json.dumps(result.to_dict(), ensure_ascii=False))
    elif args.command == "summary":
        result = store.record_review_summary(
            period=ReviewPeriod(_date(args.start), _date(args.end)),
            currency=args.currency,
            total_spending=args.total,
            category_amounts=_categories(args.category),
            category_rule_version=args.category_rule,
            history_write_confirmation=args.confirm,
        )
        print(json.dumps(result.to_dict(), ensure_ascii=False))
    elif args.command in ("balance", "report"):
        period = None
        if args.start or args.end:
            if not args.start or not args.end:
                parser.error("--start and --end must be provided together")
            period = ReviewPeriod(_date(args.start), _date(args.end))
        if args.command == "balance":
            print(store.get_balance(period=period))
        else:
            print(store.format_report(period=period))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
