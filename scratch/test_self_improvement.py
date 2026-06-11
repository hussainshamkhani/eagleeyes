import sys
import os
import json
import asyncio
from datetime import datetime, timedelta

# Adjust python path to import project files
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.constants import RULE_WEIGHTS, RISK_THRESHOLDS, VALID_CORRIDOR_EXCEPTION_KEYWORDS, VAGUE_PURPOSE_KEYWORDS
from agent.self_improvement import (
    get_active_weights,
    get_active_thresholds,
    get_active_keywords,
    merge_trace_and_review_data,
    run_self_improvement_loop,
    apply_improvement_report,
)
from rules.engine import RuleEngine
from data.models import Transaction, SenderProfile, Alert, RiskScore, RuleViolation

# ---------------------------------------------------------------------------
# MongoDB Mocks
# ---------------------------------------------------------------------------
class MockCollection:
    def __init__(self, data=None):
        self.data = data or []

    async def find_one(self, query):
        for item in self.data:
            match = True
            for k, v in query.items():
                if k == "timestamp" or k == "created_at":
                    continue
                if item.get(k) != v:
                    match = False
                    break
            if match:
                return item
        return None

    def find(self, query=None):
        query = query or {}
        results = []
        for item in self.data:
            match = True
            for k, v in query.items():
                if k == "timestamp" or k == "created_at":
                    continue
                if isinstance(v, dict):
                    if "$gte" in v:
                        if item.get(k) < v["$gte"]:
                            match = False
                elif item.get(k) != v:
                    match = False
            if match:
                results.append(item)
        
        class MockCursor:
            def __init__(self, res):
                self.res = res
            def sort(self, key, direction):
                self.res = sorted(self.res, key=lambda x: x.get(key), reverse=(direction == -1))
                return self
            def limit(self, val):
                self.res = self.res[:val]
                return self
            async def to_list(self, length=None):
                return self.res
            async def count_documents(self, q):
                return len(self.res)
                
        return MockCursor(results)

    async def count_documents(self, query):
        cursor = self.find(query)
        return len(cursor.res)

    async def insert_one(self, doc):
        self.data.append(doc)
        return True

    async def update_one(self, query, update):
        doc = await self.find_one(query)
        if doc:
            if "$set" in update:
                doc.update(update["$set"])
            return MockUpdateResult(1)
        return MockUpdateResult(0)

    async def update_many(self, query, update):
        modified = 0
        for doc in self.data:
            match = True
            for k, v in query.items():
                if doc.get(k) != v:
                    match = False
                    break
            if match:
                if "$set" in update:
                    doc.update(update["$set"])
                modified += 1
        return MockUpdateResult(modified)


class MockUpdateResult:
    def __init__(self, count):
        self.modified_count = count
        self.matched_count = count


class MockDB:
    def __init__(self):
        self.rule_weights = MockCollection()
        self.thresholds = MockCollection()
        self.keywords = MockCollection()
        self.evaluation_log = MockCollection()
        self.improvement_reports = MockCollection()
        self.alerts = MockCollection()
        self.transactions = MockCollection()

    @property
    def db(self):
        return self

    async def get_latest_improvement_report(self):
        cursor = self.improvement_reports.find()
        cursor.sort("created_at", -1)
        cursor.limit(1)
        res = await cursor.to_list()
        return res[0] if res else None

    async def count_evaluations_since(self, since):
        return await self.evaluation_log.count_documents({"timestamp": {"$gte": since}})

    async def count_alerts_since_last_batch(self, since):
        alerts = await self.alerts.find({"created_at": {"$gte": since}}).to_list()
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

    async def get_current_weights(self):
        doc = await self.rule_weights.find_one({"active": True})
        if doc:
            return doc.get("weights", RULE_WEIGHTS)
        return RULE_WEIGHTS

    async def update_weights(self, new_weights):
        await self.rule_weights.update_many({"active": True}, {"$set": {"active": False}})
        await self.rule_weights.insert_one({
            "weights": new_weights,
            "active": True,
            "updated_at": datetime.utcnow()
        })
        return True


