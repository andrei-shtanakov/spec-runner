# Maestro interop fixtures

Golden fixtures used by `tests/test_json_result_contract.py` and by Maestro's
contract tests. Maestro copies these files into its own test suite and asserts
that its `ExecutorState` / `SpecRunnerJsonResult` parsers accept them byte-for-byte.

**Any change to these files is a breaking change** requiring a major version
bump and a `BREAKING` note in `CHANGELOG.md`. See `docs/state-schema.md`.

## Files

| File | Surface | Generator |
|---|---|---|
| `json-result-single-success.json` | `--json-result` single-task successful run | `test_json_result_contract.py::test_golden_single_success` |
| `json-result-single-failure.json` | `--json-result` single-task failed run (includes `error`) | `test_json_result_contract.py::test_golden_single_failure` |
| `json-result-multi.json` | `--json-result` multi-task run (mixed outcomes) | `test_json_result_contract.py::test_golden_multi` |
| `json-result-empty.json` | `--json-result` when no tasks are ready | `test_json_result_contract.py::test_golden_empty` |
| `json-result-legacy-json-state.json` | Legacy pre-2.0 `.executor-state.json` snapshot (read-only fallback for Maestro) | hand-curated |

## Regenerating

The fixtures are produced by constructing a deterministic `ExecutorState` and
calling `spec_runner.cli.build_task_json_result()`. If you intentionally break
the contract (major bump), run:

```bash
uv run pytest tests/test_json_result_contract.py --update-golden
```

…and commit the regenerated files together with the `CHANGELOG.md` entry.
