# criteria-closure/v1 — shared token-ownership fixtures

Produced by spec-runner, vendored by devtools under `PIN` with its
`manifest.json` (spec-runner#603 design §6.3, devtools#491). Both sides run
their own parser against every `<case>.py` and must produce `<case>.expected.json`.

- `owned`: every function and method the index holds — including non-`test*`
  ones; no classes; no `def` nested in a function — sorted by `(line, qualname)`.
  `line` is the first decorator's, else the `def`'s. `tokens` are the qualified
  criterion tokens on the definition's own region and every enclosing class's
  region, deduplicated and sorted by code point. Token grammar:
  `CODE:(BEH|AC)-<digits>[a-z]?`, `CODE` = 2-6 capitals, with no letter, digit or
  `_` on either side (case 20). A class region is the class's lines outside its
  nested definitions — including the lines of an `if`/`try`/`with`/loop in the
  class body, so those count for every method; at module level such lines are
  module header and own nothing (case 21).
- `{"error": "syntax"}`: the file does not parse.
- `selection.file_only`: spec-runner's task-gate approximation of default pytest
  collection; devtools does not read it.
- `case`: the file's stem — a label for humans; a parser comparison ignores it.

The bytes are the cases (CRLF, lone CR, BOM, NUL, form feed): `-text` in
`.gitattributes`, excluded from ruff. Never re-save these files in an editor.
