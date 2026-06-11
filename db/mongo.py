import logging
from datetime import datetime, timedelta
from motor.motor_asyncio import AsyncIOMotorClient
from core.config import settings

logger = logging.getLogger(__name__)

COLLECTIONS = {
    "transactions": "transactions",
    "customers": "customers",
    "alerts": "alerts",
    "str_reports": "str_reports",
    "improvement_reports": "improvement_reports",
    "rule_weights": "rule_weights",      # Current active weights (overridden by self-improvement)
    "evaluation_log": "evaluation_log",  # Every transaction evaluated — for batch counting
}

INDEXES = {
    "transactions": [
        ("sender_id", 1),
        ("acc_number", 1),
        ("date", -1),
        ("recipient_country", 1),
        ("sender_tel", 1),
        [("sender_id", 1), ("date", -1)],       # Compound: sender history
        [("acc_number", 1), ("date", -1)],       # Compound: recipient grouping
    ],
    "customers": [
        ("sender_id", 1),       # Unique
        ("nationality", 1),
        ("residency_article", 1),
    ],
    "alerts": [
        ("sender_id", 1),
        ("ref_no", 1),
        ("status", 1),
        ("created_at", -1),
        [("sender_id", 1), ("created_at", -1)],  # Compound: repeat flag detection
    ],
}


