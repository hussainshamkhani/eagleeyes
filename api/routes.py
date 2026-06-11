import uuid
import time
import json
import logging
import asyncio
from datetime import datetime, date, timedelta
from typing import List, Dict, Optional, Any

from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from db.mongo import get_db, mongo_client
from data.models import (
    Transaction,
    SenderProfile,
    RuleViolation,
    RiskScore,
    Alert,
    SelfImprovementReport,
    STRReport,
    CustomerType
)
from rules.engine import RuleEngine
from agent.gemini_agent import evaluate_transaction_with_limit
from agent.tools import enable_batch_cache, disable_batch_cache
from reports.str_generator import STRGenerator
from opentelemetry import trace
from integrations.arize_client import ArizeClient, ALL_RULE_IDS, tracer
from agent.self_improvement import (
    maybe_trigger_self_improvement,
    apply_improvement_report as apply_improvement_report_func
)
from core.config import settings

logger = logging.getLogger("eagleeyes.api")
router = APIRouter()

# Instantiate Arize client for structured evaluation tracing
arize_client = ArizeClient()

# ---------------------------------------------------------------------------
# Pydantic Response Models
# ---------------------------------------------------------------------------

class EvaluationResponse(BaseModel):
    ref_no: str
    evaluated: bool
    risk_level: str
    risk_score: float
    rules_triggered: List[str]
    alert_generated: bool
    alert_id: Optional[str] = None
    str_generated: bool
    recommended_action: str
    gemini_narrative: Optional[str] = None
    gemini_confidence: Optional[float] = None
    arize_trace_id: Optional[str] = None

class BatchEvaluationRequest(BaseModel):
    transactions: List[Transaction]
    stop_on_high_risk: bool = False

class BatchEvaluationResponse(BaseModel):
    total_submitted: int
    evaluated: int
    flagged: int
    high_risk: int
    medium_risk: int
    low_flagged: int
    alerts_generated: int
    strs_generated: int
    results: List[EvaluationResponse]

class AlertReviewRequest(BaseModel):
    status: str = Field(..., description="REVIEWED_CLEARED | REVIEWED_ESCALATED | STR_FILED")
    reviewer: str
    notes: str
    generate_str: bool = False

class STRResponse(BaseModel):
    alert_id: str
    str_id: str
    str_content: str
    generated_at: datetime

class AlertDetailResponse(Alert):
    transaction: Optional[Transaction] = None

class AlertEditsUpdateRequest(BaseModel):
    comment: Optional[str] = None
    user_status: Optional[str] = None

class ApplyImprovementRequest(BaseModel):
    approved_by: str
    notes: str

class StatsResponse(BaseModel):
    total_transactions_evaluated: int
    total_alerts_generated: int
    pending_alerts: int
    high_risk_alerts: int
    strs_filed: int
    false_positive_rate_30d: float
    self_improvement_runs: int
    last_improvement_run: Optional[datetime] = None
    current_rule_weights: Dict[str, float]

# ---------------------------------------------------------------------------
# Evaluation Pipeline Helper
# ---------------------------------------------------------------------------

@tracer.chain(name="gemini_reasoning")
async def _run_gemini_if_needed(
    transaction: Transaction,
    sender: SenderProfile,
    risk_score: RiskScore,
    run_id: Optional[str] = None,
) -> dict:
    """
    Gate that decides whether this transaction warrants a Gemini call.
    Returns either a Gemini assessment or a pre-built response.
    """
    start_time = time.perf_counter()
    # HIGH risk or strong MEDIUM → full Gemini evaluation
    if risk_score.risk_level == "HIGH" or risk_score.final_score >= 60:
        if run_id is not None:
            result = await evaluate_transaction_with_limit(transaction, sender, risk_score, run_id=run_id)
        else:
            result = await evaluate_transaction_with_limit(transaction, sender, risk_score)

    # Weak MEDIUM (50–59) → template narrative, no Gemini call
    elif risk_score.risk_level == "MEDIUM":
        result = {
            "narrative": (
                f"Transaction {transaction.ref_no} by {sender.full_name} has been flagged "
                f"with a moderate risk score of {risk_score.final_score:.1f}/100. "
                f"Triggered rules: {', '.join(r.rule_id for r in risk_score.rules_triggered)}. "
                f"This transaction requires standard review but does not meet the threshold "
                f"for automated escalation. A compliance officer should assess manually."
            ),
            "confidence": 0.5,
            "recommended_action": "MONITOR",
            "additional_info_required": ["Manual review recommended"],
            "applicable_regulations": ["CBK AML/CFT General Guidelines"],
        }

    # LOW risk → auto-clear, no Gemini call
    else:
        result = {
            "narrative": (
                f"Transaction {transaction.ref_no} scored {risk_score.final_score:.1f}/100. "
                f"No significant AML indicators detected. Transaction cleared automatically."
            ),
            "confidence": 0.95,
            "recommended_action": "CLEAR",
            "additional_info_required": [],
            "applicable_regulations": [],
        }

    latency_ms = (time.perf_counter() - start_time) * 1000.0

    # Build prompt for span attribute logging
    from agent.gemini_agent import _build_evaluation_prompt
    input_prompt = _build_evaluation_prompt(transaction, sender, risk_score)

    span = trace.get_current_span()
    if span and span.is_recording():
        span.set_attribute("eagleeyes.input_prompt", input_prompt)
        span.set_attribute("eagleeyes.output_narrative", str(result.get("narrative", "")))
        span.set_attribute("eagleeyes.confidence", float(result.get("confidence", 0.0)))
        span.set_attribute("eagleeyes.recommended_action", str(result.get("recommended_action", "HOLD")))
        span.set_attribute("eagleeyes.model_name", settings.GEMINI_MODEL)
        span.set_attribute("eagleeyes.latency_ms", float(latency_ms))

        # Additional Gemini output fields (flattened to primitives)
        additional_info = result.get("additional_info_required", [])
        span.set_attribute(
            "eagleeyes.additional_info_required",
            ",".join(str(i) for i in additional_info) if additional_info else "none",
        )
        regulations = result.get("applicable_regulations", [])
        span.set_attribute(
            "eagleeyes.applicable_regulations",
            ",".join(str(r) for r in regulations) if regulations else "none",
        )

    return result


