import sys
import os
import json
from fastapi.testclient import TestClient

# Adjust python path to import project files
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import app

def test_evaluation_pipeline():
    with TestClient(app) as client:
        # Seed demo data first to ensure we have customers KYC registered in DB
        seed_res = client.post("/api/v1/demo/load-demo-data", json={})
        print(f"Seeded demo data: {seed_res.status_code}")
        
        # Load sample transaction data from file directly
        txn_path = "data/generated/transactions.json"
        if not os.path.exists(txn_path):
            print(f"Transaction file {txn_path} not found.")
            return

        with open(txn_path) as f:
            all_transactions = json.load(f)
        
        # Let's find a transaction that belongs to a loaded customer
        # Load customers to be sure
        cust_path = "data/generated/customers.json"
        with open(cust_path) as f:
            all_customers = json.load(f)
        
        loaded_sender_ids = {c["sender_id"] for c in all_customers}
        
        # Filter for transaction with valid sender_id
        valid_txn = None
        for t in all_transactions:
            if t.get("sender_id") in loaded_sender_ids:
                valid_txn = t
                break
                
        if not valid_txn:
            print("No valid transaction found with matching sender ID.")
            return

        print(f"Evaluating transaction: {valid_txn.get('ref_no')} for sender {valid_txn.get('sender_id')}")
        
        # Run evaluation
        eval_res = client.post("/api/v1/transactions/evaluate", json=valid_txn)
        assert eval_res.status_code == 200, f"Expected 200, got {eval_res.status_code} - {eval_res.text}"
        
        eval_data = eval_res.json()
        print("\nEvaluation Response:")
        for k, v in eval_data.items():
            if k == "gemini_narrative":
                print(f"  {k}: {str(v)[:120]}...")
            else:
                print(f"  {k}: {v}")
                
        assert eval_data.get("evaluated") is True, "Expected evaluated to be True"
        assert eval_data.get("arize_trace_id") is not None, "Expected Arize trace ID to be attached to response"
        print("\n[SUCCESS] Transaction evaluation pipeline and tracing propagation fully verified!")

if __name__ == "__main__":
    test_evaluation_pipeline()
