FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    POETRY_VERSION=1.8.4

RUN pip install --no-cache-dir poetry==$POETRY_VERSION

WORKDIR /app

COPY pyproject.toml poetry.lock* ./

RUN poetry install --no-root --without dev,ml

COPY . .

# Install after COPY so this layer always runs when app code changes; avoids stale
# cache where an older image had no fastembed (ingestion embedder requires it).
RUN pip install --no-cache-dir fastembed==0.3.6

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
