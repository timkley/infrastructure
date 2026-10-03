FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV HEISENBERG_ACCESS_MCP_ARTIFACT_DIR=/var/lib/heisenberg-access-mcp/artifacts

WORKDIR /app

COPY pyproject.toml README.md ./
COPY heisenberg_access_mcp ./heisenberg_access_mcp

RUN pip install --no-cache-dir .

RUN addgroup --gid 1000 app \
  && adduser --disabled-password --gecos '' --uid 1000 --ingroup app app \
  && mkdir -p /var/lib/heisenberg-access-mcp/artifacts \
  && chown -R app:app /var/lib/heisenberg-access-mcp
USER app

EXPOSE 8000

CMD ["python", "-m", "heisenberg_access_mcp.server"]