# ---------------------------------------------------------------------------
# External Integrations Mocks
# ---------------------------------------------------------------------------
class MockArizeClient:
    async def query_traces_for_batch(self, since):
        return {
            "total_traces": 20,
            "flagged_count": 5,
            "cleared_count": 2,
            "rule_trigger_frequency": {"VAGUE_PURPOSE": 5},
            "avg_confidence_by_rule": {"VAGUE_PURPOSE": 0.65},
            "low_confidence_traces": [],
            "high_false_positive_rules": [],
            "recommended_weight_decreases": {},
            "recommended_weight_increases": {},
        }


class MockGeminiAgent:
    async def analyze_performance(self, prompt):
        return json.dumps({
            "analysis_narrative": "The agent is performing reasonably well, but VAGUE_PURPOSE is generating too many false positives.",
            "weight_adjustments": {
                "VAGUE_PURPOSE": 42.0,
                "MINOR_SENDER": 58.0
            },
            "weight_adjustment_reasons": {
                "VAGUE_PURPOSE": "Reduced weight due to excessive false positives on general descriptions.",
                "MINOR_SENDER": "Increased weight due to strong true positive correlation."
            },
            "keyword_additions_vague": ["misc payment", "custom help"],
            "keyword_additions_valid_exceptions": ["training fee", "workshop fees"],
            "keyword_removals_valid_exceptions": ["family support"],
            "threshold_adjustments": {
                "RISK_HIGH_THRESHOLD": 80.0,
                "RISK_MEDIUM_THRESHOLD": 48.0,
                "ARTICLE_22_MONTHLY_LIMIT_KD": 165.0
            },
            "threshold_adjustment_reasons": {
                "RISK_HIGH_THRESHOLD": "Increased threshold to reduce high-risk false alerts.",
                "RISK_MEDIUM_THRESHOLD": "Decreased threshold to capture more moderate risk behaviors."
            },
            "confidence_assessment": "HIGH",
            "flags_for_human_review": ["Expat transactions matching vague codes but cleared by analysts."]
        })


