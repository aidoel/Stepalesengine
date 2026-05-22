# Deploying the corpus viewer (step.stadsbeheer.tech)

`deploy/Dockerfile` builds a self-contained image: Python + `cadquery-ocp` +
the project + a corpus of STEP files. `validate-corpus` runs at build time so
the per-file manifests carry container-correct source paths; the container
then serves the corpus viewer with `gunicorn` on port 5000.

## Build

The build context is the repo root. Place the STEP files to showcase in a
`corpus_steps/` directory at the repo root (gitignored - it is customer
geometry, never committed), then:

```
docker build -f deploy/Dockerfile -t step-corpus .
```

## Run

```
docker run -p 5000:5000 step-corpus
```

The container's `CMD` runs `gunicorn` against the
`manufacturing_pipeline.serve_corpus:app` WSGI entrypoint (a production
server, replacing werkzeug's development server). The corpus report
directory is read from the `STEPALESENGINE_CORPUS_DIR` environment variable,
which the image sets to `/app/corpus/report`; override it to serve a
different report directory. Adjust `--workers` in the `CMD` to taste.

## Coolify

Deploy as a Coolify application (Dockerfile build, context = repo root with
`corpus_steps/` added), domain `step.stadsbeheer.tech`, container port 5000.
Coolify provides the reverse proxy and automatic HTTPS.
