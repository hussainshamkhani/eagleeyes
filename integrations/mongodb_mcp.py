from datetime import datetime, timedelta
from dateutil.parser import parse as parse_date
from db.mongo import mongo_client


class MongoDBMCPTools:

    async def query_sender_history(
        self,
        sender_id: str,
        days: int = 30
    ) -> dict:
        """
        MCP Tool: query_sender_history
        Retrieves all transactions for a given sender in the last N days.
        Use this to assess transaction velocity, total amounts, and behavioral patterns.
        
        Args:
            sender_id: The civil ID or passport number of the sender
            days: Number of days to look back (default: 30)
        
        Returns:
            dict with keys:
                - transactions: list of transaction records
                - total_amount_kd: sum of all amounts in KD
                - transaction_count: number of transactions
                - date_range: {"from": ..., "to": ...}
        """
        transactions = await mongo_client.get_transactions_by_sender(sender_id, days)
        total_kd = sum(t.get("amount_kd", 0) for t in transactions)
        
        now = datetime.utcnow()
        start_date = now - timedelta(days=days)
        
        return {
            "transactions": transactions,
            "total_amount_kd": total_kd,
            "transaction_count": len(transactions),
            "date_range": {
                "from": start_date.isoformat(),
                "to": now.isoformat()
            },
        }

    async def query_recipient_network(
        self,
        acc_number: str,
        days: int = 30
    ) -> dict:
        """
        MCP Tool: query_recipient_network
        Finds all senders who have sent to the same recipient account within a time window.
        Use this to detect structuring rings and coordinated sender networks.
        
        Args:
            acc_number: Recipient account number
            days: Time window in days (default: 30)
        
        Returns:
            dict with keys:
                - senders: list of unique sender IDs
                - total_amount_kd: combined total sent to this recipient
                - transaction_count: total number of transactions
                - shared_identifiers: list of shared phones/addresses found
        """
        transactions = await mongo_client.get_transactions_by_recipient(acc_number, days)
        unique_senders = list({t["sender_id"] for t in transactions if "sender_id" in t})
        total_kd = sum(t.get("amount_kd", 0) for t in transactions)
        
        # Look up sender profiles to check for shared phone numbers or addresses
        sender_details = []
        for s_id in unique_senders:
            c = await mongo_client.get_customer(s_id)
            if c:
                sender_details.append(c)

        shared_identifiers = []
        phones = {}
        addresses = {}
        for c in sender_details:
            ph = c.get("phone")
            addr = c.get("address")
            if ph:
                phones[ph] = phones.get(ph, 0) + 1
            if addr:
                addresses[addr] = addresses.get(addr, 0) + 1

        for ph, count in phones.items():
            if count > 1:
                shared_identifiers.append(f"Shared Phone: {ph} (used by {count} senders)")
        for addr, count in addresses.items():
            if count > 1:
                shared_identifiers.append(f"Shared Address: {addr} (used by {count} senders)")

        return {
            "senders": unique_senders,
            "total_amount_kd": total_kd,
            "transaction_count": len(transactions),
            "shared_identifiers": shared_identifiers,
        }

    async def query_alert_history(
        self,
        sender_id: str,
        days: int = 30
    ) -> dict:
        """
        MCP Tool: query_alert_history
        Retrieves prior alerts generated for a sender to assess recidivist behavior.
        
        Args:
            sender_id: The civil ID or passport number of the sender
            days: Look-back window in days (default: 30)
        
        Returns:
            dict with keys:
                - alert_count: number of prior alerts
                - alerts: list of alert records with risk levels and rule triggers
                - highest_risk_level: "HIGH" | "MEDIUM" | "LOW"
        """
        alerts = await mongo_client.get_alerts_by_sender(sender_id, days)
        
        level_map = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
        highest_num = 0
        highest_level = "LOW"
        for a in alerts:
            lvl = a.get("risk_score", {}).get("risk_level", "LOW")
            num = level_map.get(lvl, 0)
            if num > highest_num:
                highest_num = num
                highest_level = lvl

        return {
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
            "highest_risk_level": highest_level,
        }

    async def get_sender_annual_total(
        self,
        sender_id: str
    ) -> dict:
        """
        MCP Tool: get_sender_annual_total
        Returns the total amount remitted by a sender in the current calendar year.
        Use this to check against declared yearly income for income mismatch detection.
        
        Args:
            sender_id: The civil ID or passport number of the sender
        
        Returns:
            dict with keys:
                - annual_total_kd: total KD sent this calendar year
                - monthly_totals: dict of month -> total KD
                - transaction_count: total transactions this year
        """
        start_of_year = datetime(datetime.utcnow().year, 1, 1)
        transactions = await mongo_client.get_transactions_by_date_range(start_of_year, datetime.utcnow())
        sender_txns = [t for t in transactions if t.get("sender_id") == sender_id]
        annual_total = sum(t.get("amount_kd", 0) for t in sender_txns)

        monthly = {}
        for t in sender_txns:
            date_val = t.get("date")
            if hasattr(date_val, "strftime"):
                month = date_val.strftime("%Y-%m")
            else:
                month = str(date_val)[:7]
            monthly[month] = monthly.get(month, 0) + t.get("amount_kd", 0)

        return {
            "annual_total_kd": annual_total,
            "monthly_totals": monthly,
            "transaction_count": len(sender_txns),
        }

    async def store_alert(
        self,
        alert: dict
    ) -> dict:
        """
        MCP Tool: store_alert
        Persists a new alert to MongoDB after a transaction has been flagged.
        
        Args:
            alert: Complete alert record as a dict (matches Alert model schema)
        
        Returns:
            dict with keys:
                - success: bool
                - alert_id: the stored alert ID
        """
        alert_id = await mongo_client.insert_alert(alert)
        return {
            "success": alert_id is not None,
            "alert_id": alert_id,
        }

    async def update_alert_review(
        self,
        alert_id: str,
        status: str,
        reviewer: str,
        notes: str
    ) -> dict:
        """
        MCP Tool: update_alert_review
        Updates an alert after a compliance officer reviews it.
        Valid statuses: REVIEWED_CLEARED, REVIEWED_ESCALATED, STR_FILED
        
        Args:
            alert_id: The alert UUID
            status: New status string
            reviewer: Compliance officer name or ID
            notes: Review notes
        
        Returns:
            dict with keys:
                - success: bool
                - updated_at: timestamp
        """
        success = await mongo_client.update_alert_status(alert_id, status, reviewer, notes)
        return {
            "success": success,
            "updated_at": datetime.utcnow().isoformat(),
        }

    async def get_batch_statistics(
        self,
        since: str  # ISO datetime string
    ) -> dict:
        """
        MCP Tool: get_batch_statistics
        Returns evaluation and alert statistics since a given datetime.
        Used by the self-improvement loop to assess agent performance.
        
        Args:
            since: ISO 8601 datetime string marking the start of the batch
        
        Returns:
            dict with keys:
                - total_evaluated: int
                - total_flagged: int
                - flagged_by_risk_level: {"HIGH": n, "MEDIUM": n, "LOW": n}
                - cleared_by_reviewers: int
                - escalated_by_reviewers: int
                - str_filed: int
                - false_positive_rate: float (cleared / total_flagged)
        """
        since_dt = parse_date(since)
        
        # Evaluations count
        evals_count = await mongo_client.count_evaluations_since(since_dt)
        
        # Fetch alerts
        cursor = mongo_client.db.alerts.find({"created_at": {"$gte": since_dt}})
        alerts = await cursor.to_list(length=10000)
        
        total_flagged = len(alerts)
        
        by_risk = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
        cleared = 0
        escalated = 0
        str_filed = 0
        
        for a in alerts:
            lvl = a.get("risk_score", {}).get("risk_level", "LOW")
            by_risk[lvl] = by_risk.get(lvl, 0) + 1
            
            status = a.get("status")
            if status == "REVIEWED_CLEARED":
                cleared += 1
            elif status == "REVIEWED_ESCALATED":
                escalated += 1
            elif status == "STR_FILED":
                str_filed += 1

        false_positive_rate = float(cleared) / float(total_flagged) if total_flagged > 0 else 0.0

        return {
            "total_evaluated": evals_count,
            "total_flagged": total_flagged,
            "flagged_by_risk_level": by_risk,
            "cleared_by_reviewers": cleared,
            "escalated_by_reviewers": escalated,
            "str_filed": str_filed,
            "false_positive_rate": false_positive_rate,
        }


# MCP Server registration
# When running as an MCP server, this registers all tools above
# with the Google Cloud Agent Builder MCP endpoint.

mongodb_mcp_tools = MongoDBMCPTools()

MCP_TOOL_REGISTRY = {
    "query_sender_history": mongodb_mcp_tools.query_sender_history,
    "query_recipient_network": mongodb_mcp_tools.query_recipient_network,
    "query_alert_history": mongodb_mcp_tools.query_alert_history,
    "get_sender_annual_total": mongodb_mcp_tools.get_sender_annual_total,
    "store_alert": mongodb_mcp_tools.store_alert,
    "update_alert_review": mongodb_mcp_tools.update_alert_review,
    "get_batch_statistics": mongodb_mcp_tools.get_batch_statistics,
}