# ---------------------------------------------------------------------------
# Main Verification Logic
# ---------------------------------------------------------------------------
async def run_tests():
    print("========================================")
    print("STARTING EAGLEEYES SELF-IMPROVEMENT TESTS")
    print("========================================\n")

    db = MockDB()
    arize = MockArizeClient()
    gemini = MockGeminiAgent()

    # ----------------------------------------------------
    # TEST 1 — Default Dynamic Config Getters
    # ----------------------------------------------------
    print("[TEST 1] Testing dynamic configuration fallback loading...")
    weights = await get_active_weights(db)
    thresholds = await get_active_thresholds(db)
    keywords = await get_active_keywords(db)

    assert weights["SANCTIONED_COUNTRY"] == 100
    assert thresholds["RISK_HIGH_THRESHOLD"] == 75.0
    assert "general" in keywords["vague"]
    print("[OK] Test 1 Passed: Dynamic config successfully falls back to defaults.")

    # ----------------------------------------------------
    # TEST 2 — Cache Expiring Logic (In-Memory TTL)
    # ----------------------------------------------------
    print("\n[TEST 2] Testing Dynamic In-Memory Cache TTL behavior...")
    # Inject a direct configuration into database
    await db.rule_weights.insert_one({"weights": {"SANCTIONED_COUNTRY": 90.0}, "active": True})
    
    # Check if weights returned are still default (due to cache)
    cached_weights = await get_active_weights(db)
    assert cached_weights["SANCTIONED_COUNTRY"] == 100.0
    print("[OK] Cache successfully holds before expiration.")

    # Force cache expiration
    import agent.self_improvement as si
    si._weights_cache_expiry = 0.0

    # Retrieve again, should fetch updated
    refetched = await get_active_weights(db)
    assert refetched["SANCTIONED_COUNTRY"] == 90.0
    print("[OK] Cache successfully expires and refetches from database.")

    # Reset cache
    si._weights_cache = None
    si._weights_cache_expiry = 0.0

    # ----------------------------------------------------
    # TEST 3 — Data Merging Metrics Calculations
    # ----------------------------------------------------
    print("\n[TEST 3] Testing merge_trace_and_review_data metrics math...")
    
    # Create mock alerts
    mock_alerts = [
        # Triggered vague purpose, cleared by human (false positive)
        {
            "alert_id": "alert-1",
            "ref_no": "ref-1",
            "status": "REVIEWED_CLEARED",
            "gemini_confidence": 0.7,
            "created_at": datetime.utcnow(),
            "transaction_purpose": "general help",
            "risk_score": {
                "risk_level": "HIGH",
                "rules_triggered": [{"rule_id": "VAGUE_PURPOSE", "base_weight": 58.0}]
            }
        },
        # Triggered structure sender, escalated (true positive)
        {
            "alert_id": "alert-2",
            "ref_no": "ref-2",
            "status": "REVIEWED_ESCALATED",
            "gemini_confidence": 0.9,
            "created_at": datetime.utcnow(),
            "transaction_purpose": "family support",
            "risk_score": {
                "risk_level": "HIGH",
                "rules_triggered": [{"rule_id": "STRUCTURING_MULTI_SENDER", "base_weight": 97.0}]
            }
        },
        # Triggered non-home corridor, cleared by human (false positive exception)
        {
            "alert_id": "alert-3",
            "ref_no": "ref-3",
            "status": "REVIEWED_CLEARED",
            "gemini_confidence": 0.4,
            "created_at": datetime.utcnow(),
            "transaction_purpose": "study tuition fee",
            "risk_score": {
                "risk_level": "MEDIUM",
                "rules_triggered": [{"rule_id": "NON_HOME_CORRIDOR", "base_weight": 72.0}]
            }
        }
    ]

    batch_stats = {"total_evaluated": 100, "total": 3}
    traces_stub = {}

    merged = merge_trace_and_review_data(traces_stub, batch_stats, mock_alerts)

    assert merged["total_evaluated"] == 100
    assert merged["total_flagged"] == 3
    # Cleared are alert-1 and alert-3: 2/3 = 66.7%
    assert abs(merged["false_positive_rate"] - 0.6667) < 0.01
    # Escalated is alert-2: 1/3 = 33.3%
    assert abs(merged["escalation_rate"] - 0.3333) < 0.01
    
    # Test keywords analysis extraction
    assert "general help" in merged["keyword_analysis"]["vague_purposes_seen"]
    assert "study tuition fee" in merged["keyword_analysis"]["valid_exceptions_missed"]
    print("[OK] Test 3 Passed: Merge calculations and keyword extractions are 100% correct.")

    # ----------------------------------------------------
    # TEST 4 — Self-Improvement Cycle Execution
    # ----------------------------------------------------
    print("\n[TEST 4] Running complete self-improvement optimization cycle...")
    
    # Populate mock collections for batch counting
    for i in range(500):
        await db.evaluation_log.insert_one({"timestamp": datetime.utcnow()})
    
    # Setup mock alerts in DB
    for ma in mock_alerts:
        await db.alerts.insert_one(ma)

    # Setup transaction mappings in DB
    await db.transactions.insert_one({"ref_no": "ref-1", "transaction_purpose": "general help"})
    await db.transactions.insert_one({"ref_no": "ref-2", "transaction_purpose": "family support"})
    await db.transactions.insert_one({"ref_no": "ref-3", "transaction_purpose": "study tuition fee"})

    # Run loop
    report = await run_self_improvement_loop(db, arize, gemini)

    assert report.transactions_evaluated == 500
    assert report.total_alerts_generated == 3
    assert report.applied is False
    assert report.rule_weight_adjustments["VAGUE_PURPOSE"] == 42.0
    assert report.rule_weight_adjustments["MINOR_SENDER"] == 58.0
    
    print("[OK] Test 4 Passed: Loop ran perfectly and SelfImprovementReport was successfully created and versioned in MongoDB.")

    # ----------------------------------------------------
    # TEST 5 — Human Review Approval Application
    # ----------------------------------------------------
    print("\n[TEST 5] Testing human approval application (apply_improvement_report)...")
    
    # Ensure cache is empty
    si._weights_cache = None
    si._thresholds_cache = None
    si._keywords_cache = None

    success = await apply_improvement_report(report.report_id, db)
    assert success is True

    # Retrieve live values and verify changes
    si._weights_cache_expiry = 0.0
    si._thresholds_cache_expiry = 0.0
    si._keywords_cache_expiry = 0.0

    applied_weights = await get_active_weights(db)
    applied_thresholds = await get_active_thresholds(db)
    applied_keywords = await get_active_keywords(db)

    # Weights adjusted correctly
    assert applied_weights["VAGUE_PURPOSE"] == 42.0
    assert applied_weights["MINOR_SENDER"] == 58.0
    
    # Keywords added correctly
    assert "misc payment" in applied_keywords["vague"]
    assert "training fee" in applied_keywords["valid_exceptions"]
    # Keyword removed correctly
    assert "family support" not in applied_keywords["valid_exceptions"]

    # Thresholds adjusted correctly
    assert applied_thresholds["RISK_HIGH_THRESHOLD"] == 80.0
    assert applied_thresholds["RISK_MEDIUM_THRESHOLD"] == 48.0
    assert applied_thresholds["ARTICLE_22_MONTHLY_LIMIT_KD"] == 165.0

    print("[OK] Test 5 Passed: Human approval applies weights, thresholds, and keyword changes perfectly to MongoDB.")

    # ----------------------------------------------------
    # TEST 6 — RuleEngine Dynamic Runtime Verification
    # ----------------------------------------------------
    print("\n[TEST 6] Verifying RuleEngine dynamic loading at runtime...")
    
    # Define a mock transaction and sender for Rule 11 (VAGUE_PURPOSE)
    txn = Transaction(
        ref_no="ref-test",
        amount=100.0,
        currency="KWD",
        amount_kd=100.0,
        bank="Mock Bank",
        date=datetime.utcnow(),
        acc_number="123456789",
        sender_id="sender-123",
        sender_name="John Doe",
        sender_nationality="IN",
        sender_tel="99999999",
        branch="Main Branch",
        recipient_name="Jane Doe",
        recipient_country="IN",
        transaction_purpose="misc payment",  # Added vague keyword!
        proof_of_wealth_provided=False,
        proof_of_relationship_provided=False,
    )

    sender = SenderProfile(
        sender_id="sender-123",
        full_name="John Doe",
        nationality="IN",
        customer_type="resident",
        residency_article="18",
        date_of_birth=datetime.utcnow().date() - timedelta(days=365*25),
        monthly_income_kd=800.0,
        phone="99999999",
        is_pep=False,
    )

    engine = RuleEngine(db)
    
    # Evaluate
    score = await engine.evaluate(
        transaction=txn,
        sender=sender,
        historical_transactions=[],
        all_transactions=[],
        flag_history=[],
    )

    # Check if VAGUE_PURPOSE rule triggered with the newly adjusted weight of 42.0
    vague_violation = next((rv for rv in score.rules_triggered if rv.rule_id == "VAGUE_PURPOSE"), None)
    assert vague_violation is not None
    assert vague_violation.base_weight == 42.0
    
    print("[OK] Test 6 Passed: RuleEngine dynamically loads and applies the new configuration weights and keywords successfully!")

    print("\n========================================")
    print("ALL TESTS PASSED SUCCESSFULLY! 6/6")
    print("========================================")


if __name__ == "__main__":
    asyncio.run(run_tests())