@tracer.chain(name="transaction_evaluation")
async def evaluate_transaction_pipeline(
    transaction: Transaction,
    db,
    start_self_improvement: bool = True,
    run_id: Optional[str] = None
) -> EvaluationResponse:
    """
    Executes the full compliance evaluation pipeline for a single transaction.
    """
    # Set root span attributes
    span = trace.get_current_span()
    if span and span.is_recording():
        span.set_attribute("eagleeyes.ref_no", transaction.ref_no)
        span.set_attribute("eagleeyes.sender_id", transaction.sender_id)
        span.set_attribute("eagleeyes.amount_kd", float(transaction.amount_kd))
        span.set_attribute("eagleeyes.recipient_country", transaction.recipient_country)
        span.set_attribute("eagleeyes.project", settings.PHOENIX_PROJECT_NAME)
        span.set_attribute("eagleeyes.timestamp", datetime.utcnow().isoformat())

    # Get active trace ID
    trace_id_hex = None
    if span:
        span_context = span.get_span_context()
        if span_context and span_context.trace_id:
            trace_id_hex = format(span_context.trace_id, "032x")

    # 1. Fetch SenderProfile from MongoDB by transaction.sender_id
    sender_doc = await db.customers.find_one({"sender_id": transaction.sender_id})
    if not sender_doc:
        raise HTTPException(
            status_code=404,
            detail="Sender not found — KYC record required"
        )
    
    # Materialize SenderProfile model instance
    sender = SenderProfile(**sender_doc)

    # 2. Fetch sender transaction history (last 30 days)
    sender_txns_docs = await mongo_client.get_transactions_by_sender(transaction.sender_id, 30)
    sender_txns = [Transaction(**t) for t in sender_txns_docs]

    # 3. Fetch sender flag history (last 30 days)
    sender_alerts_docs = await mongo_client.get_alerts_by_sender(transaction.sender_id, 30)
    sender_alerts = [Alert(**a) for a in sender_alerts_docs]

    # 4. Fetch recipient network (all senders to same acc_number, last 30 days)
    recipient_txns_docs = await mongo_client.get_transactions_by_recipient(transaction.acc_number, 30)
    recipient_txns = [Transaction(**t) for t in recipient_txns_docs]

    # 5. Run RuleEngine.evaluate() — rule engine span is auto-created by decorator
    rule_engine = RuleEngine(db)
    risk_score = await rule_engine.evaluate(
        transaction=transaction,
        sender=sender,
        historical_transactions=sender_txns,
        all_transactions=recipient_txns,
        flag_history=sender_alerts
    )
    
    rules_triggered_ids = [r.rule_id for r in risk_score.rules_triggered]

    # 6. If risk_score.risk_level == "LOW" AND no rules triggered: close trace, return clean result
    if risk_score.risk_level == "LOW" and len(rules_triggered_ids) == 0:
        # Log evaluation
        await mongo_client.log_evaluation(
            ref_no=transaction.ref_no,
            flagged=False,
            risk_level="LOW"
        )
        
        # Record clean alert decision span
        with tracer.start_as_current_span(
            "alert_decision",
            attributes={
                "eagleeyes.alert_generated": False,
                "eagleeyes.risk_level": "LOW",
                "eagleeyes.alert_id": "none",
                "eagleeyes.str_auto_generated": False
            }
        ):
            pass
        
        # Check self-improvement threshold in background
        if start_self_improvement:
            asyncio.create_task(maybe_trigger_self_improvement(db, arize_client))

        return EvaluationResponse(
            ref_no=transaction.ref_no,
            evaluated=True,
            risk_level="LOW",
            risk_score=risk_score.final_score,
            rules_triggered=[],
            alert_generated=False,
            str_generated=False,
            recommended_action="CLEAR",
            arize_trace_id=trace_id_hex
        )

    # 7. Any rules triggered (even LOW): run Gemini reasoning — span is auto-created by decorator
    if run_id is not None:
        gemini_result = await _run_gemini_if_needed(
            transaction=transaction,
            sender=sender,
            risk_score=risk_score,
            run_id=run_id
        )
    else:
        gemini_result = await _run_gemini_if_needed(
            transaction=transaction,
            sender=sender,
            risk_score=risk_score
        )

    # 8. Create Alert object
    alert_id = str(uuid.uuid4())
    alert = Alert(
        alert_id=alert_id,
        ref_no=transaction.ref_no,
        sender_id=transaction.sender_id,
        risk_score=risk_score,
        gemini_reasoning=gemini_result.get("narrative", ""),
        gemini_confidence=gemini_result.get("confidence", 0.5),
        status="PENDING",
        str_generated=False,
        created_at=datetime.utcnow(),
        recommended_action=gemini_result.get("recommended_action", "HOLD"),
        updated_at=datetime.utcnow()
    )


    # Ensure additional fields from agent are saved on the alert dict safely
    alert.__dict__["additional_info_required"] = gemini_result.get("additional_info_required", [])
    alert.__dict__["applicable_regulations"] = gemini_result.get("applicable_regulations", [])

    # 9. If risk_score.risk_level == "HIGH" AND gemini_agent.confidence >= 0.8: set str_generated=True, run STRGenerator.generate()
    str_generated = False
    if risk_score.risk_level == "HIGH" and alert.gemini_confidence >= 0.8:
        str_generator = STRGenerator()
        
        # Fetch full annual remittance context for the STR Generator
        from agent.tools import get_sender_annual_total
        annual_total_data = await get_sender_annual_total(transaction.sender_id)
        annual_total = annual_total_data.get("annual_total_kd", 0.0)
        
        # Extract month-to-date total
        mtd_total = annual_total_data.get("monthly_totals", {}).get(datetime.utcnow().strftime("%Y-%m"), 0.0)
        
        # Get historical STR count for this sender
        prior_strs_count = await db.str_reports.count_documents({"subject_id": transaction.sender_id})
        
        historical_context = {
            "txn_count_30d": len(sender_txns),
            "amount_kd_30d": sum(t.amount_kd for t in sender_txns),
            "amount_kd_this_month": mtd_total,
            "annual_total_kd": annual_total,
            "prior_alerts_count": len(sender_alerts),
            "prior_strs_count": prior_strs_count
        }
        
        str_report = str_generator.generate(
            alert=alert,
            transaction=transaction,
            sender=sender,
            historical_context=historical_context
        )
        
        # Save the suspicious transaction report document in MongoDB
        await db.str_reports.insert_one(str_report.model_dump())
        
        alert.str_generated = True
        alert.str_content = str_report.str_content
        str_generated = True

    # Attach trace ID to alert
    alert.arize_trace_id = trace_id_hex

    # 10. Store alert in MongoDB
    await db.alerts.insert_one(alert.model_dump())

    # 11. Record alert decision span
    with tracer.start_as_current_span(
        "alert_decision",
        attributes={
            "eagleeyes.alert_generated": True,
            "eagleeyes.risk_level": risk_score.risk_level,
            "eagleeyes.alert_id": alert_id,
            "eagleeyes.str_auto_generated": str_generated
        }
    ):
        pass

    # 12. Log evaluation in evaluation_log
    await mongo_client.log_evaluation(
        ref_no=transaction.ref_no,
        flagged=True,
        risk_level=risk_score.risk_level
    )

    # 13. Check maybe_trigger_self_improvement() — run in background if threshold hit
    if start_self_improvement:
        asyncio.create_task(maybe_trigger_self_improvement(db, arize_client))

    return EvaluationResponse(
            ref_no=transaction.ref_no,
            evaluated=True,
            risk_level=risk_score.risk_level,
            risk_score=risk_score.final_score,
            rules_triggered=rules_triggered_ids,
            alert_generated=True,
            alert_id=alert_id,
            str_generated=str_generated,
            recommended_action=gemini_result.get("recommended_action", "HOLD"),
            gemini_narrative=alert.gemini_reasoning,
            gemini_confidence=alert.gemini_confidence,
            arize_trace_id=alert.arize_trace_id
        )

# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------

@router.post(
    "/transactions/evaluate",
    response_model=EvaluationResponse,
    tags=["Transactions"],
    summary="Evaluate a single transaction through the compliance pipeline"
)
async def evaluate_single_transaction(
    transaction: Transaction,
    db=Depends(get_db)
):
    """
    Evaluates a single remittance transaction against the rule engine and 
    Gemini agent, optionally generating an STR and logging tracing context.
    """
    try:
        run_id = f"single-{uuid.uuid4().hex[:8]}"
        return await evaluate_transaction_pipeline(transaction, db, run_id=run_id)
    except HTTPException as he:
        raise he
    except Exception as exc:
        logger.error("Failed to evaluate transaction %s: %s", transaction.ref_no, exc)
        raise HTTPException(
            status_code=500,
            detail={"error": "Internal error", "detail": str(exc)}
        )

@router.post(
    "/transactions/batch",
    response_model=BatchEvaluationResponse,
    tags=["Transactions"],
    summary="Evaluate multiple transactions in a sequential batch"
)
async def evaluate_batch_transactions(
    payload: BatchEvaluationRequest,
    db=Depends(get_db)
):
    """
    Submits up to 100 transactions for compliance review.
    If stop_on_high_risk is enabled, the pipeline halts immediately when a HIGH-risk transaction is flagged.
    """
    total_submitted = len(payload.transactions)
    if total_submitted > 100:
        raise HTTPException(
            status_code=422,
            detail="Batch submissions are capped at 100 transactions maximum."
        )

    results = []
    evaluated = 0
    flagged = 0
    high_risk = 0
    medium_risk = 0
    low_flagged = 0
    alerts_generated = 0
    strs_generated = 0

    run_id = f"batch-{uuid.uuid4().hex[:8]}"
    enable_batch_cache()
    try:
        tasks = [
            evaluate_transaction_pipeline(txn, db, start_self_improvement=False, run_id=run_id)
            for txn in payload.transactions
        ]
        raw_results = await asyncio.gather(*tasks)
    except HTTPException as he:
        raise he
    except Exception as exc:
        logger.error("Batch error during concurrent evaluation: %s", exc)
        raise HTTPException(
            status_code=500,
            detail={"error": "Internal error", "detail": f"Failed evaluating batch: {str(exc)}"}
        )
    finally:
        disable_batch_cache()

    for txn, res in zip(payload.transactions, raw_results):
        results.append(res)
        evaluated += 1

        if res.alert_generated:
            flagged += 1
            alerts_generated += 1
            if res.risk_level == "HIGH":
                high_risk += 1
            elif res.risk_level == "MEDIUM":
                medium_risk += 1
            else:
                low_flagged += 1
        if res.str_generated:
            strs_generated += 1

        # Halt batch execution on high risk transaction if configured
        if payload.stop_on_high_risk and res.risk_level == "HIGH":
            logger.info("Batch halted early on high-risk transaction: %s", txn.ref_no)
            break

    # Post-batch background evaluation weight optimization check
    asyncio.create_task(maybe_trigger_self_improvement(db, arize_client))

    return BatchEvaluationResponse(
        total_submitted=total_submitted,
        evaluated=evaluated,
        flagged=flagged,
        high_risk=high_risk,
        medium_risk=medium_risk,
        low_flagged=low_flagged,
        alerts_generated=alerts_generated,
        strs_generated=strs_generated,
        results=results
    )

@router.get(
    "/alerts",
    response_model=List[Alert],
    tags=["Alerts"],
    summary="Retrieve compliance alerts for the dashboard"
)
async def get_alerts_dashboard(
    status: str = "PENDING",
    risk_level: Optional[str] = None,
    limit: int = 50,
    skip: int = 0,
    db=Depends(get_db)
):
    """
    Fetches compliance alerts filtered by status and optional risk level with offset pagination support.
    """
    query = {"status": status}
    if risk_level:
        query["risk_score.risk_level"] = risk_level.upper()

    cursor = db.alerts.find(query).sort("created_at", -1).skip(skip).limit(limit)
    results = await cursor.to_list(length=limit)
    
    alerts = []
    for r in results:
        r["_id"] = str(r["_id"])
        alerts.append(Alert(**r))
    return alerts


