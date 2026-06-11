import logging
import json
import time
import asyncio
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional

from google import genai
from google.genai import types

from core.config import settings
from core.constants import (
    RULE_WEIGHTS,
    RISK_THRESHOLDS,
    VALID_CORRIDOR_EXCEPTION_KEYWORDS,
    VAGUE_PURPOSE_KEYWORDS,
    ARTICLE_22_MONTHLY_LIMIT_KD,
    SELF_IMPROVE_BATCH_SIZE,
)
from data.models import SelfImprovementReport

from motor.motor_asyncio import AsyncIOMotorDatabase

def get_motor_db(db) -> AsyncIOMotorDatabase:
    if isinstance(db, AsyncIOMotorDatabase):
        return db
    return getattr(db, "db", db)

logger = logging.getLogger("eagleeyes.self_improvement")

# ---------------------------------------------------------------------------
# Dynamic Config In-Memory Cache (60-second TTL)
# ---------------------------------------------------------------------------
_cache_lock = asyncio.Lock()

_weights_cache: Optional[Dict[str, float]] = None
_weights_cache_expiry: float = 0.0

_thresholds_cache: Optional[Dict[str, float]] = None
_thresholds_cache_expiry: float = 0.0

_keywords_cache: Optional[Dict[str, List[str]]] = None
_keywords_cache_expiry: float = 0.0


async def get_active_weights(db) -> Dict[str, float]:
    """
    Load current active rule weights from MongoDB rule_weights collection.
    Falls back to RULE_WEIGHTS constant if collection is empty.
    Cache for 60 seconds to avoid DB hit on every transaction.
    """
    global _weights_cache, _weights_cache_expiry
    now = time.time()
    async with _cache_lock:
        if _weights_cache is not None and now < _weights_cache_expiry:
            return _weights_cache

        try:
            # Check if db has the helper method or query collection directly
            if hasattr(db, "rule_weights"):
                collection = db.rule_weights
            else:
                # If db is MongoClient wrapper, get motor database instance first
                motor_db = get_motor_db(db)
                collection = motor_db.rule_weights

            doc = await collection.find_one({"active": True})
            if doc and "weights" in doc:
                weights = doc["weights"]
            else:
                weights = dict(RULE_WEIGHTS)
        except Exception as exc:
            logger.warning("Failed to fetch active weights from MongoDB: %s. Using default constants.", exc)
            weights = dict(RULE_WEIGHTS)

        # Update cache
        _weights_cache = weights
        _weights_cache_expiry = now + 60.0
        return weights


async def get_active_thresholds(db) -> Dict[str, float]:
    """
    Load current active thresholds from MongoDB thresholds collection.
    Falls back to defaults if collection is empty.
    Cache for 60 seconds to avoid DB hit on every transaction.
    """
    global _thresholds_cache, _thresholds_cache_expiry
    now = time.time()
    async with _cache_lock:
        if _thresholds_cache is not None and now < _thresholds_cache_expiry:
            return _thresholds_cache

        default_thresholds = {
            "RISK_HIGH_THRESHOLD": float(RISK_THRESHOLDS["HIGH"]),
            "RISK_MEDIUM_THRESHOLD": float(RISK_THRESHOLDS["MEDIUM"]),
            "ARTICLE_22_MONTHLY_LIMIT_KD": float(ARTICLE_22_MONTHLY_LIMIT_KD),
        }

        try:
            if hasattr(db, "thresholds"):
                collection = db.thresholds
            else:
                motor_db = get_motor_db(db)
                collection = motor_db.thresholds

            doc = await collection.find_one({"active": True})
            if doc and "thresholds" in doc:
                thresholds = doc["thresholds"]
            else:
                thresholds = default_thresholds
        except Exception as exc:
            logger.warning("Failed to fetch active thresholds from MongoDB: %s. Using default constants.", exc)
            thresholds = default_thresholds

        _thresholds_cache = thresholds
        _thresholds_cache_expiry = now + 60.0
        return thresholds


