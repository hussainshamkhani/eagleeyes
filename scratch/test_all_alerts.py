import asyncio
import sys
import os
from fastapi.testclient import TestClient

# Adjust python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import app

def test_all_alerts():
    with TestClient(app) as client:
        # Fetch pending alerts
        res_list = client.get("/api/v1/alerts?status=PENDING&limit=100")
        if res_list.status_code != 200:
            print(f"Failed to fetch alerts list: {res_list.status_code}")
            return
        
        alerts = res_list.json()
        print(f"Fetched {len(alerts)} pending alerts from the database.")
        
        null_txn_count = 0
        for alert in alerts:
            alert_id = alert["alert_id"]
            res_detail = client.get(f"/api/v1/alerts/{alert_id}")
            if res_detail.status_code == 200:
                detail_data = res_detail.json()
                txn = detail_data.get("transaction")
                if txn is None:
                    null_txn_count += 1
                    print(f"Alert {alert_id} (Ref: {alert['ref_no']}) returned transaction = null!")
            else:
                print(f"Failed to get details for alert {alert_id}: {res_detail.status_code}")
                
        print(f"Total alerts returning transaction = null: {null_txn_count}")

if __name__ == "__main__":
    test_all_alerts()