@router.get(
    "/alerts/export",
    tags=["Alerts"],
    summary="Export flagged transactions to an Excel spreadsheet"
)
async def export_alerts(
    status: Optional[str] = None,
    risk_level: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    limit: Optional[int] = None,
    skip: Optional[int] = None,
    db=Depends(get_db)
):
    """
    Exports compliance alerts and their associated transaction details to an Excel spreadsheet.
    Supports query filtering matching the dashboard list view.
    """
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font

    # 1. Build the database query
    query = {}
    if status:
        query["status"] = status
    if risk_level:
        query["risk_score.risk_level"] = risk_level.upper()

    if start_date or end_date:
        date_query = {}
        if start_date:
            date_query["$gte"] = start_date
        if end_date:
            date_query["$lte"] = end_date
        query["created_at"] = date_query

    # 2. Fetch matches from db.alerts
    cursor = db.alerts.find(query).sort("created_at", -1)
    if skip is not None:
        cursor = cursor.skip(skip)
    if limit is not None:
        cursor = cursor.limit(limit)

    alerts_docs = await cursor.to_list(length=limit or 10000)

    # 3. Batch fetch corresponding transactions to prevent multiple single queries
    ref_nos = [doc.get("ref_no") for doc in alerts_docs if doc.get("ref_no")]
    transactions_map = {}
    if ref_nos:
        txns_cursor = db.transactions.find({"ref_no": {"$in": ref_nos}})
        txns_docs = await txns_cursor.to_list(length=len(ref_nos))
        transactions_map = {t["ref_no"]: t for t in txns_docs}

    # 4. Generate the workbook using openpyxl
    wb = Workbook()
    ws = wb.active
    ws.title = "Flagged Transactions"

    columns = [
        "ref_no", "date", "branch", "sender name", "sender ID", "nationality", 
        "recipient name", "recipient country", "amount", "amount_kd", 
        "risk_score", "risk_level", "agent recommended_action", "user status", 
        "last-modified timestamp", "arize_trace_id", "comment"
    ]
    ws.append(columns)

    # Style header row bold and freeze top row
    header_font = Font(bold=True)
    for col_idx in range(1, len(columns) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
    
    ws.freeze_panes = "A2"

    # 5. Populate workbook rows
    for doc in alerts_docs:
        doc["_id"] = str(doc["_id"])
        alert = Alert(**doc)
        txn_doc = transactions_map.get(alert.ref_no, {})
        
        # Determine last-modified timestamp (use updated_at, reviewed_at, or created_at)
        last_modified = alert.updated_at or alert.reviewed_at or alert.created_at
        last_modified_str = last_modified.strftime("%Y-%m-%d %H:%M:%S") if last_modified else ""

        # Format dates / times
        txn_date_str = ""
        if txn_doc.get("date"):
            t_date = txn_doc["date"]
            if isinstance(t_date, str):
                txn_date_str = t_date
            elif isinstance(t_date, datetime):
                txn_date_str = t_date.strftime("%Y-%m-%d %H:%M:%S")

        # Flat map fields in order:
        row_data = [
            alert.ref_no,
            txn_date_str,
            txn_doc.get("branch", ""),
            txn_doc.get("sender_name", ""),
            alert.sender_id,
            txn_doc.get("sender_nationality", ""),
            txn_doc.get("recipient_name", ""),
            txn_doc.get("recipient_country", ""),
            txn_doc.get("amount", ""),
            txn_doc.get("amount_kd", ""),
            alert.risk_score.final_score,
            alert.risk_score.risk_level,
            alert.recommended_action or "HOLD",
            alert.user_status or "",
            last_modified_str,
            alert.arize_trace_id or "",
            alert.comment or ""
        ]
        ws.append(row_data)

    # Set reasonable column widths automatically
    for col in ws.columns:
        max_len = 0
        for cell in col:
            val = str(cell.value or '')
            if len(val) > max_len:
                max_len = len(val)
        col_letter = col[0].column_letter
        ws.column_dimensions[col_letter].width = max(max_len + 3, 12)

    # 6. Stream file via BytesIO
    file_stream = io.BytesIO()
    wb.save(file_stream)
    file_stream.seek(0)

    filename = f"flagged_transactions_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.xlsx"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"'
    }
    
    return StreamingResponse(
        file_stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers
    )

@router.get(
    "/alerts/{alert_id}",
    response_model=AlertDetailResponse,
    tags=["Alerts"],
    summary="Get full alert details"
)
async def get_single_alert(alert_id: str, db=Depends(get_db)):
    """
    Fetch a single alert from MongoDB by its ID, including full transaction details.
    """
    doc = await db.alerts.find_one({"alert_id": alert_id})
    if not doc:
        raise HTTPException(
            status_code=404,
            detail="Alert not found — check ID correctness"
        )
    doc["_id"] = str(doc["_id"])
    alert_obj = Alert(**doc)
    
    # Fetch the underlying transaction to embed it in the response
    txn_doc = await db.transactions.find_one({"ref_no": alert_obj.ref_no})
    transaction_obj = None
    if txn_doc:
        txn_doc["_id"] = str(txn_doc["_id"])
        transaction_obj = Transaction(**txn_doc)
        
    return AlertDetailResponse(
        **alert_obj.model_dump(),
        transaction=transaction_obj
    )