async def get_active_keywords(db) -> Dict[str, List[str]]:
    """
    Load current active keywords from MongoDB keywords collection.
    Falls back to defaults if collection is empty.
    Cache for 60 seconds to avoid DB hit on every transaction.
    """
    global _keywords_cache, _keywords_cache_expiry
    now = time.time()
    async with _cache_lock:
        if _keywords_cache is not None and now < _keywords_cache_expiry:
            return _keywords_cache

        default_keywords = {
            "vague": list(VAGUE_PURPOSE_KEYWORDS),
            "valid_exceptions": list(VALID_CORRIDOR_EXCEPTION_KEYWORDS),
        }

        try:
            if hasattr(db, "keywords"):
                collection = db.keywords
            else:
                motor_db = get_motor_db(db)
                collection = motor_db.keywords

            doc = await collection.find_one({"active": True})
            if doc:
                keywords = {
                    "vague": doc.get("vague", default_keywords["vague"]),
                    "valid_exceptions": doc.get("valid_exceptions", default_keywords["valid_exceptions"]),
                }
            else:
                keywords = default_keywords
        except Exception as exc:
            logger.warning("Failed to fetch active keywords from MongoDB: %s. Using default constants.", exc)
            keywords = default_keywords

        _keywords_cache = keywords
        _keywords_cache_expiry = now + 60.0
        return keywords


# ---------------------------------------------------------------------------
# Gemini Agent Meta-Analysis Wrapper
# ---------------------------------------------------------------------------
class GeminiAgent:
    """Wrapper around Gemini client to perform MLOps meta-analysis reasoning."""

    def __init__(self):
        # google-genai Client automatically reads GEMINI_API_KEY / GOOGLE_API_KEY from env,
        # but we pass the setting as fallback explicitly
        api_key = settings.GOOGLE_API_KEY or None
        self.client = genai.Client(api_key=api_key)
        self.model = settings.GEMINI_MODEL

    async def analyze_performance(self, prompt: str) -> str:
        """Call Gemini model asynchronously to perform meta-analysis."""
        loop = asyncio.get_event_loop()

        def _call_gemini():
            response = self.client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.3,  # Temperature 0.3 for self-improvement meta-analysis
                    response_mime_type="application/json",
                ),
            )
            return response.text

        return await loop.run_in_executor(None, _call_gemini)


