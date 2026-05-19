# Lint Cleanup

One-shot sweep to bring `manufacturing_pipeline/` and `tests/` to a clean ruff
state without changing behaviour. Test suite remained at 390 passing / 0
failures before and after.

## Initial state

`ruff check manufacturing_pipeline/ tests/` reported **662 errors** (583
auto-fixable). Breakdown by code:

| Code   | Count | Meaning |
|--------|------:|---------|
| UP006  | 412   | non-pep585-annotation (`List` -> `list`, etc.) |
| UP045  |  87   | non-pep604-annotation-optional (`Optional[X]` -> `X \| None`) |
| UP035  |  59   | deprecated-import |
| I001   |  35   | unsorted-imports |
| F401   |  14   | unused-import |
| UP007  |  11   | non-pep604-annotation-union |
| UP037  |  11   | quoted-annotation |
| B905   |  10   | zip-without-explicit-strict |
| B007   |   7   | unused-loop-control-variable |
| E702   |   7   | multiple-statements-one-line |
| F841   |   4   | unused-variable |
| E741   |   3   | ambiguous-variable-name |
| UP012  |   2   | unnecessary-encode-utf8 |

## Auto-fixes

`ruff check --fix` resolved **670 issues** (some new ones surfaced after
formatter ran). `ruff format` then reformatted 47 files.

## Manual fixes

- **F841 (4)**: removed unused locals `e` (`cross_section.py:207`), `n`
  (`cross_section.py:707`), `tables_bottom_y` (`pdf_writer.py:597`); dropped
  the dummy `OCP =` assignment from `test_matcher.py:13`
  (`pytest.importorskip("OCP")` is enough on its own).
- **B007 (7)**: renamed unused loop variables to `_name` in
  `step_strategies.py`, `pipeline/diff.py`, `pmi/extractor.py`,
  `tests/cache/test_disk.py`.
- **E741 (3)**: per-line `# noqa: E741` on the three plate-builder signatures
  in `tests/fixtures/synthetic_steps.py` where `l` is the deliberate
  domain name for "length".

## Ignores added to `pyproject.toml`

- `B905` (zip-without-strict): pervasive call sites where adding `strict=`
  could change semantics. Kept as an ignore rather than landing 10 risky
  edits.

## Final state

- `ruff check manufacturing_pipeline/ tests/` -> **All checks passed!**
- `python3 -m pytest` -> **390 passed**.