@router.patch(
    "/alerts/{alert_id}/review",
    response_model=Alert,
    tags=["Alerts"],
    summary="Log a compliance human review decision on an alert"
)
async def review_compliance_alert(
    alert_id: str,
    payload: AlertReviewRequest,
    db=Depends(get_db)
):
    """
    Updates the status and human notes on a flagged alert.
    If generate_str is true and status becomes STR_FILED, auto-generates a compliance report.
    """
    alert_doc = await db.alerts.find_one({"alert_id": alert_id})
    if not alert_doc:
        raise HTTPException(
            status_code=404,
            detail="Alert not found"
        )

    alert = Alert(**alert_doc)
    status = payload.status.upper()
    valid_statuses = {"REVIEWED_CLEARED", "REVIEWED_ESCALATED", "STR_FILED"}
    if status not in valid_statuses:
        raise HTTPException(
            status_code=422,
            detail="Status must be one of: REVIEWED_CLEARED, REVIEWED_ESCALATED, STR_FILED"
        )

    str_generated = alert.str_generated
    str_content = alert.str_content

    # Human approved STR filing and generating report
    if payload.generate_str and status == "STR_FILED" and not str_generated:
        txn_doc = await db.transactions.find_one({"ref_no": alert.ref_no})
        if not txn_doc:
            raise HTTPException(status_code=404, detail="Underlying transaction record not found")
        transaction = Transaction(**txn_doc)

        sender_doc = await db.customers.find_one({"sender_id": alert.sender_id})
        if not sender_doc:
            raise HTTPException(status_code=404, detail="KYC customer profile not found")
        sender = SenderProfile(**sender_doc)

        # Retrieve prior statistics for formatting grounds section
        sender_txns_docs = await db.transactions.find({"sender_id": alert.sender_id}).to_list(length=1000)
        sender_txns = [Transaction(**t) for t in sender_txns_docs]
        sender_alerts_docs = await db.alerts.find({"sender_id": alert.sender_id}).to_list(length=1000)
        sender_alerts = [Alert(**a) for a in sender_alerts_docs]

        from agent.tools import get_sender_annual_total
        annual_total_data = await get_sender_annual_total(alert.sender_id)
        annual_total = annual_total_data.get("annual_total_kd", 0.0)
        mtd_total = annual_total_data.get("monthly_totals", {}).get(datetime.utcnow().strftime("%Y-%m"), 0.0)
        prior_strs_count = await db.str_reports.count_documents({"subject_id": alert.sender_id})

        historical_context = {
            "txn_count_30d": len(sender_txns),
            "amount_kd_30d": sum(t.amount_kd for t in sender_txns),
            "amount_kd_this_month": mtd_total,
            "annual_total_kd": annual_total,
            "prior_alerts_count": len(sender_alerts),
            "prior_strs_count": prior_strs_count
        }

        str_generator = STRGenerator()
        str_report = str_generator.generate(
            alert=alert,
            transaction=transaction,
            sender=sender,
            historical_context=historical_context
        )

        await db.str_reports.insert_one(str_report.model_dump())
        str_generated = True
        str_content = str_report.str_content

    updates = {
        "status": status,
        "reviewed_by": payload.reviewer,
        "reviewer_notes": payload.notes,
        "reviewed_at": datetime.utcnow(),
        "str_generated": str_generated,
        "str_content": str_content
    }

    await db.alerts.update_one(
        {"alert_id": alert_id},
        {"$set": updates}
    )

    updated_doc = await db.alerts.find_one({"alert_id": alert_id})
    updated_doc["_id"] = str(updated_doc["_id"])
    return Alert(**updated_doc)

@router.patch(
    "/alerts/{alert_id}",
    response_model=Alert,
    tags=["Alerts"],
    summary="Update user-editable comments and status on an alert"
)
async def update_alert_edits(
    alert_id: str,
    payload: AlertEditsUpdateRequest,
    db=Depends(get_db)
):
    """
    Persists user edits (comments and status changes) to MongoDB for a given alert.
    """
    alert_doc = await db.alerts.find_one({"alert_id": alert_id})
    if not alert_doc:
        raise HTTPException(
            status_code=404,
            detail="Alert not found"
        )
        
    updates = {}
    if payload.comment is not None:
        updates["comment"] = payload.comment
    if payload.user_status is not None:
        updates["user_status"] = payload.user_status
        
    if updates:
        updates["updated_at"] = datetime.utcnow()
        await db.alerts.update_one(
            {"alert_id": alert_id},
            {"$set": updates}
        )
        
    updated_doc = await db.alerts.find_one({"alert_id": alert_id})
    updated_doc["_id"] = str(updated_doc["_id"])
    return Alert(**updated_doc)

@router.get(
    "/alerts/{alert_id}/str",
    response_model=STRResponse,
    tags=["Alerts"],
    summary="Get full Suspicious Transaction Report plain text"
)
async def get_alert_suspicious_report(alert_id: str, db=Depends(get_db)):
    """
    Returns Suspicious Transaction Report plain-text document linked to this alert.
    """
    doc = await db.str_reports.find_one({"alert_id": alert_id})
    if not doc:
        # Check if the alert holds generated content directly
        alert_doc = await db.alerts.find_one({"alert_id": alert_id})
        if alert_doc and alert_doc.get("str_generated") and alert_doc.get("str_content"):
            return STRResponse(
                alert_id=alert_id,
                str_id=f"STR-{alert_doc.get('ref_no')}",
                str_content=alert_doc.get("str_content"),
                generated_at=alert_doc.get("reviewed_at") or datetime.utcnow()
            )
        raise HTTPException(
            status_code=404,
            detail="STR report not generated yet for this alert — review and file to generate."
        )

    return STRResponse(
        alert_id=alert_id,
        str_id=doc.get("str_id"),
        str_content=doc.get("str_content"),
        generated_at=doc.get("report_date") or datetime.utcnow()
    )

@router.get(
    "/customers/{sender_id}",
    response_model=SenderProfile,
    tags=["Customers"],
    summary="Get customer KYC profile details"
)
async def get_customer_profile(sender_id: str, db=Depends(get_db)):
    """
    Retrieve Sender Profile KYC data by Civil ID or Passport number.
    """
    doc = await db.customers.find_one({"sender_id": sender_id})
    if not doc:
        raise HTTPException(
            status_code=404,
            detail="Sender not found — onboarding and KYC required"
        )
    doc["_id"] = str(doc["_id"])
    return SenderProfile(**doc)

@router.post(
    "/customers",
    response_model=SenderProfile,
    tags=["Customers"],
    summary="Create a new customer KYC profile"
)
async def onboard_customer_profile(
    customer: SenderProfile,
    db=Depends(get_db)
):
    """
    Registers a new Customer KYC Profile in MongoDB to allow legal transaction routing.
    """
    existing = await db.customers.find_one({"sender_id": customer.sender_id})
    if existing:
        raise HTTPException(
            status_code=400,
            detail="KYC customer profile already registered under this sender ID"
        )

    cust_dict = customer.model_dump()
    if isinstance(cust_dict.get("date_of_birth"), date) and not isinstance(cust_dict.get("date_of_birth"), datetime):
        cust_dict["date_of_birth"] = datetime.combine(cust_dict["date_of_birth"], datetime.min.time())
    await db.customers.insert_one(cust_dict)
    inserted = await db.customers.find_one({"sender_id": customer.sender_id})
    inserted["_id"] = str(inserted["_id"])
    return SenderProfile(**inserted)

