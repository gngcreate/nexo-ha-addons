#!/usr/bin/env bash
set -euo pipefail

# Rutas fijas para evitar confusiones cuando se ejecuta desde otra ventana/carpeta.
REPO_ROOT="/Users/carlosgualdrondiaz/TRABAJO/nexo_tunnel_agent"
ADDON_SOURCE="/Users/carlosgualdrondiaz/TRABAJO/Nexo/addons/nexo_tunnel_agent"
ADDON_DEST="${REPO_ROOT}/nexo_tunnel_agent"

DRY_RUN=false
PUSH=true
COMMIT_MESSAGE=""

usage() {
  cat <<'EOF'
Uso:
  ./actualizar_nexo_repo.sh [opciones]

Opciones:
  -m, --message "mensaje"  Mensaje del commit.
      --no-push            Hace commit pero no hace push.
      --dry-run            Solo muestra qué haría, no modifica nada.
  -h, --help               Muestra esta ayuda.
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

if [[ ! -d "$REPO_ROOT/.git" ]]; then
  echo "Error: no existe repositorio git en $REPO_ROOT"
  exit 1
fi

if [[ ! -d "$ADDON_SOURCE" ]]; then
  echo "Error: no existe carpeta fuente: $ADDON_SOURCE"
  exit 1
fi

cd "$REPO_ROOT"

REAL_TOPLEVEL="$(git rev-parse --show-toplevel)"
if [[ "$REAL_TOPLEVEL" != "$REPO_ROOT" ]]; then
  echo "Error: toplevel detectado ($REAL_TOPLEVEL) no coincide con $REPO_ROOT"
  exit 1
fi

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$CURRENT_BRANCH" == "HEAD" ]]; then
  echo "Error: repositorio en detached HEAD. Cambia a una rama antes de continuar."
  exit 1
fi

echo "[1/5] Sincronizando archivos del addon"
RSYNC_CMD=(
  rsync -av --delete
  --exclude '.git/'
  --exclude '.idea/'
  --exclude '__pycache__/'
  "$ADDON_SOURCE/"
  "$ADDON_DEST/"
)

if [[ "$DRY_RUN" == true ]]; then
  RSYNC_CMD=("${RSYNC_CMD[@]:0:${#RSYNC_CMD[@]}-2}" --dry-run "${RSYNC_CMD[@]: -2:1}" "${RSYNC_CMD[@]: -1:1}")
fi

"${RSYNC_CMD[@]}"

echo "[2/5] Estado actual"
git --no-pager status --short

if [[ "$DRY_RUN" == true ]]; then
  echo "Dry-run finalizado. No se hicieron cambios."
  exit 0
fi

echo "[3/5] Preparando cambios"
git add -A

if git diff --cached --quiet; then
  echo "No hay cambios para commit."
  exit 0
fi

if [[ -z "$COMMIT_MESSAGE" ]]; then
  COMMIT_MESSAGE="chore: sync cambios desde ${ADDON_SOURCE}"
fi

echo "[4/5] Creando commit"
git commit -m "$COMMIT_MESSAGE"

if [[ "$PUSH" == true ]]; then
  echo "[5/5] Enviando a origin/${CURRENT_BRANCH}"
  git push origin "$CURRENT_BRANCH"
else
  echo "[5/5] Push omitido por --no-push"
fi

echo "Listo."

