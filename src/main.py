from fastapi import FastAPI, Depends, HTTPException, Request, status, Query
from fastapi.responses import StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import text, or_, delete
from sqlalchemy.orm import selectinload
from typing import List
import asyncio
import json

from . import models, schemas
from .database import engine, get_db, Base
from .agent_runner import execute_agent_task
from .scheduler_service import get_scheduler, sync_jobs_from_db, register_job, remove_job
from .auth import create_access_token, get_current_user, get_current_user_query
from .config import settings
import sentry_sdk

if settings.sentry_dsn:
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        traces_sample_rate=1.0,
        profiles_sample_rate=1.0,
    )

app = FastAPI(title="Cron Heartbeat API")
templates = Jinja2Templates(directory="src/templates")
scheduler = get_scheduler()


@app.on_event("startup")
async def on_startup():
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        # Create all tables if they don't exist
        from . import models
        await conn.run_sync(models.Base.metadata.create_all)

    scheduler.start()
    # Sync all active jobs from database into scheduler
    await sync_jobs_from_db(scheduler)
    print("APScheduler started with persistent job store!")


# --- FRONTEND UI ---

@app.get("/")
async def serve_frontend(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# --- USER / AUTH ENDPOINTS ---

@app.post("/auth/login", response_model=schemas.Token)
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(models.User).filter(models.User.username == form_data.username))
    user = result.scalars().first()
    
    # Auto-create user if they don't exist
    if not user:
        user = models.User(username=form_data.username)
        db.add(user)
        await db.commit()
        await db.refresh(user)
        
    access_token = create_access_token(data={"sub": str(user.id)})
    return {"access_token": access_token, "token_type": "bearer"}

@app.get("/users/me", response_model=schemas.UserResponse)
async def read_users_me(current_user: models.User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(models.User).options(selectinload(models.User.agents)).filter(models.User.id == current_user.id))
    return result.scalars().first()

@app.get("/users/", response_model=List[schemas.UserResponse])
async def read_users(db: AsyncSession = Depends(get_db)):
    # TEMPORARY: keeping this so old frontend still kind of works during transition
    result = await db.execute(select(models.User).options(selectinload(models.User.agents)))
    return result.scalars().all()


# --- AGENT ENDPOINTS ---

