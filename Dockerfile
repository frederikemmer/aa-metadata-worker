# syntax=docker/dockerfile:1
# AA Metadata Worker - single image for api + sync services.

FROM python:3.11-slim-bookworm AS build

WORKDIR /build

COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip wheel --wheel-dir /wheels -r requirements.txt


FROM python:3.11-slim-bookworm AS runtime

LABEL name="aa-metadata-worker" \
      org.opencontainers.image.source="https://github.com/frederikemmer/aa-metadata-worker" \
      org.opencontainers.image.description="Anna's Archive metadata index service for FE.Library (metadata-only)"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Berlin

RUN groupadd -r metadata && useradd -r -g metadata -d /app metadata

WORKDIR /app

COPY requirements.txt ./requirements.txt
COPY --from=build /wheels /tmp/wheels
RUN pip install --no-cache-dir --no-index --find-links=/tmp/wheels -r requirements.txt \
    && rm -rf /tmp/wheels \
    && mkdir -p /work/sync /tmp/app \
    && chown -R metadata:metadata /work /tmp/app

COPY app ./app
COPY common ./common
COPY sync ./sync
COPY migrations ./migrations

# Build-Context kann restriktive Modi mitbringen (z. B. NAS/SMB-Shares);
# für den Non-Root-User lesbar normalisieren.
RUN chmod -R a+rX /app

USER metadata

EXPOSE 8010

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010"]
