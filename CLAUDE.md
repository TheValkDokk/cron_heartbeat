# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Cron Heartbeat is a FastAPI-based AI agent scheduling system. Agents powered by Google Gemini execute recurring tasks on cron schedules and stream results to browsers via Server-Sent Events (SSE).

## Development Commands

```bash
# Start PostgreSQL database
docker-compose up -d

# Run the development server
uvicorn src.main:app --reload

# Run a manual agent task (for testing)
python test_agent.py

# Verify SSE + message persistence flow
python verify_sse.py
```

## Environment Setup

Create a `.env` file with:
```
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/cron_db
GEMINI_API_KEY=<your-key>
```

## Architecture

### Core Flow
1. **APScheduler** polls every 30 seconds and triggers `execute_agent_task()` for scheduled jobs
2. **Agent Runner** runs the agentic loop with Gemini (max 5 tool rounds)
3. **Results** are saved to `job_results/`, persisted to DB as `Message`, and broadcast via SSE

### Key Modules

| File | Purpose |
|------|---------|
| `src/main.py` | FastAPI app, REST endpoints, SSE streaming, scheduler setup |
| `src/agent_runner.py` | Agentic loop with Gemini, tool execution, message persistence |
| `src/tools.py` | Gemini FunctionDeclarations + Python executors (web_search, http_get, write_note, math_eval, get_current_datetime, create_agent) |
| `src/broadcaster.py` | In-memory pub/sub for SSE - `subscribe()`/`publish()` pattern |
| `src/models.py` | SQLAlchemy models: User, Agent, CronJob, Message |
| `src/schemas.py` | Pydantic request/response models |
| `src/config.py` | Settings via pydantic-settings from `.env` |

### Data Model

- **User** → owns multiple **Agents**
- **Agent** → has `system_prompt`, optional `parent_agent_id` (factory pattern), multiple **CronJobs**
- **CronJob** → `schedule` (cron string), `task_description`
- **Message** → `source` (user/agent/cron), `owner_id`, `from_agent_id`, `chat_agent_id`, `content`

### Agent Factory Pattern

Agents can spawn child agents via the `create_agent` tool. The parent's `owner_id` is inherited. This enables self-replicating agent workflows.

## Adding New Tools

1. Add a `types.FunctionDeclaration` to `TOOL_DECLARATIONS` in `src/tools.py`
2. Add the executor logic in `execute_tool()` function
3. For special handling (like `create_agent`), add logic in `agent_runner.py` instead

## SSE Streaming

- Client connects to `GET /users/{user_id}/stream`
- Server sends `data: {...}\n\n` events for cron results
- Keep-alive pings every 25 seconds prevent proxy timeouts
- Uses in-memory `asyncio.Queue` per subscriber (see `broadcaster.py`)
