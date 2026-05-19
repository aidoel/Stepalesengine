# Contributing

## Running tests

Install the package with dev and OCCT extras, then run pytest:

```bash
pip install -e ".[dev,occt]"
pytest -q
```

If `cadquery-ocp` is not available on your platform, install only the dev
extras and run the OCP-free subset of the test suite:

```bash
pip install -e ".[dev]"
pytest -q tests/parsing/ tests/classification/ tests/io/test_xml_writer.py
```

## Code style

Code is linted and formatted with [ruff](https://docs.astral.sh/ruff/). The
configuration lives in `pyproject.toml` under `[tool.ruff]` and
`[tool.ruff.lint]`. To check your changes locally:

```bash
ruff check manufacturing_pipeline/ tests/
ruff format manufacturing_pipeline/ tests/
```

## Local hooks

Install the pre-commit hooks so basic checks run on every commit:

```bash
pip install pre-commit
pre-commit install
```

The hooks run trailing-whitespace, end-of-file, YAML/TOML checks, and ruff
(`--fix` plus `ruff-format`). They are configured in
`.pre-commit-config.yaml`.
