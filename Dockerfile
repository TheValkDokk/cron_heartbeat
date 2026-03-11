FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_SYSTEM_PYTHON=1

WORKDIR /app

# Install uv for fast dependency management
RUN pip install uv

# Copy uv dependency files
COPY pyproject.toml uv.lock ./

# Install dependencies (this leverages Docker cache if files haven't changed)
RUN uv sync --frozen

# Copy the rest of the application
COPY . .

# Expose port 8000
EXPOSE 8000

# Start FastAPI application
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