# ---------------------------------------------------------------------------
# Data Merging Logic
# ---------------------------------------------------------------------------
def merge_trace_and_review_data(
    traces: dict,         # From ArizeClient.query_traces_for_batch()
    batch_stats: dict,    # From MongoClient.count_alerts_since_last_batch() or get_batch_statistics()
    alerts: list[dict],   # All alerts from this batch with their final statuses
) -> dict:
    """
    Produces a structured performance summary for Gemini to analyze.
    """
    # 1. Determine batch window from alerts or default to current time
    if alerts:
        timestamps = [a.get("created_at") for a in alerts if a.get("created_at")]
        batch_window = {
            "from": min(timestamps) if timestamps else datetime.utcnow() - timedelta(days=1),
            "to": max(timestamps) if timestamps else datetime.utcnow(),
        }
    else:
        batch_window = {
            "from": datetime.utcnow() - timedelta(days=1),
            "to": datetime.utcnow(),
        }

    total_flagged = len(alerts)
    
    # 2. Extract total evaluated count from batch stats
    total_evaluated = batch_stats.get("total_evaluated") or batch_stats.get("total", 0)
    
    # 3. Calculate general alert status rates
    cleared_count = sum(1 for a in alerts if a.get("status") == "REVIEWED_CLEARED")
    escalated_count = sum(1 for a in alerts if a.get("status") == "REVIEWED_ESCALATED")
    str_count = sum(1 for a in alerts if a.get("status") == "STR_FILED")
    
    false_positive_rate = float(cleared_count) / total_flagged if total_flagged > 0 else 0.0
    escalation_rate = float(escalated_count) / total_flagged if total_flagged > 0 else 0.0

    high_alerts = [a for a in alerts if a.get("risk_score", {}).get("risk_level") == "HIGH"]
    str_high_alerts = [a for a in high_alerts if a.get("status") == "STR_FILED"]
    str_rate = float(len(str_high_alerts)) / len(high_alerts) if high_alerts else 0.0

    # 4. Extract active weights based on alerts or constants
    current_weights = dict(RULE_WEIGHTS)
    for a in alerts:
        rules_triggered = a.get("risk_score", {}).get("rules_triggered", [])
        for r in rules_triggered:
            r_id = r.get("rule_id") if isinstance(r, dict) else getattr(r, "rule_id", None)
            weight = r.get("base_weight") if isinstance(r, dict) else getattr(r, "base_weight", None)
            if r_id and weight is not None:
                current_weights[r_id] = float(weight)

    # 5. Populate rule performance tracking
    rule_performance = {}
    for rule_id in current_weights.keys():
        rule_performance[rule_id] = {
            "trigger_count": 0,
            "false_positive_count": 0,
            "true_positive_count": 0,
            "avg_gemini_confidence": 0.0,
            "false_positive_rate": 0.0,
            "precision": 0.0,
        }

    for a in alerts:
        status = a.get("status")
        confidence = float(a.get("gemini_confidence", 0.5))
        
        rules_triggered = a.get("risk_score", {}).get("rules_triggered", [])
        triggered_ids = []
        for r in rules_triggered:
            r_id = r.get("rule_id") if isinstance(r, dict) else getattr(r, "rule_id", None)
            if r_id:
                triggered_ids.append(r_id)

        for r_id in triggered_ids:
            if r_id not in rule_performance:
                rule_performance[r_id] = {
                    "trigger_count": 0,
                    "false_positive_count": 0,
                    "true_positive_count": 0,
                    "avg_gemini_confidence": 0.0,
                    "false_positive_rate": 0.0,
                    "precision": 0.0,
                }
            perf = rule_performance[r_id]
            perf["trigger_count"] += 1
            perf["avg_gemini_confidence"] += confidence
            
            if status == "REVIEWED_CLEARED":
                perf["false_positive_count"] += 1
            elif status in ["REVIEWED_ESCALATED", "STR_FILED"]:
                perf["true_positive_count"] += 1

    # Finalize rule metric math
    for r_id, perf in rule_performance.items():
        tc = perf["trigger_count"]
        if tc > 0:
            perf["avg_gemini_confidence"] = round(perf["avg_gemini_confidence"] / tc, 4)
            perf["false_positive_rate"] = round(perf["false_positive_count"] / tc, 4)
            perf["precision"] = round(perf["true_positive_count"] / tc, 4)
        else:
            perf["avg_gemini_confidence"] = 0.0
            perf["false_positive_rate"] = 0.0
            perf["precision"] = 0.0

    # 6. Extract low/high precision rules
    low_precision_rules = [r_id for r_id, perf in rule_performance.items() if perf["precision"] < 0.4 and perf["trigger_count"] > 0]
    high_precision_rules = [r_id for r_id, perf in rule_performance.items() if perf["precision"] > 0.8 and perf["trigger_count"] > 0]

    # 7. Keywords extraction
    vague_purposes_seen = []
    valid_exceptions_missed = []
    for a in alerts:
        purpose = a.get("transaction_purpose", "")
        rules_triggered = a.get("risk_score", {}).get("rules_triggered", [])
        triggered_ids = []
        for r in rules_triggered:
            r_id = r.get("rule_id") if isinstance(r, dict) else getattr(r, "rule_id", None)
            if r_id:
                triggered_ids.append(r_id)

        if "VAGUE_PURPOSE" in triggered_ids and purpose:
            vague_purposes_seen.append(purpose)
        if "NON_HOME_CORRIDOR" in triggered_ids and a.get("status") == "REVIEWED_CLEARED" and purpose:
            valid_exceptions_missed.append(purpose)

    # 8. Baseline weight adjustments suggestions (Programmatic heuristics)
    weight_suggestions = {}
    for rule_id, current_weight in current_weights.items():
        perf = rule_performance.get(rule_id, {})
        tc = perf.get("trigger_count", 0)
        fpr = perf.get("false_positive_rate", 0.0)

        # Only suggest weight adjustments for rules with enough data (trigger_count >= 10)
        if tc >= 10:
            suggested = current_weight
            reason = ""
            if fpr == 1.0:
                # 100% false positive rate: suggest reducing weight by 15-25 points
                suggested = max(30.0, current_weight - 20)
                reason = f"Rule triggered {tc} times with a 100% false positive rate. Suggested weight reduction."
            elif fpr == 0.0:
                # 0% false positive rate: suggest increasing weight by 5-10 points
                suggested = min(100.0, current_weight + 5)
                reason = f"Rule triggered {tc} times with a 0% false positive rate. Suggested weight increase."

            if suggested != current_weight:
                weight_suggestions[rule_id] = {
                    "current_weight": float(current_weight),
                    "suggested_weight": float(suggested),
                    "reason": reason
                }

    return {
        "batch_window": batch_window,
        "total_evaluated": int(total_evaluated),
        "total_flagged": int(total_flagged),
        "false_positive_rate": float(false_positive_rate),
        "escalation_rate": float(escalation_rate),
        "str_rate": float(str_rate),
        "rule_performance": rule_performance,
        "low_precision_rules": low_precision_rules,
        "high_precision_rules": high_precision_rules,
        "keyword_analysis": {
            "vague_purposes_seen": vague_purposes_seen,
            "valid_exceptions_missed": valid_exceptions_missed,
        },
        "weight_suggestions": weight_suggestions,
    }


