"""Allow ``python -m manufacturing_pipeline.cli`` to run the CLI."""

from .main import main

if __name__ == "__main__":
    raise SystemExit(main())
