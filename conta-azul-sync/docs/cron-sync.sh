#!/bin/bash
# =============================================================
# brugs — Cron Sync para GitHub
# Roda a cada N minutos via cron. Commita e faz push de qualquer
# alteração pendente no repositório.
#
# Configuração recomendada no crontab:
#   */5 * * * * bash /data/.openclaw/workspace/cfo/scripts/conta-azul-sync/docs/cron-sync.sh
#
# Log: /tmp/brugs-cron-sync.log
# =============================================================

REPO_DIR="/data/.openclaw/workspace/cfo/scripts"
LOG_FILE="/tmp/brugs-cron-sync.log"
MAX_LOG_LINES=500

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
    # Mantém o log enxuto
    if [ "$(wc -l < "$LOG_FILE")" -gt "$MAX_LOG_LINES" ]; then
        tail -n 300 "$LOG_FILE" > "${LOG_FILE}.tmp" && mv "${LOG_FILE}.tmp" "$LOG_FILE"
    fi
}

cd "$REPO_DIR" || { log "ERRO: não encontrou $REPO_DIR"; exit 1; }

# Verifica se há mudanças (tracked ou untracked não-ignoradas)
HAS_CHANGES=$(git status --short 2>/dev/null)

if [ -z "$HAS_CHANGES" ]; then
    exit 0  # Nada a fazer — sem log para não poluir
fi

TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
CHANGED_FILES=$(echo "$HAS_CHANGES" | awk '{print $2}' | tr '\n' ', ' | sed 's/,$//')

log "Alterações detectadas: $CHANGED_FILES"

git add -A

COMMIT_MSG="chore: auto-sync [$TIMESTAMP]

Arquivos: $CHANGED_FILES"

git commit -m "$COMMIT_MSG" >> "$LOG_FILE" 2>&1

if git push origin main >> "$LOG_FILE" 2>&1; then
    log "✅ Push realizado — $CHANGED_FILES"
else
    log "❌ Push falhou — verifique: git push no $REPO_DIR"
fi
