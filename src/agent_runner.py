from google import genai
from google.genai import types
import asyncio
from pathlib import Path
from datetime import datetime
from sqlalchemy.future import select

from .database import AsyncSessionLocal
from . import models
from .config import settings
from .tools import TOOL_DECLARATIONS, execute_tool
from .broadcaster import publish

# Initialize Gemini Client
client = genai.Client(api_key=settings.gemini_api_key)

# Shared results directory (also kept for raw file backup)
JOB_RESULTS_DIR = Path(__file__).parent.parent / "job_results"
JOB_RESULTS_DIR.mkdir(exist_ok=True)

MODEL = "gemini-2.5-flash"
MAX_TOOL_ROUNDS = 5


def save_job_result(agent_id: int, task_description: str, output: str):
    """Write a cron job result to a timestamped .txt file in /job_results/."""
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = JOB_RESULTS_DIR / f"agent_{agent_id}_{timestamp}.txt"
    content = (
        f"Agent ID   : {agent_id}\n"
        f"Task       : {task_description}\n"
        f"Ran at     : {datetime.utcnow().isoformat()} UTC\n"
        f"{'-'*60}\n"
        f"{output}\n"
    )
    filename.write_text(content, encoding="utf-8")
    print(f"[{agent_id}] Result saved → {filename}")


async def save_message(
    owner_id: int,
    content: str,
    source: str,
    from_agent_id: int | None = None,
    chat_agent_id: int | None = None,
) -> models.Message:
    """Persist a message to the DB with its vector embedding and return the saved record."""
    try:
        embed_res = client.models.embed_content(
            model="text-embedding-004",
            contents=content
        )
        embedding_vals = embed_res.embeddings[0].values
    except Exception as e:
        print(f"Embedding failed: {e}")
        embedding_vals = None

    async with AsyncSessionLocal() as session:
        msg = models.Message(
            owner_id=owner_id,
            from_agent_id=from_agent_id,
            chat_agent_id=chat_agent_id,
            source=source,
            content=content,
            embedding=embedding_vals,
        )
        session.add(msg)
        await session.commit()
        await session.refresh(msg)
        return msg


async def _get_relevant_context(owner_id: int, query: str, limit: int = 10) -> str:
    """Perform a similarity search to find past messages related to the query."""
    try:
        embed_res = client.models.embed_content(
            model="text-embedding-004",
            contents=query
        )
        query_embedding = embed_res.embeddings[0].values
        
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(models.Message)
                .filter(models.Message.owner_id == owner_id)
                # Ensure the embedding column is populated
                .filter(models.Message.embedding.is_not(None))
                .order_by(models.Message.embedding.cosine_distance(query_embedding))
                .limit(limit)
            )
            similar_messages = result.scalars().all()
            
        if not similar_messages:
            return ""
            
        context_lines = []
        for m in similar_messages:
            ts = m.created_at.strftime('%Y-%m-%d %H:%M:%S') if m.created_at else 'Unknown'
            context_lines.append(f"[{ts}] {m.source.upper()}: {m.content}")
            
        return "\n\n".join(context_lines)
    except Exception as e:
        print(f"RAG search failed: {e}")
        return ""


async def _run_agentic_loop(
    agent_id: int,
    system_prompt: str,
    initial_prompt: str,
) -> str:
    """
    Core agentic loop: prompt → tool calls → tool results → … → final text.
    Returns the final text response from the model.
    """
    contents = [types.Content(role="user", parts=[types.Part(text=initial_prompt)])]
    tool_config = types.Tool(function_declarations=TOOL_DECLARATIONS)
    final_text = ""

    for _ in range(MAX_TOOL_ROUNDS):
        response = client.models.generate_content(
            model=MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                tools=[tool_config],
            ),
        )

        contents.append(types.Content(role="model", parts=response.candidates[0].content.parts))

        if not response.function_calls:
            final_text = response.text or ""
            break

        tool_result_parts = []
        for call in response.function_calls:
            print(f"  [tool] {call.name}({dict(call.args)})")
            if call.name == "create_agent":
                await handle_create_agent_tool(agent_id, dict(call.args))
                result_str = f"Agent '{call.args.get('name')}' created and scheduled successfully."
            else:
                result_str = await execute_tool(call.name, dict(call.args), agent_id)

            tool_result_parts.append(
                types.Part.from_function_response(
                    name=call.name,
                    response={"result": result_str},
                )
            )
        contents.append(types.Content(role="user", parts=tool_result_parts))

    return final_text


