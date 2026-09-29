# criteria-closure/v1 — shared token-ownership fixtures

Produced by spec-runner, vendored by devtools under `PIN` with its
`manifest.json` (spec-runner#603 design §6.3, devtools#491). Both sides run
their own parser against every `<case>.py` and must produce `<case>.expected.json`.

- `owned`: every function and method the index holds — including non-`test*`
  ones; no classes; no `def` nested in a function — sorted by `(line, qualname)`.
  `line` is the first decorator's, else the `def`'s. `tokens` are the qualified
  tokens (`CODE:ID`, whole-token boundaries) on the definition's own region and
  every enclosing class's region.
- `{"error": "syntax"}`: the file does not parse.
- `selection.file_only`: spec-runner's task-gate approximation of default pytest
  collection; devtools does not read it.

The bytes are the cases (CRLF, lone CR, BOM, NUL, form feed): `-text` in
`.gitattributes`, excluded from ruff. Never re-save these files in an editor.
