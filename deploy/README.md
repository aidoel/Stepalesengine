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

## Deploying an update

To ship a change to the live viewer at `step.stadsbeheer.tech`:

1. **Merge the change into `main`.** Merge PR #3 (`corpus-viewer-improvements`)
   so `deploy/Dockerfile` and `manufacturing_pipeline/serve_corpus.py` are on
   `main`.
2. **Prepare the build context.** On the build host, check out `main` and make
   sure the `corpus_steps/` directory (the STEP files to showcase) is present
   at the repo root. It is gitignored — customer geometry, never committed — so
   it will not arrive with the checkout and must be copied in. Without it the
   `COPY corpus_steps /app/corpus/steps` step in the Dockerfile fails.
3. **Rebuild the image** from the repo root:

   ```
   docker build -f deploy/Dockerfile -t step-corpus .
   ```

   `validate-corpus` runs during the build; the per-file manifests it writes
   carry container-correct source paths.
4. **Deploy via Coolify.** Trigger a redeploy of the `step.stadsbeheer.tech`
   application in Coolify. Coolify rebuilds the image from the configured
   context and rolls the container; the reverse proxy and HTTPS are handled
   automatically.
