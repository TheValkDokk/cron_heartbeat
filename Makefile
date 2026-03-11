.PHONY: dev db up down logs build

# Start the database and the local development server (Backend + Frontend templates)
dev: db
	@echo "Starting local development server..."
	uv run uvicorn src.main:app --reload --host 0.0.0.0 --port 8000

# Start only the PostgreSQL database via Docker
db:
	@echo "Starting Postgres database..."
	docker compose up -d postgres

# Start the entire stack (DB + API) via Docker Compose
up:
	docker compose up -d

# Stop all Docker Compose services
down:
	docker compose down

# Build Docker containers
build:
	docker compose build

# View Docker Compose logs
logs:
	docker compose logs -f
