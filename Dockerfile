# Canopy — Dockerfile
# Base: python:3.12-slim (Python 3.10+ required per pyproject.toml)

FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Create a non-root user for security
RUN useradd --create-home --shell /bin/bash canopy

WORKDIR /app

# Copy the full project source
COPY . .

# Install the package and all runtime dependencies (locked, system Python)
RUN uv pip install --system --no-cache .

# Create data and logs directories and make the entrypoint executable
RUN mkdir -p /app/data /app/logs && \
    chmod +x /app/docker-entrypoint.sh && \
    chown -R canopy:canopy /app

# Switch to non-root user
USER canopy

# Persistent data and logs as volumes
VOLUME ["/app/data", "/app/logs"]

# Environment variables — read by Config.from_env() at runtime
ENV CANOPY_DATA_DIR=/app/data

# Expose Web UI / REST API and P2P mesh ports
EXPOSE 7770 7771

# docker-entrypoint.sh initialises the DB on the live volume then exec's the app.
# Extra CLI args (e.g. --debug) can be appended to `docker run` or CMD override.
ENTRYPOINT ["/app/docker-entrypoint.sh"]
