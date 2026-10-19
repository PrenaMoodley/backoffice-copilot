FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --create-home copilot
USER copilot
WORKDIR /home/copilot
ENV COPILOT_DATA_DIR=/home/copilot

EXPOSE 8000
# ANTHROPIC_API_KEY must be provided by the host's secret store. Set a spending limit on the key.
CMD ["uvicorn", "copilot.web:app", "--host", "0.0.0.0", "--port", "8000"]
