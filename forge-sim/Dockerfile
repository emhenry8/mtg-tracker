FROM eclipse-temurin:17-jre

RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb \
      libxtst6 \
      libxrender1 \
      libxi6 \
      libxext6 \
      fontconfig \
      curl \
      bzip2 \
      python3 \
      python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Forge's tarball is a pre-extracted distribution (despite the name),
# not a GUI installer. It bundles its own card database, so no
# network access is needed at runtime.
RUN curl -sL -o /tmp/forge.tar.bz2 \
      "https://github.com/Card-Forge/forge/releases/download/forge-2.0.14/forge-installer-2.0.14.tar.bz2" \
    && mkdir -p /opt/forge \
    && tar -xjf /tmp/forge.tar.bz2 -C /opt/forge \
    && rm /tmp/forge.tar.bz2

RUN pip install --no-cache-dir --break-system-packages fastapi "uvicorn[standard]"

WORKDIR /app

COPY server.py .

# Forge bundles Sentry crash telemetry that silently swallows fatal
# errors while it tries to phone home. Disabling it also stops it
# reporting to a third party on every simulated match.
ENV SENTRY_DSN=

CMD ["python3", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
