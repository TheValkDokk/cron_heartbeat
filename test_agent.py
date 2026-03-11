import asyncio
from src.agent_runner import execute_agent_task

async def main():
    await execute_agent_task(12, "Say hello and introduce yourself briefly.")

if __name__ == "__main__":
    asyncio.run(main())
