#!/bin/bash
# ============================================================
# Conta Azul Sync — Execução Automática (sem diálogos)
# Roda: sync financeiro + sync vendas + auditoria + report CFO
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="$SCRIPT_DIR/conta_azul_sync.log"
PYTHON="$SCRIPT_DIR/venv/bin/python3"

cd "$SCRIPT_DIR"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] ========================================" >> "$LOG_FILE"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Iniciando sincronização automática..." >> "$LOG_FILE"

# 1. Sync financeiro (receitas e despesas)
echo "[$(date '+%Y-%m-%d %H:%M:%S')] [1/4] conta_azul_supabase.py sync..." >> "$LOG_FILE"
"$PYTHON" conta_azul_supabase.py sync >> "$LOG_FILE" 2>&1
EXIT_CODE_1=$?

# 2. Sync de vendas
echo "[$(date '+%Y-%m-%d %H:%M:%S')] [2/4] conta_azul_vendas.py sync..." >> "$LOG_FILE"
"$PYTHON" conta_azul_vendas.py sync >> "$LOG_FILE" 2>&1
EXIT_CODE_2=$?

# 3. Auditoria financeira
echo "[$(date '+%Y-%m-%d %H:%M:%S')] [3/4] financial_ai.py auditoria..." >> "$LOG_FILE"
"$PYTHON" financial_ai.py auditoria >> "$LOG_FILE" 2>&1
EXIT_CODE_3=$?

# 4. Report CFO
echo "[$(date '+%Y-%m-%d %H:%M:%S')] [4/4] financial_ai.py cfo..." >> "$LOG_FILE"
"$PYTHON" financial_ai.py cfo >> "$LOG_FILE" 2>&1
EXIT_CODE_4=$?

# Verifica resultados
ERROS=""
[ $EXIT_CODE_1 -ne 0 ] && ERROS="${ERROS}financeiro "
[ $EXIT_CODE_2 -ne 0 ] && ERROS="${ERROS}vendas "
[ $EXIT_CODE_3 -ne 0 ] && ERROS="${ERROS}auditoria "
[ $EXIT_CODE_4 -ne 0 ] && ERROS="${ERROS}cfo "

if [ -z "$ERROS" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✅ Sincronização concluída com sucesso." >> "$LOG_FILE"
    exit 0
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ❌ Erros em: $ERROS. Verifique o log: $LOG_FILE" >> "$LOG_FILE"
    exit 1
fi