class MongoClient:
    def __init__(self):
        self.client = None
        self._db = None

    @property
    def db(self):
        """Lazy database initializer if connect wasn't called explicitly."""
        if self._db is not None:
            return self._db
        self.client = AsyncIOMotorClient(settings.MONGODB_URI)
        self._db = self.client[settings.MONGODB_DB_NAME]
        return self._db

    async def connect(self):
        """Initialize motor connection and create all indexes."""
        if self.client is None:
            self.client = AsyncIOMotorClient(settings.MONGODB_URI)
            self._db = self.client[settings.MONGODB_DB_NAME]
        
        # Ping to check health
        await self._db.command("ping")
        logger.info("MongoDB connected successfully.")

        # Create indexes
        for collection, index_list in INDEXES.items():
            for idx in index_list:
                if isinstance(idx, list):
                    keys = idx
                elif isinstance(idx, tuple):
                    keys = [idx]
                else:
                    continue

                is_unique = (collection == "customers" and keys == [("sender_id", 1)])
                try:
                    await self._db[collection].create_index(keys, unique=is_unique)
                except Exception as e:
                    logger.warning(f"Failed to create index {keys} on {collection}: {e}")

    async def disconnect(self):
        """Close motor connection."""
        if self.client:
            self.client.close()
            self.client = None
            self._db = None
            logger.info("MongoDB disconnected successfully.")

    async def ping(self) -> bool:
        """Health check method."""
        try:
            await self.db.command("ping")
            return True
        except Exception:
            return False

    # --- Transactions ---
    async def insert_transaction(self, txn: dict) -> str:
        await self.db.transactions.insert_one(txn)
        return txn.get("ref_no")

    async def get_transaction(self, ref_no: str) -> dict | None:
        doc = await self.db.transactions.find_one({"ref_no": ref_no})
        if doc:
            doc["_id"] = str(doc["_id"])
        return doc

    async def get_transactions_by_sender(self, sender_id: str, days: int = 30) -> list[dict]:
        cutoff = datetime.utcnow() - timedelta(days=days)
        cursor = self.db.transactions.find({
            "sender_id": sender_id,
            "date": {"$gte": cutoff}
        })
        results = await cursor.to_list(length=1000)
        for r in results:
            r["_id"] = str(r["_id"])
        return results

    async def get_transactions_by_recipient(self, acc_number: str, days: int = 30) -> list[dict]:
        cutoff = datetime.utcnow() - timedelta(days=days)
        cursor = self.db.transactions.find({
            "acc_number": acc_number,
            "date": {"$gte": cutoff}
        })
        results = await cursor.to_list(length=1000)
        for r in results:
            r["_id"] = str(r["_id"])
        return results

    async def get_transactions_by_date_range(self, start: datetime, end: datetime) -> list[dict]:
        cursor = self.db.transactions.find({
            "date": {"$gte": start, "$lte": end}
        })
        results = await cursor.to_list(length=1000)
        for r in results:
            r["_id"] = str(r["_id"])
        return results

    async def count_transactions_total(self) -> int:
        return await self.db.transactions.count_documents({})

    # --- Customers ---
    async def insert_customer(self, customer: dict) -> str:
        await self.db.customers.insert_one(customer)
        return customer.get("sender_id")

    async def get_customer(self, sender_id: str) -> dict | None:
        doc = await self.db.customers.find_one({"sender_id": sender_id})
        if doc:
            doc["_id"] = str(doc["_id"])
        return doc

    async def update_customer(self, sender_id: str, updates: dict) -> bool:
        result = await self.db.customers.update_one(
            {"sender_id": sender_id},
            {"$set": updates}
        )
        return result.modified_count > 0 or result.matched_count > 0

    # --- Alerts ---
    async def insert_alert(self, alert: dict) -> str:
        await self.db.alerts.insert_one(alert)
        return alert.get("alert_id")

    async def get_alert(self, alert_id: str) -> dict | None:
        doc = await self.db.alerts.find_one({"alert_id": alert_id})
        if doc:
            doc["_id"] = str(doc["_id"])
        return doc

    async def get_alerts_by_sender(self, sender_id: str, days: int = 30) -> list[dict]:
        cutoff = datetime.utcnow() - timedelta(days=days)
        cursor = self.db.alerts.find({
            "sender_id": sender_id,
            "created_at": {"$gte": cutoff}
        })
        results = await cursor.to_list(length=1000)
        for r in results:
            r["_id"] = str(r["_id"])
        return results

    async def get_pending_alerts(self, limit: int = 50) -> list[dict]:
        cursor = self.db.alerts.find({"status": "PENDING"}).limit(limit)
        results = await cursor.to_list(length=limit)
        for r in results:
            r["_id"] = str(r["_id"])
        return results

    async def update_alert_status(self, alert_id: str, status: str, reviewer: str, notes: str) -> bool:
        result = await self.db.alerts.update_one(
            {"alert_id": alert_id},
            {
                "$set": {
                    "status": status,
                    "reviewed_by": reviewer,
                    "reviewer_notes": notes,
                    "reviewed_at": datetime.utcnow()
                }
            }
        )
        return result.modified_count > 0 or result.matched_count > 0

    async def count_alerts_since_last_batch(self, batch_start: datetime) -> dict:
        cursor = self.db.alerts.find({"created_at": {"$gte": batch_start}})
        alerts = await cursor.to_list(length=10000)
        by_risk = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
        total = 0
        cleared = 0
        escalated = 0
        str_filed = 0

        for a in alerts:
            total += 1
            risk_level = a.get("risk_score", {}).get("risk_level", "LOW")
            by_risk[risk_level] = by_risk.get(risk_level, 0) + 1

            status = a.get("status")
            if status == "REVIEWED_CLEARED":
                cleared += 1
            elif status == "REVIEWED_ESCALATED":
                escalated += 1
            elif status == "STR_FILED":
                str_filed += 1

        return {
            "total": total,
            "flagged_by_risk_level": by_risk,
            "cleared": cleared,
            "escalated": escalated,
            "str_filed": str_filed
        }

    # --- Rule Weights (live, overridable by self-improvement) ---
    async def get_current_weights(self) -> dict[str, float]:
        from core.constants import RULE_WEIGHTS
        doc = await self.db.rule_weights.find_one({"active": True})
        if doc:
            return doc.get("weights", RULE_WEIGHTS)
        return RULE_WEIGHTS

    async def update_weights(self, new_weights: dict[str, float]) -> bool:
        await self.db.rule_weights.update_many({"active": True}, {"$set": {"active": False}})
        result = await self.db.rule_weights.insert_one({
            "weights": new_weights,
            "active": True,
            "updated_at": datetime.utcnow()
        })
        return result.inserted_id is not None

    # --- Evaluation Log (for batch counting) ---
    async def log_evaluation(self, ref_no: str, flagged: bool, risk_level: str) -> None:
        await self.db.evaluation_log.insert_one({
            "ref_no": ref_no,
            "flagged": flagged,
            "risk_level": risk_level,
            "timestamp": datetime.utcnow()
        })

    async def count_evaluations_since(self, since: datetime) -> int:
        return await self.db.evaluation_log.count_documents({"timestamp": {"$gte": since}})

    # --- Improvement Reports ---
    async def insert_improvement_report(self, report: dict) -> str:
        await self.db.improvement_reports.insert_one(report)
        return report.get("report_id")

    async def get_latest_improvement_report(self) -> dict | None:
        cursor = self.db.improvement_reports.find().sort("created_at", -1).limit(1)
        results = await cursor.to_list(length=1)
        if results:
            results[0]["_id"] = str(results[0]["_id"])
            return results[0]
        return None

    async def apply_improvement_report(self, report_id: str) -> bool:
        result = await self.db.improvement_reports.update_one(
            {"report_id": report_id},
            {"$set": {"applied": True, "applied_at": datetime.utcnow()}}
        )
        if result.modified_count > 0 or result.matched_count > 0:
            report = await self.db.improvement_reports.find_one({"report_id": report_id})
            if report and "rule_weight_adjustments" in report:
                await self.update_weights(report["rule_weight_adjustments"])
            return True
        return False


# Global singleton instance
mongo_client = MongoClient()


async def get_db():
    """FastAPI dependency — yields the connected db instance."""
    if mongo_client._db is None:
        await mongo_client.connect()
    return mongo_client.db