@router.get(
    "/improvement/latest",
    response_model=SelfImprovementReport,
    tags=["Improvement"],
    summary="Fetch latest self-improvement meta-analysis report"
)
async def get_latest_self_improvement_run(db=Depends(get_db)):
    """
    Retrieves the latest self-improvement report (pending human compliance approval).
    """
    cursor = db.improvement_reports.find().sort("created_at", -1).limit(1)
    results = await cursor.to_list(length=1)
    if not results:
        raise HTTPException(
            status_code=404,
            detail="No self-improvement runs executed yet."
        )

    report = results[0]
    
    # Flatten keyword objects for standard Pydantic schema
    additions = []
    if "keyword_additions" in report:
        additions = report["keyword_additions"].get("vague", []) + report["keyword_additions"].get("valid_exceptions", [])
    removals = []
    if "keyword_removals" in report:
        removals = report["keyword_removals"].get("valid_exceptions", [])

    return SelfImprovementReport(
        report_id=report["report_id"],
        batch_number=report["batch_number"],
        transactions_evaluated=report["performance_data"]["total_evaluated"],
        total_alerts_generated=report["performance_data"]["total_flagged"],
        false_positive_estimate=report["performance_data"]["false_positive_rate"] * 100.0,
        false_negative_estimate=0.0,
        rule_weight_adjustments={r_id: float(w) for r_id, w in report.get("weight_adjustments", {}).items()},
        keyword_additions=additions,
        keyword_removals=removals,
        threshold_adjustments={k: float(v) for k, v in report.get("threshold_adjustments", {}).items() if v is not None},
        gemini_analysis=report.get("gemini_analysis", ""),
        applied=report.get("applied", False),
        created_at=report["created_at"],
        applied_at=report.get("applied_at")
    )

@router.post(
    "/improvement/{report_id}/apply",
    tags=["Improvement"],
    summary="Approve and apply a pending self-improvement report live"
)
async def approve_self_improvement_report(
    report_id: str,
    payload: ApplyImprovementRequest,
    db=Depends(get_db)
):
    """
    Compliance officer reviews and applies dynamic configuration changes suggested by the meta-analysis agent.
    Gated to prevent applying low-confidence warnings or reports with parsing errors.
    """
    report = await db.improvement_reports.find_one({"report_id": report_id})
    if not report:
        raise HTTPException(
            status_code=404,
            detail="Self-improvement report not found."
        )

    if report.get("applied"):
        raise HTTPException(
            status_code=400,
            detail="This self-improvement report has already been approved and applied."
        )

    # Prevent applying low confidence reports or reports containing execution errors
    if report.get("warning") or report.get("confidence_assessment") == "LOW":
        raise HTTPException(
            status_code=400,
            detail="Cannot apply report: Contains low-confidence warning flags or meta-analysis parsing anomalies."
        )

    # Insert compliance approval context into report before applying live weights
    await db.improvement_reports.update_one(
        {"report_id": report_id},
        {"$set": {"approved_by": payload.approved_by, "notes": payload.notes}}
    )

    # Apply configuration weights and keywords
    success = await apply_improvement_report_func(report_id, db)
    if not success:
        raise HTTPException(
            status_code=500,
            detail="Critical error occurred while writing live weight configurations to MongoDB."
        )

    return {
        "success": True,
        "message": f"Successfully approved by {payload.approved_by} and applied configurations.",
        "applied_at": datetime.utcnow(),
        "weight_adjustments": report.get("weight_adjustments", {}),
        "threshold_adjustments": report.get("threshold_adjustments", {}),
        "keyword_additions": report.get("keyword_additions", {}),
        "keyword_removals": report.get("keyword_removals", {})
    }

@router.get(
    "/stats",
    response_model=StatsResponse,
    tags=["Stats"],
    summary="Fetch aggregate dashboard analytical statistics"
)
async def get_dashboard_analytics_stats(db=Depends(get_db)):
    """
    Compiles operational KPI metrics for compliance tracking dashboards.
    """
    total_txns = await db.evaluation_log.count_documents({})
    total_alerts = await db.alerts.count_documents({})
    pending_alerts = await db.alerts.count_documents({"status": "PENDING"})
    high_risk_alerts = await db.alerts.count_documents({"risk_score.risk_level": "HIGH"})
    strs_filed = await db.alerts.count_documents({"status": "STR_FILED"})

    # Clear false positive metrics over the last 30 days
    cutoff_30d = datetime.utcnow() - timedelta(days=30)
    flagged_30d = await db.alerts.count_documents({"created_at": {"$gte": cutoff_30d}})
    cleared_30d = await db.alerts.count_documents({"created_at": {"$gte": cutoff_30d}, "status": "REVIEWED_CLEARED"})
    fpr_30d = float(cleared_30d) / flagged_30d if flagged_30d > 0 else 0.0

    # Self-improvement statistics
    self_improvement_runs = await db.improvement_reports.count_documents({})
    cursor = db.improvement_reports.find().sort("created_at", -1).limit(1)
    results = await cursor.to_list(length=1)
    last_run = results[0]["created_at"] if results else None

    # Load active rule weights
    from core.constants import RULE_WEIGHTS
    weights_doc = await db.rule_weights.find_one({"active": True})
    current_weights = weights_doc["weights"] if weights_doc else dict(RULE_WEIGHTS)

    return StatsResponse(
        total_transactions_evaluated=total_txns,
        total_alerts_generated=total_alerts,
        pending_alerts=pending_alerts,
        high_risk_alerts=high_risk_alerts,
        strs_filed=strs_filed,
        false_positive_rate_30d=fpr_30d,
        self_improvement_runs=self_improvement_runs,
        last_improvement_run=last_run,
        current_rule_weights={k: float(v) for k, v in current_weights.items()}
    )

# ---------------------------------------------------------------------------
# Hackathon Demo Endpoints (Gated by non-production constraint)
# ---------------------------------------------------------------------------

