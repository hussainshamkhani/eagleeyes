from google.adk.tools import FunctionTool
from db.mongo import mongo_client
from core.constants import REGULATION_REFERENCES
import asyncio
from contextlib import asynccontextmanager

# Module-level batch cache — populated during batch runs, cleared after
_batch_cache: dict = {}
_cache_enabled: bool = False


def enable_batch_cache():
    """Call this at the start of a batch evaluation run."""
    global _cache_enabled, _batch_cache
    _batch_cache = {}
    _cache_enabled = True


def disable_batch_cache():
    """Call this at the end of a batch evaluation run to free memory."""
    global _cache_enabled, _batch_cache
    _cache_enabled = False
    _batch_cache = {}


def _cache_key(fn_name: str, *args) -> str:
    return f"{fn_name}:{':'.join(str(a) for a in args)}"


async def get_sender_transaction_history(sender_id: str, days: int = 30) -> dict:
    """
    Retrieve all transactions for a sender in the last N days.
    Returns total count, total KD amount, and transaction list.
    Use this to assess transaction velocity and behavioral patterns.
    """
    key = _cache_key("sender_history", sender_id, days)
    if _cache_enabled and key in _batch_cache:
        return _batch_cache[key]

    transactions = await mongo_client.get_transactions_by_sender(sender_id, days)
    total_kd = sum(t.get("amount_kd", 0) for t in transactions)
    result = {
        "transaction_count": len(transactions),
        "total_amount_kd": total_kd,
        "transactions": transactions[:10],
    }

    if _cache_enabled:
        _batch_cache[key] = result
    return result


async def get_recipient_network(acc_number: str, days: int = 30) -> dict:
    """
    Find all senders who sent to the same recipient account within N days.
    Returns sender list, combined total, and any shared phone/address identifiers.
    Use this to detect structuring rings and coordinated sender networks.
    """
    key = _cache_key("recipient_network", acc_number, days)
    if _cache_enabled and key in _batch_cache:
        return _batch_cache[key]

    transactions = await mongo_client.get_transactions_by_recipient(acc_number, days)
    unique_senders = list({t["sender_id"] for t in transactions})
    total_kd = sum(t.get("amount_kd", 0) for t in transactions)
    result = {
        "unique_sender_count": len(unique_senders),
        "sender_ids": unique_senders,
        "combined_amount_kd": total_kd,
        "transaction_count": len(transactions),
    }

    if _cache_enabled:
        _batch_cache[key] = result
    return result


async def get_sender_alert_history(sender_id: str, days: int = 30) -> dict:
    """
    Retrieve prior alerts for a sender to assess recidivist behavior.
    Returns alert count, risk levels, and triggered rules from prior alerts.
    """
    key = _cache_key("alert_history", sender_id, days)
    if _cache_enabled and key in _batch_cache:
        return _batch_cache[key]

    alerts = await mongo_client.get_alerts_by_sender(sender_id, days)
    result = {
        "alert_count": len(alerts),
        "alerts": [
            {
                "alert_id": a.get("alert_id"),
                "risk_level": a.get("risk_score", {}).get("risk_level"),
                "rules_triggered": [r.get("rule_id") for r in a.get("risk_score", {}).get("rules_triggered", [])],
                "created_at": str(a.get("created_at")),
                "status": a.get("status"),
            }
            for a in alerts
        ],
    }

    if _cache_enabled:
        _batch_cache[key] = result
    return result


async def get_sender_annual_total(sender_id: str) -> dict:
    """
    Return the total KD amount remitted by a sender in the current calendar year.
    Use this to check against declared yearly income for income mismatch detection.
    """
    key = _cache_key("annual_total", sender_id)
    if _cache_enabled and key in _batch_cache:
        return _batch_cache[key]

    from datetime import datetime
    start_of_year = datetime(datetime.utcnow().year, 1, 1)
    transactions = await mongo_client.get_transactions_by_date_range(start_of_year, datetime.utcnow())
    sender_txns = [t for t in transactions if t.get("sender_id") == sender_id]
    annual_total = sum(t.get("amount_kd", 0) for t in sender_txns)

    monthly = {}
    for t in sender_txns:
        month = t.get("date", "").strftime("%Y-%m") if hasattr(t.get("date"), "strftime") else str(t.get("date", ""))[:7]
        monthly[month] = monthly.get(month, 0) + t.get("amount_kd", 0)

    result = {
        "annual_total_kd": annual_total,
        "monthly_totals": monthly,
        "transaction_count": len(sender_txns),
    }

    if _cache_enabled:
        _batch_cache[key] = result
    return result


def get_applicable_regulations(rule_ids: list[str]) -> dict:
    """
    Return the CBK/FATF regulation references for a list of triggered rule IDs.
    Use this to cite applicable regulations in the compliance narrative.
    """
    return {
        rule_id: REGULATION_REFERENCES.get(rule_id, "CBK AML/CFT General Guidelines")
        for rule_id in rule_ids
    }
