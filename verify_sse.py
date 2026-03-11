
import asyncio
from src.database import AsyncSessionLocal
from src.agent_runner import execute_agent_task
from sqlalchemy.future import select
from src import models
import json

async def verify_sse_flow():
    agent_id = 12
    user_id = 1 # owner of agent 12
    
    print("--- Starting SSE + Messages Verification ---")
    
    async with AsyncSessionLocal() as session:
        # 1. Check initial message count
        res = await session.execute(select(models.Message).filter(models.Message.owner_id == user_id))
        count_before = len(res.scalars().all())
        print(f"Messages before: {count_before}")
        
        # 2. Trigger a job
        print(f"Triggering execution for agent {agent_id}...")
        await execute_agent_task(agent_id, "Just say 'Verification successful' and nothing else.")
        
        # 3. Check message count after
        res = await session.execute(select(models.Message).filter(models.Message.owner_id == user_id))
        count_after = len(res.scalars().all())
        print(f"Messages after: {count_after}")
        
        if count_after > count_before:
            print("✅ Message persisted to DB.")
        else:
            print("❌ Message NOT persisted to DB.")
            
        # 4. Check the latest message content
        res = await session.execute(
            select(models.Message)
            .filter(models.Message.owner_id == user_id)
            .order_by(models.Message.created_at.desc())
            .limit(1)
        )
        latest = res.scalars().first()
        print(f"Latest message source: {latest.source}")
        print(f"Latest message content: {latest.content}")

if __name__ == "__main__":
    asyncio.run(verify_sse_flow())
