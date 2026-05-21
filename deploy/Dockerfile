# Corpus viewer for step.erpdoel.nl - Flask serve-corpus + OCP, corpus baked in.
# Build context = repo root. A corpus_steps/ directory with the STEP files to
# showcase must be present in the context at build time (gitignored).
#   docker build -f deploy/Dockerfile -t step-corpus .
FROM python:3.12-slim

# OpenCASCADE (cadquery-ocp) runtime libraries.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglu1-mesa libxext6 libxrender1 libsm6 libxmu6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps first for layer caching.
RUN pip install --no-cache-dir \
        "cadquery-ocp>=7.7" flask numpy "ezdxf>=1.3" pyyaml \
        "reportlab>=4.0" "watchdog>=3.0" trimesh shapely

# Project source + install.
COPY pyproject.toml ./
COPY manufacturing_pipeline ./manufacturing_pipeline
RUN pip install --no-cache-dir --no-deps -e .

# Corpus STEP files; validate-corpus at build time so manifest source paths
# resolve inside the container (the GLB routes read the STEP file per request).
COPY corpus_steps /app/corpus/steps
RUN python -m manufacturing_pipeline.cli validate-corpus /app/corpus/steps \
        --write-outputs --out-dir /app/corpus/report \
        --html /app/corpus/report/report.html --workers 4 || true

EXPOSE 5000
CMD ["python", "-m", "manufacturing_pipeline.cli", "serve-corpus", \
     "/app/corpus/report", "--host", "0.0.0.0", "--port", "5000"]
