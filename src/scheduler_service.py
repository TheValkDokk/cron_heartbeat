"""
Scheduler Service
=================
Centralized management for APScheduler with SQLAlchemy persistence.
Handles job registration, removal, and syncing from database.
"""

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from . import models
from .database import AsyncSessionLocal
from .config import settings
from .agent_runner import execute_agent_task


def get_scheduler() -> AsyncIOScheduler:
    """Create and return a configured scheduler with SQLAlchemy job store."""
    jobstores = {
        'default': SQLAlchemyJobStore(url=settings.sync_database_url)
    }

    return AsyncIOScheduler(jobstores=jobstores)


async def sync_jobs_from_db(scheduler: AsyncIOScheduler):
    """
    Load all active jobs from database and register them with the scheduler.
    Should be called on application startup.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(models.CronJob)
            .options(selectinload(models.CronJob.agent))
            .filter(models.CronJob.is_active == True)
        )
        jobs = result.scalars().all()

        for job in jobs:
            await register_job(scheduler, job)

        print(f"[Scheduler] Synced {len(jobs)} active jobs from database")


async def register_job(scheduler: AsyncIOScheduler, cron_job: models.CronJob):
    """
    Register or update a cron job with the scheduler.
    Uses job ID as string for APScheduler tracking.
    """
    job_id = f"cron_{cron_job.id}"

    # Remove existing job if present (handles updates)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    # Parse schedule expression and create trigger
    try:
        schedule_str = cron_job.schedule.strip()
        if len(schedule_str.split()) == 5:
            trigger = CronTrigger.from_crontab(schedule_str)
        else:
            from datetime import datetime
            run_date = datetime.fromisoformat(schedule_str)
            trigger = DateTrigger(run_date=run_date)
    except ValueError as e:
        print(f"[Scheduler] Invalid schedule expression for job {cron_job.id}: {e}")
        return False

    # Add job to scheduler
    scheduler.add_job(
        execute_agent_task,
        trigger=trigger,
        id=job_id,
        kwargs={"agent_id": cron_job.agent_id, "task_description": cron_job.task_description, "job_id": cron_job.id},
        name=f"Agent {cron_job.agent_id}: {cron_job.task_description[:50]}",
        replace_existing=True,
    )

    print(f"[Scheduler] Registered job {job_id}: {cron_job.schedule}")
    return True


def remove_job(scheduler: AsyncIOScheduler, cron_job_id: int):
    """
    Remove a job from the scheduler.
    """
    job_id = f"cron_{cron_job_id}"

    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
        print(f"[Scheduler] Removed job {job_id}")
        return True
    return False


def pause_job(scheduler: AsyncIOScheduler, cron_job_id: int):
    """
    Pause a scheduled job (keeps it in scheduler but doesn't execute).
    """
    job_id = f"cron_{cron_job_id}"

    if scheduler.get_job(job_id):
        scheduler.pause_job(job_id)
        print(f"[Scheduler] Paused job {job_id}")
        return True
    return False


def resume_job(scheduler: AsyncIOScheduler, cron_job_id: int):
    """
    Resume a paused job.
    """
    job_id = f"cron_{cron_job_id}"

    if scheduler.get_job(job_id):
        scheduler.resume_job(job_id)
        print(f"[Scheduler] Resumed job {job_id}")
        return True
    return False
