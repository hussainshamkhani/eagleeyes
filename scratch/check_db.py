import asyncio
import sys
import os

# Adjust python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.mongo import mongo_client

async def check_db():
    await mongo_client.connect()
    db = mongo_client.db
    alert_count = await db.alerts.count_documents({})
    txn_count = await db.transactions.count_documents({})
    print(f"Alert count: {alert_count}")
    print(f"Transaction count: {txn_count}")
    
    missing_txns = 0
    async for alert in db.alerts.find():
        ref_no = alert.get("ref_no")
        txn = await db.transactions.find_one({"ref_no": ref_no})
        if not txn:
            missing_txns += 1
            print(f"Alert {alert.get('alert_id')} references ref_no {ref_no} but it is missing from transactions collection!")
            
    print(f"Total alerts with missing transactions: {missing_txns}")
    await mongo_client.disconnect()

if __name__ == "__main__":
    asyncio.run(check_db())