@router.post(
    "/demo/load-sample-data",
    tags=["Demo"],
    summary="Instantiates synthetic dataset loading (Gated)"
)
async def demo_load_synthetic_dataset(db=Depends(get_db)):
    """
    Generates realistic remittance profiles and transactions (including 10 seeded fraud patterns).
    Clears current collections to ensure a completely clean data seeding state.
    """
    if settings.ENVIRONMENT == "production":
        raise HTTPException(
            status_code=403,
            detail="Demo endpoints are restricted and disabled in production environment settings."
        )

    from data.generator import DataGenerator
    import anyio

    def execute_seeding_generator():
        # Using a compact seed generation (100 customers / 600 transactions) to make evaluations swift
        generator = DataGenerator(seed=42, n_customers=100, n_transactions=600)
        generator.generate_customers()
        generator.generate_transactions(generator.customers)
        return generator

    generator = await anyio.to_thread.run_sync(execute_seeding_generator)

    # Wipe MongoDB collections
    await db.customers.delete_many({})
    await db.transactions.delete_many({})

    # Seed
    if generator.customers:
        await db.customers.insert_many(generator.customers)
    if generator.transactions:
        await db.transactions.insert_many(generator.transactions)

    return {
        "success": True,
        "customers_loaded": len(generator.customers),
        "transactions_loaded": len(generator.transactions),
        "pattern_counts": generator.pattern_counts
    }

@router.post("/demo/load-demo-data", tags=["Demo"])
async def load_demo_data(db=Depends(get_db)):
    """
    Load a curated 100-transaction demo subset into MongoDB.
    Generates the dataset in-memory (fixed seed -> reproducible) and picks a
    representative spread:
    - 50 clean transactions
    - 30 HIGH risk transactions
    - 15 MEDIUM risk transactions
    - 5 LOW risk transactions
    No files on disk required, so this works in a stateless container.
    Only available in non-production environments.
    """
    if settings.ENVIRONMENT == "production":
        raise HTTPException(status_code=403, detail="Demo routes not available in production")

    from data.generator import DataGenerator
    import anyio

    # Generate in-memory in a worker thread so the (CPU-bound, sync) generator
    # does not block the event loop. Defaults (500/5000) give enough volume to
    # populate the high/medium/low buckets below.
    def execute_seeding_generator():
        generator = DataGenerator(seed=42)
        generator.generate_customers()
        generator.generate_transactions(generator.customers)
        return generator

    generator = await anyio.to_thread.run_sync(execute_seeding_generator)

    all_transactions = generator.transactions
    all_customers = generator.customers
    ground_truth = generator.ground_truth

    # Separate by fraud flag
    flagged = [t for t in all_transactions if ground_truth.get(t.get("ref_no"))]
    clean = [t for t in all_transactions if not ground_truth.get(t.get("ref_no"))]

    # Estimate risk level by base score for selection
    RULE_WEIGHTS = {
        "SANCTIONED_COUNTRY": 100, "STRUCTURING_MULTI_SENDER": 97,
        "SHARED_IDENTIFIER_NETWORK": 94, "REPEAT_FLAGS": 90,
        "INCOME_MISMATCH": 87, "TOURIST_NO_POW": 83,
        "ARTICLE_22_BREACH": 80, "NON_HOME_CORRIDOR": 72,
        "CORPORATE_PURPOSE_MISMATCH": 69, "INDIVIDUAL_TO_COMPANY": 65,
        "VAGUE_PURPOSE": 58, "MINOR_SENDER": 50,
    }

    high_risk = []
    medium_risk = []
    low_risk = []

    for t in flagged:
        rules = ground_truth.get(t.get("ref_no"), [])
        score = min(100, sum(RULE_WEIGHTS.get(r, 0) for r in rules))
        if score >= 75:
            high_risk.append(t)
        elif score >= 50:
            medium_risk.append(t)
        else:
            low_risk.append(t)

    # Curate demo subset
    demo_transactions = (
        clean[:50] +
        high_risk[:30] +
        medium_risk[:15] +
        low_risk[:5]
    )

    # Get sender IDs from demo transactions
    demo_sender_ids = {t.get("sender_id") for t in demo_transactions}
    demo_customers = [c for c in all_customers if c.get("sender_id") in demo_sender_ids]

    # NOTE: in-memory records already hold native datetime objects (the generator
    # builds them via Pydantic .model_dump()), so no ISO-string reconversion is
    # needed here, unlike the old file-based path.

    # Clear existing data and load demo subset
    await db.transactions.delete_many({})
    await db.customers.delete_many({})
    await db.alerts.delete_many({})
    await db.evaluation_log.delete_many({})

    if demo_transactions:
        await db.transactions.insert_many(demo_transactions)
    if demo_customers:
        await db.customers.insert_many(demo_customers)

    return {
        "success": True,
        "loaded": {
            "transactions": len(demo_transactions),
            "customers": len(demo_customers),
            "breakdown": {
                "clean": min(50, len(clean)),
                "high_risk": min(30, len(high_risk)),
                "medium_risk": min(15, len(medium_risk)),
                "low_risk": min(5, len(low_risk)),
            }
        },
        "message": "Demo dataset loaded. Run batch evaluation to process."
    }

# Global variable to track batch cancellation
_evaluation_cancelled = False

@router.post(
    "/demo/stop-batch-evaluation",
    tags=["Demo"],
    summary="Stops the currently running batch compliance evaluation"
)
async def demo_stop_batch_evaluation():
    global _evaluation_cancelled
    _evaluation_cancelled = True
    logger.info("Batch evaluation stop requested by compliance officer.")
    return {"success": True, "message": "Stop signal sent successfully."}

