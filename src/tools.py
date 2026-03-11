"""
Agent Tool Suite
================
Defines both the Gemini FunctionDeclarations (schema sent to the LLM)
and the actual Python executors that run when the LLM calls a tool.

Add new tools in two places:
  1. TOOL_DECLARATIONS  – the schema Gemini sees
  2. execute_tool()     – the Python logic that runs the tool
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from google.genai import types

# Mirrors the same path used in agent_runner.py
JOB_RESULTS_DIR = Path(__file__).parent.parent / "job_results"
JOB_RESULTS_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# 1. TOOL DECLARATIONS  (sent to Gemini as function definitions)
# ---------------------------------------------------------------------------

TOOL_DECLARATIONS = [

    # -- Agent Factory -------------------------------------------------------
    types.FunctionDeclaration(
        name="create_agent",
        description=(
            "Create a new persistent AI agent and schedule a recurring task for it. "
            "Use this when the user asks you to set up an automated job or an agent "
            "that should run on a schedule. NOTE: Any text the created agent outputs "
            "will automatically appear in the user's native web UI. You do NOT need to "
            "instruct the agent to use Slack or Discord unless the user explicitly requests it."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "name":             types.Schema(type=types.Type.STRING, description="Name of the new agent"),
                "system_prompt":    types.Schema(type=types.Type.STRING, description="Instructions the new agent follows."),
                "schedule":         types.Schema(type=types.Type.STRING, description="Cron expression (e.g. '0 9 * * 1-5') for recurring tasks, OR an ISO-8601 datetime string (e.g. '2026-03-11T09:56:00+07:00') for one-off future tasks."),
                "task_description": types.Schema(type=types.Type.STRING, description="The task the agent performs each run."),
            },
            required=["name", "system_prompt", "schedule", "task_description"],
        ),
    ),

    # -- Date / Time ---------------------------------------------------------
    types.FunctionDeclaration(
        name="get_current_datetime",
        description="Return the current system local date and time. Useful for timestamping results or date-aware tasks.",
        parameters=types.Schema(type=types.Type.OBJECT, properties={}),
    ),

    # -- Web Search ----------------------------------------------------------
    types.FunctionDeclaration(
        name="web_search",
        description=(
            "Search the web and return a short summary of the top result. "
            "Use for fetching news, prices, weather summaries, or any real-time info."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "query": types.Schema(type=types.Type.STRING, description="The search query string."),
            },
            required=["query"],
        ),
    ),

    # -- HTTP GET ------------------------------------------------------------
    types.FunctionDeclaration(
        name="http_get",
        description=(
            "Perform an HTTP GET request to any URL and return the response body "
            "(truncated to 2000 characters). Useful for fetching data from public APIs."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "url": types.Schema(type=types.Type.STRING, description="Full URL to request, e.g. 'https://api.example.com/data'"),
            },
            required=["url"],
        ),
    ),

    # -- Write Note ----------------------------------------------------------
    types.FunctionDeclaration(
        name="write_note",
        description=(
            "Save a text note or summary to a file in the job_results folder. "
            "Useful for persisting research findings, generated content, or reports."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "filename": types.Schema(type=types.Type.STRING, description="Filename without extension, e.g. 'daily_summary'"),
                "content":  types.Schema(type=types.Type.STRING, description="The text content to save."),
            },
            required=["filename", "content"],
        ),
    ),

    # -- Math Eval -----------------------------------------------------------
    types.FunctionDeclaration(
        name="math_eval",
        description="Evaluate a safe mathematical expression and return the result. E.g. '2 ** 10 + 3 * 5'.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "expression": types.Schema(type=types.Type.STRING, description="A Python math expression using only numbers and operators +-*/()**."),
            },
            required=["expression"],
        ),
    ),

    # -- Slack Delivery ------------------------------------------------------
    types.FunctionDeclaration(
        name="send_slack_message",
        description="Send a message to a configured Slack channel. The webhook URL is securely managed in the agent's settings.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "message": types.Schema(type=types.Type.STRING, description="The text message content to send to Slack."),
            },
            required=["message"],
        ),
    ),

    # -- Discord Delivery ----------------------------------------------------
    types.FunctionDeclaration(
        name="send_discord_message",
        description="Send a message to a configured Discord channel. The webhook URL is securely managed in the agent's settings.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "message": types.Schema(type=types.Type.STRING, description="The text message content to send to Discord."),
            },
            required=["message"],
        ),
    ),
]


# ---------------------------------------------------------------------------
# 2. TOOL EXECUTOR
# ---------------------------------------------------------------------------

async def execute_tool(name: str, args: dict[str, Any], agent_id: int | None = None) -> str:
    """
    Run the tool identified by `name` with `args` and return a string result.
    This string is fed back to Gemini as the tool response.
    """

    if name == "create_agent":
        # Handled specially inside agent_runner.py – return a signal string.
        return "__CREATE_AGENT__"

    elif name == "get_current_datetime":
        from datetime import datetime
        now = datetime.now().astimezone()
        return f"Current local datetime: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}"

    elif name == "web_search":
        query = args.get("query", "")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # DuckDuckGo Instant Answer API – free, no key required
                r = await client.get(
                    "https://api.duckduckgo.com/",
                    params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
                )
                data = r.json()
                abstract = data.get("AbstractText") or data.get("Answer") or ""
                related = [t.get("Text", "") for t in data.get("RelatedTopics", [])[:3] if "Text" in t]
                if abstract:
                    return f"Search result for '{query}':\n{abstract}"
                elif related:
                    return f"Search result for '{query}':\n" + "\n".join(f"- {t}" for t in related)
                else:
                    return f"No instant answer found for '{query}'. Try a more specific query."
        except Exception as e:
            return f"web_search error: {e}"

    elif name == "http_get":
        url = args.get("url", "")
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                r = await client.get(url, headers={"User-Agent": "CronHeartbeatAgent/1.0"})
                body = r.text[:2000]
                return f"HTTP GET {url}\nStatus: {r.status_code}\nBody (truncated):\n{body}"
        except Exception as e:
            return f"http_get error: {e}"

    elif name == "write_note":
        filename = args.get("filename", "note").replace("/", "_")
        content = args.get("content", "")
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        path = JOB_RESULTS_DIR / f"{filename}_{timestamp}.txt"
        path.write_text(content, encoding="utf-8")
        return f"Note saved to {path}"

    elif name == "math_eval":
        expression = args.get("expression", "")
        # Only allow safe characters
        allowed = set("0123456789+-*/().** ")
        if not all(c in allowed for c in expression):
            return "math_eval error: expression contains disallowed characters."
        try:
            result = eval(expression, {"__builtins__": {}})  # sandboxed
            return f"{expression} = {result}"
        except Exception as e:
            return f"math_eval error: {e}"

    elif name == "send_slack_message":
        if not agent_id:
            return "Error: Slack delivery requires an active agent context to retrieve webhooks."
        from .database import AsyncSessionLocal
        from . import models
        from sqlalchemy.future import select
        
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(models.Agent).filter(models.Agent.id == agent_id))
            agent = result.scalars().first()
            
        webhook_url = agent.settings.get("slack_webhook_url") if agent and agent.settings else None
        if not webhook_url:
            return "Error: No 'slack_webhook_url' configured in agent settings."
        
        message = args.get("message", "")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(webhook_url, json={"text": message})
                r.raise_for_status()
                return "Successfully sent message to Slack."
        except Exception as e:
            return f"send_slack_message error: {str(e)}"

    elif name == "send_discord_message":
        if not agent_id:
            return "Error: Discord delivery requires an active agent context to retrieve webhooks."
        from .database import AsyncSessionLocal
        from . import models
        from sqlalchemy.future import select
        
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(models.Agent).filter(models.Agent.id == agent_id))
            agent = result.scalars().first()
            
        webhook_url = agent.settings.get("discord_webhook_url") if agent and agent.settings else None
        if not webhook_url:
            return "Error: No 'discord_webhook_url' configured in agent settings."
        
        message = args.get("message", "")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(webhook_url, json={"content": message})
                r.raise_for_status()
                return "Successfully sent message to Discord."
        except Exception as e:
            return f"send_discord_message error: {str(e)}"

    else:
        return f"Unknown tool: {name}"
