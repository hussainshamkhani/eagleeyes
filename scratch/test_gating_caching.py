import sys
import os
import asyncio
from unittest.mock import AsyncMock, patch

# Adjust python path to import project files
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.models import Transaction, SenderProfile, RiskScore, RuleViolation
from agent.tools import (
    enable_batch_cache,
    disable_batch_cache,
    get_sender_transaction_history,
    get_recipient_network,
    get_sender_alert_history,
    get_sender_annual_total,
)
from api.routes import _run_gemini_if_needed

from datetime import date

# Create mock data objects for testing
mock_sender = SenderProfile(
    sender_id="sender_123",
    full_name="Fatima Al-Sabah",
    nationality="KW",
    customer_type="resident",
    phone="96599999999",
    is_pep=False,
    date_of_birth=date(1990, 1, 1)
)

from datetime import datetime

mock_transaction = Transaction(
    ref_no="REF_9999",
    amount=500.0,
    currency="KWD",
    amount_kd=500.0,
    bank="NBK",
    acc_number="123456",
    sender_id="sender_123",
    sender_name="Fatima Al-Sabah",
    sender_nationality="KW",
    sender_tel="96599999999",
    branch="Salmiya",
    recipient_name="John Smith",
    recipient_country="US",
    transaction_purpose="Family Support",
    proof_of_wealth_provided=False,
    proof_of_relationship_provided=False,
    date=datetime.utcnow()
)

async def test_gating():
    print("--- Testing Gemini Call Gating ---")

    # Scenario 1: HIGH risk -> should trigger Gemini call
    risk_score_high = RiskScore(
        base_score=85.0,
        risk_level="HIGH",
        final_score=85.0,
        rules_triggered=[],
    )
    with patch("api.routes.evaluate_transaction_with_limit", new_callable=AsyncMock) as mock_gemini:
        mock_gemini.return_value = {"narrative": "Gemini assessed HIGH risk", "confidence": 0.9}
        res = await _run_gemini_if_needed(mock_transaction, mock_sender, risk_score_high)
        assert res["narrative"] == "Gemini assessed HIGH risk"
        mock_gemini.assert_called_once_with(mock_transaction, mock_sender, risk_score_high)
        print("[OK] HIGH risk correctly escalates to Gemini.")

    # Scenario 2: Strong MEDIUM risk (score >= 60) -> should trigger Gemini call
    risk_score_strong_med = RiskScore(
        base_score=65.0,
        risk_level="MEDIUM",
        final_score=65.0,
        rules_triggered=[],
    )
    with patch("api.routes.evaluate_transaction_with_limit", new_callable=AsyncMock) as mock_gemini:
        mock_gemini.return_value = {"narrative": "Gemini assessed strong MEDIUM risk", "confidence": 0.8}
        res = await _run_gemini_if_needed(mock_transaction, mock_sender, risk_score_strong_med)
        assert res["narrative"] == "Gemini assessed strong MEDIUM risk"
        mock_gemini.assert_called_once_with(mock_transaction, mock_sender, risk_score_strong_med)
        print("[OK] Strong MEDIUM risk (score >= 60) correctly escalates to Gemini.")

    # Scenario 3: Weak MEDIUM risk (score < 60) -> should return template, NO Gemini call
    risk_score_weak_med = RiskScore(
        base_score=55.0,
        risk_level="MEDIUM",
        final_score=55.0,
        rules_triggered=[
            RuleViolation(rule_id="RULE_01", rule_name="Rule 01", base_weight=10.0, description="Vague purpose", contributing_factors=[])
        ],
    )
    with patch("api.routes.evaluate_transaction_with_limit", new_callable=AsyncMock) as mock_gemini:
        res = await _run_gemini_if_needed(mock_transaction, mock_sender, risk_score_weak_med)
        mock_gemini.assert_not_called()
        assert "moderate risk score of 55.0/100" in res["narrative"]
        assert res["recommended_action"] == "MONITOR"
        assert res["confidence"] == 0.5
        print("[OK] Weak MEDIUM risk (score < 60) correctly gated with template narrative.")

    # Scenario 4: LOW risk -> should return template, NO Gemini call
    risk_score_low = RiskScore(
        base_score=12.5,
        risk_level="LOW",
        final_score=12.5,
        rules_triggered=[],
    )
    with patch("api.routes.evaluate_transaction_with_limit", new_callable=AsyncMock) as mock_gemini:
        res = await _run_gemini_if_needed(mock_transaction, mock_sender, risk_score_low)
        mock_gemini.assert_not_called()
        assert "scored 12.5/100" in res["narrative"]
        assert "No significant AML indicators detected" in res["narrative"]
        assert res["recommended_action"] == "CLEAR"
        assert res["confidence"] == 0.95
        print("[OK] LOW risk correctly gated with auto-clear narrative.")


async def test_caching():
    print("--- Testing In-Memory Batch Caching ---")

    # We will mock the mongo_client functions used in agent/tools.py
    from db.mongo import mongo_client

    # 1. Test get_sender_transaction_history caching
    with patch.object(mongo_client, "get_transactions_by_sender", new_callable=AsyncMock) as mock_db:
        mock_db.return_value = [{"amount_kd": 100}, {"amount_kd": 200}]

        # Without cache enabled, every call should trigger DB query
        res1 = await get_sender_transaction_history("sender_123", 30)
        res2 = await get_sender_transaction_history("sender_123", 30)
        assert mock_db.call_count == 2
        print("[OK] Without cache, DB is queried each time.")

        mock_db.reset_mock()

        # Enable cache
        enable_batch_cache()
        res3 = await get_sender_transaction_history("sender_123", 30)
        res4 = await get_sender_transaction_history("sender_123", 30)
        assert mock_db.call_count == 1
        assert res3 == res4
        print("[OK] With cache enabled, DB is queried only once.")

        mock_db.reset_mock()

        # Disable cache
        disable_batch_cache()
        res5 = await get_sender_transaction_history("sender_123", 30)
        assert mock_db.call_count == 1
        print("[OK] Disabling cache clears and bypasses the cache.")


async def main():
    try:
        await test_gating()
        await test_caching()
        print("\nALL VERIFICATION TESTS PASSED SUCCESSFULLY!")
    except AssertionError as e:
        print("\nTEST FAILURE DETECTED!")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
