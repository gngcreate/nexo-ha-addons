#!/usr/bin/env bash
set -euo pipefail

# Usa la carpeta donde vive este script como "proyecto".
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_DIR="$SCRIPT_DIR"

DRY_RUN=false
PUSH=true
COMMIT_MESSAGE=""

usage() {
  cat <<'EOF'
Uso:
  ./actualizar_nexo_repo.sh [opciones]

Opciones:
  -m, --message "mensaje"  Mensaje base del commit (se agrega fecha/hora automaticamente).
      --no-push            Hace commit pero no hace push.
      --dry-run            Solo muestra estado del proyecto, no modifica nada.
  -h, --help               Muestra esta ayuda.

Comportamiento por defecto (sin -m):
  Hace add/commit/push SOLO del proyecto donde esta este script.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -m|--message)
      shift
      [[ $# -gt 0 ]] || { echo "Error: falta el mensaje tras --message"; exit 1; }
      COMMIT_MESSAGE="$1"
      ;;
    --no-push)
      PUSH=false
      ;;
    --dry-run)
      DRY_RUN=true
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Error: opcion no reconocida: $1"
      usage
      exit 1
      ;;
  esac
  shift
done

if ! command -v git >/dev/null 2>&1; then
  echo "Error: git no esta instalado o no esta en PATH"
  exit 1
fi

if [[ ! -d "$PROJECT_DIR" ]]; then
  echo "Error: no existe el directorio del proyecto: $PROJECT_DIR"
  exit 1
fi

if ! REPO_ROOT="$(git -C "$PROJECT_DIR" rev-parse --show-toplevel 2>/dev/null)"; then
  echo "Error: $PROJECT_DIR no esta dentro de un repositorio git"
  exit 1
fi

if [[ "$PROJECT_DIR" == "$REPO_ROOT" ]]; then
  PROJECT_SUBPATH="."
else
  PROJECT_SUBPATH="${PROJECT_DIR#"$REPO_ROOT"/}"
fi

CURRENT_BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD)"
if [[ "$CURRENT_BRANCH" == "HEAD" ]]; then
  echo "Error: repositorio en detached HEAD. Cambia a una rama antes de continuar."
  exit 1
fi

echo "Proyecto: $PROJECT_DIR"
echo "Repo: $REPO_ROOT"
echo "Ruta objetivo: $PROJECT_SUBPATH"
echo "Rama actual: $CURRENT_BRANCH"

if [[ -n "$(git -C "$REPO_ROOT" diff --cached --name-only)" ]]; then
  echo "Error: hay cambios ya staged en el repo."
  echo "Haz commit/reset de esos cambios antes de ejecutar este script."
  exit 1
fi

echo "[1/4] Estado del proyecto"
git -C "$REPO_ROOT" --no-pager status --short -- "$PROJECT_SUBPATH"

if [[ "$DRY_RUN" == true ]]; then
  echo "Dry-run finalizado. No se hicieron cambios."
  exit 0
fi

echo "[2/4] Preparando cambios del proyecto"
git -C "$REPO_ROOT" add -A -- "$PROJECT_SUBPATH"

if git -C "$REPO_ROOT" diff --cached --quiet -- "$PROJECT_SUBPATH"; then
  echo "No hay cambios para commit."
  exit 0
fi

TIMESTAMP="$(date '+%Y-%m-%d %H:%M:%S %z')"

if [[ -z "$COMMIT_MESSAGE" ]]; then
  COMMIT_MESSAGE="chore: actualizacion de cambios | ${TIMESTAMP}"
else
  COMMIT_MESSAGE="${COMMIT_MESSAGE} | ${TIMESTAMP}"
fi

echo "[3/4] Creando commit"
echo "Mensaje: $COMMIT_MESSAGE"
git -C "$REPO_ROOT" commit -m "$COMMIT_MESSAGE"

if [[ "$PUSH" == true ]]; then
  echo "[4/4] Enviando a origin/${CURRENT_BRANCH}"
  git -C "$REPO_ROOT" push origin "$CURRENT_BRANCH"
else
  echo "[4/4] Push omitido por --no-push"
fi

echo "Listo."