# ---------------------------------------------------------------------------
# Main Orchestration Loop
# ---------------------------------------------------------------------------
async def maybe_trigger_self_improvement(db, arize) -> bool:
    """
    Check if we've hit 500 evaluations since the last improvement run.
    If yes, trigger the loop and return True. Otherwise return False.
    """
    # Connect db if needed
    if hasattr(db, "connect") and not getattr(db, "client", None):
        await db.connect()

    # If db is MongoClient wrapper, use the db property or collection directly
    db_wrapper = db
    from motor.motor_asyncio import AsyncIOMotorDatabase
    if isinstance(db, AsyncIOMotorDatabase):
        # Fetch latest report directly
        collection = db.improvement_reports
        cursor = collection.find().sort("created_at", -1).limit(1)
        results = await cursor.to_list(length=1)
        last_report = results[0] if results else None
    else:
        last_report = await db.get_latest_improvement_report()

    last_run_time = last_report["created_at"] if last_report else datetime(2000, 1, 1)

    if isinstance(db, AsyncIOMotorDatabase):
        collection = db.evaluation_log
        count_since = await collection.count_documents({"timestamp": {"$gte": last_run_time}})
    else:
        count_since = await db.count_evaluations_since(last_run_time)

    logger.info("Evaluation log count since last self-improvement: %d/%d", count_since, SELF_IMPROVE_BATCH_SIZE)

    if count_since >= SELF_IMPROVE_BATCH_SIZE:
        gemini_agent = GeminiAgent()
        await run_self_improvement_loop(db_wrapper, arize, gemini_agent)
        return True
    return False


async def run_self_improvement_loop(
    db,
    arize,
    gemini_agent: GeminiAgent,
) -> SelfImprovementReport:
    """
    Full self-improvement pipeline.
    Must complete within 120 seconds.
    """
    return await asyncio.wait_for(
        _run_self_improvement_loop_impl(db, arize, gemini_agent),
        timeout=120.0
    )


