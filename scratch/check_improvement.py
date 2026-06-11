import asyncio
import sys
import os
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.mongo import mongo_client

async def run():
    await mongo_client.connect()
    db = mongo_client.db
    r_count = await db.improvement_reports.count_documents({})
    print(f"Total reports: {r_count}")
    
    latest = await db.improvement_reports.find_one(sort=[("created_at", -1)])
    if latest:
        print("Latest Report details:")
        print(f"  Report ID: {latest.get('report_id')}")
        print(f"  Batch Number: {latest.get('batch_number')}")
        print(f"  Created At: {latest.get('created_at')}")
        print(f"  Applied: {latest.get('applied')}")
        print(f"  Warning flag: {latest.get('warning')}")
        print(f"  Confidence: {latest.get('confidence_assessment')}")
        print(f"  Weight Adjustments: {latest.get('weight_adjustments')}")
        print(f"  Threshold Adjustments: {latest.get('threshold_adjustments')}")
        print(f"  Keyword Additions: {latest.get('keyword_additions')}")
        print(f"  Keyword Removals: {latest.get('keyword_removals')}")
        print(f"  Human Review Flags: {latest.get('flags_for_human_review')}")
    else:
        print("No reports found.")
    
    # Check evaluation logs
    evals_count = await db.evaluation_log.count_documents({})
    print(f"Total evaluations: {evals_count}")
    
    await mongo_client.disconnect()

if __name__ == "__main__":
    asyncio.run(run())
