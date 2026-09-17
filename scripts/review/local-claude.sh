#!/bin/sh
# Обёртка над вендоренным scripts/review/local.sh: гонит локальную итерацию
# через harness-claude вместо дефолтного codex кита. НЕ входит в PIN кита
# (scripts/review/PIN) — обычный сосед вендор-копии, как install-hook.sh/
# настроенный review-prompt.md (см. §5 в checksum.sh: «лишний файл не должен
# спотыкать целостность»).
#
# Зачем: приёмочный профиль ai-prosto (~/.config/ai-prosto/harness.env) уже
# с 2026-09-03 публикует приёмочное ревью через claude — у codex кончались
# лимиты. Локальный цикл до 2026-09-17 продолжал дефолтить на codex (сам
# local.sh, без REVIEW_HARNESS в окружении) и на PR #522 круге 8 это
# разошлось: локальный codex-прогон нашёл не те находки, что уже
# опубликованный вердикт harness-claude, и сжёг лимит codex попутно.
# Этот скрипт держит локальную итерацию на том же харнессе, что и приёмку,
# чтобы вердикты не расходились по инструменту.
#
# Использование: sh scripts/review/local-claude.sh [флаги local.sh]
set -eu
export REVIEW_HARNESS="${REVIEW_HARNESS:-claude}"
export REVIEW_MODEL="${REVIEW_MODEL:-claude-opus-5}"
exec "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/local.sh" "$@"
