#!/usr/bin/env bash
# Bubble Detector daily runner.
#
#   ./run.sh                 run, then open reports/latest.html in your browser
#   ./run.sh --no-open       run without opening the report
#   ./run.sh --no-pdf        any other flags are passed straight to bubble_detector
#   ./run.sh --json          JSON to stdout (report is not opened)
#
# Works from any directory. Creates .venv on first use if it is missing.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

VENV="$HERE/.venv"
PY="$VENV/bin/python"

# ---- pass-through flags, minus our own --------------------------------------
OPEN=1
ARGS=()
for a in "$@"; do
  case "$a" in
    --no-open) OPEN=0 ;;
    --json|--no-html) OPEN=0; ARGS+=("$a") ;;
    *) ARGS+=("$a") ;;
  esac
done

# ---- make sure the venv exists ---------------------------------------------
if [ ! -x "$PY" ]; then
  echo "No .venv found, creating one..." >&2
  if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.12 "$VENV" >&2
    uv pip install --python "$PY" -r requirements.txt >&2
  else
    # need python >= 3.9
    for cand in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
      if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then
        "$cand" -m venv "$VENV"
        break
      fi
    done
    [ -x "$PY" ] || { echo "error: no Python 3.9+ found. Install uv (brew install uv) or a newer Python." >&2; exit 1; }
    "$PY" -m pip install -q -r requirements.txt >&2
  fi
fi

# ---- run --------------------------------------------------------------------
# "${ARGS[@]+"${ARGS[@]}"}" expands to nothing when ARGS is empty; plain "${ARGS[@]}" is an
# "unbound variable" error under set -u on the bash 3.2 that ships with macOS.
status=0
"$PY" -m bubble_detector ${ARGS[@]+"${ARGS[@]}"} || status=$?

# ---- open the report --------------------------------------------------------
if [ $status -eq 0 ] && [ $OPEN -eq 1 ]; then
  read -r OUTDIR LATEST < <("$PY" -c 'from bubble_detector import config as c; print(c.DEFAULT_OUTDIR, c.LATEST_HTML)')
  for ((i = 0; i < ${#ARGS[@]}; i++)); do
    [ "${ARGS[$i]}" = "--outdir" ] && OUTDIR="${ARGS[$((i + 1))]}"
  done
  REPORT="$OUTDIR/$LATEST"
  if [ -f "$REPORT" ]; then
    case "$(uname -s)" in
      Darwin) open "$REPORT" ;;
      Linux)  command -v xdg-open >/dev/null && xdg-open "$REPORT" >/dev/null 2>&1 || true ;;
    esac
  fi
fi
exit $status
