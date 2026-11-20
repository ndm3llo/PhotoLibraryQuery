# Local/CI Postgres with pgvector + PostGIS (Supabase provides both in production).
# Built FROM the multi-arch pgvector image because the official postgis image
# has no arm64 build for every tag (Apple Silicon).
FROM pgvector/pgvector:pg16
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-16-postgis-3 postgresql-16-postgis-3-scripts \
    && rm -rf /var/lib/apt/lists/*