@app.post("/agents/", response_model=schemas.AgentResponse)
async def create_agent(agent: schemas.AgentCreate, current_user: models.User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    # Override owner_id with the authenticated user
    agent.owner_id = current_user.id

    # Verify parent agent exists and belongs to user if specified
    if agent.parent_agent_id:
        result = await db.execute(select(models.Agent).filter(models.Agent.id == agent.parent_agent_id))
        parent = result.scalars().first()
        if not parent or parent.owner_id != current_user.id:
            raise HTTPException(status_code=404, detail="Parent agent not found or unauthorized")

    db_agent = models.Agent(**agent.model_dump())
    db.add(db_agent)
    await db.commit()
    await db.refresh(db_agent)

    return {
        "id": db_agent.id,
        "name": db_agent.name,
        "system_prompt": db_agent.system_prompt,
        "owner_id": db_agent.owner_id,
        "parent_agent_id": db_agent.parent_agent_id,
        "settings": db_agent.settings or {},
    }


@app.get("/agents/{agent_id}", response_model=schemas.AgentResponse)
async def read_agent(agent_id: int, current_user: models.User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    stmt = select(models.Agent).options(selectinload(models.Agent.cron_jobs)).filter(models.Agent.id == agent_id)
    result = await db.execute(stmt)
    agent_obj = result.scalars().first()

    if agent_obj is None or agent_obj.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Agent not found or unauthorized")
    return agent_obj


@app.delete("/agents/{agent_id}", status_code=204)
async def delete_agent(agent_id: int, current_user: models.User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(models.Agent).filter(models.Agent.id == agent_id))
    agent_obj = result.scalars().first()
    if agent_obj is None or agent_obj.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Agent not found or unauthorized")

    # Cascade: remove all cron jobs from scheduler and database
    jobs_result = await db.execute(select(models.CronJob).filter(models.CronJob.agent_id == agent_id))
    for job in jobs_result.scalars().all():
        remove_job(scheduler, job.id)
        await db.delete(job)

    await db.delete(agent_obj)
    await db.commit()


@app.post("/agents/{agent_id}/chat", response_model=schemas.ChatResponse)
async def chat_interaction(agent_id: int, request: schemas.ChatRequest, current_user: models.User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from .agent_runner import chat_with_agent

    result = await db.execute(select(models.Agent).filter(models.Agent.id == agent_id))
    agent_obj = result.scalars().first()
    if not agent_obj or agent_obj.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Agent not found or unauthorized")

    response_data = await chat_with_agent(agent_obj.id, agent_obj.owner_id, request.message)
    return response_data


@app.patch("/agents/{agent_id}/settings", response_model=schemas.AgentResponse)
async def update_agent_settings(
    agent_id: int,
    settings_update: schemas.AgentSettingsUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Update agent settings (webhooks, etc.)."""
    result = await db.execute(select(models.Agent).filter(models.Agent.id == agent_id))
    agent_obj = result.scalars().first()
    if not agent_obj:
        raise HTTPException(status_code=404, detail="Agent not found")
        
    if agent_obj.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized")

    # Merge settings
    current_settings = agent_obj.settings or {}
    current_settings.update(settings_update.settings)
    agent_obj.settings = current_settings

    await db.commit()
    await db.refresh(agent_obj)

    return {
        "id": agent_obj.id,
        "name": agent_obj.name,
        "system_prompt": agent_obj.system_prompt,
        "owner_id": agent_obj.owner_id,
        "parent_agent_id": agent_obj.parent_agent_id,
        "settings": agent_obj.settings or {},
    }


# --- MESSAGE HISTORY + SSE ---

@app.get("/users/{user_id}/messages")
async def get_messages(user_id: int, limit: int = 50, current_user: models.User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Return recent messages for a user (conversation history + cron results)."""
    if user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to view these messages")
    result = await db.execute(
        select(models.Message)
        .filter(models.Message.owner_id == user_id)
        .order_by(models.Message.created_at.desc())
        .limit(limit)
    )
    msgs = list(reversed(result.scalars().all()))
    return [
        {
            "id": m.id,
            "source": m.source,
            "from_agent_id": m.from_agent_id,
            "chat_agent_id": m.chat_agent_id,
            "content": m.content,
            "created_at": m.created_at.isoformat(),
        }
        for m in msgs
    ]


@app.delete("/agents/{agent_id}/messages", status_code=204)
async def clear_agent_messages(agent_id: int, current_user: models.User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Clear all messages associated with an agent for the current user."""
    # Ensure agent belongs to user
    result = await db.execute(select(models.Agent).filter(models.Agent.id == agent_id))
    agent_obj = result.scalars().first()
    if not agent_obj or agent_obj.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Agent not found or unauthorized")

    # Delete messages where the agent generated it or it was sent to the agent
    await db.execute(
        delete(models.Message)
        .where(models.Message.owner_id == current_user.id)
        .where(
            or_(
                models.Message.chat_agent_id == agent_id,
                models.Message.from_agent_id == agent_id
            )
        )
    )
    await db.commit()


@app.get("/users/{user_id}/stream")
async def sse_stream(user_id: int, current_user: models.User = Depends(get_current_user_query)):
    """SSE endpoint — browser connects here, receives cron results in real time."""
    if user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to stream these messages")
    from .broadcaster import subscribe, unsubscribe

    q = subscribe(user_id)

    async def event_generator():
        try:
            yield f"data: {json.dumps({'type': 'connected', 'user_id': user_id})}\n\n"
            while True:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=25.0)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            unsubscribe(user_id, q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# --- CRON JOB ENDPOINTS ---

@app.get("/agents/{agent_id}/jobs/", response_model=List[schemas.CronJobResponse])
async def list_cron_jobs(agent_id: int, current_user: models.User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(models.Agent).filter(models.Agent.id == agent_id))
    agent_obj = result.scalars().first()
    if not agent_obj or agent_obj.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Agent not found or unauthorized")

    result = await db.execute(
        select(models.CronJob).filter(models.CronJob.agent_id == agent_id)
    )
    return result.scalars().all()


@app.get("/agents/{agent_id}/jobs/{job_id}", response_model=schemas.CronJobResponse)
async def get_cron_job(agent_id: int, job_id: int, current_user: models.User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(models.Agent).filter(models.Agent.id == agent_id))
    agent_obj = result.scalars().first()
    if not agent_obj or agent_obj.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Agent not found or unauthorized")

    result = await db.execute(
        select(models.CronJob).filter(
            models.CronJob.id == job_id,
            models.CronJob.agent_id == agent_id
        )
    )
    job = result.scalars().first()
    if not job:
        raise HTTPException(status_code=404, detail="Cron job not found")
    return job


@app.post("/agents/{agent_id}/jobs/", response_model=schemas.CronJobResponse)
async def create_cron_job(agent_id: int, job: schemas.CronJobCreate, current_user: models.User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(models.Agent).filter(models.Agent.id == agent_id))
    agent_obj = result.scalars().first()
    if not agent_obj or agent_obj.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Agent not found or unauthorized")

    db_job = models.CronJob(**job.model_dump(), agent_id=agent_id)
    db.add(db_job)
    await db.commit()
    await db.refresh(db_job)

    # Register with scheduler
    # Note: normally we'd do this synchronously if we don't need async, but register_job might be async.
    # Ah wait, register_job is async! Let's await it.
    await register_job(scheduler, db_job)
    return db_job


@app.patch("/agents/{agent_id}/jobs/{job_id}/toggle", response_model=schemas.CronJobResponse)
async def toggle_cron_job(agent_id: int, job_id: int, current_user: models.User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(models.Agent).filter(models.Agent.id == agent_id))
    agent_obj = result.scalars().first()
    if not agent_obj or agent_obj.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Agent not found or unauthorized")

    result = await db.execute(
        select(models.CronJob).filter(
            models.CronJob.id == job_id,
            models.CronJob.agent_id == agent_id
        )
    )
    job = result.scalars().first()
    if not job:
        raise HTTPException(status_code=404, detail="Cron job not found")

    job.is_active = not job.is_active
    await db.commit()
    await db.refresh(job)

    if job.is_active:
        await register_job(scheduler, job)
    else:
        remove_job(scheduler, job.id)

    return job



@app.delete("/agents/{agent_id}/jobs/{job_id}", status_code=204)
async def delete_cron_job(agent_id: int, job_id: int, current_user: models.User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(models.Agent).filter(models.Agent.id == agent_id))
    agent_obj = result.scalars().first()
    if not agent_obj or agent_obj.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Agent not found or unauthorized")

    result = await db.execute(
        select(models.CronJob).filter(
            models.CronJob.id == job_id,
            models.CronJob.agent_id == agent_id
        )
    )
    job = result.scalars().first()
    if not job:
        raise HTTPException(status_code=404, detail="Cron job not found")

    remove_job(scheduler, job_id)
    await db.delete(job)
    await db.commit()


@app.get("/agents/{agent_id}/jobs/{job_id}/logs")
async def get_cron_job_logs(agent_id: int, job_id: int, current_user: models.User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Fetch the latest 50 messages execution logs specific to a cron job."""
    result = await db.execute(select(models.Agent).filter(models.Agent.id == agent_id))
    agent_obj = result.scalars().first()
    if not agent_obj or agent_obj.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Agent not found or unauthorized")

    result = await db.execute(
        select(models.Message)
        .filter(models.Message.from_agent_id == agent_id)
        .filter(models.Message.source.in_(["cron", "error"]))
        .filter(models.Message.content.like(f"%[Job #{job_id}]%"))
        .order_by(models.Message.created_at.desc())
        .limit(50)
    )
    msgs = result.scalars().all()
    return [{"created_at": m.created_at, "content": m.content, "source": m.source} for m in msgs]

