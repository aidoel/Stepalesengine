# Deploying the corpus viewer (step.erpdoel.nl)

`deploy/Dockerfile` builds a self-contained image: Python + `cadquery-ocp` +
the project + a corpus of STEP files. `validate-corpus` runs at build time so
the per-file manifests carry container-correct source paths; the container
then runs `serve-corpus` (Flask) on port 5000.

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

## Coolify

Deploy as a Coolify application (Dockerfile build, context = repo root with
`corpus_steps/` added), domain `step.erpdoel.nl`, container port 5000.
Coolify provides the reverse proxy and automatic HTTPS.