async def execute_agent_task(agent_id: int, task_description: str, job_id: int | None = None):
    """
    Triggered by APScheduler when a cron job is due.
    Runs the agentic loop, saves result to DB, broadcasts via SSE.
    """
    print(f"[{agent_id}] Waking up agent: {task_description}")

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(models.Agent).filter(models.Agent.id == agent_id))
        agent = result.scalars().first()
        if not agent:
            print(f"[{agent_id}] Agent not found, skipping.")
            return
        system_prompt = agent.system_prompt
        owner_id = agent.owner_id
        agent_name = agent.name

    # Fetch RAG context
    rag_context = await _get_relevant_context(owner_id, task_description)
    if rag_context:
        system_prompt += f"\n\n--- RELEVANT KNOWLEDGE / PAST CONTEXT ---\n{rag_context}\n-----------------------------------------\n"

    prompt = (
        f"System Event (Cron Trigger): Execute the following scheduled task now.\n"
        f"Task: {task_description}"
    )

    try:
        final_text = await _run_agentic_loop(agent_id, system_prompt, prompt)
        output = final_text or "[no text output]"
        print(f"[{agent_id}] Done: {output[:120]}")

        # 1. Save to .txt
        save_job_result(agent_id, task_description, output)

        # 2. Save to messages table (source=cron, broadcast = no specific chat)
        job_prefix = f"[Job #{job_id}] " if job_id else ""
        msg = await save_message(
            owner_id=owner_id,
            content=f"{job_prefix}{output}",
            source="cron",
            from_agent_id=agent_id,
            chat_agent_id=None,  # visible in any agent's chat for this user
        )

        # 3. Push to SSE — any open browser tab for this user will receive it
        await publish(owner_id, {
            "type": "cron_result",
            "agent_id": agent_id,
            "agent_name": agent_name,
            "message_id": msg.id,
            "content": f"{job_prefix}{output}",
            "created_at": msg.created_at.isoformat(),
        })

    except Exception as e:
        error_msg = f"[Job #{job_id}] Execution Error: {str(e)}" if job_id else f"Execution Error: {str(e)}"
        print(f"[{agent_id}] {error_msg}")
        # Save error message to DB so the user sees it
        msg = await save_message(
            owner_id=owner_id,
            content=error_msg,
            source="error",
            from_agent_id=agent_id,
            chat_agent_id=None,
        )
        # Broadcast error
        await publish(owner_id, {
            "type": "cron_result",
            "agent_id": agent_id,
            "agent_name": agent_name,
            "message_id": msg.id,
            "content": error_msg,
            "created_at": msg.created_at.isoformat(),
        })


async def chat_with_agent(
    agent_id: int,
    owner_id: int,
    message: str,
) -> dict:
    """
    Direct chat from the UI.
    - Loads full message history for context.
    - Runs the agentic loop.
    - Saves user + agent messages to DB.
    """
    async with AsyncSessionLocal() as session:
        # Fetch agent
        result = await session.execute(select(models.Agent).filter(models.Agent.id == agent_id))
        agent = result.scalars().first()
        if not agent:
            raise ValueError("Agent not found")
        system_prompt = agent.system_prompt

        # Fetch RAG context
        rag_context = await _get_relevant_context(owner_id, message)
        if rag_context:
            system_prompt += f"\n\n--- RELEVANT KNOWLEDGE / PAST CONTEXT ---\n{rag_context}\n-----------------------------------------\n"

        # Load last 30 messages for this user as context history
        history_result = await session.execute(
            select(models.Message)
            .filter(models.Message.owner_id == owner_id)
            .order_by(models.Message.created_at.desc())
            .limit(30)
        )
        history = list(reversed(history_result.scalars().all()))

    # Save the user's message
    await save_message(
        owner_id=owner_id,
        content=message,
        source="user",
        from_agent_id=None,
        chat_agent_id=agent_id,
    )

    # Build Gemini conversation from history
    contents: list[types.Content] = []
    for h in history:
        role = "user" if h.source == "user" else "model"
        prefix = "" if h.source == "user" else f"[{h.source.upper()}] "
        contents.append(types.Content(role=role, parts=[types.Part(text=f"{prefix}{h.content}")]))

    # Append the new user message
    contents.append(types.Content(role="user", parts=[types.Part(text=message)]))

    tool_log: list[dict] = []
    tool_config = types.Tool(function_declarations=TOOL_DECLARATIONS)
    final_text = ""

    try:
        for _ in range(MAX_TOOL_ROUNDS):
            response = client.models.generate_content(
                model=MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    tools=[tool_config],
                ),
            )
            contents.append(types.Content(role="model", parts=response.candidates[0].content.parts))

            if not response.function_calls:
                final_text = response.text or ""
                break

            tool_result_parts = []
            for call in response.function_calls:
                if call.name == "create_agent":
                    await handle_create_agent_tool(agent_id, dict(call.args))
                    result_str = f"Agent '{call.args.get('name')}' created and scheduled successfully."
                else:
                    result_str = await execute_tool(call.name, dict(call.args))

                tool_log.append({"name": call.name, "args": dict(call.args)})
                tool_result_parts.append(
                    types.Part.from_function_response(
                        name=call.name,
                        response={"result": result_str},
                    )
                )
            contents.append(types.Content(role="user", parts=tool_result_parts))

        # Save agent's reply to DB
        await save_message(
            owner_id=owner_id,
            content=final_text,
            source="agent",
            from_agent_id=agent_id,
            chat_agent_id=agent_id,
        )

        return {"response": final_text, "tool_calls": tool_log}

    except Exception as e:
        print(f"[{agent_id}] Chat error: {e}")
        return {"response": f"Error: {e}", "tool_calls": tool_log}


async def handle_create_agent_tool(parent_agent_id: int, args: dict):
    """Persist a new agent + cron job to the DB."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(models.Agent).filter(models.Agent.id == parent_agent_id))
        parent = result.scalars().first()
        if not parent:
            return

        print(f"[Tool: create_agent] Creating '{args['name']}'...")
        new_agent = models.Agent(
            name=args["name"],
            system_prompt=args["system_prompt"],
            owner_id=parent.owner_id,
            parent_agent_id=parent_agent_id,
        )
        session.add(new_agent)
        await session.commit()
        await session.refresh(new_agent)

        new_job = models.CronJob(
            agent_id=new_agent.id,
            schedule=args["schedule"],
            task_description=args["task_description"],
        )
        session.add(new_job)
        await session.commit()
        await session.refresh(new_job)

        # Register with the live APScheduler instance
        from .main import scheduler
        from .scheduler_service import register_job
        await register_job(scheduler, new_job)
        
        print(f"[Tool: create_agent] Agent #{new_agent.id} created and scheduled.")
