from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://user:password@localhost:5432/cron_db"
    gemini_api_key: str = ""
    sentry_dsn: str = ""

    class Config:
        env_file = ".env"

    @property
    def sync_database_url(self) -> str:
        """Return sync URL for tools that need it (like APScheduler)."""
        return self.database_url.replace('+asyncpg', '')

settings = Settings()
