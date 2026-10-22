FROM python:3.13-slim

# Logs straight to stdout so the host's log viewer sees them; no .pyc files in the image
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --create-home copilot
USER copilot
WORKDIR /home/copilot
ENV COPILOT_DATA_DIR=/home/copilot

# Hosts such as Render set PORT; 8000 otherwise
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/health', timeout=4)"

# No API key: demo mode. ANTHROPIC_API_KEY must come from the host's secret store, never the image.
# Set a spending limit on the key as well.
CMD ["sh", "-c", "exec uvicorn copilot.web:app --host 0.0.0.0 --port ${PORT:-8000}"]
