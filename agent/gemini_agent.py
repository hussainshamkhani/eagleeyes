import json
import logging
import uuid
from datetime import datetime
 
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types
from opentelemetry import trace
 
from agent.eagleeyes_agent import root_agent
from data.models import Transaction, SenderProfile, RiskScore, Alert
from integrations.arize_client import tracer
 
logger = logging.getLogger(__name__)
 
# ADK session service — in-memory is fine for stateless per-transaction evaluation
session_service = InMemorySessionService()
 
runner = Runner(
    agent=root_agent,
    app_name="eagleeyes",
    session_service=session_service,
)
 
 
import asyncio
 
SEMAPHORE = asyncio.Semaphore(5)
 
async def evaluate_transaction_with_limit(transaction, sender, risk_score, run_id=None):
    async with SEMAPHORE:
        if run_id is not None:
            return await evaluate_transaction(transaction, sender, risk_score, run_id=run_id)
        else:
            return await evaluate_transaction(transaction, sender, risk_score)
 
 
def _build_evaluation_prompt(
    transaction: Transaction,
    sender: SenderProfile,
    risk_score: RiskScore,
) -> str:
    """
    Build the structured evaluation prompt from transaction data.
    This is passed as the user message to the ADK runner.
    """
    from dateutil.relativedelta import relativedelta
 
    today = datetime.utcnow().date()
    dob = sender.date_of_birth
    age = relativedelta(today, dob).years if dob else "Unknown"
 
    rules_text = "\n".join([
        f"  - {r.rule_id} (weight: {r.base_weight}): {r.description}"
        for r in risk_score.rules_triggered
    ])
 
    return f"""
TRANSACTION UNDER REVIEW
========================
Reference Number: {transaction.ref_no}
Date: {transaction.date}
Branch: {transaction.branch}
 
SENDER DETAILS
--------------
Name: {sender.full_name}
ID: {sender.sender_id}
Nationality: {sender.nationality}
Customer Type: {sender.customer_type}
Residency Article: {sender.residency_article or "N/A"}
Monthly Income (Declared): {sender.monthly_income_kd or "Not declared"} KD
Is PEP: {sender.is_pep}
Age: {age}
 
TRANSACTION DETAILS
-------------------
Amount: {transaction.amount} {transaction.currency} = {transaction.amount_kd:.3f} KD
Recipient: {transaction.recipient_name} ({transaction.recipient_country})
Account: {transaction.acc_number}
Recipient is Company: {transaction.recipient_is_company}
{f"Recipient Company: {transaction.recipient_company_name}" if transaction.recipient_is_company else ""}
Transaction Purpose: {transaction.transaction_purpose}
Proof of Wealth: {transaction.proof_of_wealth_provided}
Proof of Relationship: {transaction.proof_of_relationship_provided}
Sender is Corporate: {transaction.sender_is_corporate}
{f"Sender Company: {transaction.sender_company_name}" if transaction.sender_is_corporate else ""}
 
RISK SCORE
----------
Final Score: {risk_score.final_score:.1f} / 100
Risk Level: {risk_score.risk_level}
Behavior Multiplier: {risk_score.behavior_multiplier:.2f}x
Recurrence Multiplier: {risk_score.recurrence_multiplier:.2f}x
Network Multiplier: {risk_score.network_multiplier:.2f}x
 
TRIGGERED RULES
---------------
{rules_text if rules_text else "None"}
 
Use your tools to look up sender history, recipient network, and prior alerts before concluding.
Then provide your JSON compliance assessment.
""".strip()
 
 
async def evaluate_transaction(
    transaction: Transaction,
    sender: SenderProfile,
    risk_score: RiskScore,
    run_id: str | None = None,
) -> dict:
    """
    [TEMPORARY MOCK FOR DEMO PURPOSES]
    Instantly returns a realistic evaluation response based on the triggered rules.
    """
    logger.info(f"DEMO MOCK: Evaluating transaction {transaction.ref_no} instantly.")
    await asyncio.sleep(0.05)
    
    rules = [r.rule_id for r in risk_score.rules_triggered]
    action = "CLEAR"
    if risk_score.risk_level == "HIGH":
        action = "FILE_STR" if risk_score.final_score >= 85 else "HOLD"
    elif risk_score.risk_level == "MEDIUM":
        action = "MONITOR"
        
    narrative_points = [
        f"- Flagged indicators: {', '.join(rules) if rules else 'None'}.",
        f"- Customer {sender.full_name} ({sender.nationality}) article {sender.residency_article or 'N/A'} is reviewed.",
        f"- Remitted amount of {transaction.amount_kd:.2f} KD is evaluated against historical patterns."
    ]
    
    return {
        "narrative": "\n".join(narrative_points),
        "confidence": 0.85 if action in ["HOLD", "FILE_STR"] else 0.60,
        "recommended_action": action,
        "additional_info_required": ["Source of income statement"] if action != "CLEAR" else [],
        "applicable_regulations": [f"Regulation Reference for {r.replace('_', ' ')}" for r in rules]
    }
 
 
def _parse_agent_response(response_text: str) -> dict:
    """
    Parse JSON from ADK agent response.
    Handles markdown code fences if present.
    Falls back to safe default on parse failure.
    """
    if not response_text:
        return _default_fallback_response()
 
    # Strip markdown fences if present
    clean = response_text.strip()
    if clean.startswith("```"):
        lines = clean.split("\n")
        clean = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
 
    try:
        result = json.loads(clean)
        # Validate and clamp confidence
        result["confidence"] = max(0.0, min(1.0, float(result.get("confidence", 0.5))))
        # Validate recommended_action
        valid_actions = {"CLEAR", "MONITOR", "HOLD", "FILE_STR"}
        if result.get("recommended_action") not in valid_actions:
            result["recommended_action"] = "HOLD"
        return result
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"Failed to parse agent response: {e}\nRaw: {response_text[:500]}")
        return _default_fallback_response()
 
 
def _default_fallback_response() -> dict:
    return {
        "narrative": "Agent evaluation failed — manual review required. The automated assessment could not be completed. A compliance officer should review this transaction manually.",
        "confidence": 0.5,
        "recommended_action": "HOLD",
        "additional_info_required": ["Manual review required — agent parsing failed"],
        "applicable_regulations": ["CBK AML/CFT General Guidelines"],
    }