@router.post(
    "/demo/run-batch-evaluation",
    tags=["Demo"],
    summary="Runs batch compliance evaluation via Server-Sent Events (SSE) (Gated)"
)
async def demo_run_batch_evaluation_stream(request: Request, db=Depends(get_db)):
    """
    Sequentially processes all transactions in the database through the full evaluation pipeline.
    Streams evaluation logs in real-time via SSE and automatically triggers MLOps weight adjustments on completion.
    """
    if settings.ENVIRONMENT == "production":
        raise HTTPException(
            status_code=403,
            detail="Demo endpoints are restricted and disabled in production environment settings."
        )

    global _evaluation_cancelled
    _evaluation_cancelled = False

    cursor = db.transactions.find().sort("date", 1)
    transactions_docs = await cursor.to_list(length=10000)

    if not transactions_docs:
        raise HTTPException(
            status_code=400,
            detail="MongoDB transactions collection is empty. Run /demo/load-sample-data first."
        )

    async def compliance_sse_evaluator():
        global _evaluation_cancelled
        total = len(transactions_docs)
        flagged = 0
        high_risk = 0
        medium_risk = 0
        low_flagged = 0
        alerts_gen = 0
        strs_gen = 0
        failed_count = 0
        run_id = f"demo-batch-{uuid.uuid4().hex[:8]}"

        # Reset compliance records for fresh hackathon demonstration run
        await db.alerts.delete_many({})
        await db.str_reports.delete_many({})
        await db.evaluation_log.delete_many({})
        await db.improvement_reports.delete_many({})

        # Restore base rule weights to ensure optimization loop is clear
        await db.rule_weights.delete_many({})
        from core.constants import RULE_WEIGHTS
        await db.rule_weights.insert_one({
            "weights": dict(RULE_WEIGHTS),
            "active": True,
            "updated_at": datetime.utcnow()
        })

        yield "data: " + json.dumps({
            "status": "started",
            "total_transactions": total,
            "message": f"Successfully loaded {total} chronologically sorted transactions. Starting batch run..."
        }) + "\n\n"

        last_idx = 0
        for idx, txn_doc in enumerate(transactions_docs):
            if _evaluation_cancelled:
                logger.info("Batch evaluation cancelled: Stop flag set.")
                break

            if await request.is_disconnected():
                logger.info("Batch evaluation cancelled: Client disconnected.")
                break

            txn = Transaction(**txn_doc)
            last_idx = idx
            try:
                # Disable individual maybe_trigger_self_improvement during batch loop to avoid race conditions.
                # Loop will run meta-analysis once collectively at the end!
                res = await evaluate_transaction_pipeline(txn, db, start_self_improvement=False, run_id=run_id)

                if res.alert_generated:
                    flagged += 1
                    alerts_gen += 1
                    if res.risk_level == "HIGH":
                        high_risk += 1
                    elif res.risk_level == "MEDIUM":
                        medium_risk += 1
                    else:
                        low_flagged += 1
                if res.str_generated:
                    strs_gen += 1

                progress = round(((idx + 1) / total) * 100.0, 2)

                yield "data: " + json.dumps({
                    "progress": progress,
                    "evaluated_count": idx + 1,
                    "ref_no": txn.ref_no,
                    "risk_level": res.risk_level,
                    "risk_score": res.risk_score,
                    "rules_triggered": res.rules_triggered,
                    "alert_generated": res.alert_generated,
                    "str_generated": res.str_generated
                }) + "\n\n"

                # Tiny yield suspend to keep the streaming flow readable in dev UI
                await asyncio.sleep(0.01)

            except Exception as exc:
                logger.error("Batch evaluation error at transaction %s: %s", txn.ref_no, exc)
                failed_count += 1
                progress = round(((idx + 1) / total) * 100.0, 2)

                # Log the failure in evaluation_log
                try:
                    await db.evaluation_log.insert_one({
                        "ref_no": txn.ref_no,
                        "flagged": False,
                        "risk_level": "ERROR",
                        "status": "failed",
                        "error": str(exc),
                        "timestamp": datetime.utcnow()
                    })
                except Exception as db_exc:
                    logger.warning("Failed to log failed evaluation to database: %s", db_exc)

                yield "data: " + json.dumps({
                    "progress": progress,
                    "evaluated_count": idx + 1,
                    "ref_no": txn.ref_no,
                    "status": "failed",
                    "error": f"Failed evaluating {txn.ref_no}: {str(exc)}"
                }) + "\n\n"

                # Tiny yield suspend to keep the streaming flow readable in dev UI
                await asyncio.sleep(0.01)

        if _evaluation_cancelled:
            yield "data: " + json.dumps({
                "status": "cancelled",
                "message": "Batch evaluation stopped by compliance officer.",
                "summary": {
                    "total_submitted": total,
                    "evaluated": last_idx + 1 - failed_count,
                    "failed": failed_count,
                    "flagged": flagged,
                    "high_risk": high_risk,
                    "medium_risk": medium_risk,
                    "low_flagged": low_flagged,
                    "alerts_generated": alerts_gen,
                    "strs_generated": strs_gen
                }
            }) + "\n\n"
            return

        # Sequential processing complete — verify self-improvement threshold
        triggered_improve = False
        report_details = None
        if total >= settings.SELF_IMPROVE_AFTER_N_TRANSACTIONS:
            yield "data: " + json.dumps({
                "status": "improving",
                "message": f"All {total} transactions processed. Running self-improvement MLOps loop..."
            }) + "\n\n"

            try:
                # Force flushing OTel telemetry to ensure Arize Phoenix indexes the spans before query fetches
                from integrations.arize_client import _provider
                if _provider:
                    _provider.force_flush()
                
                # Buffer trace ingestion latency
                await asyncio.sleep(2.0)

                from agent.self_improvement import run_self_improvement_loop, GeminiAgent as ImproveAgent
                
                gemini_agent = ImproveAgent()
                report = await run_self_improvement_loop(db, arize_client, gemini_agent)
                
                triggered_improve = True
                report_details = {
                    "report_id": report.report_id,
                    "batch_number": report.batch_number,
                    "suggested_weight_adjustments": report.rule_weight_adjustments,
                    "analysis": report.gemini_analysis
                }
            except Exception as e:
                logger.error("MLOps self-improvement execution failed: %s", e)
                report_details = {"error": f"MLOps loop error: {str(e)}"}

        yield "data: " + json.dumps({
            "status": "completed",
            "summary": {
                "total_submitted": total,
                "evaluated": total - failed_count,
                "failed": failed_count,
                "flagged": flagged,
                "high_risk": high_risk,
                "medium_risk": medium_risk,
                "low_flagged": low_flagged,
                "alerts_generated": alerts_gen,
                "strs_generated": strs_gen
            },
            "self_improvement": {
                "triggered": triggered_improve,
                "report": report_details
            }
        }) + "\n\n"

    async def cache_wrapped_evaluator():
        enable_batch_cache()
        try:
            async for chunk in compliance_sse_evaluator():
                yield chunk
        finally:
            disable_batch_cache()

    return StreamingResponse(cache_wrapped_evaluator(), media_type="text/event-stream")
