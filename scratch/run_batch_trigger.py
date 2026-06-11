import asyncio
import httpx
import json

async def run_batch():
    url = "http://localhost:8000/api/v1/demo/load-demo-data"
    print("Loading demo data...")
    async with httpx.AsyncClient(timeout=None) as client:
        res = await client.post(url, json={})
        print(f"Load demo status: {res.status_code}")
        print(res.json())
        
        print("\nStarting batch evaluation...")
        batch_url = "http://localhost:8000/api/v1/demo/run-batch-evaluation"
        async with client.stream("POST", batch_url) as response:
            print(f"Batch evaluation status: {response.status_code}")
            buffer = ""
            async for chunk in response.aiter_text():
                buffer += chunk
                while "\n\n" in buffer:
                    line, buffer = buffer.split("\n\n", 1)
                    if line.startswith("data: "):
                        data_str = line[6:].strip()
                        if data_str:
                            try:
                                data = json.loads(data_str)
                                if "status" in data:
                                    print(f"[{data['status'].upper()}] {data.get('message', '')}")
                                    if data['status'] == 'completed':
                                        print(f"Summary: {data.get('summary')}")
                                        print(f"Self-Improvement: {data.get('self_improvement')}")
                                elif "evaluated_count" in data:
                                    if data['evaluated_count'] % 10 == 0 or data['evaluated_count'] == 95:
                                        print(f"Processed: {data['evaluated_count']} / {data.get('progress')}% (Flagged: {data.get('alert_generated')})")
                            except Exception as e:
                                print(f"Parse error: {e} - Raw line: {line}")

if __name__ == "__main__":
    asyncio.run(run_batch())