async def _run_self_improvement_loop_impl(
    db,
    arize,
    gemini_agent: GeminiAgent,
) -> SelfImprovementReport:
    # 1. Determine batch window (since last run)
    from motor.motor_asyncio import AsyncIOMotorDatabase
    if isinstance(db, AsyncIOMotorDatabase):
        cursor = db.improvement_reports.find().sort("created_at", -1).limit(1)
        results = await cursor.to_list(length=1)
        last_report = results[0] if results else None
    else:
        last_report = await db.get_latest_improvement_report()

    last_run_time = last_report["created_at"] if last_report else datetime(2000, 1, 1)
    
    logger.info("Starting self-improvement loop for batch since %s", last_run_time.isoformat())

    # 2. Query Arize traces for this batch
    traces = await arize.query_traces_for_batch(last_run_time)

    # 3. Query MongoDB for human review outcomes
    motor_db = get_motor_db(db)
    
    # Fetch all alerts since last run time
    cursor = motor_db.alerts.find({"created_at": {"$gte": last_run_time}})
    alerts = await cursor.to_list(length=10000)

    # Fetch corresponding transactions to get stated purposes
    ref_nos = [a["ref_no"] for a in alerts if "ref_no" in a]
    txns = await motor_db.transactions.find({"ref_no": {"$in": ref_nos}}).to_list(length=10000)
    txn_map = {t["ref_no"]: t for t in txns}

    # Add transaction purpose denormalization to alert records for merging
    for a in alerts:
        ref_no = a.get("ref_no")
        a["transaction_purpose"] = txn_map.get(ref_no, {}).get("transaction_purpose", "")

    # Retrieve batch statistics
    from motor.motor_asyncio import AsyncIOMotorDatabase
    if not isinstance(db, AsyncIOMotorDatabase):
        batch_stats = await db.count_alerts_since_last_batch(last_run_time)
        evals_count = await db.count_evaluations_since(last_run_time)
        batch_stats["total_evaluated"] = evals_count
    else:
        # Programmatic retrieval
        evals_count = await motor_db.evaluation_log.count_documents({"timestamp": {"$gte": last_run_time}})
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
        batch_stats = {
            "total": len(alerts),
            "total_evaluated": evals_count,
            "flagged_by_risk_level": by_risk,
            "cleared": cleared,
            "escalated": escalated,
            "str_filed": str_filed
        }

    # 4. Merge trace data with review outcomes
    merged = merge_trace_and_review_data(traces, batch_stats, alerts)

    # 5. Get current active weights from MongoDB
    from motor.motor_asyncio import AsyncIOMotorDatabase
    if not isinstance(db, AsyncIOMotorDatabase):
        current_weights = await db.get_current_weights()
    else:
        doc = await motor_db.rule_weights.find_one({"active": True})
        current_weights = doc["weights"] if doc else dict(RULE_WEIGHTS)

    # 6. Call Gemini for meta-analysis
    # Format placeholders for the prompt
    total_evaluated = merged["total_evaluated"]
    total_flagged = merged["total_flagged"]
    flagged_pct = (total_flagged / total_evaluated * 100.0) if total_evaluated > 0 else 0.0

    # Build Rule Table Markdown
    table_lines = ["| Rule ID | Triggers | FP Count | TP Count | Precision | Avg Conf |", "|---|---|---|---|---|---|"]
    for r_id, perf in merged["rule_performance"].items():
        table_lines.append(
            f"| {r_id} | {perf['trigger_count']} | {perf['false_positive_count']} | "
            f"{perf['true_positive_count']} | {perf['precision']:.1%} | {perf['avg_gemini_confidence']:.2f} |"
        )
    formatted_rule_performance_table = "\n".join(table_lines)

    formatted_current_weights = "\n".join([f"- {r_id}: {weight}" for r_id, weight in current_weights.items()])
    low_precision_rules = ", ".join(merged["low_precision_rules"]) if merged["low_precision_rules"] else "None"
    high_precision_rules = ", ".join(merged["high_precision_rules"]) if merged["high_precision_rules"] else "None"

    unique_vague = list(set(merged["keyword_analysis"]["vague_purposes_seen"]))[:30]
    vague_purposes_list = ", ".join([f'"{p}"' for p in unique_vague]) if unique_vague else "None"

    unique_exceptions = list(set(merged["keyword_analysis"]["valid_exceptions_missed"]))[:30]
    cleared_exception_strings = ", ".join([f'"{p}"' for p in unique_exceptions]) if unique_exceptions else "None"

    # Assemble and populate the final prompt
    prompt = f"""You are EagleEyes, an AML compliance agent performing a self-evaluation of your detection performance.

You have just completed evaluating {total_evaluated} transactions. Here is your performance data:

BATCH PERFORMANCE SUMMARY
=========================
Total Transactions Evaluated: {total_evaluated}
Total Flagged as Suspicious: {total_flagged} ({flagged_pct:.1f}%)
False Positive Rate: {merged['false_positive_rate']:.1%} (flagged but cleared by human reviewers)
Escalation Rate: {merged['escalation_rate']:.1%} (flagged and escalated by human reviewers)
STR Conversion Rate: {merged['str_rate']:.1%} (HIGH alerts that became STRs)

RULE-BY-RULE PERFORMANCE
=========================
{formatted_rule_performance_table}

CURRENT RULE WEIGHTS
====================
{formatted_current_weights}

LOW PRECISION RULES (likely generating too many false positives):
{low_precision_rules}

HIGH PRECISION RULES (working well):
{high_precision_rules}

KEYWORD ANALYSIS
================
Vague purpose strings seen this batch: {vague_purposes_list}
Corridor exception strings that led to cleared alerts: {cleared_exception_strings}

INSTRUCTIONS
============
Analyze this performance data and suggest improvements. Respond ONLY as a JSON object with these keys:

{{
  "analysis_narrative": "3-5 sentence plain-English explanation of what the data shows about your current performance",
  
  "weight_adjustments": {{
    "RULE_ID": new_weight_float,
    ...
  }},
  
  "weight_adjustment_reasons": {{
    "RULE_ID": "one sentence reason for this adjustment",
    ...
  }},
  
  "keyword_additions_vague": ["new keywords to add to VAGUE_PURPOSE_KEYWORDS"],
  "keyword_additions_valid_exceptions": ["new keywords to add to VALID_CORRIDOR_EXCEPTION_KEYWORDS"],
  "keyword_removals_valid_exceptions": ["keywords to remove from VALID_CORRIDOR_EXCEPTION_KEYWORDS — generating too many clears"],
  
  "threshold_adjustments": {{
    "RISK_HIGH_THRESHOLD": new_value_or_null,
    "RISK_MEDIUM_THRESHOLD": new_value_or_null,
    "ARTICLE_22_MONTHLY_LIMIT_KD": new_value_or_null
  }},
  
  "threshold_adjustment_reasons": {{
    "threshold_name": "one sentence reason"
  }},
  
  "confidence_assessment": "HIGH | MEDIUM | LOW — how confident are you in these suggestions based on the data quality?",
  
  "flags_for_human_review": ["any patterns or anomalies the compliance officer should manually investigate"]
}}

Rules:
- Only suggest weight adjustments for rules with enough data (trigger_count >= 10)
- Do not decrease any weight below 30 or increase above 100
- Do not add keywords that are too broad (e.g., do not add "pay" to vague keywords)
- If false_positive_rate is below 15%, do not suggest major changes — the system is performing well
- If a rule has 100% false positive rate, suggest reducing its weight by 15–25 points
- If a rule has 0% false positive rate (all triggers confirmed), consider increasing weight by 5–10 points"""

    # Run LLM-as-a-Judge evaluations on recent traces
    try:
        avg_alignment_score = await arize.run_compliance_llm_evaluations(last_run_time)
        logger.info("LLM-as-a-Judge compliance alignment score: %.2f", avg_alignment_score)
    except Exception as e:
        logger.error("Error running LLM-as-a-Judge evaluations: %s", e)
        avg_alignment_score = 1.0

    # Call Gemini Meta-Analysis
    warning_flag = False
    gemini_raw_resp = ""
    parsed_analysis = {}

    try:
        gemini_raw_resp = await gemini_agent.analyze_performance(prompt)
        # Parse Gemini response
        clean_resp = gemini_raw_resp.strip()
        if clean_resp.startswith("```"):
            lines = clean_resp.split("\n")
            clean_resp = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
        
        parsed_analysis = json.loads(clean_resp)
        
        # Clamp confidence assessment to valid levels and check if LOW
        confidence_assessment = parsed_analysis.get("confidence_assessment", "MEDIUM").upper()
        
        if avg_alignment_score < 0.80:
            parsed_analysis["confidence_assessment"] = "LOW"
            confidence_assessment = "LOW"
            warning_flag = True
            msg = f"Gating self-improvement: compliance alignment score is low ({avg_alignment_score:.2f} < 0.80). Gating updates."
            logger.warning(msg)
            if "flags_for_human_review" not in parsed_analysis:
                parsed_analysis["flags_for_human_review"] = []
            parsed_analysis["flags_for_human_review"].append(msg)
            
        if "LOW" in confidence_assessment:
            warning_flag = True
            logger.warning("Gemini self-improvement confidence is LOW. Storing report with warning flag.")
    except Exception as exc:
        logger.error("Gemini meta-analysis failed or returned invalid JSON: %s. Raw: %s", exc, gemini_raw_resp)
        warning_flag = True
        parsed_analysis = {
            "analysis_narrative": f"Meta-analysis failed. Error details: {str(exc)}",
            "weight_adjustments": {},
            "weight_adjustment_reasons": {},
            "keyword_additions_vague": [],
            "keyword_additions_valid_exceptions": [],
            "keyword_removals_valid_exceptions": [],
            "threshold_adjustments": {},
            "threshold_adjustment_reasons": {},
            "confidence_assessment": "LOW",
            "flags_for_human_review": [f"Meta-analysis parsing error: {str(exc)}"]
        }

    # 7. Assemble report storage data
    report_id = str(uuid.uuid4())
    
    # Calculate next batch number
    reports_count = await motor_db.improvement_reports.count_documents({})
    batch_number = reports_count + 1

    report_doc = {
        "_id": report_id,
        "report_id": report_id,
        "batch_number": batch_number,
        "batch_window": {
            "from": merged["batch_window"]["from"],
            "to": merged["batch_window"]["to"],
        },
        "created_at": datetime.utcnow(),
        "applied": False,
        "applied_at": None,
        "weight_adjustments": parsed_analysis.get("weight_adjustments", {}),
        "keyword_additions": {
            "vague": parsed_analysis.get("keyword_additions_vague", []),
            "valid_exceptions": parsed_analysis.get("keyword_additions_valid_exceptions", [])
        },
        "keyword_removals": {
            "valid_exceptions": parsed_analysis.get("keyword_removals_valid_exceptions", [])
        },
        "threshold_adjustments": parsed_analysis.get("threshold_adjustments", {}),
        "gemini_analysis": parsed_analysis.get("analysis_narrative", ""),
        "performance_data": merged,  # Full merged metrics for audittrail
        "confidence_assessment": parsed_analysis.get("confidence_assessment", "MEDIUM"),
        "flags_for_human_review": parsed_analysis.get("flags_for_human_review", []),
    }

    if warning_flag:
        report_doc["warning"] = True

    # 8. Store in MongoDB
    await motor_db.improvement_reports.insert_one(report_doc)
    logger.info("Successfully created SelfImprovementReport %s (applied=False, warning=%s)", report_id, warning_flag)

    # 9. Map and return as Pydantic model
    # Flatten keyword additions/removals for pydantic compatibility
    additions = (
        parsed_analysis.get("keyword_additions_vague", []) + 
        parsed_analysis.get("keyword_additions_valid_exceptions", [])
    )
    removals = parsed_analysis.get("keyword_removals_valid_exceptions", [])

    return SelfImprovementReport(
        report_id=report_id,
        batch_number=batch_number,
        transactions_evaluated=merged["total_evaluated"],
        total_alerts_generated=merged["total_flagged"],
        false_positive_estimate=merged["false_positive_rate"] * 100.0,
        false_negative_estimate=0.0,  # Estimated from trace analysis (fallback)
        rule_weight_adjustments={r_id: float(w) for r_id, w in parsed_analysis.get("weight_adjustments", {}).items()},
        keyword_additions=additions,
        keyword_removals=removals,
        threshold_adjustments={k: float(v) for k, v in parsed_analysis.get("threshold_adjustments", {}).items() if v is not None},
        gemini_analysis=parsed_analysis.get("analysis_narrative", ""),
        applied=False,
        created_at=report_doc["created_at"],
        applied_at=None,
    )


