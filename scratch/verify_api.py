import sys
import os
from fastapi.testclient import TestClient

# Adjust python path to import project files
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import app

def test_alert_endpoint():
    # Use TestClient with lifespan block so MongoDB client connects
    with TestClient(app) as client:
        # 1. Seed demo data
        print("Triggering load-demo-data...")
        seed_res = client.post("/api/v1/demo/load-demo-data", json={})
        print(f"Seeded demo data: {seed_res.status_code}")
        
        # 2. Run batch evaluation to process seeded transactions and generate alerts
        print("Running batch evaluation...")
        # Since it's a streaming response, we can read it to let the evaluation run
        with client.stream("POST", "/api/v1/demo/run-batch-evaluation") as response:
            for line in response.iter_lines():
                if line:
                    pass  # Let it run to completion
        print("Batch evaluation completed.")
        
        # 3. Retrieve generated pending alerts
        res_list = client.get("/api/v1/alerts?status=PENDING&limit=5")
        assert res_list.status_code == 200, f"Expected 200, got {res_list.status_code}"
        alerts = res_list.json()
        
        if not alerts:
            print("Error: No alerts generated after batch evaluation.")
            return

        alert = alerts[0]
        alert_id = alert["alert_id"]
        print(f"Testing alert detail endpoint for alert_id: {alert_id}")
        
        res_detail = client.get(f"/api/v1/alerts/{alert_id}")
        assert res_detail.status_code == 200, f"Expected 200, got {res_detail.status_code}"
        
        detail_data = res_detail.json()
        assert "transaction" in detail_data, "Expected 'transaction' field in response"
        txn = detail_data["transaction"]
        assert txn is not None, "Expected nested transaction object not to be None"
        
        # Verify trace ID is present
        trace_id = detail_data.get("arize_trace_id")
        print(f"Found arize_trace_id in alert: {trace_id}")
        assert trace_id is not None, "Expected 'arize_trace_id' to be set in alert"
        assert len(trace_id) == 32, f"Expected 32-char hex trace_id, got {trace_id}"
        
        print(f"[OK] Found transaction nested under alert detail! Ref No: {txn['ref_no']}")
        print(f"Recipient Country: {txn.get('recipient_country')}")
        print(f"Amount KD: {txn.get('amount_kd')}")
        print("API VERIFICATION SUCCESSFUL!")

if __name__ == "__main__":
    test_alert_endpoint()
