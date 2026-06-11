import asyncio
import sys
import os
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.mongo import mongo_client
from agent.gemini_agent import evaluate_transaction
from data.models import Transaction, SenderProfile, RiskScore, RuleViolation

async def test():
    await mongo_client.connect()
    db = mongo_client.db
    
    # Let's construct a mock transaction that triggers VAGUE_PURPOSE rule
    txn = Transaction(
        ref_no="TXN_TEST_AGENT",
        amount=100.0,
        currency="KWD",
        amount_kd=100.0,
        bank="Gulf Bank",
        date=datetime.utcnow(),
        acc_number="123456789",
        sender_id="29005231215958",  # Standard sender ID
        sender_name="John Doe",
        sender_nationality="IN",
        sender_tel="99999999",
        branch="Avenues Branch",
        recipient_name="Jane Doe",
        recipient_country="IN",
        transaction_purpose="help",  # vague purpose -> triggers rule!
        proof_of_wealth_provided=False,
        proof_of_relationship_provided=False,
    )
    
    sender_doc = await db.customers.find_one({"sender_id": txn.sender_id})
    if not sender_doc:
        print("KYC customer profile not found in DB. Run load-demo-data first.")
        await mongo_client.disconnect()
        return
        
    sender = SenderProfile(**sender_doc)
    
    risk_score = RiskScore(
        base_score=58.0,
        behavior_multiplier=1.0,
        recurrence_multiplier=1.0,
        network_multiplier=1.0,
        final_score=58.0,
        risk_level="MEDIUM",
        rules_triggered=[
            RuleViolation(
                rule_id="VAGUE_PURPOSE",
                rule_name="Vague Purpose indicator",
                description="Transaction purpose contains vague keywords",
                base_weight=58.0,
                contributing_factors=[]
            )
        ]
    )
    
    print("Evaluating transaction with real Gemini ADK Agent...")
    start_time = datetime.utcnow()
    try:
        result = await evaluate_transaction(txn, sender, risk_score)
        print(f"Success! Duration: {(datetime.utcnow() - start_time).total_seconds()}s")
        print("Result:")
        print(result)
    except Exception as e:
        print(f"Error evaluating transaction: {e}")
        
    await mongo_client.disconnect()

if __name__ == "__main__":
    asyncio.run(test())