# ---------------------------------------------------------------------------
# Applying Approved Improvement Report
# ---------------------------------------------------------------------------
async def apply_improvement_report(
    report_id: str,
    db,
) -> bool:
    """
    Called when a human compliance officer approves a report.
    Applies weight adjustments, keyword changes, and threshold updates.
    Updates the report's applied=True and applied_at fields.
    """
    motor_db = get_motor_db(db)

    # 1. Fetch the report from MongoDB
    report = await motor_db.improvement_reports.find_one({"report_id": report_id})
    if not report:
        logger.error("Could not find improvement report %s to apply.", report_id)
        return False

    # Check for warning flags — do not apply even if requested
    if report.get("warning") or report.get("confidence_assessment") == "LOW":
        logger.warning(
            "Cannot apply improvement report %s. Report contains low-confidence warnings or parsing errors.",
            report_id
        )
        return False

    if report.get("applied"):
        logger.info("Improvement report %s has already been applied.", report_id)
        return True

    # 2. Apply weight adjustments to rule_weights collection
    weight_adjustments = report.get("weight_adjustments", {})
    if weight_adjustments:
        from motor.motor_asyncio import AsyncIOMotorDatabase
        if not isinstance(db, AsyncIOMotorDatabase):
            before_weights = await db.get_current_weights()
        else:
            doc = await motor_db.rule_weights.find_one({"active": True})
            before_weights = doc["weights"] if doc else dict(RULE_WEIGHTS)

        new_weights = dict(before_weights)
        # Ensure new_weights contains all 12 rules (backfilling any missing rules)
        for r_id, base_w in RULE_WEIGHTS.items():
            if r_id not in new_weights:
                new_weights[r_id] = float(base_w)

        for rule_id, adj_weight in weight_adjustments.items():
            if rule_id in new_weights:
                old_val = new_weights[rule_id]
                new_weights[rule_id] = float(adj_weight)
                logger.info("Weight adjustment for %s: %s -> %s", rule_id, old_val, adj_weight)

        # Invalidate cache & update collection
        global _weights_cache
        _weights_cache = None

        from motor.motor_asyncio import AsyncIOMotorDatabase
        if not isinstance(db, AsyncIOMotorDatabase):
            await db.update_weights(new_weights)
        else:
            await motor_db.rule_weights.update_many({"active": True}, {"$set": {"active": False}})
            await motor_db.rule_weights.insert_one({
                "weights": new_weights,
                "active": True,
                "updated_at": datetime.utcnow()
            })

    # 3. Apply keyword changes to a keywords collection
    keyword_additions = report.get("keyword_additions", {})
    keyword_removals = report.get("keyword_removals", {})
    if keyword_additions or keyword_removals:
        # Load active keywords
        active_keywords = await get_active_keywords(db)
        vague_kws = list(active_keywords["vague"])
        exception_kws = list(active_keywords["valid_exceptions"])

        # Add additions
        for kw in keyword_additions.get("vague", []):
            if kw not in vague_kws:
                vague_kws.append(kw)
                logger.info("Keyword added (vague): %s", kw)
        for kw in keyword_additions.get("valid_exceptions", []):
            if kw not in exception_kws:
                exception_kws.append(kw)
                logger.info("Keyword added (valid_exceptions): %s", kw)

        # Apply removals
        for kw in keyword_removals.get("valid_exceptions", []):
            if kw in exception_kws:
                exception_kws.remove(kw)
                logger.info("Keyword removed (valid_exceptions): %s", kw)

        # Invalidate cache & save back to db
        global _keywords_cache
        _keywords_cache = None

        await motor_db.keywords.update_many({"active": True}, {"$set": {"active": False}})
        await motor_db.keywords.insert_one({
            "vague": vague_kws,
            "valid_exceptions": exception_kws,
            "active": True,
            "updated_at": datetime.utcnow()
        })

    # 4. Apply threshold adjustments to a thresholds collection
    threshold_adjustments = report.get("threshold_adjustments", {})
    if threshold_adjustments:
        # Load active thresholds
        active_thresholds = await get_active_thresholds(db)
        new_thresholds = dict(active_thresholds)

        for name, val in threshold_adjustments.items():
            if val is not None:
                old_val = new_thresholds.get(name)
                new_thresholds[name] = float(val)
                logger.info("Threshold adjustment for %s: %s -> %s", name, old_val, val)

        # Invalidate cache & save
        global _thresholds_cache
        _thresholds_cache = None

        await motor_db.thresholds.update_many({"active": True}, {"$set": {"active": False}})
        await motor_db.thresholds.insert_one({
            "thresholds": new_thresholds,
            "active": True,
            "updated_at": datetime.utcnow()
        })

    # 5. Mark report as applied
    await motor_db.improvement_reports.update_one(
        {"report_id": report_id},
        {"$set": {"applied": True, "applied_at": datetime.utcnow()}}
    )
    logger.info("Improvement report %s successfully marked as applied.", report_id)
    return True
