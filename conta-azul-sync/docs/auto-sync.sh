#!/bin/bash
# =============================================================
# brugs — Auto-Sync para GitHub
# Monitora alterações nos arquivos do projeto e commita/push
# automaticamente ao repositório privado.
#
# Uso:
#   bash conta-azul-sync/docs/auto-sync.sh           # roda em foreground
#   bash conta-azul-sync/docs/auto-sync.sh &         # roda em background
#   nohup bash conta-azul-sync/docs/auto-sync.sh &   # roda desanexado do terminal
#
# Log: /tmp/brugs-autosync.log
# PID: /tmp/brugs-autosync.pid
# =============================================================

# REPO_DIR = raiz do repositório brugs (dois níveis acima de docs/)
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOG_FILE="/tmp/brugs-autosync.log"
PID_FILE="/tmp/brugs-autosync.pid"
DEBOUNCE_SECONDS=10  # aguarda X segundos após última alteração antes de commitar

# Salva PID
echo $$ > "$PID_FILE"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

log "=========================================="
log "brugs auto-sync iniciado (PID: $$)"
log "Monitorando: $REPO_DIR"
log "=========================================="

# Verifica dependências
if ! command -v inotifywait &>/dev/null; then
    log "ERRO: inotifywait não encontrado. Instale com: sudo apt-get install inotify-tools"
    exit 1
fi

# Função para commitar e fazer push
commit_and_push() {
    cd "$REPO_DIR" || return 1

    # Verifica se há mudanças
    if git diff --quiet && git diff --cached --quiet && [ -z "$(git ls-files --others --exclude-standard)" ]; then
        return 0
    fi

    TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
    CHANGED_FILES=$(git status --short | awk '{print $2}' | tr '\n' ', ' | sed 's/,$//')

    log "Alterações detectadas: $CHANGED_FILES"

    git add -A
    git commit -m "chore: auto-sync [$TIMESTAMP]

Arquivos alterados: $CHANGED_FILES"

    if git push origin main; then
        log "✅ Push realizado com sucesso"
    else
        log "❌ Falha no push — verifique credenciais e conexão"
        return 1
    fi
}

# Loop de monitoramento com debounce
LAST_EVENT=0

inotifywait -m -r \
    --exclude '(\.git|__pycache__|venv|\.log|\.tmp|\.bak|\.pyc)' \
    -e modify,create,delete,move \
    "$REPO_DIR" 2>/dev/null | while read -r directory event filename; do

    NOW=$(date +%s)
    LAST_EVENT=$NOW

    log "Evento: $event em $directory$filename"

    # Debounce: aguarda silêncio por DEBOUNCE_SECONDS segundos
    sleep "$DEBOUNCE_SECONDS"

    CURRENT=$(date +%s)
    DIFF=$((CURRENT - LAST_EVENT))
    if [ "$DIFF" -ge "$DEBOUNCE_SECONDS" ] || [ "$DIFF" -eq 0 ]; then
        commit_and_push
    fi
done
