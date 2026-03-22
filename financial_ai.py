"""
Financial AI — Chat, Insights Automáticos e Detecção de Anomalias
==================================================================
Três modos de operação:

  python3 financial_ai.py chat               → Chat interativo no terminal
  python3 financial_ai.py insights           → Gera e envia insights diários por e-mail
  python3 financial_ai.py insights --semanal → Resumo semanal (rodar às sextas)
  python3 financial_ai.py anomalias          → Detecta e alerta anomalias por e-mail
  python3 financial_ai.py web                → Inicia interface web local (browser)

Requisitos adicionais:
    pip install openai supabase python-dotenv flask scipy numpy

Variáveis de ambiente adicionais no .env:
    EMAIL_SMTP_HOST      (ex: smtp.gmail.com)
    EMAIL_SMTP_PORT      (ex: 587)
    EMAIL_USER           (seu e-mail)
    EMAIL_PASSWORD       (senha de app do Gmail)
    EMAIL_DEST           (destinatário dos alertas)
"""

import os
import json
import logging
import smtplib
import time
import numpy as np
from datetime import datetime, date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from dotenv import load_dotenv
from supabase import create_client, Client
from openai import OpenAI

load_dotenv()

LOG_FILE = "financial_ai.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),                              # console
        logging.FileHandler(LOG_FILE, encoding="utf-8"),     # arquivo
    ]
)
log = logging.getLogger(__name__)

# Suprime logs verbosos de bibliotecas externas no arquivo
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)

SUPABASE_URL   = os.getenv("SUPABASE_URL")
SUPABASE_KEY   = os.getenv("SUPABASE_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

EMAIL_SMTP_HOST = os.getenv("EMAIL_SMTP_HOST", "smtp.gmail.com")
EMAIL_SMTP_PORT = int(os.getenv("EMAIL_SMTP_PORT", 587))
EMAIL_USER      = os.getenv("EMAIL_USER")
EMAIL_PASSWORD  = os.getenv("EMAIL_PASSWORD")
EMAIL_DEST      = os.getenv("EMAIL_DEST")
WEB_TOKEN       = os.getenv("WEB_TOKEN")  # token opcional para proteger a interface web

# Valida variáveis obrigatórias antes de iniciar
_missing = [k for k, v in {"SUPABASE_URL": SUPABASE_URL, "SUPABASE_KEY": SUPABASE_KEY, "OPENAI_API_KEY": OPENAI_API_KEY}.items() if not v]
if _missing:
    raise EnvironmentError(
        f"Variáveis de ambiente obrigatórias ausentes: {', '.join(_missing)}\n"
        "Configure-as no arquivo .env"
    )

# Parâmetros configuráveis (evita magic numbers espalhados pelo código)
CONFIG = {
    "anomaly_zscore_threshold":    2.0,     # desvios padrão para flaggar anomalia
    "anomaly_new_value_threshold": 5000.0,  # R$ mínimo para alertar despesa sem histórico
    "variation_min_pct":           10.0,    # % mínimo de variação diária
    "web_port":                    5050,
    "web_host":                    "127.0.0.1",
    "chat_max_tokens":             2500,
    "context_days":                60,
}

openai_client = OpenAI(api_key=OPENAI_API_KEY)


# ---------------------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------------------

class FinancialDB:
    def __init__(self):
        self.client: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

    def execute_sql(self, sql: str) -> dict:
        """Executa um SELECT dinâmico via função run_select no Supabase.
        Retorna {rows: [...], total_rows: N} ou {error: '...'} em caso de falha.
        Somente SELECT é permitido — qualquer outra instrução é rejeitada no banco."""
        try:
            resp = self.client.rpc("run_select", {"query": sql}).execute()
            rows = resp.data if isinstance(resp.data, list) else (resp.data or [])
            return {"rows": rows, "total_rows": len(rows)}
        except Exception as e:
            return {"error": str(e), "rows": [], "total_rows": 0}

    def get_ultima_extracao_global(self) -> str | None:
        """Retorna a extraction_date mais recente entre todos os syncs (is_initial_load=FALSE)."""
        resp = (
            self.client.table("financial_transactions")
            .select("extraction_date")
            .eq("is_initial_load", False)
            .order("extraction_date", desc=True)
            .limit(1)
            .execute()
        )
        return resp.data[0]["extraction_date"] if resp.data else None

    def get_latest_transactions(self, days: int = 90, tipo: str = None) -> list[dict]:
        """Retorna transações com due_date nos últimos N dias até hoje.
        Não retorna registros de datas futuras — para pendentes futuros use get_pendentes_mes."""
        since = (date.today() - timedelta(days=days)).isoformat()
        hoje  = date.today().isoformat()
        q = (
            self.client.table("financial_transactions_latest")
            .select("tipo,description,value,due_date,status,category_name,payee_name,payment_date,bank_account_name")
            .gte("due_date", since)
            .lte("due_date", hoje)        # nunca retorna parcelas de datas futuras
            .order("due_date", desc=True)
        )
        if tipo:
            q = q.eq("tipo", tipo)
        try:
            resp = q.execute()
            return resp.data or []
        except Exception as e:
            log.error(f"Erro ao buscar transações recentes: {e}")
            return []

    def get_pendentes_mes(self, ano: int, mes: int, tipo: str = None) -> list[dict]:
        """Retorna TODOS os registros pendentes (não quitados) com due_date dentro do mês informado.
        Esta é a consulta correta para 'o que tenho pendente para pagar/receber no mês X'."""
        inicio = f"{ano}-{mes:02d}-01"
        fim    = f"{ano + 1}-01-01" if mes == 12 else f"{ano}-{mes + 1:02d}-01"
        q = (
            self.client.table("financial_transactions_latest")
            .select("tipo,description,value,due_date,status,category_name,payee_name,payment_date,bank_account_name")
            .neq("status", "ACQUITTED")
            .gte("due_date", inicio)
            .lt("due_date", fim)
            .order("due_date")
        )
        if tipo:
            q = q.eq("tipo", tipo)
        try:
            resp = q.execute()
            return resp.data or []
        except Exception as e:
            log.error(f"Erro ao buscar pendentes do mês: {e}")
            return []

    def get_vencimentos_periodo(self, dias: int = 10, tipo: str = None,
                                 status: str = None) -> list[dict]:
        """Retorna transações com vencimento nos próximos N dias.
        Útil para listar faturas/contas a vencer com nome do cliente/fornecedor."""
        hoje = date.today().isoformat()
        ate = (date.today() + timedelta(days=dias)).isoformat()
        q = (
            self.client.table("financial_transactions_latest")
            .select("tipo,description,value,due_date,status,category_name,payee_name,payment_date,bank_account_name")
            .gte("due_date", hoje)
            .lte("due_date", ate)
            .order("due_date")
        )
        if tipo:
            q = q.eq("tipo", tipo)
        if status:
            q = q.eq("status", status)
        else:
            q = q.neq("status", "ACQUITTED")  # por padrão só pendentes
        try:
            resp = q.execute()
            return resp.data or []
        except Exception as e:
            log.error(f"Erro ao buscar vencimentos: {e}")
            return []

    def get_summary_by_period(self, start: str, end: str) -> dict:
        """Agrega receitas e despesas por período."""
        try:
            resp = self.client.rpc("summarize_period", {
                "p_start": start,
                "p_end": end,
            }).execute()
            return resp.data or {}
        except Exception as e:
            log.error(f"Erro ao buscar resumo por período: {e}")
            return {}

    def get_recurring_expenses(self, months: int = 6) -> list[dict]:
        """Busca despesas recorrentes por payee nos últimos N meses."""
        since = (date.today() - timedelta(days=months * 30)).isoformat()
        try:
            resp = (
                self.client.table("financial_transactions_latest")
                .select("payee_name, category_name, value, due_date, status")
                .eq("tipo", "despesa")
                .gte("due_date", since)
                .order("payee_name")
                .execute()
            )
            return resp.data or []
        except Exception as e:
            log.error(f"Erro ao buscar despesas recorrentes: {e}")
            return []

    def get_transactions_for_context(self, days: int = 60) -> list[dict]:
        """Retorna dados resumidos para contexto do LLM (sem embedding para economizar tokens)."""
        since = (date.today() - timedelta(days=days)).isoformat()
        try:
            resp = (
                self.client.table("financial_transactions_latest")
                .select("tipo,description,value,due_date,competence_date,status,category_name,payee_name,payment_date")
                .gte("due_date", since)
                .order("due_date", desc=True)
                .limit(300)
                .execute()
            )
            return resp.data or []
        except Exception as e:
            log.error(f"Erro ao buscar transações para contexto: {e}")
            return []

    def get_daily_variations(self, variacao_min_pct: float = 10.0) -> list[dict]:
        """
        Retorna dias do mês com variação acima do threshold.
        Usa a view daily_financial_summary.
        """
        resp = (
            self.client.table("daily_financial_summary")
            .select("*")
            .not_.is_("variacao_pct", "null")
            .gte("variacao_pct", variacao_min_pct)
            .order("dia", desc=True)
            .execute()
        )
        # Também pega variações negativas relevantes
        resp_neg = (
            self.client.table("daily_financial_summary")
            .select("*")
            .not_.is_("variacao_pct", "null")
            .lte("variacao_pct", -variacao_min_pct)
            .order("dia", desc=True)
            .execute()
        )
        return (resp.data or []) + (resp_neg.data or [])

    def get_records_by_due_date(self, due_date: str, tipo: str = None) -> list[dict]:
        """Retorna registros de um dia específico para explicar variação."""
        q = (
            self.client.table("financial_transactions_latest")
            .select("tipo,description,value,status,category_name,payee_name,payment_date,due_date,extraction_date")
            .eq("due_date", due_date)
        )
        if tipo:
            q = q.eq("tipo", tipo)
        resp = q.order("value", desc=True).execute()
        return resp.data or []

    def get_diff_by_date(self, data_atual: str) -> list[dict]:
        """Retorna registros que mudaram em uma data específica (via diff view)."""
        resp = (
            self.client.table("financial_transactions_diff")
            .select("*")
            .eq("data_atual", data_atual)
            .execute()
        )
        return resp.data or []

    def get_audit_changes(self, ano: int, mes: int,
                          ultima_extracao_global: str | None = None) -> dict:
        """
        Auditoria baseada na última extração GLOBAL de SYNC (is_initial_load=FALSE).

        Quando ultima_extracao_global é fornecida (recomendado), usa-a diretamente
        como referência e retorna vazio para meses que o sync global não tocou.
        Isso evita exibir mudanças de syncs anteriores para meses onde o sync mais
        recente não trouxe registros.

        1. Determina ultima_extracao (global ou por mês como fallback)
        2. Se o mês não tem registros na ultima_extracao → retorna vazio
        3. Pega todos os registros dessa extração (estado atual do mês)
        4. Para cada registro, baseline = penúltima extração do mês (ou carga inicial)
        5. REMOVIDOS via snapshot
        """
        inicio = f"{ano}-{mes:02d}-01"
        fim    = f"{ano+1}-01-01" if mes == 12 else f"{ano}-{mes+1:02d}-01"

        # ── 1. Determina ultima_extracao ──────────────────────────────────────
        _vazio = {"mes": mes, "ano": ano, "inicio": inicio, "fim": fim,
                  "ultima_extracao": ultima_extracao_global, "penultima_extracao": None,
                  "alterados": [], "novos": [], "removidos": []}

        if ultima_extracao_global:
            ultima_extracao = ultima_extracao_global
            # Verifica se o sync global tocou este mês; se não, retorna vazio
            resp_check = (
                self.client.table("financial_transactions")
                .select("id")
                .gte("due_date", inicio)
                .lt("due_date", fim)
                .eq("extraction_date", ultima_extracao)
                .eq("is_initial_load", False)
                .limit(1)
                .execute()
            )
            if not resp_check.data:
                return _vazio
        else:
            # Fallback: detecção por mês (comportamento anterior sem parâmetro global)
            resp_max = (
                self.client.table("financial_transactions")
                .select("extraction_date")
                .gte("due_date", inicio)
                .lt("due_date", fim)
                .eq("is_initial_load", False)
                .order("extraction_date", desc=True)
                .limit(1)
                .execute()
            )
            if not resp_max.data:
                return _vazio
            ultima_extracao = resp_max.data[0]["extraction_date"]

        # ── 1b. Penúltima extraction_date de SYNC do período ─────────────────
        resp_datas_mes = (
            self.client.table("financial_transactions")
            .select("extraction_date")
            .gte("due_date", inicio)
            .lt("due_date", fim)
            .eq("is_initial_load", False)
            .order("extraction_date", desc=True)
            .limit(5000)
            .execute()
        )
        datas_sync_mes = sorted(set(
            r["extraction_date"] for r in (resp_datas_mes.data or [])
        ), reverse=True)
        # datas_sync_mes[0] = ultima (já conhecida), [1] = penultima (se existir)
        penultima_extracao = datas_sync_mes[1] if len(datas_sync_mes) >= 2 else None

        # ── 1c. Pré-carrega registros da penúltima como baseline ──────────────
        # Busca a versão mais recente de cada registro em QUALQUER sync anterior
        # à ultima_extracao — não apenas no penultima_extracao exato.
        # Isso evita classificar como NOVO um registro que mudou de valor em um
        # sync anterior ao penultima (ex: apareceu em 10/03, último sync foi 12/03,
        # atual é 14/03 — sem esta correção o registro seria "NOVO" no 14/03).
        penultima_dict: dict = {}
        resp_pen = (
            self.client.table("financial_transactions")
            .select("installment_id,id,extraction_date,description,value,status,category_name,"
                    "payee_name,due_date,payment_date,bank_account_name,created_at")
            .gte("due_date", inicio)
            .lt("due_date", fim)
            .lt("extraction_date", ultima_extracao)   # qualquer sync ANTES da ultima
            .eq("is_initial_load", False)
            .order("created_at", desc=True)           # mais recente primeiro
            .execute()
        )
        for r in (resp_pen.data or []):
            if r["installment_id"] not in penultima_dict:
                penultima_dict[r["installment_id"]] = r

        # ── 2. Registros do sync da última extração ──────────────────────────
        # Ordena por created_at DESC e deduplica por installment_id
        # para garantir que usamos a versão mais recente de cada registro
        resp_atual = (
            self.client.table("financial_transactions")
            .select("id,installment_id,tipo,description,value,status,"
                    "category_name,payee_name,due_date,extraction_date,"
                    "payment_date,bank_account_name,created_at")
            .gte("due_date", inicio)
            .lt("due_date", fim)
            .eq("extraction_date", ultima_extracao)
            .eq("is_initial_load", False)
            .order("created_at", desc=True)
            .execute()
        )
        # Deduplica: mantém apenas a versão mais recente de cada installment_id
        seen = set()
        registros_atuais = []
        for r in (resp_atual.data or []):
            if r["installment_id"] not in seen:
                seen.add(r["installment_id"])
                registros_atuais.append(r)

        # ── 3. Para cada registro, decide NOVO ou ALTERADO ───────────────────
        alterados = []
        novos     = []

        for reg in registros_atuais:
            iid = reg["installment_id"]

            # Baseline: versão mais recente antes da ultima_extracao (preferencial)
            # → carga inicial (fallback se nunca apareceu em syncs anteriores)
            # Isso garante que registros alterados em syncs intermediários (não
            # apenas na penúltima) também sejam detectados como ALTERADOS.
            if iid in penultima_dict:
                versao_base = penultima_dict[iid]
            else:
                # Fallback: carga inicial — registro nunca apareceu em nenhum sync anterior
                resp_base = (
                    self.client.table("financial_transactions")
                    .select("id,extraction_date,description,value,status,category_name,"
                            "payee_name,due_date,payment_date,bank_account_name,created_at")
                    .eq("installment_id", iid)
                    .eq("is_initial_load", True)
                    .order("created_at")
                    .limit(1)
                    .execute()
                )
                versao_base = resp_base.data[0] if resp_base.data else None

            if not versao_base:
                # Nenhuma versão na carga inicial → NOVO
                novos.append({
                    "installment_id":  iid,
                    "tipo":            reg["tipo"],
                    "description":     reg["description"],
                    "value":           reg.get("value"),
                    "status":          reg.get("status"),
                    "category_name":   reg.get("category_name"),
                    "payee_name":      reg.get("payee_name"),
                    "due_date":        reg["due_date"],
                    "extraction_date": reg["extraction_date"],
                })
            else:
                # Verifica mudanças em todos os campos — incluindo descrição
                mudancas = []

                # Valor
                val_ant = float(versao_base.get("value") or 0)
                val_atu = float(reg.get("value") or 0)
                if abs(val_atu - val_ant) > 0.01:
                    variacao_pct = ((val_atu - val_ant) / val_ant * 100) if val_ant != 0 else None
                    mudancas.append({
                        "campo":        "valor",
                        "label":        "Valor",
                        "anterior":     f"R$ {val_ant:,.2f}",
                        "atual":        f"R$ {val_atu:,.2f}",
                        "variacao_pct": round(variacao_pct, 1) if variacao_pct is not None else None,
                        "anterior_raw": val_ant,
                        "atual_raw":    val_atu,
                    })

                # Campos de texto simples (incluindo descrição — mudanças parciais
                # como adição de informação são ALTERADO, não NOVO)
                campos_texto = [
                    ("description",      "Descrição"),
                    ("status",           "Status"),
                    ("category_name",    "Categoria"),
                    ("payee_name",       "Fornecedor/Cliente"),
                    ("due_date",         "Vencimento"),
                    ("payment_date",     "Data de pagamento"),
                    ("bank_account_name","Conta bancária"),
                ]
                for campo, label in campos_texto:
                    ant = versao_base.get(campo) or ""
                    atu = reg.get(campo) or ""
                    # Normaliza None e string vazia
                    ant = None if ant == "" else ant
                    atu = None if atu == "" else atu
                    if ant != atu:
                        mudancas.append({
                            "campo":    campo,
                            "label":    label,
                            "anterior": ant or "—",
                            "atual":    atu or "—",
                        })

                if mudancas:
                    # Histórico completo ordenado por created_at
                    resp_hist = (
                        self.client.table("financial_transactions")
                        .select("extraction_date,value,status,category_name,"
                                "payee_name,due_date,payment_date,"
                                "bank_account_name,is_initial_load,created_at")
                        .eq("installment_id", iid)
                        .order("created_at")
                        .execute()
                    )
                    historico_display = [
                        {
                            "extraction_date":  v["extraction_date"],
                            "value":            v.get("value"),
                            "status":           v.get("status"),
                            "category_name":    v.get("category_name"),
                            "payee_name":       v.get("payee_name"),
                            "due_date":         v.get("due_date"),
                            "payment_date":     v.get("payment_date"),
                            "bank_account_name":v.get("bank_account_name"),
                            "is_initial_load":  v.get("is_initial_load"),
                        }
                        for v in (resp_hist.data or [])
                    ]
                    alterados.append({
                        "installment_id":   iid,
                        "tipo":             reg["tipo"],
                        "description":      reg["description"],
                        "payee_name":       reg.get("payee_name"),
                        "category_name":    reg.get("category_name"),
                        "due_date":         reg["due_date"],
                        "payment_date":     reg.get("payment_date"),
                        "bank_account_name":reg.get("bank_account_name"),
                        "data_base":        versao_base["extraction_date"],
                        "data_atual":       ultima_extracao,
                        "qtd_versoes":      len(historico_display),
                        "mudancas":         mudancas,
                        "historico":        historico_display,
                    })

        # ── 4. REMOVIDOS via snapshot ────────────────────────────────────────
        resp_snaps = (
            self.client.table("daily_snapshot")
            .select("snapshot_date")
            .gte("snapshot_date", inicio)
            .lt("snapshot_date", fim)
            .order("snapshot_date")
            .execute()
        )
        datas_snapshot = sorted(set(
            r["snapshot_date"] for r in (resp_snaps.data or [])
        ))

        removidos = []
        if len(datas_snapshot) >= 2:
            snap_inicial = datas_snapshot[0]
            snap_final   = datas_snapshot[-1]

            resp_ini = (
                self.client.table("daily_snapshot")
                .select("installment_id,tipo,description,value,status,payee_name,due_date")
                .eq("snapshot_date", snap_inicial)
                .gte("due_date", inicio)
                .lt("due_date", fim)
                .execute()
            )
            ids_iniciais = {r["installment_id"]: r for r in (resp_ini.data or [])}

            resp_fin = (
                self.client.table("daily_snapshot")
                .select("installment_id")
                .eq("snapshot_date", snap_final)
                .execute()
            )
            ids_finais = {r["installment_id"] for r in (resp_fin.data or [])}

            for iid, reg in ids_iniciais.items():
                if iid not in ids_finais:
                    removidos.append({
                        "installment_id":        iid,
                        "tipo":                  reg["tipo"],
                        "description":           reg["description"],
                        "value":                 reg.get("value"),
                        "status":                reg.get("status"),
                        "payee_name":            reg.get("payee_name"),
                        "due_date":              reg.get("due_date"),
                        "snapshot_inicial":      snap_inicial,
                        "ultimo_snapshot_visto": snap_final,
                    })

        return {
            "mes":                mes,
            "ano":                ano,
            "inicio":             inicio,
            "fim":                fim,
            "ultima_extracao":    ultima_extracao,
            "penultima_extracao": penultima_extracao,
            "alterados":          sorted(alterados, key=lambda x: x["due_date"]),
            "novos":              sorted(novos,     key=lambda x: x["due_date"]),
            "removidos":          sorted(removidos, key=lambda x: x["due_date"]),
        }

    def get_totais_por_extracao(self, ano: int) -> dict:
        """
        Retorna receita e despesa totais do ano para 3 snapshots.

        Regra de classificação do ano de um registro:
          - Se payment_date preenchido → usa o ANO de payment_date
          - Se payment_date vazio      → usa o ANO de due_date

        Snapshots:
          - carga_inicial: soma direta dos registros is_initial_load=TRUE do ano
          - ultima/penultima: parte da carga inicial e substitui os installment_ids
            que aparecem no sync — remove o valor da carga inicial daquele ID
            e usa o valor mais recente do sync no lugar.
        """
        def _ano_registro(r):
            """Retorna o ano que deve ser atribuído ao registro."""
            pd = (r.get("payment_date") or "").strip()
            if pd:
                return int(pd[:4])
            dd = (r.get("due_date") or "").strip()
            return int(dd[:4]) if dd else None

        def _somar(registros):
            receita = sum(float(r["value"] or 0) for r in registros if r["tipo"] == "receita")
            despesa = sum(float(r["value"] or 0) for r in registros if r["tipo"] == "despesa")
            lucro   = receita - despesa
            margem  = (lucro / receita * 100) if receita > 0 else 0
            return {"receita": receita, "despesa": despesa,
                    "lucro": lucro, "margem": round(margem, 1)}

        # ── 1. Carga inicial ──────────────────────────────────────────────────
        # Busca todos os registros de carga inicial (sem filtro de data no banco,
        # pois o ano depende de payment_date ou due_date — classificado no Python)
        # Supabase tem limite padrão de 1000 rows por query.
        # A carga inicial pode ter mais registros — busca todas as páginas.
        base_bruto = []
        _page_size = 1000
        _offset = 0
        while True:
            _resp = (
                self.client.table("financial_transactions")
                .select("installment_id,tipo,value,due_date,payment_date")
                .eq("is_initial_load", True)
                .range(_offset, _offset + _page_size - 1)
                .execute()
            )
            _batch = _resp.data or []
            base_bruto.extend(_batch)
            if len(_batch) < _page_size:
                break
            _offset += _page_size

        # Filtra pelo ano correto e monta dicionário base (1 entrada por installment_id)
        base = {
            r["installment_id"]: r
            for r in base_bruto
            if _ano_registro(r) == ano
        }
        carga_inicial = _somar(list(base.values()))

        # ── 1b. IDs ativos no último snapshot (para excluir deletados) ────────
        snapshot_ids = self._fetch_snapshot_ids()

        # ── 2. Datas de sync disponíveis ──────────────────────────────────────
        resp_datas = (
            self.client.table("financial_transactions")
            .select("extraction_date")
            .eq("is_initial_load", False)
            .order("extraction_date", desc=True)
            .limit(5000)
            .execute()
        )
        datas_sync = sorted(set(
            r["extraction_date"] for r in (resp_datas.data or [])
        ), reverse=True)

        def _totais_para_data(extraction_date):
            """
            Busca todos os registros de syncs ATÉ a extraction_date informada
            (visão cumulativa), filtra pelo ano (via payment_date ou due_date),
            deduplica por installment_id (versão mais recente até essa data),
            e substitui na base da carga inicial.
            Usar .lte em vez de .eq garante que mudanças de syncs anteriores
            continuem refletidas mesmo quando o sync mais recente não tocou o mês.
            """
            resp = (
                self.client.table("financial_transactions")
                .select("installment_id,tipo,value,due_date,payment_date,created_at")
                .eq("is_initial_load", False)
                .lte("extraction_date", extraction_date)   # cumulativo até esta data
                .order("created_at", desc=True)
                .limit(10000)
                .execute()
            )
            # Deduplica: versão mais recente de cada installment_id do sync
            seen = set()
            sync = {}
            for r in (resp.data or []):
                if r["installment_id"] not in seen:
                    seen.add(r["installment_id"])
                    # Só inclui se o ano do registro bater com o ano solicitado
                    if _ano_registro(r) == ano:
                        sync[r["installment_id"]] = r

            # Começa com carga inicial do ano e substitui/adiciona IDs do sync
            visao = dict(base)   # cópia da carga inicial já filtrada pelo ano
            visao.update(sync)   # sobrescreve IDs alterados, adiciona novos

            # Exclui registros deletados do Conta Azul: registros que estão
            # no nosso banco mas não aparecem mais no último snapshot são
            # transações que o usuário removeu no ERP.
            if snapshot_ids is not None:
                visao = {k: v for k, v in visao.items() if k in snapshot_ids}

            return _somar(list(visao.values()))

        ultima    = _totais_para_data(datas_sync[0]) if len(datas_sync) >= 1 else None
        penultima = _totais_para_data(datas_sync[1]) if len(datas_sync) >= 2 else None

        return {
            "carga_inicial":  carga_inicial,
            "penultima":      penultima,
            "penultima_data": datas_sync[1] if len(datas_sync) >= 2 else None,
            "ultima":         ultima,
            "ultima_data":    datas_sync[0] if len(datas_sync) >= 1 else None,
        }

    def get_variacao_mensal_por_extracao(self, ano: int) -> dict:
        """
        Retorna receita e despesa por mês para ultima e penultima extração,
        com delta entre elas. Usa a mesma regra de classificação de ano/mês:
          - Se payment_date preenchido → usa payment_date
          - Se payment_date vazio      → usa due_date
        """
        def _ano_mes(r):
            pd = (r.get("payment_date") or "").strip()
            if pd and len(pd) >= 7:
                return int(pd[:4]), int(pd[5:7])
            dd = (r.get("due_date") or "").strip()
            if dd and len(dd) >= 7:
                return int(dd[:4]), int(dd[5:7])
            return None, None

        # ── Carga inicial paginada ────────────────────────────────────────────
        base_bruto = []
        _page_size = 1000
        _offset = 0
        while True:
            _resp = (
                self.client.table("financial_transactions")
                .select("installment_id,tipo,value,due_date,payment_date")
                .eq("is_initial_load", True)
                .range(_offset, _offset + _page_size - 1)
                .execute()
            )
            _batch = _resp.data or []
            base_bruto.extend(_batch)
            if len(_batch) < _page_size:
                break
            _offset += _page_size

        base = {
            r["installment_id"]: r
            for r in base_bruto
            if _ano_mes(r)[0] == ano
        }

        # ── IDs ativos no último snapshot (para excluir deletados) ────────────
        _vmm_snapshot_ids = self._fetch_snapshot_ids()

        # ── Datas de sync disponíveis ─────────────────────────────────────────
        resp_datas = (
            self.client.table("financial_transactions")
            .select("extraction_date")
            .eq("is_initial_load", False)
            .order("extraction_date", desc=True)
            .limit(5000)
            .execute()
        )
        datas_sync = sorted(set(
            r["extraction_date"] for r in (resp_datas.data or [])
        ), reverse=True)

        def _visao_para_data(extraction_date):
            # Visão cumulativa: acumula todos os syncs ATÉ a data informada.
            # Usar .lte (não .eq) garante que mudanças de syncs anteriores
            # permaneçam refletidas mesmo que o sync mais recente não tocou o mês.
            resp = (
                self.client.table("financial_transactions")
                .select("installment_id,tipo,value,due_date,payment_date,created_at")
                .eq("is_initial_load", False)
                .lte("extraction_date", extraction_date)   # cumulativo até esta data
                .order("created_at", desc=True)
                .limit(10000)
                .execute()
            )
            seen = set()
            sync = {}
            for r in (resp.data or []):
                if r["installment_id"] not in seen:
                    seen.add(r["installment_id"])
                    a, _ = _ano_mes(r)
                    if a == ano:
                        sync[r["installment_id"]] = r
            visao = dict(base)
            visao.update(sync)
            # Exclui registros deletados do Conta Azul (não presentes no snapshot)
            if _vmm_snapshot_ids is not None:
                visao = {k: v for k, v in visao.items() if k in _vmm_snapshot_ids}
            return visao

        def _somar_por_mes(visao: dict) -> dict:
            """Agrega registros da visao por mês. Retorna {mes: {receita, despesa}}."""
            por_mes: dict = {}
            for r in visao.values():
                _, m = _ano_mes(r)
                if not m:
                    continue
                if m not in por_mes:
                    por_mes[m] = {"receita": 0.0, "despesa": 0.0}
                tipo = r.get("tipo", "")
                val  = float(r.get("value") or 0)
                if tipo == "receita":
                    por_mes[m]["receita"] += val
                elif tipo == "despesa":
                    por_mes[m]["despesa"] += val
            return por_mes

        ultima_data    = datas_sync[0] if len(datas_sync) >= 1 else None
        penultima_data = datas_sync[1] if len(datas_sync) >= 2 else None

        # Carga inicial por mês — usado como fallback quando não há penúltima
        carga_inicial_por_mes = _somar_por_mes(base)

        ultima_por_mes    = _somar_por_mes(_visao_para_data(ultima_data))    if ultima_data    else {}
        penultima_por_mes = _somar_por_mes(_visao_para_data(penultima_data)) if penultima_data else carga_inicial_por_mes

        # Indica com o que a última está sendo comparada
        anterior_label = "Penúltima" if penultima_data else "Carga Inicial"

        todos_meses = sorted(set(list(ultima_por_mes.keys()) + list(penultima_por_mes.keys())))
        meses = []
        for m in todos_meses:
            u = ultima_por_mes.get(m,    {"receita": 0.0, "despesa": 0.0})
            p = penultima_por_mes.get(m, {"receita": 0.0, "despesa": 0.0})
            meses.append({
                "mes":      m,
                "nome":     MESES_PT.get(m, str(m)),
                "ultima":   u,
                "penultima": p,
                "delta": {
                    "receita": u["receita"] - p["receita"],
                    "despesa": u["despesa"] - p["despesa"],
                },
            })

        return {
            "meses":          meses,
            "ultima_data":    ultima_data,
            "penultima_data": penultima_data,
            "anterior_label": anterior_label,
        }

    # ══════════════════════════════════════════════════════════════════════════
    # Métodos do Report CFO
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _data_ref(r: dict) -> str:
        """Data de referência da transação para classificação de período.
        Regra: usa payment_date se existir e preenchida, caso contrário usa due_date.
        Transações pagas/recebidas sem payment_date são classificadas pelo due_date."""
        return (r.get("payment_date") or r.get("due_date") or "")

    def _fetch_snapshot_ids(self) -> set | None:
        """Retorna o conjunto de installment_ids ativos no último daily_snapshot.
        Usado para excluir registros deletados do Conta Azul dos cálculos."""
        resp_max = (
            self.client.table("daily_snapshot")
            .select("snapshot_date")
            .order("snapshot_date", desc=True)
            .limit(1)
            .execute()
        )
        if not resp_max.data:
            return None
        snap_date = resp_max.data[0]["snapshot_date"]
        ids: set = set()
        _off = 0
        _pg = 1000
        while True:
            _r = (
                self.client.table("daily_snapshot")
                .select("installment_id")
                .eq("snapshot_date", snap_date)
                .range(_off, _off + _pg - 1)
                .execute()
            )
            _b = _r.data or []
            for x in _b:
                ids.add(x["installment_id"])
            if len(_b) < _pg:
                break
            _off += _pg
        return ids

    def _fetch_latest_all(self, ano: int) -> list[dict]:
        """Busca todos os registros da view financial_transactions_latest para o ano,
        excluindo registros que foram deletados do Conta Azul (não presentes no
        último daily_snapshot)."""
        registros = []
        _page_size = 1000
        _offset = 0
        while True:
            resp = (
                self.client.table("financial_transactions_latest")
                .select("installment_id,tipo,description,value,status,"
                        "category_name,payee_name,due_date,payment_date,"
                        "bank_account_name")
                .or_(f"due_date.gte.{ano}-01-01,payment_date.gte.{ano}-01-01")
                .range(_offset, _offset + _page_size - 1)
                .execute()
            )
            batch = resp.data or []
            registros.extend(batch)
            if len(batch) < _page_size:
                break
            _offset += _page_size

        # Filtra registros deletados do Conta Azul usando o último snapshot
        snapshot_ids = self._fetch_snapshot_ids()
        if snapshot_ids is not None:
            registros = [r for r in registros if r["installment_id"] in snapshot_ids]

        return registros

    def get_posicao_caixa(self, ano: int, mes: int) -> dict:
        """Seção 1 — Posição de Caixa Hoje (regime de caixa)."""
        hoje = date.today()
        inicio_mes = f"{ano}-{mes:02d}-01"
        if mes == 12:
            fim_mes = f"{ano + 1}-01-01"
        else:
            fim_mes = f"{ano}-{mes + 1:02d}-01"
        amanha = (hoje + timedelta(days=1)).isoformat()

        registros = self._fetch_latest_all(ano)

        # Realizado: ACQUITTED — data_ref = payment_date se existir, senão due_date
        realizado_rec = sum(float(r["value"] or 0) for r in registros
                           if r["tipo"] == "receita"
                           and r.get("status") == "ACQUITTED"
                           and self._data_ref(r) >= inicio_mes
                           and self._data_ref(r) < fim_mes)
        realizado_desp = sum(float(r["value"] or 0) for r in registros
                            if r["tipo"] == "despesa"
                            and r.get("status") == "ACQUITTED"
                            and self._data_ref(r) >= inicio_mes
                            and self._data_ref(r) < fim_mes)

        # A realizar: ≠ACQUITTED + due_date de hoje até fim do mês
        a_realizar_rec = sum(float(r["value"] or 0) for r in registros
                            if r["tipo"] == "receita"
                            and r.get("status") != "ACQUITTED"
                            and (r.get("due_date") or "") >= amanha
                            and (r.get("due_date") or "") < fim_mes)
        a_realizar_desp = sum(float(r["value"] or 0) for r in registros
                             if r["tipo"] == "despesa"
                             and r.get("status") != "ACQUITTED"
                             and (r.get("due_date") or "") >= amanha
                             and (r.get("due_date") or "") < fim_mes)

        # Comparativo: mês anterior até o mesmo dia
        mes_ant = mes - 1 if mes > 1 else 12
        ano_ant = ano if mes > 1 else ano - 1
        inicio_ant = f"{ano_ant}-{mes_ant:02d}-01"
        dia_ref = min(hoje.day, 28)  # evita dia 31 em mês com 28 dias
        corte_ant = f"{ano_ant}-{mes_ant:02d}-{dia_ref:02d}"
        if mes_ant == 12:
            fim_ant = f"{ano_ant + 1}-01-01"
        else:
            fim_ant = f"{ano_ant}-{mes_ant + 1:02d}-01"

        ant_rec = sum(float(r["value"] or 0) for r in registros
                      if r["tipo"] == "receita"
                      and r.get("status") == "ACQUITTED"
                      and self._data_ref(r) >= inicio_ant
                      and self._data_ref(r) <= corte_ant)
        ant_desp = sum(float(r["value"] or 0) for r in registros
                       if r["tipo"] == "despesa"
                       and r.get("status") == "ACQUITTED"
                       and self._data_ref(r) >= inicio_ant
                       and self._data_ref(r) <= corte_ant)

        return {
            "realizado":  {"receita": realizado_rec,  "despesa": realizado_desp,
                           "liquido": realizado_rec - realizado_desp},
            "a_realizar": {"receita": a_realizar_rec, "despesa": a_realizar_desp,
                           "liquido": a_realizar_rec - a_realizar_desp},
            "mes_anterior_mesmo_dia": {"receita": ant_rec, "despesa": ant_desp,
                                       "liquido": ant_rec - ant_desp},
            "dia_ref": dia_ref,
            "mes_ant_label": f"{MESES_PT.get(mes_ant, str(mes_ant))}/{ano_ant}",
        }

    def get_fluxo_semana(self) -> dict:
        """Seção 2 — Fluxo de Caixa da Semana (próximos 7 dias)."""
        hoje = date.today()
        registros = self._fetch_latest_all(hoje.year)

        dias = []
        itens_destaque = []
        total_ent = total_sai = 0.0
        for i in range(1, 8):
            d = (hoje + timedelta(days=i)).isoformat()
            rec = [r for r in registros
                   if r["tipo"] == "receita" and r.get("status") != "ACQUITTED"
                   and (r.get("due_date") or "") == d]
            desp = [r for r in registros
                    if r["tipo"] == "despesa" and r.get("status") != "ACQUITTED"
                    and (r.get("due_date") or "") == d]
            s_rec = sum(float(r["value"] or 0) for r in rec)
            s_desp = sum(float(r["value"] or 0) for r in desp)
            total_ent += s_rec
            total_sai += s_desp
            dias.append({"data": d, "receita": s_rec, "despesa": s_desp,
                         "saldo": s_rec - s_desp})
            for r in rec + desp:
                if float(r.get("value") or 0) >= 50000:
                    itens_destaque.append(r)

        return {
            "entradas": total_ent,
            "saidas": total_sai,
            "saldo_projetado": total_ent - total_sai,
            "dias": dias,
            "itens_destaque": sorted(itens_destaque,
                                     key=lambda x: float(x.get("value") or 0),
                                     reverse=True),
        }

    def get_inadimplencia(self) -> dict:
        """Seção 3 — Inadimplência / Vencidos."""
        hoje = date.today().isoformat()
        registros = self._fetch_latest_all(date.today().year)

        rec_vencidas = [r for r in registros
                        if r["tipo"] == "receita"
                        and r.get("status") != "ACQUITTED"
                        and (r.get("due_date") or "9999") < hoje]
        desp_vencidas = [r for r in registros
                         if r["tipo"] == "despesa"
                         and r.get("status") != "ACQUITTED"
                         and (r.get("due_date") or "9999") < hoje]

        # Aging
        aging = {"1-15d": 0.0, "16-30d": 0.0, "31-60d": 0.0, "60+d": 0.0}
        aging_qtd = {"1-15d": 0, "16-30d": 0, "31-60d": 0, "60+d": 0}
        for r in rec_vencidas:
            dias = (date.today() - date.fromisoformat(r["due_date"])).days
            val = float(r.get("value") or 0)
            if dias <= 15:
                aging["1-15d"] += val; aging_qtd["1-15d"] += 1
            elif dias <= 30:
                aging["16-30d"] += val; aging_qtd["16-30d"] += 1
            elif dias <= 60:
                aging["31-60d"] += val; aging_qtd["31-60d"] += 1
            else:
                aging["60+d"] += val; aging_qtd["60+d"] += 1

        # Todos os devedores (receitas vencidas), ordenados por valor desc
        devedores: dict = {}
        for r in rec_vencidas:
            pn = r.get("payee_name") or "Sem nome"
            if pn not in devedores:
                devedores[pn] = {"total": 0.0, "qtd": 0, "dias_total": 0}
            devedores[pn]["total"] += float(r.get("value") or 0)
            devedores[pn]["qtd"] += 1
            devedores[pn]["dias_total"] += (date.today() - date.fromisoformat(r["due_date"])).days
        todos_devedores = sorted(devedores.items(), key=lambda x: x[1]["total"], reverse=True)
        todos_devedores = [
            {"payee_name": k, "total": v["total"], "qtd": v["qtd"],
             "dias_medio": round(v["dias_total"] / v["qtd"]) if v["qtd"] else 0}
            for k, v in todos_devedores
        ]
        total_devedores = sum(d["total"] for d in todos_devedores)

        return {
            "receitas_vencidas": {
                "total": sum(float(r.get("value") or 0) for r in rec_vencidas),
                "qtd": len(rec_vencidas),
                "aging": aging,
                "aging_qtd": aging_qtd,
            },
            "despesas_vencidas": {
                "total": sum(float(r.get("value") or 0) for r in desp_vencidas),
                "qtd": len(desp_vencidas),
            },
            "top_devedores": todos_devedores,
            "total_devedores": total_devedores,
        }

    def get_resumo_mes(self, ano: int, mes: int) -> dict:
        """Seção 4 — Resumo do Mês Corrente (regime de caixa)."""
        inicio_mes = f"{ano}-{mes:02d}-01"
        fim_mes = f"{ano + 1}-01-01" if mes == 12 else f"{ano}-{mes + 1:02d}-01"
        registros = self._fetch_latest_all(ano)

        rec_real = sum(float(r["value"] or 0) for r in registros
                       if r["tipo"] == "receita" and r.get("status") == "ACQUITTED"
                       and self._data_ref(r) >= inicio_mes
                       and self._data_ref(r) < fim_mes)
        rec_prev = sum(float(r["value"] or 0) for r in registros
                       if r["tipo"] == "receita" and r.get("status") != "ACQUITTED"
                       and (r.get("due_date") or "") >= inicio_mes
                       and (r.get("due_date") or "") < fim_mes)
        desp_real = sum(float(r["value"] or 0) for r in registros
                        if r["tipo"] == "despesa" and r.get("status") == "ACQUITTED"
                        and self._data_ref(r) >= inicio_mes
                        and self._data_ref(r) < fim_mes)
        desp_prev = sum(float(r["value"] or 0) for r in registros
                        if r["tipo"] == "despesa" and r.get("status") != "ACQUITTED"
                        and (r.get("due_date") or "") >= inicio_mes
                        and (r.get("due_date") or "") < fim_mes)

        pct_rec = (rec_real / (rec_real + rec_prev) * 100) if (rec_real + rec_prev) else 0
        pct_desp = (desp_real / (desp_real + desp_prev) * 100) if (desp_real + desp_prev) else 0

        lucro_real = rec_real - desp_real
        lucro_proj = (rec_real + rec_prev) - (desp_real + desp_prev)
        margem_real = (lucro_real / rec_real * 100) if rec_real else 0
        margem_proj = (lucro_proj / (rec_real + rec_prev) * 100) if (rec_real + rec_prev) else 0

        return {
            "receita_realizada": rec_real, "receita_prevista": rec_prev,
            "despesa_realizada": desp_real, "despesa_prevista": desp_prev,
            "pct_execucao_receita": round(pct_rec, 1),
            "pct_execucao_despesa": round(pct_desp, 1),
            "lucro_realizado": lucro_real,
            "lucro_projetado": lucro_proj,
            "margem_realizada": round(margem_real, 1),
            "margem_projetada": round(margem_proj, 1),
        }

    def get_concentracao_risco(self, ano: int, mes: int) -> dict:
        """Seção 5 — Concentração de Risco (pendentes do mês)."""
        inicio_mes = f"{ano}-{mes:02d}-01"
        fim_mes = f"{ano + 1}-01-01" if mes == 12 else f"{ano}-{mes + 1:02d}-01"
        registros = self._fetch_latest_all(ano)

        pendentes = [r for r in registros
                     if r.get("status") != "ACQUITTED"
                     and (r.get("due_date") or "") >= inicio_mes
                     and (r.get("due_date") or "") < fim_mes]

        desp_pend = sorted([r for r in pendentes if r["tipo"] == "despesa"],
                           key=lambda x: float(x.get("value") or 0), reverse=True)
        rec_pend  = sorted([r for r in pendentes if r["tipo"] == "receita"],
                           key=lambda x: float(x.get("value") or 0), reverse=True)

        # Concentração por categoria
        def _concentracao(regs):
            cats: dict = {}
            total = sum(float(r.get("value") or 0) for r in regs)
            for r in regs:
                cat = r.get("category_name") or "Sem categoria"
                cats[cat] = cats.get(cat, 0) + float(r.get("value") or 0)
            resultado = sorted(
                [{"category_name": k, "total": v,
                  "pct": round(v / total * 100, 1) if total else 0}
                 for k, v in cats.items()],
                key=lambda x: x["total"], reverse=True
            )
            return resultado[:10]

        return {
            "top_despesas": [{"description": r["description"],
                              "payee_name": r.get("payee_name"),
                              "value": float(r.get("value") or 0),
                              "due_date": r.get("due_date")}
                             for r in desp_pend[:5]],
            "top_receitas": [{"description": r["description"],
                              "payee_name": r.get("payee_name"),
                              "value": float(r.get("value") or 0),
                              "due_date": r.get("due_date")}
                             for r in rec_pend[:5]],
            "concentracao_despesa": _concentracao(desp_pend),
            "concentracao_receita": _concentracao(rec_pend),
        }

    def get_kpis(self, ano: int, mes: int) -> dict:
        """Seção 6 — KPIs financeiros do mês."""
        inicio_mes = f"{ano}-{mes:02d}-01"
        fim_mes = f"{ano + 1}-01-01" if mes == 12 else f"{ano}-{mes + 1:02d}-01"
        hoje = date.today()
        dias_corridos = max((hoje - date.fromisoformat(inicio_mes)).days, 1)
        registros = self._fetch_latest_all(ano)

        # Receita e despesa realizadas no mês — data_ref = payment_date se existir, senão due_date
        rec_real = sum(float(r["value"] or 0) for r in registros
                       if r["tipo"] == "receita" and r.get("status") == "ACQUITTED"
                       and self._data_ref(r) >= inicio_mes
                       and self._data_ref(r) < fim_mes)
        desp_real = sum(float(r["value"] or 0) for r in registros
                        if r["tipo"] == "despesa" and r.get("status") == "ACQUITTED"
                        and self._data_ref(r) >= inicio_mes
                        and self._data_ref(r) < fim_mes)
        lucro = rec_real - desp_real
        margem = (lucro / rec_real * 100) if rec_real else 0

        # Prazo médio de recebimento (dias entre due_date e payment_date)
        # Só considera transações com ambas as datas; usa _data_ref para filtrar o período
        prazos_rec = []
        for r in registros:
            if (r["tipo"] == "receita" and r.get("status") == "ACQUITTED"
                    and r.get("payment_date") and r.get("due_date")
                    and self._data_ref(r) >= inicio_mes
                    and self._data_ref(r) < fim_mes):
                try:
                    d1 = date.fromisoformat(r["due_date"][:10])
                    d2 = date.fromisoformat(r["payment_date"][:10])
                    prazos_rec.append((d2 - d1).days)
                except (ValueError, TypeError):
                    pass
        prazo_rec = round(sum(prazos_rec) / len(prazos_rec), 1) if prazos_rec else 0

        # Prazo médio de pagamento
        prazos_desp = []
        for r in registros:
            if (r["tipo"] == "despesa" and r.get("status") == "ACQUITTED"
                    and r.get("payment_date") and r.get("due_date")
                    and self._data_ref(r) >= inicio_mes
                    and self._data_ref(r) < fim_mes):
                try:
                    d1 = date.fromisoformat(r["due_date"][:10])
                    d2 = date.fromisoformat(r["payment_date"][:10])
                    prazos_desp.append((d2 - d1).days)
                except (ValueError, TypeError):
                    pass
        prazo_desp = round(sum(prazos_desp) / len(prazos_desp), 1) if prazos_desp else 0

        # Taxa de inadimplência: receitas vencidas / receita total do mês
        rec_total_mes = sum(float(r["value"] or 0) for r in registros
                            if r["tipo"] == "receita"
                            and (r.get("due_date") or "") >= inicio_mes
                            and (r.get("due_date") or "") < fim_mes)
        rec_vencidas = sum(float(r["value"] or 0) for r in registros
                           if r["tipo"] == "receita"
                           and r.get("status") != "ACQUITTED"
                           and (r.get("due_date") or "") >= inicio_mes
                           and (r.get("due_date") or "") < hoje.isoformat())
        taxa_inad = (rec_vencidas / rec_total_mes * 100) if rec_total_mes else 0

        # Burn rate e runway
        burn_rate = desp_real / dias_corridos if dias_corridos else 0
        runway = round(lucro / burn_rate) if burn_rate > 0 else 999

        return {
            "margem_operacional": round(margem, 1),
            "prazo_medio_recebimento": prazo_rec,
            "prazo_medio_pagamento": prazo_desp,
            "taxa_inadimplencia": round(taxa_inad, 1),
            "burn_rate_diario": round(burn_rate, 2),
            "runway_dias": runway,
        }

    def get_resumo_ano(self, ano: int) -> dict:
        """Resumo financeiro do ano completo: mês a mês + totais calculados em Python.
        Evita erros aritméticos do LLM ao somar 12 meses manualmente."""
        MESES_NOME = {
            1: "Janeiro", 2: "Fevereiro", 3: "Março", 4: "Abril",
            5: "Maio", 6: "Junho", 7: "Julho", 8: "Agosto",
            9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro",
        }
        # _fetch_latest_all já inclui registros de anos anteriores com payment_date neste ano
        # via filtro: due_date >= ano-01-01 OR payment_date >= ano-01-01
        # NÃO buscar o ano anterior separadamente — causaria double-counting
        registros = self._fetch_latest_all(ano)

        meses = []
        tot_rec_real = tot_rec_prev = tot_desp_real = tot_desp_prev = 0.0

        for mes in range(1, 13):
            inicio = f"{ano}-{mes:02d}-01"
            fim    = f"{ano + 1}-01-01" if mes == 12 else f"{ano}-{mes + 1:02d}-01"

            rec_real = sum(float(r["value"] or 0) for r in registros
                           if r["tipo"] == "receita" and r.get("status") == "ACQUITTED"
                           and self._data_ref(r) >= inicio
                           and self._data_ref(r) < fim)
            rec_prev = sum(float(r["value"] or 0) for r in registros
                           if r["tipo"] == "receita" and r.get("status") != "ACQUITTED"
                           and (r.get("due_date") or "") >= inicio
                           and (r.get("due_date") or "") < fim)
            desp_real = sum(float(r["value"] or 0) for r in registros
                            if r["tipo"] == "despesa" and r.get("status") == "ACQUITTED"
                            and self._data_ref(r) >= inicio
                            and self._data_ref(r) < fim)
            desp_prev = sum(float(r["value"] or 0) for r in registros
                            if r["tipo"] == "despesa" and r.get("status") != "ACQUITTED"
                            and (r.get("due_date") or "") >= inicio
                            and (r.get("due_date") or "") < fim)

            rec_total  = rec_real  + rec_prev
            desp_total = desp_real + desp_prev
            lucro      = rec_total - desp_total

            tot_rec_real  += rec_real
            tot_rec_prev  += rec_prev
            tot_desp_real += desp_real
            tot_desp_prev += desp_prev

            meses.append({
                "mes":          mes,
                "mes_nome":     MESES_NOME[mes],
                "receita_realizada":  round(rec_real,  2),
                "receita_prevista":   round(rec_prev,  2),
                "receita_total":      round(rec_total, 2),
                "despesa_realizada":  round(desp_real,  2),
                "despesa_prevista":   round(desp_prev,  2),
                "despesa_total":      round(desp_total, 2),
                "lucro":              round(lucro,      2),
            })

        rec_anual  = tot_rec_real  + tot_rec_prev
        desp_anual = tot_desp_real + tot_desp_prev
        lucro_anual = rec_anual - desp_anual

        return {
            "ano": ano,
            "meses": meses,
            "totais": {
                "receita_realizada":  round(tot_rec_real,  2),
                "receita_prevista":   round(tot_rec_prev,  2),
                "receita_anual":      round(rec_anual,     2),
                "despesa_realizada":  round(tot_desp_real, 2),
                "despesa_prevista":   round(tot_desp_prev, 2),
                "despesa_anual":      round(desp_anual,    2),
                "lucro_anual":        round(lucro_anual,   2),
                "margem_anual":       round(lucro_anual / rec_anual * 100, 1) if rec_anual else 0,
            },
        }

    def search_semantic(self, query: str, tipo: str = None, top_k: int = 15) -> list[dict]:
        """Busca semântica usando pgvector."""
        embedding = _generate_embedding(query)
        resp = self.client.rpc("search_transactions", {
            "query_embedding": embedding,
            "tipo_filter": tipo,
            "match_count": top_k,
            "apenas_latest": True,
        }).execute()
        return resp.data or []

    # -----------------------------------------------------------------------
    # Vendas (sales_transactions)
    # -----------------------------------------------------------------------

    def get_vendas(self, days: int = 90, situacao: str = None,
                   cliente: str = None) -> list[dict]:
        """Retorna vendas recentes da view sales_transactions_latest."""
        since = (date.today() - timedelta(days=days)).isoformat()
        q = (
            self.client.table("sales_transactions_latest")
            .select("sale_id,numero,data_venda,total,tipo_venda,situacao,"
                    "cliente_nome,cliente_email,categoria_id,categoria_nome,"
                    "evento_financeiro_id,versao")
            .gte("data_venda", since)
            .order("data_venda", desc=True)
        )
        if situacao:
            q = q.eq("situacao", situacao)
        if cliente:
            q = q.ilike("cliente_nome", f"%{cliente}%")
        try:
            resp = q.execute()
            return resp.data or []
        except Exception as e:
            log.error(f"Erro ao buscar vendas: {e}")
            return []

    def get_resumo_vendas_mes(self, ano: int, mes: int) -> dict:
        """Resumo de vendas do mês: total, quantidade, por situação, por categoria, top clientes."""
        inicio = f"{ano}-{mes:02d}-01"
        fim = f"{ano + 1}-01-01" if mes == 12 else f"{ano}-{mes + 1:02d}-01"
        try:
            resp = (
                self.client.table("sales_transactions_latest")
                .select("numero,data_venda,total,situacao,cliente_nome,"
                        "categoria_nome,tipo_venda,evento_financeiro_id")
                .gte("data_venda", inicio)
                .lt("data_venda", fim)
                .order("total", desc=True)
                .execute()
            )
            vendas = resp.data or []
        except Exception as e:
            log.error(f"Erro ao buscar resumo de vendas: {e}")
            vendas = []

        total_geral = sum(float(v.get("total") or 0) for v in vendas)
        qtd = len(vendas)

        # Por situação
        por_situacao: dict[str, dict] = {}
        for v in vendas:
            sit = v.get("situacao") or "Sem situação"
            if sit not in por_situacao:
                por_situacao[sit] = {"total": 0, "quantidade": 0}
            por_situacao[sit]["total"] += float(v.get("total") or 0)
            por_situacao[sit]["quantidade"] += 1

        # Por categoria
        por_categoria: dict[str, dict] = {}
        for v in vendas:
            cat = v.get("categoria_nome") or "Sem categoria"
            if cat not in por_categoria:
                por_categoria[cat] = {"total": 0, "quantidade": 0}
            por_categoria[cat]["total"] += float(v.get("total") or 0)
            por_categoria[cat]["quantidade"] += 1

        # Todos os clientes (ordenados por total desc)
        por_cliente: dict[str, dict] = {}
        for v in vendas:
            cli = v.get("cliente_nome") or "Sem nome"
            if cli not in por_cliente:
                por_cliente[cli] = {"total": 0, "quantidade": 0}
            por_cliente[cli]["total"] += float(v.get("total") or 0)
            por_cliente[cli]["quantidade"] += 1
        clientes_ordenados = sorted(por_cliente.items(), key=lambda x: x[1]["total"], reverse=True)

        return {
            "periodo": f"{ano}-{mes:02d}",
            "total_vendas": round(total_geral, 2),
            "quantidade_vendas": qtd,
            "total_clientes": len(clientes_ordenados),
            "por_situacao": por_situacao,
            "por_categoria": por_categoria,
            "clientes": [{"cliente": c, **d} for c, d in clientes_ordenados],
        }

    def get_venda_com_financeiro(self, numero_venda: int = None,
                                  cliente: str = None) -> list[dict]:
        """Busca vendas com vínculo financeiro (JOIN via evento_financeiro_id = installment_id).
        Retorna dados da venda + dados financeiros vinculados."""
        # 1. Busca vendas
        q = (
            self.client.table("sales_transactions_latest")
            .select("sale_id,numero,data_venda,total,situacao,cliente_nome,"
                    "categoria_nome,evento_financeiro_id")
        )
        if numero_venda:
            q = q.eq("numero", numero_venda)
        if cliente:
            q = q.ilike("cliente_nome", f"%{cliente}%")
        try:
            resp_vendas = q.order("data_venda", desc=True).limit(50).execute()
            vendas = resp_vendas.data or []
        except Exception as e:
            log.error(f"Erro ao buscar vendas para vínculo: {e}")
            return []

        if not vendas:
            return []

        # 2. Busca registros financeiros vinculados
        ev_ids = [v["evento_financeiro_id"] for v in vendas if v.get("evento_financeiro_id")]
        financeiro_map = {}
        if ev_ids:
            try:
                resp_fin = (
                    self.client.table("financial_transactions_latest")
                    .select("installment_id,description,value,due_date,payment_date,"
                            "status,category_name,payee_name,bank_account_name")
                    .in_("installment_id", ev_ids)
                    .execute()
                )
                for f in (resp_fin.data or []):
                    financeiro_map[f["installment_id"]] = f
            except Exception as e:
                log.error(f"Erro ao buscar financeiro vinculado: {e}")

        # 3. Monta resultado combinado
        resultado = []
        for v in vendas:
            item = {
                "venda_numero": v.get("numero"),
                "data_venda": v.get("data_venda"),
                "total_venda": v.get("total"),
                "situacao_venda": v.get("situacao"),
                "cliente": v.get("cliente_nome"),
                "categoria": v.get("categoria_nome"),
            }
            ev_id = v.get("evento_financeiro_id")
            fin = financeiro_map.get(ev_id) if ev_id else None
            if fin:
                item["financeiro"] = {
                    "descricao": fin.get("description"),
                    "valor": fin.get("value"),
                    "vencimento": fin.get("due_date"),
                    "data_pagamento": fin.get("payment_date"),
                    "status": fin.get("status"),
                    "categoria_financeira": fin.get("category_name"),
                    "conta_bancaria": fin.get("bank_account_name"),
                }
            else:
                item["financeiro"] = None
            resultado.append(item)

        return resultado


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------

def _generate_embedding(text: str) -> list[float]:
    resp = openai_client.embeddings.create(
        model="text-embedding-3-small",
        input=text[:8000],
    )
    return resp.data[0].embedding


# ---------------------------------------------------------------------------
# E-mail
# ---------------------------------------------------------------------------

def send_email(subject: str, html_body: str, max_retries: int = 3):
    """Envia e-mail HTML via SMTP com retry e backoff exponencial."""
    if not all([EMAIL_USER, EMAIL_PASSWORD, EMAIL_DEST]):
        log.warning("Configurações de e-mail ausentes. Configure EMAIL_USER, EMAIL_PASSWORD e EMAIL_DEST no .env")
        print("\n" + "=" * 60)
        print(subject)
        print("=" * 60)
        print(html_body)
        return

    # Suporta múltiplos destinatários separados por vírgula ou ponto-e-vírgula
    destinatarios = [e.strip() for e in EMAIL_DEST.replace(";", ",").split(",") if e.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_USER
    msg["To"]      = ", ".join(destinatarios)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    for attempt in range(max_retries):
        try:
            with smtplib.SMTP(EMAIL_SMTP_HOST, EMAIL_SMTP_PORT) as server:
                server.ehlo()
                server.starttls()
                server.login(EMAIL_USER, EMAIL_PASSWORD)
                server.sendmail(EMAIL_USER, destinatarios, msg.as_string())
            log.info(f"✓ E-mail enviado: {subject}")
            return
        except smtplib.SMTPAuthenticationError:
            log.error("Falha de autenticação SMTP — verifique EMAIL_USER e EMAIL_PASSWORD no .env")
            return
        except smtplib.SMTPException as e:
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                log.warning(f"Erro SMTP, tentando novamente em {wait}s ({attempt + 1}/{max_retries}): {e}")
                time.sleep(wait)
            else:
                log.error(f"Falha ao enviar e-mail após {max_retries} tentativas: {e}")
        except Exception as e:
            log.error(f"Erro inesperado ao enviar e-mail: {e}")
            return


def _html_wrap(title: str, body: str) -> str:
    return f"""
    <html><body style="font-family: Arial, sans-serif; max-width: 700px; margin: auto; padding: 24px;">
    <h2 style="color: #2c3e50;">{title}</h2>
    {body}
    <hr style="margin-top: 40px;">
    <p style="color: #aaa; font-size: 12px;">Gerado automaticamente em {datetime.now().strftime('%d/%m/%Y %H:%M')}</p>
    </body></html>
    """


# ---------------------------------------------------------------------------
# Detecção de Anomalias
# ---------------------------------------------------------------------------

def detect_anomalies(db: FinancialDB) -> list[dict]:
    """
    Detecta anomalias usando Z-score por fornecedor/categoria.
    Lógica:
      - Agrupa despesas recorrentes por payee_name
      - Calcula média e desvio padrão histórico
      - Flagga valores que se desviam mais de 2 desvios padrão
      - Também detecta despesas novas sem histórico acima de threshold
    """
    transactions = db.get_recurring_expenses(months=12)
    if not transactions:
        return []

    # Agrupa por fornecedor
    from collections import defaultdict
    grupos: dict[str, list[float]] = defaultdict(list)
    ultimos: dict[str, dict] = {}

    for t in transactions:
        key = t.get("payee_name") or t.get("category_name") or "Sem fornecedor"
        val = t.get("value") or 0
        grupos[key].append(float(val))
        ultimos[key] = t  # guarda o mais recente

    anomalias = []

    for fornecedor, valores in grupos.items():
        if len(valores) < 3:
            # Sem histórico suficiente — verifica só se valor é alto (>R$5.000)
            ultimo_val = valores[-1]
            if ultimo_val > CONFIG["anomaly_new_value_threshold"]:
                anomalias.append({
                    "tipo": "novo_alto_valor",
                    "fornecedor": fornecedor,
                    "valor_atual": ultimo_val,
                    "media_historica": None,
                    "desvio": None,
                    "zscore": None,
                    "detalhe": ultimos[fornecedor],
                    "descricao": f"Despesa sem histórico suficiente com valor alto: R$ {ultimo_val:,.2f}",
                })
            continue

        arr      = np.array(valores[:-1])  # histórico (sem o mais recente)
        media    = arr.mean()
        desvio   = arr.std()
        ultimo   = valores[-1]

        if desvio == 0:
            continue  # valor sempre constante, sem variação

        zscore = (ultimo - media) / desvio

        if abs(zscore) >= CONFIG["anomaly_zscore_threshold"]:
            direcao = "acima" if zscore > 0 else "abaixo"
            anomalias.append({
                "tipo": "zscore",
                "fornecedor": fornecedor,
                "valor_atual": ultimo,
                "media_historica": round(media, 2),
                "desvio": round(desvio, 2),
                "zscore": round(zscore, 2),
                "detalhe": ultimos[fornecedor],
                "descricao": (
                    f"Valor {direcao} do esperado: R$ {ultimo:,.2f} "
                    f"(média R$ {media:,.2f}, z-score {zscore:.1f})"
                ),
            })

    return anomalias


def run_anomalias(send_mail: bool = True):
    """Detecta anomalias e envia e-mail de alerta."""
    log.info("Executando detecção de anomalias...")
    db = FinancialDB()

    # ── Fase 1: coleta todos os dados ───────────────────────────────────────
    anomalias = detect_anomalies(db)

    if not anomalias:
        log.info("Nenhuma anomalia detectada.")
        return

    log.info(f"✓ {len(anomalias)} anomalia(s) detectada(s) — montando relatório...")

    # ── Fase 2: output apenas após tudo processado ──────────────────────────
    titulo = f"⚠️ Alerta Financeiro — {len(anomalias)} anomalia(s) — {date.today().strftime('%d/%m/%Y')}"

    if send_mail:
        linhas = ""
        for a in anomalias:
            cor = "#e74c3c" if a["tipo"] == "zscore" and a["zscore"] > 0 else "#e67e22"
            linhas += f"""
            <div style="border-left: 4px solid {cor}; padding: 12px 16px;
                        margin-bottom: 16px; background: #fdfafa; border-radius: 4px;">
                <strong style="color: {cor};">{a['fornecedor']}</strong><br>
                {a['descricao']}<br>
                <small style="color: #777;">
                    Categoria: {a['detalhe'].get('category_name', '—')} |
                    Vencimento: {a['detalhe'].get('due_date', '—')} |
                    Status: {a['detalhe'].get('status', '—')}
                </small>
            </div>
            """
        html = _html_wrap(
            titulo,
            f"<p>As seguintes despesas apresentaram comportamento fora do padrão histórico:</p>{linhas}"
            f"<p style='color:#777; font-size:13px;'>Método: Z-score (desvio ≥ 2σ da média histórica por fornecedor)</p>"
        )
        send_email(titulo, html)
    else:
        sep = "=" * 60
        print(f"\n{sep}")
        print(titulo)
        print(sep)
        for a in anomalias:
            print(f"\n  • {a['fornecedor']}")
            print(f"    {a['descricao']}")
            print(f"    Categoria: {a['detalhe'].get('category_name','—')} | "
                  f"Vencimento: {a['detalhe'].get('due_date','—')} | "
                  f"Status: {a['detalhe'].get('status','—')}")
        print(f"\n{sep}\n")


# ---------------------------------------------------------------------------
# Insights automáticos via GPT-4o
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_INSIGHTS = """Você é um analista financeiro especializado em PMEs brasileiras.
Analisa dados reais de receitas e despesas e fornece insights práticos, diretos e acionáveis.
Responda sempre em português do Brasil.
Seja objetivo: foque nos pontos mais relevantes, não repita dados óbvios.
Use R$ para valores monetários e formate números com separadores de milhar."""

SYSTEM_PROMPT_CHAT = """Você é um assistente financeiro especializado em PMEs brasileiras.
Responde perguntas sobre receitas, despesas e saúde financeira da empresa com base nos dados fornecidos.
Seja direto, prático e use linguagem acessível.
Sempre responda em português do Brasil.
Quando não tiver dados suficientes para responder algo, diga claramente.
Use R$ para valores monetários."""


def _build_system_prompt_tools() -> str:
    """Gera system prompt dinâmico com data atual para o chat com function calling."""
    hoje = date.today()
    return f"""Você é um assistente financeiro especializado em PMEs brasileiras.
Você tem acesso a ferramentas para consultar dados financeiros em tempo real da empresa.

REGRA ABSOLUTA — ZERO CÁLCULOS PELO MODELO:
Você NUNCA deve realizar qualquer operação aritmética (soma, subtração, multiplicação, divisão, porcentagem, média). Todo e qualquer número que aparecer na sua resposta deve vir diretamente dos campos retornados pelas ferramentas. Se um valor não foi retornado por uma ferramenta, não o calcule — informe que o dado não está disponível ou chame a ferramenta adequada para obtê-lo.

REGRAS IMPORTANTES:
- Sempre que o usuário perguntar sobre dados financeiros, USE AS FERRAMENTAS para buscar dados atualizados. Não invente números.
- Hoje é {hoje.isoformat()}. O mês atual é {hoje.month}, ano {hoje.year}.
- REGRA DE PERÍODO PADRÃO: Quando o usuário NÃO mencionar período, mês, data ou intervalo de tempo na pergunta, SEMPRE assuma o MÊS ATUAL (ano={hoje.year}, mes={hoje.month}). Exemplos: "qual o lucro?" → mês atual. "tem recebimento em atraso?" → dados atuais. "qual a margem?" → mês atual.
- Quando o usuário mencionar "este mês" ou "mês atual", use ano={hoje.year} e mes={hoje.month}.
- Quando o usuário fizer uma pergunta de acompanhamento (follow-up), use as ferramentas novamente se precisar de dados adicionais.
- Responda sempre em português do Brasil.
- Use R$ para valores monetários e formate com separadores de milhar.
- Seja direto, prático e use linguagem acessível.
- REGRA FUNDAMENTAL: Atenda EXATAMENTE o que o usuário pedir. Se pediu "TODOS", mostre TODOS os dados sem resumir ou limitar. Se pediu "os principais" ou "os maiores", aí sim filtre. NUNCA resuma, corte ou limite dados por conta própria — o usuário decide o nível de detalhe.
- TOTAIS E SUBTOTAIS: Use sempre os campos `resumo.*` retornados pelas ferramentas. Nunca some os itens de uma lista para obter um total — o campo `resumo.total_*` já está calculado e é o valor correto.

REGRA DE CLAREZA — SEMPRE QUESTIONE ANTES DE RESPONDER quando a solicitação tiver ambiguidade relevante:
Antes de buscar dados, avalie se a pergunta do usuário tem algum ponto que pode mudar significativamente o resultado. Se sim, faça UMA pergunta objetiva e curta para esclarecer. Só então busque os dados e responda.

Situações que EXIGEM questionamento antes de responder:
1. **Período ambíguo com escopo amplo** — "lucro do ano", "receita do ano", "despesas de 2026", "balanço anual": questione se quer (a) apenas o realizado até hoje ou (b) realizado + previsto até o final do ano.
2. **"Todos os meses"** ou comparativo temporal sem especificar se inclui projeções: questione o horizonte desejado.
3. **Realizado vs. previsto não especificado**: quando o usuário pede resultado de um período futuro ou parcialmente futuro, questione se quer só o confirmado ou também o projetado.
4. **Tipo de dado ambíguo**: se a pergunta mistura possíveis interpretações de "financeiro" (receitas/despesas) com "vendas" (faturamento), questione qual dimensão interessa.
5. **Escopo de cliente/fornecedor ambíguo**: se há mais de uma possível interpretação para o filtro solicitado.

Como questionar — modelo a seguir:
- Seja breve e objetivo: uma única pergunta com 2 ou 3 opções claras.
- Exemplo: "Quer o lucro do ano inteiro de 2026? Se sim, me confirme: **(a)** apenas o realizado até hoje (março) ou **(b)** realizado + previsto até dezembro?"
- Não faça múltiplas perguntas ao mesmo tempo.
- Se a pergunta for clara e sem ambiguidade relevante, responda direto sem questionar.

Situações que NÃO precisam de questionamento (responda direto):
- Perguntas sobre o mês atual sem escopo amplo ("qual o lucro deste mês?", "tem inadimplência?")
- Follow-ups que já têm contexto suficiente da conversa
- Pedidos de listagem simples ("mostre as vendas", "quais clientes em atraso?")

REGRAS DE FORMATAÇÃO (MUITO IMPORTANTE — a interface renderiza Markdown):
- SEMPRE use formatação Markdown para tornar as respostas legíveis e profissionais.
- **Negrito** para valores importantes, nomes de clientes, totais e destaques.
- Use tabelas Markdown quando apresentar listas de dados (transações, vendas, vencimentos, clientes):
  | Coluna 1 | Coluna 2 | Coluna 3 |
  |----------|----------|----------|
  | dado     | dado     | dado     |
- Formate datas no padrão brasileiro: **DD/MM/AAAA** (nunca exiba YYYY-MM-DD).
- Formate valores monetários: **R$ 1.234,56** (com separador de milhar e vírgula decimal).
- Use listas com marcadores (- ou *) para resumos e tópicos.
- Use ### para títulos de seções quando a resposta tiver múltiplas partes.
- Use `código` para destacar números de venda, IDs ou códigos.
- Ao apresentar resumos financeiros, destaque o resultado principal em negrito.
- Use --- (linha horizontal) para separar seções diferentes.
- NUNCA use formato pipe-separated sem formatação de tabela Markdown.
- Exemplo de resposta bem formatada:
  ### Resumo Financeiro — Março/2026
  | Tipo | Realizado | Previsto |
  |------|-----------|----------|
  | Receita | **R$ 50.000,00** | R$ 65.000,00 |
  | Despesa | **R$ 30.000,00** | R$ 40.000,00 |
  | **Lucro** | **R$ 20.000,00** | R$ 25.000,00 |

REGRA DE DATAS PARA CLASSIFICAÇÃO DE TRANSAÇÕES:
Para determinar em qual mês/período uma transação é contabilizada, use esta regra:
- Se a transação possui **data de pagamento** (payment_date preenchida) → classifica pelo mês da data de pagamento.
- Se NÃO possui data de pagamento → classifica pelo mês da **data de vencimento** (due_date).
Isso vale tanto para receitas quanto para despesas. Nunca use apenas due_date para transações já pagas/recebidas — use sempre payment_date quando disponível.
Exemplo: uma despesa com vencimento em 28/02 mas paga em 05/03 → pertence a março, não fevereiro.

IMPORTANTE — DISTINÇÃO ENTRE DADOS FINANCEIROS E VENDAS:
A empresa possui DOIS conjuntos de dados diferentes:
- FINANCEIRO (receitas e despesas): transações de contas a pagar/receber, pagamentos, cobranças → use ferramentas financeiras
- VENDAS (pedidos e faturamento): vendas emitidas no ERP, com número da venda, cliente, categoria, situação (FATURADO, EM_ANDAMENTO, etc.) → use ferramentas de vendas
As vendas podem estar vinculadas a receitas financeiras via evento_financeiro_id. Use get_venda_com_financeiro quando precisar cruzar os dois.

SCHEMA DO BANCO DE DADOS (use para construir queries SQL com execute_sql):

VIEW: financial_transactions_latest  — transações financeiras (contas a pagar/receber)
  installment_id   TEXT        — ID único da parcela (chave de JOIN com sales)
  tipo             TEXT        — 'receita' (entrada) ou 'despesa' (saída)
  description      TEXT        — descrição da transação
  value            NUMERIC     — valor em R$
  status           TEXT        — 'ACQUITTED' = pago/recebido; QUALQUER OUTRO VALOR = pendente (ex: 'PENDING', 'OVERDUE')
  category_name    TEXT        — categoria (ex: 'Folha de Pagamento', 'Serviços Prestados')
  payee_name       TEXT        — nome do cliente (receita) ou fornecedor (despesa)
  due_date         DATE        — data de vencimento
  payment_date     DATE        — data de pagamento real (NULL se ainda não pago/recebido)
  bank_account_name TEXT       — conta bancária

REGRA DE DATA DE REFERÊNCIA EM SQL:
Para classificar uma transação em um período (mês/ano), use sempre:
  COALESCE(payment_date, due_date)  — equivale ao _data_ref() do Python
Isso garante: se foi pago/recebido, usa payment_date; se ainda pendente, usa due_date.
Para registros vencidos (não pagos e já passaram da data): status != 'ACQUITTED' AND due_date < CURRENT_DATE

VIEW: sales_transactions_latest  — vendas emitidas no ERP
  sale_id          TEXT        — ID único da venda
  numero           INTEGER     — número da venda (ex: 2142)
  data_venda       DATE        — data de emissão da venda
  total            NUMERIC     — valor total da venda
  tipo_venda       TEXT        — tipo: 'PRODUTO' ou 'SERVICO'
  situacao         TEXT        — 'FATURADO' (concluída), 'EM_ANDAMENTO' (em execução), 'CANCELADO' (cancelada)
  cliente_nome     TEXT        — nome do cliente
  cliente_email    TEXT        — e-mail do cliente
  categoria_nome   TEXT        — categoria da venda
  evento_financeiro_id TEXT    — FK para financial_transactions_latest.installment_id (pode ser NULL)

RELACIONAMENTO ENTRE AS VIEWS:
  sales_transactions_latest s
    JOIN financial_transactions_latest f ON f.installment_id = s.evento_financeiro_id

EXEMPLOS DE QUERIES ÚTEIS:
-- Receitas do mês com venda vinculada (usa COALESCE para data de referência):
SELECT f.payee_name, f.value, f.due_date, f.payment_date, f.status,
       s.numero AS numero_venda, s.cliente_nome, s.total AS total_venda
FROM financial_transactions_latest f
LEFT JOIN sales_transactions_latest s ON s.evento_financeiro_id = f.installment_id
WHERE f.tipo = 'receita'
  AND (COALESCE(f.payment_date, f.due_date) BETWEEN '2026-03-01' AND '2026-03-31')
ORDER BY f.due_date;

-- Despesas pendentes do mês agrupadas por categoria:
SELECT category_name, COUNT(*) AS qtd, SUM(value) AS total
FROM financial_transactions_latest
WHERE tipo = 'despesa' AND status != 'ACQUITTED'
  AND due_date BETWEEN '2026-03-01' AND '2026-03-31'
GROUP BY category_name ORDER BY total DESC;

-- Receitas vencidas (não recebidas e já passaram da data):
SELECT payee_name, description, value, due_date,
       (CURRENT_DATE - due_date) AS dias_atraso
FROM financial_transactions_latest
WHERE tipo = 'receita' AND status != 'ACQUITTED'
  AND due_date < CURRENT_DATE
ORDER BY due_date;

-- Vendas com receita ainda não recebida (cross-table):
SELECT s.numero, s.cliente_nome, s.total AS total_venda,
       f.value AS valor_financeiro, f.due_date, f.status
FROM sales_transactions_latest s
JOIN financial_transactions_latest f ON f.installment_id = s.evento_financeiro_id
WHERE s.situacao = 'FATURADO' AND f.status != 'ACQUITTED'
  AND f.tipo = 'receita'
ORDER BY f.due_date;

FERRAMENTAS FINANCEIRAS:
1. get_resumo_mes: Resumo financeiro de UM mês específico (receita, despesa, lucro, margem — realizados e projetados)
2. get_resumo_ano: Resumo financeiro do ANO INTEIRO — retorna os 12 meses + totais anuais calculados com precisão. Use SEMPRE para perguntas sobre o ano, visão anual, balanço anual, lucro do ano. NUNCA chame get_resumo_mes 12 vezes — os totais seriam calculados pelo modelo e estariam errados.
3. get_inadimplencia: Inadimplência (receitas/despesas vencidas, aging, top devedores com nomes)
4. get_posicao_caixa: Posição de caixa (realizado, a realizar, comparativo mês anterior)
5. get_kpis: KPIs (margem, prazo médio recebimento/pagamento, taxa inadimplência, burn rate, runway)
6. get_concentracao_risco: Concentração de risco (maiores pendências, concentração por categoria)
7. get_fluxo_semana: Fluxo de caixa próximos 7 dias
8. search_transactions: Busca semântica por transações financeiras (fornecedor, cliente, descrição)
9. get_latest_transactions: Transações com due_date nos últimos N dias até HOJE — nunca retorna parcelas de datas futuras
10. get_vencimentos_periodo: Pendentes que vencem nos PRÓXIMOS N dias a partir de hoje (curto prazo, ex: próximos 7 dias)
11. get_pendentes_mes: TODOS os pendentes (não quitados) com due_date dentro de um mês específico. Use para "o que tenho a pagar/receber no mês X", "pendentes de março", "contas do mês"

FERRAMENTAS DE VENDAS:
12. get_vendas: Lista vendas recentes (número, cliente, total, situação, categoria)
13. get_resumo_vendas_mes: Resumo de vendas do mês (total faturado, por situação, por categoria, TODOS os clientes com valores)
14. get_venda_com_financeiro: Vendas com dados financeiros vinculados (venda + receita correspondente, status de pagamento)

QUANDO USAR execute_sql vs ferramentas específicas:
- Use as ferramentas específicas (get_resumo_mes, get_inadimplencia, etc.) para os casos que elas cobrem — são mais rápidas e retornam dados já agregados.
- Use execute_sql quando: a consulta cruza tabelas, tem filtros customizados, o usuário quer combinação de dados que nenhum get_* cobre, ou quando a ferramenta específica não retorna exatamente o que foi pedido.
- Exemplos que REQUEREM execute_sql:
  * "receitas do mês com o número da fatura (venda) correspondente" → JOIN entre as views
  * "quais clientes tiveram venda mas a receita ainda não foi recebida?" → JOIN + filtro
  * "despesas por fornecedor nos últimos 6 meses" → GROUP BY + date range
  * "todas as parcelas do fornecedor X" → filter específico por payee_name
  * Qualquer consulta que combine receitas/despesas com dados de vendas

DICAS DE USO:
- "Como estão as finanças?" → get_resumo_mes + get_posicao_caixa
- "Lucro do ano" → get_resumo_ano
- "O que tenho pendente para pagar/receber este mês?" → get_pendentes_mes
- "O que vence nos próximos X dias?" → get_vencimentos_periodo(dias=X)
- "Receitas do mês com fatura (venda) correspondente" → execute_sql com JOIN
- "Quais clientes inadimplentes / com pagamento atrasado?" → get_inadimplencia
- "Vendas faturadas com receita ainda pendente" → execute_sql com JOIN (exemplo acima)
- "Receitas por cliente nos últimos 6 meses" → execute_sql com GROUP BY payee_name + date range
- "Todas as parcelas do fornecedor X" → execute_sql com WHERE payee_name ILIKE '%X%'
- Para follow-ups, use as ferramentas novamente com os termos relevantes.
- Você pode chamar múltiplas ferramentas em uma única resposta se precisar de dados complementares."""


# ---------------------------------------------------------------------------
# Function Calling — definições de tools para GPT-4o
# ---------------------------------------------------------------------------

CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_resumo_mes",
            "description": "Retorna o resumo financeiro do mês: receita e despesa (realizadas e previstas), lucro, margem. Use quando o usuário perguntar sobre resultados do mês, receita, despesa, lucro ou margem.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ano": {"type": "integer", "description": "Ano (ex: 2026)"},
                    "mes": {"type": "integer", "description": "Mês (1-12)"},
                },
                "required": ["ano", "mes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_inadimplencia",
            "description": "Retorna dados de inadimplência: receitas e despesas vencidas (total e quantidade), aging buckets (1-15d, 16-30d, 31-60d, 60+d), e top 5 devedores com nome, valor total e dias médios de atraso. Use para perguntas sobre pagamentos atrasados, vencidos, devedores, clientes inadimplentes.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_posicao_caixa",
            "description": "Retorna a posição de caixa do mês: receita/despesa realizadas, a realizar, e comparativo com mês anterior. Use para perguntas sobre caixa, saldo, fluxo do mês, comparação com mês passado.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ano": {"type": "integer", "description": "Ano (ex: 2026)"},
                    "mes": {"type": "integer", "description": "Mês (1-12)"},
                },
                "required": ["ano", "mes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_kpis",
            "description": "Retorna KPIs financeiros: margem operacional, prazo médio de recebimento/pagamento, taxa de inadimplência, burn rate diário e runway. Use para perguntas sobre indicadores, métricas, burn rate, margem, prazo médio.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ano": {"type": "integer", "description": "Ano (ex: 2026)"},
                    "mes": {"type": "integer", "description": "Mês (1-12)"},
                },
                "required": ["ano", "mes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_concentracao_risco",
            "description": "Retorna concentração de risco: top 5 despesas e receitas pendentes (com nome do fornecedor/cliente), e concentração por categoria. Use para perguntas sobre maiores despesas/receitas pendentes, categorias, onde está concentrado o risco.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ano": {"type": "integer", "description": "Ano (ex: 2026)"},
                    "mes": {"type": "integer", "description": "Mês (1-12)"},
                },
                "required": ["ano", "mes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_fluxo_semana",
            "description": "Retorna projeção de fluxo de caixa dos próximos 7 dias: receitas e despesas por dia, itens destaque > R$50k. Use para perguntas sobre próxima semana, vencimentos próximos, fluxo de caixa semanal.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_transactions",
            "description": "Busca semântica por transações específicas. Use para encontrar transações por nome de fornecedor/cliente, descrição, ou qualquer termo específico. Retorna transações com todos os campos: description, value, due_date, status, category_name, payee_name, payment_date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Termo de busca (nome, descrição, etc.)"},
                    "tipo": {"type": "string", "enum": ["receita", "despesa"], "description": "Filtrar por tipo (opcional)"},
                    "top_k": {"type": "integer", "description": "Quantidade máxima de resultados (padrão: 10, máx: 25)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_latest_transactions",
            "description": "Retorna lista de transações recentes com todos os campos (description, value, due_date, status, category_name, payee_name, payment_date, bank_account_name). Use para listar transações de um período, filtrar por tipo, ou quando o usuário quer detalhes das transações individuais.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Dias para trás a partir de hoje (padrão: 90)"},
                    "tipo": {"type": "string", "enum": ["receita", "despesa"], "description": "Filtrar por tipo (opcional)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_vencimentos_periodo",
            "description": "Retorna transações pendentes com vencimento nos próximos N dias. Inclui todos os campos: description, value, due_date, status, payee_name (cliente/fornecedor), category_name. Por padrão retorna apenas pendentes (não quitados). Use para perguntas sobre 'o que vence nos próximos X dias', 'quais clientes têm faturas a vencer', 'contas a pagar essa semana'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "dias": {"type": "integer", "description": "Número de dias à frente a partir de hoje (padrão: 10)"},
                    "tipo": {"type": "string", "enum": ["receita", "despesa"], "description": "Filtrar por tipo (opcional)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_pendentes_mes",
            "description": "Retorna TODOS os registros pendentes (não pagos/recebidos) com vencimento dentro de um mês específico. Use SEMPRE que o usuário perguntar 'o que tenho pendente para pagar/receber no mês X', 'quais despesas/receitas pendentes de março', 'o que tem a pagar este mês'. Filtra exatamente pelo due_date dentro do mês — nunca retorna registros de outros meses ou anos. É a ferramenta correta para pendentes de um mês específico; NÃO use get_latest_transactions para isso.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ano":  {"type": "integer", "description": "Ano (ex: 2026)"},
                    "mes":  {"type": "integer", "description": "Mês (1-12)"},
                    "tipo": {"type": "string", "enum": ["receita", "despesa"], "description": "Filtrar por tipo: 'despesa' para contas a pagar, 'receita' para contas a receber. Omita para trazer ambos."},
                },
                "required": ["ano", "mes"],
            },
        },
    },
    # ── Anual (visão do ano completo) ─────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_resumo_ano",
            "description": "Retorna o resumo financeiro do ano inteiro, mês a mês, com totais calculados com precisão em Python (não pelo modelo). Use SEMPRE que o usuário pedir resultados anuais, visão do ano, lucro do ano, receita anual, balanço anual ou comparativo de todos os meses. Retorna: breakdown mensal (receita realizada, prevista, despesa realizada, prevista, lucro) + totais anuais consolidados. NUNCA chame get_resumo_mes 12 vezes e some manualmente — use este método.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ano": {"type": "integer", "description": "Ano desejado (ex: 2026)"},
                },
                "required": ["ano"],
            },
        },
    },
    # ── SQL dinâmico ─────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "execute_sql",
            "description": (
                "Executa um SELECT SQL dinâmico diretamente no banco de dados. "
                "Use esta ferramenta quando nenhuma das outras ferramentas específicas atende ao pedido — "
                "especialmente para consultas que cruzam tabelas, filtros customizados, agrupamentos específicos ou "
                "qualquer dado que o usuário pedir que não tenha um get_* correspondente. "
                "REGRAS OBRIGATÓRIAS ao escrever o SQL:\n"
                "1. Somente SELECT — nunca INSERT, UPDATE, DELETE, DROP, ALTER.\n"
                "2. Use as views (não as tabelas brutas): financial_transactions_latest e sales_transactions_latest.\n"
                "3. Para cruzar receitas com vendas: "
                "   JOIN sales_transactions_latest s ON s.evento_financeiro_id = f.installment_id\n"
                "4. Datas no formato ISO: '2026-03-01'.\n"
                "5. Status ACQUITTED = pago/recebido; qualquer outro = pendente.\n"
                "6. tipo = 'receita' ou 'despesa' na tabela financial_transactions_latest.\n"
                "7. Sempre inclua LIMIT (máx 500) para não sobrecarregar.\n"
                "8. NUNCA calcule na resposta — use SUM(), COUNT(), AVG() no próprio SQL."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "Query SELECT completa e válida para PostgreSQL.",
                    },
                    "descricao": {
                        "type": "string",
                        "description": "Breve descrição do que esta query busca (para log).",
                    },
                },
                "required": ["sql", "descricao"],
            },
        },
    },
    # ── Vendas (sales_transactions) ──────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_vendas",
            "description": "Retorna lista de vendas recentes com: numero, data_venda, total, situacao (FATURADO, EM_ANDAMENTO, CANCELADO, etc.), cliente_nome, categoria_nome, evento_financeiro_id. Use para perguntas sobre vendas, faturamento, pedidos, clientes de vendas. NÃO confundir com transações financeiras (receitas/despesas).",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Dias para trás a partir de hoje (padrão: 90)"},
                    "situacao": {"type": "string", "description": "Filtrar por situação (ex: FATURADO, EM_ANDAMENTO, CANCELADO)"},
                    "cliente": {"type": "string", "description": "Filtrar por nome do cliente (busca parcial)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_resumo_vendas_mes",
            "description": "Retorna resumo de vendas do mês: total faturado, quantidade, vendas por situação, por categoria, e TODOS os clientes com valores (ordenados por total). Use para perguntas sobre 'quanto vendemos este mês', 'quais clientes faturaram', 'vendas por categoria'. Retorna a lista completa de clientes, não apenas os maiores.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ano": {"type": "integer", "description": "Ano (ex: 2026)"},
                    "mes": {"type": "integer", "description": "Mês (1-12)"},
                },
                "required": ["ano", "mes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_venda_com_financeiro",
            "description": "Busca vendas com seus dados financeiros vinculados (receita correspondente). Retorna dados da venda + vencimento, status de pagamento, conta bancária da receita vinculada. Use quando o usuário perguntar sobre o status financeiro de uma venda específica, se uma venda foi paga, ou detalhes de cobrança de um cliente. Pode filtrar por número da venda ou nome do cliente.",
            "parameters": {
                "type": "object",
                "properties": {
                    "numero_venda": {"type": "integer", "description": "Número da venda (ex: 2142)"},
                    "cliente": {"type": "string", "description": "Nome do cliente (busca parcial)"},
                },
                "required": [],
            },
        },
    },
]


def _agregar_financeiro(registros: list[dict]) -> dict:
    """Pré-computa todos os totais de uma lista de transações financeiras em Python.
    O LLM deve usar apenas estes valores e NUNCA fazer cálculos próprios."""
    from collections import defaultdict

    receitas  = [r for r in registros if r.get("tipo") == "receita"]
    despesas  = [r for r in registros if r.get("tipo") == "despesa"]

    def _val(r): return float(r.get("value") or 0)

    rec_pagas   = [r for r in receitas if r.get("status") == "ACQUITTED"]
    rec_pend    = [r for r in receitas if r.get("status") != "ACQUITTED"]
    desp_pagas  = [r for r in despesas if r.get("status") == "ACQUITTED"]
    desp_pend   = [r for r in despesas if r.get("status") != "ACQUITTED"]

    # Totais por categoria (receita)
    cat_rec: dict[str, float] = defaultdict(float)
    for r in receitas:
        cat_rec[r.get("category_name") or "Sem categoria"] += _val(r)

    # Totais por categoria (despesa)
    cat_desp: dict[str, float] = defaultdict(float)
    for r in despesas:
        cat_desp[r.get("category_name") or "Sem categoria"] += _val(r)

    # Totais por payee (top devedores/credores)
    payee_rec: dict[str, float] = defaultdict(float)
    for r in receitas:
        payee_rec[r.get("payee_name") or "Sem nome"] += _val(r)

    payee_desp: dict[str, float] = defaultdict(float)
    for r in despesas:
        payee_desp[r.get("payee_name") or "Sem nome"] += _val(r)

    total_rec      = round(sum(_val(r) for r in receitas),  2)
    total_desp     = round(sum(_val(r) for r in despesas),  2)
    total_rec_pago = round(sum(_val(r) for r in rec_pagas), 2)
    total_rec_pend = round(sum(_val(r) for r in rec_pend),  2)
    total_desp_pago= round(sum(_val(r) for r in desp_pagas),2)
    total_desp_pend= round(sum(_val(r) for r in desp_pend), 2)
    saldo_liquido  = round(total_rec - total_desp, 2)

    return {
        "resumo": {
            "total_receitas":          total_rec,
            "total_despesas":          total_desp,
            "saldo_liquido":           saldo_liquido,
            "receitas_recebidas":      total_rec_pago,
            "receitas_pendentes":      total_rec_pend,
            "despesas_pagas":          total_desp_pago,
            "despesas_pendentes":      total_desp_pend,
            "qtd_receitas":            len(receitas),
            "qtd_despesas":            len(despesas),
            "qtd_receitas_pendentes":  len(rec_pend),
            "qtd_despesas_pendentes":  len(desp_pend),
            "por_categoria_receita":   sorted(
                [{"categoria": k, "total": round(v, 2)} for k, v in cat_rec.items()],
                key=lambda x: x["total"], reverse=True),
            "por_categoria_despesa":   sorted(
                [{"categoria": k, "total": round(v, 2)} for k, v in cat_desp.items()],
                key=lambda x: x["total"], reverse=True),
            "por_cliente_receita":     sorted(
                [{"nome": k, "total": round(v, 2)} for k, v in payee_rec.items()],
                key=lambda x: x["total"], reverse=True)[:20],
            "por_fornecedor_despesa":  sorted(
                [{"nome": k, "total": round(v, 2)} for k, v in payee_desp.items()],
                key=lambda x: x["total"], reverse=True)[:20],
        },
        "registros": registros,
    }


def _agregar_vendas(vendas: list[dict]) -> dict:
    """Pré-computa todos os totais de uma lista de vendas em Python.
    O LLM deve usar apenas estes valores e NUNCA fazer cálculos próprios."""
    from collections import defaultdict

    def _val(v): return float(v.get("total") or 0)

    por_situacao: dict[str, dict] = defaultdict(lambda: {"total": 0.0, "quantidade": 0})
    por_categoria: dict[str, dict] = defaultdict(lambda: {"total": 0.0, "quantidade": 0})
    por_cliente: dict[str, dict] = defaultdict(lambda: {"total": 0.0, "quantidade": 0})

    for v in vendas:
        sit = v.get("situacao") or "Sem situação"
        por_situacao[sit]["total"]     += _val(v)
        por_situacao[sit]["quantidade"] += 1

        cat = v.get("categoria_nome") or "Sem categoria"
        por_categoria[cat]["total"]     += _val(v)
        por_categoria[cat]["quantidade"] += 1

        cli = v.get("cliente_nome") or "Sem nome"
        por_cliente[cli]["total"]     += _val(v)
        por_cliente[cli]["quantidade"] += 1

    total_geral = round(sum(_val(v) for v in vendas), 2)

    return {
        "resumo": {
            "total_vendas":    total_geral,
            "quantidade":      len(vendas),
            "por_situacao":    {k: {"total": round(v["total"], 2), "quantidade": v["quantidade"]}
                                for k, v in por_situacao.items()},
            "por_categoria":   sorted(
                [{"categoria": k, "total": round(v["total"], 2), "quantidade": v["quantidade"]}
                 for k, v in por_categoria.items()],
                key=lambda x: x["total"], reverse=True),
            "por_cliente":     sorted(
                [{"cliente": k, "total": round(v["total"], 2), "quantidade": v["quantidade"]}
                 for k, v in por_cliente.items()],
                key=lambda x: x["total"], reverse=True),
        },
        "registros": vendas,
    }


def _execute_tool(db, tool_name: str, arguments: dict) -> str:
    """Executa uma tool call e retorna o resultado como JSON string."""
    try:
        if tool_name == "execute_sql":
            log.info(f"🔍 SQL: {arguments.get('descricao', '')} | {arguments['sql'][:120]}...")
            result = db.execute_sql(arguments["sql"])
        elif tool_name == "get_resumo_ano":
            result = db.get_resumo_ano(arguments["ano"])
        elif tool_name == "get_resumo_mes":
            result = db.get_resumo_mes(arguments["ano"], arguments["mes"])
        elif tool_name == "get_inadimplencia":
            result = db.get_inadimplencia()
        elif tool_name == "get_posicao_caixa":
            result = db.get_posicao_caixa(arguments["ano"], arguments["mes"])
        elif tool_name == "get_kpis":
            result = db.get_kpis(arguments["ano"], arguments["mes"])
        elif tool_name == "get_concentracao_risco":
            result = db.get_concentracao_risco(arguments["ano"], arguments["mes"])
        elif tool_name == "get_fluxo_semana":
            result = db.get_fluxo_semana()
        elif tool_name == "search_transactions":
            top_k = min(arguments.get("top_k", 10), 25)
            raw = db.search_semantic(
                arguments["query"],
                tipo=arguments.get("tipo"),
                top_k=top_k,
            )
            result = _agregar_financeiro(raw)
        elif tool_name == "get_latest_transactions":
            days = arguments.get("days", 90)
            raw = db.get_latest_transactions(days=days, tipo=arguments.get("tipo"))
            result = _agregar_financeiro(raw[:100])
        elif tool_name == "get_pendentes_mes":
            raw = db.get_pendentes_mes(
                arguments["ano"], arguments["mes"],
                tipo=arguments.get("tipo"),
            )
            result = _agregar_financeiro(raw)
        elif tool_name == "get_vencimentos_periodo":
            dias = arguments.get("dias", 10)
            raw = db.get_vencimentos_periodo(
                dias=dias, tipo=arguments.get("tipo")
            )
            result = _agregar_financeiro(raw)
        # ── Vendas ──
        elif tool_name == "get_vendas":
            days = arguments.get("days", 90)
            raw = db.get_vendas(
                days=days,
                situacao=arguments.get("situacao"),
                cliente=arguments.get("cliente"),
            )
            result = _agregar_vendas(raw[:100])
        elif tool_name == "get_resumo_vendas_mes":
            result = db.get_resumo_vendas_mes(arguments["ano"], arguments["mes"])
        elif tool_name == "get_venda_com_financeiro":
            raw = db.get_venda_com_financeiro(
                numero_venda=arguments.get("numero_venda"),
                cliente=arguments.get("cliente"),
            )
            result = _agregar_vendas(raw)
        else:
            result = {"error": f"Tool '{tool_name}' não encontrada"}

        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        log.error(f"Erro ao executar tool {tool_name}: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def _summarize_transactions(transactions: list[dict]) -> str:
    """Converte lista de transações em texto resumido para o contexto do LLM."""
    if not transactions:
        return "Nenhuma transação encontrada no período."

    receitas  = [t for t in transactions if t.get("tipo") == "receita"]
    despesas  = [t for t in transactions if t.get("tipo") == "despesa"]

    total_rec = sum(float(t.get("value") or 0) for t in receitas)
    total_des = sum(float(t.get("value") or 0) for t in despesas)

    # Top categorias de despesa
    from collections import defaultdict
    cat_despesa: dict[str, float] = defaultdict(float)
    cat_receita: dict[str, float] = defaultdict(float)
    for t in despesas:
        cat_despesa[t.get("category_name") or "Sem categoria"] += float(t.get("value") or 0)
    for t in receitas:
        cat_receita[t.get("category_name") or "Sem categoria"] += float(t.get("value") or 0)

    top_despesas = sorted(cat_despesa.items(), key=lambda x: x[1], reverse=True)[:8]
    top_receitas = sorted(cat_receita.items(), key=lambda x: x[1], reverse=True)[:8]

    # Pendentes
    pendentes_rec = [t for t in receitas if t.get("status") != "ACQUITTED"]
    pendentes_des = [t for t in despesas if t.get("status") != "ACQUITTED"]
    total_pend_rec = sum(float(t.get("value") or 0) for t in pendentes_rec)
    total_pend_des = sum(float(t.get("value") or 0) for t in pendentes_des)

    linhas = [
        f"=== RESUMO FINANCEIRO (últimos 60 dias) ===",
        f"Período: {date.today() - timedelta(days=60)} até {date.today()}",
        f"",
        f"RECEITAS:",
        f"  Total recebido/a receber: R$ {total_rec:,.2f}",
        f"  Pendentes: R$ {total_pend_rec:,.2f} ({len(pendentes_rec)} registros)",
        f"  Por categoria: " + ", ".join(f"{c}: R$ {v:,.2f}" for c, v in top_receitas),
        f"",
        f"DESPESAS:",
        f"  Total pago/a pagar: R$ {total_des:,.2f}",
        f"  Pendentes: R$ {total_pend_des:,.2f} ({len(pendentes_des)} registros)",
        f"  Por categoria: " + ", ".join(f"{c}: R$ {v:,.2f}" for c, v in top_despesas),
        f"",
        f"RESULTADO: R$ {total_rec - total_des:,.2f}",
        f"",
        f"ÚLTIMAS TRANSAÇÕES (máx 50):",
    ]

    for t in transactions[:50]:
        linhas.append(
            f"  [{t.get('tipo','').upper()}] {t.get('description','')} | "
            f"R$ {float(t.get('value') or 0):,.2f} | "
            f"Venc: {t.get('due_date','')} | "
            f"Status: {t.get('status','')} | "
            f"Cat: {t.get('category_name','')} | "
            f"Fornecedor/Cliente: {t.get('payee_name','')}"
        )

    return "\n".join(linhas)


def generate_daily_insights(db: FinancialDB, semanal: bool = False) -> str:
    """Gera insights via GPT-4o com base nos dados recentes."""
    days   = 7 if not semanal else 30
    label  = "diário" if not semanal else "semanal"
    transactions = db.get_transactions_for_context(days=days)
    context = _summarize_transactions(transactions)

    if semanal:
        prompt = f"""Com base nos dados financeiros abaixo, faça um resumo semanal executivo com:

1. **Resultado da semana**: receitas x despesas, saldo
2. **Destaques positivos**: o que foi bem
3. **Pontos de atenção**: despesas relevantes, inadimplências, atrasos
4. **Top 3 categorias de despesa** da semana
5. **Recomendações**: 2-3 ações concretas para a próxima semana

Dados:
{context}"""
    else:
        prompt = f"""Com base nos dados financeiros de hoje, gere um briefing diário com:

1. **Situação do dia**: pagamentos realizados, recebimentos confirmados
2. **Vencimentos próximos** (próximos 7 dias): o que vence e quanto
3. **Alertas**: contas em atraso, valores pendentes relevantes
4. **Indicador rápido**: receitas vs despesas do mês até agora

Dados:
{context}"""

    response = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT_INSIGHTS},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        max_tokens=1500,
    )
    return response.choices[0].message.content


def run_insights(semanal: bool = False, send_mail: bool = True):
    """Gera insights e envia por e-mail."""
    label = "Semanal" if semanal else "Diário"
    log.info(f"Gerando insights {label.lower()}...")

    # ── Fase 1: coleta dados e gera análise ─────────────────────────────────
    db       = FinancialDB()
    insights = generate_daily_insights(db, semanal=semanal)
    log.info("✓ Análise concluída — montando relatório...")

    # ── Fase 2: output apenas após tudo processado ──────────────────────────
    emoji  = "📊" if semanal else "📈"
    titulo = f"{emoji} Insights Financeiros {label} — {date.today().strftime('%d/%m/%Y')}"

    if send_mail:
        import re
        html_body = "<div style='line-height: 1.7; color: #2c3e50;'>"
        for linha in insights.split("\n"):
            if linha.startswith("## "):
                html_body += f"<h3>{linha[3:]}</h3>"
            elif linha.startswith("**") and linha.endswith("**"):
                html_body += f"<strong>{linha[2:-2]}</strong><br>"
            elif linha.strip().startswith("- ") or linha.strip().startswith("* "):
                html_body += f"<li>{linha.strip()[2:]}</li>"
            elif linha.strip() == "":
                html_body += "<br>"
            else:
                linha = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', linha)
                html_body += f"<p style='margin: 4px 0;'>{linha}</p>"
        html_body += "</div>"
        html = _html_wrap(titulo, html_body)
        send_email(titulo, html)
    else:
        sep = "=" * 60
        print(f"\n{sep}")
        print(titulo)
        print(sep)
        print(insights)
        print(f"\n{sep}\n")


# ---------------------------------------------------------------------------
# Auditoria de mudanças entre extrações
# ---------------------------------------------------------------------------

MESES_PT = {
    1: "Janeiro", 2: "Fevereiro", 3: "Março",    4: "Abril",
    5: "Maio",    6: "Junho",     7: "Julho",     8: "Agosto",
    9: "Setembro",10: "Outubro", 11: "Novembro", 12: "Dezembro",
}


def _resumo_auditoria_llm(dados_mes: dict) -> str:
    """
    Passa o resumo das mudanças para o GPT-4o e pede uma sumarização
    executiva do que aconteceu no mês.
    """
    alterados = dados_mes["alterados"]
    novos     = dados_mes["novos"]
    removidos = dados_mes["removidos"]
    mes_label = f"{MESES_PT[dados_mes['mes']]}/{dados_mes['ano']}"

    if not alterados and not novos and not removidos:
        return "Nenhuma mudança detectada neste mês."

    # Monta contexto compacto para o LLM
    linhas = [f"AUDITORIA DE MUDANÇAS — {mes_label}\n"]

    if alterados:
        linhas.append(f"REGISTROS ALTERADOS ({len(alterados)}):")
        for a in alterados[:30]:
            mudancas_str = " | ".join(
                (f"{m['label']}: {m['anterior']} → {m['atual']} ({m['variacao_pct']:+.1f}%)"
                 if m["campo"] == "valor" and m.get("variacao_pct") is not None
                 else f"{m['label']}: {m['anterior']} → {m['atual']}")
                for m in a["mudancas"]
            )
            linhas.append(
                f"  [{a['tipo'].upper()}] {a['description']} ({a['payee_name']}) | "
                f"Venc: {a['due_date']} | Mudanças: {mudancas_str}"
            )

    if novos:
        linhas.append(f"\nREGISTROS NOVOS ({len(novos)}):")
        for n in novos[:20]:
            linhas.append(
                f"  [{n['tipo'].upper()}] {n['description']} ({n['payee_name']}) | "
                f"R$ {float(n['value'] or 0):,.2f} | Status: {n['status']} | Venc: {n['due_date']}"
            )

    if removidos:
        linhas.append(f"\nREGISTROS REMOVIDOS ({len(removidos)}):")
        for r in removidos[:20]:
            linhas.append(
                f"  [{r['tipo'].upper()}] {r['description']} ({r['payee_name']}) | "
                f"R$ {float(r['value'] or 0):,.2f} | Última vez visto: {r['ultima_extracao_vista']}"
            )

    contexto = "\n".join(linhas)

    response = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT_INSIGHTS},
            {"role": "user", "content": f"""Com base nas mudanças financeiras abaixo, faça uma sumarização executiva de {mes_label} com:

1. **O que mudou**: principais alterações de valor e status, impacto financeiro total
2. **O que entrou**: novos registros relevantes incluídos
3. **O que saiu**: registros que desapareceram e possível impacto
4. **Conclusão**: em 2 linhas, o que o gestor precisa saber sobre este mês

Seja direto e foque no que tem impacto financeiro real.

{contexto}"""},
        ],
        temperature=0.3,
        max_tokens=600,
    )
    return response.choices[0].message.content


def _formatar_mes_terminal(dados_mes: dict, resumo_llm: str) -> str:
    """Formata o resultado de um mês para exibição no terminal."""
    import re
    mes_label = f"{MESES_PT[dados_mes['mes']]}/{dados_mes['ano']}"
    alterados = dados_mes["alterados"]
    novos     = dados_mes["novos"]
    removidos = dados_mes["removidos"]

    linhas = []
    sep_grosso = "═" * 60
    sep_fino   = "─" * 58

    linhas.append(f"\n{sep_grosso}")
    linhas.append(f"  {mes_label}")
    linhas.append(f"  Alterados: {len(alterados)} | Novos: {len(novos)} | Removidos: {len(removidos)}")
    linhas.append(sep_grosso)

    # ── Alterados ────────────────────────────────────────────────
    if alterados:
        linhas.append(f"\n  📝 ALTERADOS ({len(alterados)})")
        linhas.append(f"  {sep_fino}")
        for a in alterados:
            pago = f" | Pago: {a.get('payment_date')}" if a.get('payment_date') else ""
            linhas.append(f"\n  [{a['tipo'].upper()}] {a['description']}")
            linhas.append(f"  Fornecedor: {a['payee_name'] or '—'} | Categoria: {a['category_name'] or '—'} | Venc: {a['due_date']}{pago}")
            linhas.append(f"  Versões: {a['qtd_versoes']} | Base: {a['data_base']} → Atual: {a['data_atual']}")
            linhas.append(f"  O que mudou:")
            for m in a["mudancas"]:
                if m["campo"] == "valor":
                    aumentou  = m["atual_raw"] > m["anterior_raw"]
                    delta     = m["atual_raw"] - m["anterior_raw"]
                    sinal     = "▲" if aumentou else "▼"
                    pct       = f" ({m['variacao_pct']:+.1f}%)" if m.get("variacao_pct") is not None else ""
                    delta_str = f"+R$ {abs(delta):,.2f}" if aumentou else f"-R$ {abs(delta):,.2f}"
                    linhas.append(f"    {sinal} {m['label']}: {m['anterior']} → {m['atual']}{pct} [{delta_str}]")
                else:
                    linhas.append(f"    ↔ {m['label']}: {m['anterior']} → {m['atual']}")
            # Histórico de versões
            if a.get("historico"):
                linhas.append(f"  Histórico completo:")
                for h in a["historico"]:
                    tag  = " [carga inicial]" if h.get("is_initial_load") else ""
                    pago = f" | Pago: {h['payment_date']}" if h.get("payment_date") else ""
                    linhas.append(
                        f"    {h['extraction_date']}{tag} → "
                        f"R$ {float(h['value'] or 0):,.2f} | {h['status']}{pago}"
                    )

    # ── Novos ────────────────────────────────────────────────────
    if novos:
        linhas.append(f"\n  ✅ NOVOS ({len(novos)})")
        linhas.append(f"  {sep_fino}")
        for n in novos:
            linhas.append(
                f"  [{n['tipo'].upper()}] {n['description']} | "
                f"R$ {float(n['value'] or 0):,.2f} | {n['status']} | Venc: {n['due_date']}"
            )

    # ── Removidos ────────────────────────────────────────────────
    if removidos:
        linhas.append(f"\n  ❌ REMOVIDOS ({len(removidos)})")
        linhas.append(f"  {sep_fino}")
        for r in removidos:
            linhas.append(
                f"  [{r['tipo'].upper()}] {r['description']} | "
                f"R$ {float(r['value'] or 0):,.2f} | Visto em: {r.get('snapshot_inicial','—')} → sumiu em: {r.get('ultimo_snapshot_visto','—')}"
            )

    # ── Resumo LLM ───────────────────────────────────────────────
    linhas.append(f"\n  💡 RESUMO EXECUTIVO")
    linhas.append(f"  {sep_fino}")
    for linha in resumo_llm.strip().split("\n"):
        # Remove markdown bold para terminal
        linha_limpa = re.sub(r'\*\*(.*?)\*\*', r'\1', linha)
        linhas.append(f"  {linha_limpa}")

    return "\n".join(linhas)


def _formatar_mes_html(dados_mes: dict, resumo_llm: str) -> str:
    """Formata o resultado de um mês para e-mail HTML."""
    import re
    mes_label = f"{MESES_PT[dados_mes['mes']]}/{dados_mes['ano']}"
    alterados = dados_mes["alterados"]
    novos     = dados_mes["novos"]
    removidos = dados_mes["removidos"]

    html = f"""
    <div style="border: 1px solid #ddd; border-radius: 8px; margin-bottom: 32px;
                overflow: hidden; background: white;">

      <!-- Cabeçalho do mês -->
      <div style="background: #1a1a2e; color: white; padding: 14px 20px;
                  display: flex; justify-content: space-between; align-items: center;">
        <strong style="font-size: 16px;">{mes_label}</strong>
        <span style="font-size: 13px; opacity: 0.8;">
          📝 {len(alterados)} alterado(s) &nbsp;|&nbsp;
          ✅ {len(novos)} novo(s) &nbsp;|&nbsp;
          ❌ {len(removidos)} removido(s)
        </span>
      </div>

      <div style="padding: 16px 20px;">
    """

    def _fmt_data(d):
        """Converte YYYY-MM-DD para DD/MM/YYYY, retorna '—' se vazio."""
        if not d:
            return "—"
        try:
            return datetime.strptime(str(d)[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
        except Exception:
            return str(d)

    # ── Alterados — agrupados por tipo de mudança ─────────────────
    if alterados:
        html += f"<h4 style='color:#e67e22; margin: 12px 0 8px;'>📝 Alterados ({len(alterados)})</h4>"

        # Prioridade de agrupamento dos campos de mudança
        _grupo_ordem = {
            "valor": 1, "status": 2, "payment_date": 3, "due_date": 4,
            "category_name": 5, "payee_name": 6, "bank_account_name": 7,
        }
        _grupo_label = {
            "valor": "💲 Mudança de Valor",
            "status": "🔄 Mudança de Status",
            "payment_date": "📅 Mudança de Data de Pagamento",
            "due_date": "📅 Mudança de Vencimento",
            "category_name": "🏷️ Mudança de Categoria",
            "payee_name": "👤 Mudança de Fornecedor/Cliente",
            "bank_account_name": "🏦 Mudança de Conta Bancária",
        }
        _grupo_cor = {
            "valor": "#e67e22",
            "status": "#2980b9",
            "payment_date": "#8e44ad",
            "due_date": "#8e44ad",
            "category_name": "#7f8c8d",
            "payee_name": "#7f8c8d",
            "bank_account_name": "#7f8c8d",
        }

        # Classifica cada alterado pelo campo principal (primeiro mudança = prioridade)
        def _campo_principal(a):
            """Retorna o campo de maior prioridade entre as mudanças."""
            if not a.get("mudancas"):
                return "valor"
            return min(a["mudancas"], key=lambda m: _grupo_ordem.get(m["campo"], 99))["campo"]

        # Agrupa por campo principal
        from collections import OrderedDict
        grupos = OrderedDict()
        for a in alterados:
            chave = _campo_principal(a)
            grupos.setdefault(chave, []).append(a)

        # Ordena os grupos pela prioridade
        grupos_ordenados = sorted(grupos.items(), key=lambda kv: _grupo_ordem.get(kv[0], 99))

        def _render_item_alterado(a):
            """Renderiza um item alterado com suas mudanças."""
            pago = f" | Pago: {_fmt_data(a.get('payment_date'))}" if a.get("payment_date") else ""
            cor_borda = _grupo_cor.get(_campo_principal(a), "#e67e22")
            item_html = f"""
            <div style="border-left: 3px solid {cor_borda}; padding: 8px 12px;
                        margin-bottom: 8px; background: #fffaf5; border-radius: 0 4px 4px 0;">
              <strong>[{a['tipo'].upper()}] {a['description']}</strong><br>
              <small style="color:#777;">
                Fornecedor: {a['payee_name'] or '—'} | Categoria: {a['category_name'] or '—'} | Venc: {_fmt_data(a['due_date'])}{pago}
              </small><br>
              <small style="color:#999;">Versões: {a['qtd_versoes']} | Base: {_fmt_data(a['data_base'])} → Atual: {_fmt_data(a['data_atual'])}</small><br>
              <div style="margin-top:6px;">
                <strong style="font-size:12px;">O que mudou:</strong><br>
            """
            for m in a["mudancas"]:
                if m["campo"] == "valor":
                    aumentou = m["atual_raw"] > m["anterior_raw"]
                    delta    = m["atual_raw"] - m["anterior_raw"]
                    if a.get("tipo") == "despesa":
                        cor = "#e74c3c" if aumentou else "#27ae60"
                    else:
                        cor = "#27ae60" if aumentou else "#e74c3c"
                    sinal     = "▲" if aumentou else "▼"
                    pct       = f" ({m['variacao_pct']:+.1f}%)" if m.get("variacao_pct") is not None else ""
                    delta_abs = abs(delta)
                    delta_fmt = f"R$\u00a0{delta_abs:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                    delta_str = f"+{delta_fmt}" if aumentou else f"-{delta_fmt}"
                    item_html += (
                        f'<span style="color:{cor}; font-weight:bold;">'
                        f'{sinal} {m["label"]}: {m["anterior"]} → {m["atual"]}{pct}'
                        f'</span>'
                        f' <span style="color:{cor}; font-size:11px; font-weight:bold;'
                        f' background:{cor}18; padding:1px 6px; border-radius:3px;'
                        f' border:1px solid {cor}40;">{delta_str}</span><br>'
                    )
                elif m["campo"] in ("due_date", "payment_date"):
                    item_html += f'<span style="color:#8e44ad;">↔ {m["label"]}: {_fmt_data(m["anterior"])} → {_fmt_data(m["atual"])}</span><br>'
                else:
                    item_html += f'<span style="color:#8e44ad;">↔ {m["label"]}: {m["anterior"]} → {m["atual"]}</span><br>'

            # Histórico completo
            if a.get("historico"):
                item_html += '<div style="margin-top:8px; font-size:12px; color:#555; background:#f5f5f5; padding:6px 8px; border-radius:4px;">'
                item_html += '<strong>Histórico completo:</strong><br>'
                for h in a["historico"]:
                    tag  = " <em>[carga inicial]</em>" if h.get("is_initial_load") else ""
                    pago_h = f" | Pago: {_fmt_data(h['payment_date'])}" if h.get("payment_date") else ""
                    item_html += f"&nbsp;&nbsp;{_fmt_data(h['extraction_date'])}{tag} → R$ {float(h['value'] or 0):,.2f} | {h['status']}{pago_h}<br>"
                item_html += '</div>'

            item_html += "</div></div>"
            return item_html

        for campo, itens_grupo in grupos_ordenados:
            label_grupo = _grupo_label.get(campo, f"Outras mudanças ({campo})")
            cor_grupo   = _grupo_cor.get(campo, "#e67e22")
            html += f"""
            <div style="margin: 16px 0 8px; padding: 6px 12px; background:{cor_grupo}12;
                        border-left: 4px solid {cor_grupo}; border-radius: 0 4px 4px 0;">
              <strong style="color:{cor_grupo}; font-size:13px;">{label_grupo} ({len(itens_grupo)})</strong>
            </div>"""
            for a in itens_grupo:
                html += _render_item_alterado(a)

    # ── Novos ────────────────────────────────────────────────────
    if novos:
        html += f"<h4 style='color:#27ae60; margin: 12px 0 8px;'>✅ Novos ({len(novos)})</h4>"
        for n in novos:
            pago_n = f" | Pago: {_fmt_data(n.get('payment_date'))}" if n.get("payment_date") else ""
            html += f"""
            <div style="border-left: 3px solid #27ae60; padding: 8px 12px;
                        margin-bottom: 8px; background: #f5fdf7; border-radius: 0 4px 4px 0;">
              <strong>[{n['tipo'].upper()}] {n['description']}</strong><br>
              <small style="color:#777;">
                Fornecedor: {n.get('payee_name') or '—'} | Categoria: {n.get('category_name') or '—'} | Venc: {_fmt_data(n['due_date'])}{pago_n}
              </small><br>
              <small style="color:#999;">R$ {float(n.get('value') or 0):,.2f} | {n.get('status') or '—'} | Extraído em: {_fmt_data(n.get('extraction_date'))}</small>
            </div>
            """

    # ── Removidos ────────────────────────────────────────────────
    if removidos:
        html += f"<h4 style='color:#e74c3c; margin: 12px 0 8px;'>❌ Removidos ({len(removidos)})</h4>"
        for r in removidos:
            html += f"""
            <div style="border-left: 3px solid #e74c3c; padding: 8px 12px;
                        margin-bottom: 6px; background: #fdf5f5; border-radius: 0 4px 4px 0;">
              <strong>[{r['tipo'].upper()}] {r['description']}</strong> —
              R$ {float(r['value'] or 0):,.2f} | Visto em: {r.get('snapshot_inicial','—')} → sumiu em: {r.get('ultimo_snapshot_visto','—')}
            </div>
            """

    # ── Sem mudanças: mensagem compacta ──────────────────────────
    if not alterados and not novos and not removidos:
        html += """
        <p style="color:#999; font-size:13px; font-style:italic; margin:8px 0;">
          Nenhuma alteração detectada neste mês.
        </p>
        """

    # ── Resumo LLM (apenas quando há mudanças) ───────────────────
    if resumo_llm:
        resumo_html = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', resumo_llm)
        resumo_html = resumo_html.replace("\n", "<br>")
        html += f"""
        <div style="background: #f0f4ff; border-radius: 6px; padding: 14px;
                    margin-top: 16px; font-size: 13px; line-height: 1.7; color: #2c3e50;">
          <strong style="color:#1a1a2e;">💡 Resumo Executivo</strong><br><br>
          {resumo_html}
        </div>
        """

    html += """
      </div>
    </div>
    """
    return html


def _html_painel_totais(totais: dict, ano: int) -> str:
    """Gera o painel HTML com receita, despesa, lucro e margem por snapshot."""

    def _fmt_data(d):
        if not d:
            return "—"
        try:
            return datetime.strptime(str(d)[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
        except Exception:
            return str(d)

    def _cor_margem(m):
        if m >= 20: return "#1A7A3C"
        if m >= 5:  return "#C75B00"
        return "#C0392B"

    def _delta_html(delta, pct, favoravel_positivo=True):
        """
        Gera a sub-linha HTML do delta.
        favoravel_positivo=True → delta positivo é bom (receita/lucro subindo).
        favoravel_positivo=False → delta positivo é ruim (despesa subindo).
        """
        if abs(delta) < 0.01:
            return ('<tr><td colspan="2" style="text-align:right; font-size:11px; '
                    'color:#999; padding:0 0 6px;">— R$ 0,00 (0.0%)</td></tr>')
        favoravel = (delta > 0) == favoravel_positivo
        cor  = "#1A7A3C" if favoravel else "#C0392B"
        seta = "&#9650;" if delta > 0 else "&#9660;"   # ▲ ou ▼
        sinal = "+" if delta > 0 else "-"
        return (f'<tr><td colspan="2" style="text-align:right; font-size:11px; '
                f'color:{cor}; padding:0 0 6px;">'
                f'{seta} {sinal}R$ {abs(delta):,.2f} ({pct:+.1f}%)</td></tr>')

    def _bloco(label, data_label, t, destaque=False, anterior=None):
        if not t:
            return ""
        cor_lucro  = "#1A7A3C" if t["lucro"] >= 0 else "#C0392B"
        cor_margem = _cor_margem(t["margem"])
        bg         = "#E8F1F8" if destaque else "#F9F9F9"
        borda      = "#1E6BA8" if destaque else "#DDDDDD"

        # Calcula deltas se anterior fornecido
        delta_receita_html = ""
        delta_despesa_html = ""
        delta_lucro_html   = ""
        if anterior:
            d_rec = t["receita"] - anterior["receita"]
            p_rec = (d_rec / anterior["receita"] * 100) if anterior["receita"] else 0
            delta_receita_html = _delta_html(d_rec, p_rec, favoravel_positivo=True)

            d_desp = t["despesa"] - anterior["despesa"]
            p_desp = (d_desp / anterior["despesa"] * 100) if anterior["despesa"] else 0
            delta_despesa_html = _delta_html(d_desp, p_desp, favoravel_positivo=False)

            d_lucro = t["lucro"] - anterior["lucro"]
            p_lucro = (d_lucro / abs(anterior["lucro"]) * 100) if anterior["lucro"] else 0
            delta_lucro_html = _delta_html(d_lucro, p_lucro, favoravel_positivo=True)

        return f"""
        <div style="flex:1; min-width:200px; background:{bg}; border:1px solid {borda};
                    border-top: 4px solid {borda}; border-radius:6px; padding:14px 16px;">
          <div style="font-size:12px; color:#777; margin-bottom:6px;">{data_label}</div>
          <div style="font-size:14px; font-weight:bold; color:#1A1A1A; margin-bottom:10px;">{label}</div>
          <table style="width:100%; border-collapse:collapse; font-size:13px;">
            <tr>
              <td style="color:#555; padding:3px 0;">📈 Receita</td>
              <td style="text-align:right; color:#1A7A3C; font-weight:bold;">R$ {t['receita']:,.2f}</td>
            </tr>
            {delta_receita_html}
            <tr>
              <td style="color:#555; padding:3px 0;">📉 Despesa</td>
              <td style="text-align:right; color:#C0392B; font-weight:bold;">R$ {t['despesa']:,.2f}</td>
            </tr>
            {delta_despesa_html}
            <tr style="border-top:1px solid #DDD;">
              <td style="color:#555; padding:5px 0 3px;">💰 Lucro</td>
              <td style="text-align:right; color:{cor_lucro}; font-weight:bold;">R$ {t['lucro']:,.2f}</td>
            </tr>
            {delta_lucro_html}
            <tr>
              <td style="color:#555; padding:3px 0;">📊 Margem</td>
              <td style="text-align:right; color:{cor_margem}; font-weight:bold;">{t['margem']:+.1f}%</td>
            </tr>
          </table>
        </div>"""

    blocos  = _bloco("Carga Inicial", "Baseline do período", totais["carga_inicial"])
    blocos += _bloco(
        "Penúltima Extração",
        _fmt_data(totais["penultima_data"]),
        totais["penultima"]
    ) if totais.get("penultima") else ""
    blocos += _bloco(
        "Última Extração",
        _fmt_data(totais["ultima_data"]),
        totais["ultima"],
        destaque=True,
        anterior=totais.get("penultima") or totais["carga_inicial"]
    ) if totais.get("ultima") else ""

    return f"""
    <div style="margin-bottom:28px;">
      <h3 style="color:#1E6BA8; font-size:16px; margin-bottom:12px; padding-bottom:6px;
                 border-bottom:2px solid #1E6BA8;">
        📊 Totais do Ano {ano}
      </h3>
      <div style="display:flex; gap:12px; flex-wrap:wrap;">
        {blocos}
      </div>
    </div>
    """



def _html_variacao_mensal(dados: dict, ano: int) -> str:
    """
    Gera tabela HTML comparando receita e despesa por mês entre
    última extração e penúltima (ou carga inicial como fallback),
    com deltas coloridos.
    """
    if not dados.get("meses") or not dados.get("ultima_data"):
        return ""

    def _fmt(v: float) -> str:
        return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    def _fmt_delta(v: float) -> str:
        sinal = "+" if v >= 0 else ""
        return f"{sinal}R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    def _cor_delta_receita(v: float) -> str:
        return "#1A7A3C" if v >= 0 else "#C0392B"

    def _cor_delta_despesa(v: float) -> str:
        # Despesa aumentou = ruim (vermelho); diminuiu = bom (verde)
        return "#C0392B" if v > 0 else "#1A7A3C"

    from datetime import datetime as _dt
    def _fmt_data(d):
        if not d:
            return "—"
        try:
            return _dt.strptime(str(d)[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
        except Exception:
            return str(d)

    def _cor_lucro(v: float) -> str:
        return "#1A7A3C" if v >= 0 else "#C0392B"

    def _cor_margem(m: float) -> str:
        if m >= 20: return "#1A7A3C"
        if m >= 5:  return "#C75B00"
        return "#C0392B"

    linhas_html = ""
    tot_u_rec = tot_p_rec = tot_u_des = tot_p_des = 0.0
    for item in dados["meses"]:
        u = item["ultima"]
        p = item["penultima"]
        d = item["delta"]
        tot_u_rec += u["receita"]; tot_p_rec += p["receita"]
        tot_u_des += u["despesa"]; tot_p_des += p["despesa"]
        dr = d["receita"]; dd = d["despesa"]
        lucro  = u["receita"] - u["despesa"]
        margem = (lucro / u["receita"] * 100) if u["receita"] else 0
        linhas_html += f"""
        <tr style="border-bottom:1px solid #EEE;">
          <td style="padding:7px 10px; font-weight:500;">{item['nome']}</td>
          <td style="padding:7px 10px; text-align:right; color:#555;">{_fmt(p['receita'])}</td>
          <td style="padding:7px 10px; text-align:right; color:#1A7A3C; font-weight:bold;">{_fmt(u['receita'])}</td>
          <td style="padding:7px 10px; text-align:right; color:{_cor_delta_receita(dr)}; font-weight:bold;">{_fmt_delta(dr)}</td>
          <td style="padding:7px 10px; text-align:right; color:#555; border-left:1px solid #DDD;">{_fmt(p['despesa'])}</td>
          <td style="padding:7px 10px; text-align:right; color:#C0392B; font-weight:bold;">{_fmt(u['despesa'])}</td>
          <td style="padding:7px 10px; text-align:right; color:{_cor_delta_despesa(dd)}; font-weight:bold;">{_fmt_delta(dd)}</td>
          <td style="padding:7px 10px; text-align:right; color:{_cor_lucro(lucro)}; font-weight:bold; border-left:1px solid #DDD; background:#F5F9FF;">{_fmt(lucro)}</td>
          <td style="padding:7px 10px; text-align:right; color:{_cor_margem(margem)}; font-weight:bold; background:#F5F9FF;">{margem:+.1f}%</td>
        </tr>"""

    # Linha de totais anuais
    delta_rec = tot_u_rec - tot_p_rec
    delta_des = tot_u_des - tot_p_des
    tot_lucro  = tot_u_rec - tot_u_des
    tot_margem = (tot_lucro / tot_u_rec * 100) if tot_u_rec else 0
    linhas_html += f"""
        <tr style="background:#F0F4F8; font-weight:bold; border-top:2px solid #1E6BA8;">
          <td style="padding:8px 10px;">Total {ano}</td>
          <td style="padding:8px 10px; text-align:right; color:#555;">{_fmt(tot_p_rec)}</td>
          <td style="padding:8px 10px; text-align:right; color:#1A7A3C;">{_fmt(tot_u_rec)}</td>
          <td style="padding:8px 10px; text-align:right; color:{_cor_delta_receita(delta_rec)};">{_fmt_delta(delta_rec)}</td>
          <td style="padding:8px 10px; text-align:right; color:#555; border-left:1px solid #DDD;">{_fmt(tot_p_des)}</td>
          <td style="padding:8px 10px; text-align:right; color:#C0392B;">{_fmt(tot_u_des)}</td>
          <td style="padding:8px 10px; text-align:right; color:{_cor_delta_despesa(delta_des)};">{_fmt_delta(delta_des)}</td>
          <td style="padding:8px 10px; text-align:right; color:{_cor_lucro(tot_lucro)}; border-left:1px solid #DDD; background:#E3EDF8;">{_fmt(tot_lucro)}</td>
          <td style="padding:8px 10px; text-align:right; color:{_cor_margem(tot_margem)}; background:#E3EDF8;">{tot_margem:+.1f}%</td>
        </tr>"""

    anterior_label = dados.get("anterior_label", "Anterior")
    data_ant = _fmt_data(dados.get("penultima_data") or "Carga Inicial")
    data_ult = _fmt_data(dados["ultima_data"])
    # Se penultima_data é None, a referência é a carga inicial (sem data específica)
    ref_texto = f"carga inicial" if not dados.get("penultima_data") else f"extração de {data_ant}"

    return f"""
    <div style="margin-bottom:28px;">
      <h3 style="color:#1E6BA8; font-size:16px; margin-bottom:12px; padding-bottom:6px;
                 border-bottom:2px solid #1E6BA8;">
        📅 Variação Mensal — Receita &amp; Despesa {ano}
      </h3>
      <p style="font-size:12px; color:#777; margin-bottom:10px;">
        Comparação entre {ref_texto} e última extração ({data_ult}).
        Δ Receita: verde = aumento, vermelho = queda.
        Δ Despesa: verde = redução (bom), vermelho = aumento.
      </p>
      <table style="width:100%; border-collapse:collapse; font-size:13px;">
        <thead>
          <tr style="background:#1E6BA8; color:#FFF;">
            <th style="padding:8px 10px; text-align:left;">Mês</th>
            <th style="padding:8px 10px; text-align:right;">Rec. {anterior_label}</th>
            <th style="padding:8px 10px; text-align:right;">Rec. Última</th>
            <th style="padding:8px 10px; text-align:right;">Δ Receita</th>
            <th style="padding:8px 10px; text-align:right; border-left:1px solid rgba(255,255,255,0.3);">Desp. {anterior_label}</th>
            <th style="padding:8px 10px; text-align:right;">Desp. Última</th>
            <th style="padding:8px 10px; text-align:right;">Δ Despesa</th>
            <th style="padding:8px 10px; text-align:right; border-left:1px solid rgba(255,255,255,0.3); background:#15548A;">Lucro</th>
            <th style="padding:8px 10px; text-align:right; background:#15548A;">Margem</th>
          </tr>
        </thead>
        <tbody>
          {linhas_html}
        </tbody>
      </table>
    </div>
    """


def run_auditoria(ano: int, send_mail: bool = True):
    """
    Gera relatório de auditoria de mudanças mês a mês para o ano informado.
    Compara versões consecutivas de cada registro para detectar:
      - Alterações de valor ou status
      - Registros novos
      - Registros removidos
    """
    log.info(f"Iniciando auditoria do ano {ano}...")
    db = FinancialDB()

    # Processa todos os 12 meses — registros podem ter vencimento em meses futuros
    # (ex: despesa com vencimento em abril já existe no banco em março)
    ano_atual = date.today().year
    mes_atual = date.today().month
    meses = range(1, 13)

    # ── Fase 1: coleta todos os dados mês a mês ──────────────────────────────
    # Busca a última extração global UMA vez para evitar N queries e garantir
    # que todos os meses usem o mesmo ponto de referência.
    ultima_global = db.get_ultima_extracao_global()
    log.info(f"  Última extração global: {ultima_global}")

    resultados_meses = []
    for mes in meses:
        mes_label = f"{MESES_PT[mes]}/{ano}"
        log.info(f"  Processando {mes_label}...")
        dados_mes = db.get_audit_changes(ano=ano, mes=mes,
                                         ultima_extracao_global=ultima_global)

        total = len(dados_mes["alterados"]) + len(dados_mes["novos"]) + len(dados_mes["removidos"])
        if total == 0:
            log.info(f"  {mes_label}: nenhuma mudança detectada.")
            continue

        log.info(
            f"  {mes_label}: {len(dados_mes['alterados'])} alterados, "
            f"{len(dados_mes['novos'])} novos, {len(dados_mes['removidos'])} removidos — "
            f"gerando resumo..."
        )
        resumo = _resumo_auditoria_llm(dados_mes)
        resultados_meses.append({
            "dados":  dados_mes,
            "resumo": resumo,
        })

    log.info(f"✓ Auditoria concluída — {len(resultados_meses)}/12 mês(es) com mudanças — montando relatório...")

    # ── Fase 2: output após tudo processado ──────────────────────────────────
    titulo = f"🔍 Auditoria Financeira {ano} — {len(resultados_meses)}/12 mês(es) com mudanças"

    # Sumarização geral do ano
    total_alterados = sum(len(r["dados"]["alterados"]) for r in resultados_meses)
    total_novos     = sum(len(r["dados"]["novos"])     for r in resultados_meses)
    total_removidos = sum(len(r["dados"]["removidos"]) for r in resultados_meses)

    if send_mail:
        log.info("  Calculando totais por extração...")
        totais = db.get_totais_por_extracao(ano)
        painel_totais = _html_painel_totais(totais, ano)

        log.info("  Calculando variação mensal...")
        variacao_mensal = db.get_variacao_mensal_por_extracao(ano)
        painel_variacao = _html_variacao_mensal(variacao_mensal, ano)

        intro = f"""
        <div style="background: #f8f9fa; border-radius: 8px; padding: 16px 20px;
                    margin-bottom: 24px; font-size: 14px;">
          <strong>Resumo do ano {ano}</strong><br><br>
          📝 Total alterados: <strong>{total_alterados}</strong> &nbsp;|&nbsp;
          ✅ Total novos: <strong>{total_novos}</strong> &nbsp;|&nbsp;
          ❌ Total removidos: <strong>{total_removidos}</strong>
        </div>
        {painel_totais}
        {painel_variacao}
        """
        corpo = intro + "".join(_formatar_mes_html(r["dados"], r["resumo"]) for r in resultados_meses)
        html  = _html_wrap(titulo, corpo)
        send_email(titulo, html)
    else:
        sep = "=" * 60
        print(f"\n{sep}")
        print(f"  AUDITORIA FINANCEIRA {ano}")
        print(f"  Total: {total_alterados} alterados | {total_novos} novos | {total_removidos} removidos")
        print(sep)
        for r in resultados_meses:
            print(_formatar_mes_terminal(r["dados"], r["resumo"]))
        print(f"\n{sep}\n")




def explain_daily_variation(db: FinancialDB, variation: dict) -> str:
    """
    Passa os registros do dia para o GPT-4o e pede explicação
    da variação detectada pela view daily_financial_summary.
    """
    dia    = variation["dia"]
    tipo   = variation["tipo"]
    total  = variation["total"]
    total_ant = variation["total_dia_anterior"]
    var_pct   = variation["variacao_pct"]
    var_val   = variation["variacao_valor"]

    # Registros do dia com variação
    registros_dia = db.get_records_by_due_date(dia, tipo)
    # Mudanças detectadas via diff (registros alterados nesse dia)
    diffs = db.get_diff_by_date(dia)

    # Monta contexto para o LLM
    linhas_registros = "\n".join(
        f"  - {r.get('description','')} | R$ {float(r.get('value') or 0):,.2f} | "
        f"Cat: {r.get('category_name','')} | {r.get('payee_name','')} | Status: {r.get('status','')}"
        for r in registros_dia[:30]
    )

    linhas_diffs = ""
    if diffs:
        linhas_diffs = "\nREGISTROS QUE MUDARAM NESTE DIA:\n" + "\n".join(
            f"  - {d.get('description','')} | "
            f"Valor: R$ {float(d.get('valor_anterior') or 0):,.2f} → R$ {float(d.get('valor_atual') or 0):,.2f} | "
            f"Status: {d.get('status_anterior','')} → {d.get('status_atual','')} | "
            f"Categoria: {d.get('categoria_anterior','')} → {d.get('categoria_atual','')}"
            for d in diffs[:20]
        )

    direcao = "aumento" if var_val > 0 else "redução"
    prompt = f"""Analise a seguinte variação financeira e explique de forma objetiva o que a causou:

DIA: {dia}
TIPO: {tipo.upper()}
TOTAL ATUAL: R$ {float(total):,.2f}
TOTAL DIA ANTERIOR: R$ {float(total_ant):,.2f}
VARIAÇÃO: {direcao} de R$ {abs(float(var_val)):,.2f} ({var_pct:+.1f}%)

REGISTROS DO DIA:
{linhas_registros}
{linhas_diffs}

Explique em 3-4 linhas:
1. O que causou essa variação (quais lançamentos ou mudanças)
2. Se é algo esperado ou merece atenção
3. Se há alguma ação recomendada"""

    response = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT_INSIGHTS},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        max_tokens=400,
    )
    return response.choices[0].message.content


def run_daily_variations(send_mail: bool = True, variacao_min_pct: float = 10.0):
    """
    Detecta dias do mês com variações relevantes e envia explicação por e-mail.
    Roda diariamente junto com o sync.
    Todas as consultas e análises são feitas antes de qualquer output.
    """
    log.info("Analisando variações diárias...")
    db         = FinancialDB()
    variations = db.get_daily_variations(variacao_min_pct=variacao_min_pct)

    if not variations:
        log.info("Nenhuma variação relevante detectada.")
        return

    log.info(f"{len(variations)} variação(ões) detectada(s) — gerando explicações...")

    # ── Fase 1: coleta todos os dados e explicações em memória ──────────────
    resultados = []
    for i, v in enumerate(variations, 1):
        log.info(f"  Processando {i}/{len(variations)}: {v['tipo'].upper()} {v['dia']} ({float(v.get('variacao_pct') or 0):+.1f}%)...")
        explicacao = explain_daily_variation(db, v)
        resultados.append({
            "variacao":   v,
            "explicacao": explicacao,
        })

    log.info("✓ Todas as análises concluídas — montando relatório...")

    # ── Fase 2: monta output apenas após tudo processado ────────────────────
    import re
    mes_atual = datetime.now().strftime("%B/%Y")
    titulo    = f"📊 Variações Diárias — {mes_atual} — {date.today().strftime('%d/%m/%Y')}"

    if send_mail:
        secoes = ""
        for r in resultados:
            v          = r["variacao"]
            explicacao = r["explicacao"]
            direcao    = "📈" if float(v.get("variacao_valor") or 0) > 0 else "📉"
            cor        = "#27ae60" if float(v.get("variacao_valor") or 0) > 0 else "#e74c3c"

            explicacao_html = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', explicacao)
            explicacao_html = explicacao_html.replace("\n", "<br>")

            secoes += f"""
            <div style="border: 1px solid #e0e0e0; border-radius: 8px;
                        padding: 16px; margin-bottom: 20px; background: white;">
                <div style="display: flex; justify-content: space-between; align-items: center;
                            margin-bottom: 12px;">
                    <strong style="font-size: 15px;">{direcao} {v['tipo'].upper()} — {v['dia']}</strong>
                    <span style="color: {cor}; font-weight: bold; font-size: 15px;">
                        {float(v.get('variacao_pct') or 0):+.1f}%
                        (R$ {float(v.get('variacao_valor') or 0):+,.2f})
                    </span>
                </div>
                <div style="color: #555; font-size: 13px; margin-bottom: 8px;">
                    Total: <strong>R$ {float(v.get('total') or 0):,.2f}</strong> |
                    Anterior: R$ {float(v.get('total_dia_anterior') or 0):,.2f}
                </div>
                <div style="background: #f8f9fa; border-radius: 6px; padding: 12px;
                            font-size: 13px; line-height: 1.6; color: #2c3e50;">
                    {explicacao_html}
                </div>
            </div>
            """

        intro = f"""
            <p style="color: #555; margin-bottom: 20px;">
                {len(resultados)} variação(ões) acima de {variacao_min_pct:.0f}% detectada(s) este mês.
                Threshold configurado: ±{variacao_min_pct:.0f}%
            </p>
        """
        html = _html_wrap(titulo, intro + secoes)
        send_email(titulo, html)

    else:
        # Output limpo no terminal — somente após tudo processado
        sep = "=" * 60
        print(f"\n{sep}")
        print(titulo)
        print(f"Threshold: ±{variacao_min_pct:.0f}% | {len(resultados)} variação(ões) encontrada(s)")
        print(sep)

        for r in resultados:
            v          = r["variacao"]
            explicacao = r["explicacao"]
            direcao    = "▲" if float(v.get("variacao_valor") or 0) > 0 else "▼"
            print(f"\n{direcao} {v['tipo'].upper()} — {v['dia']}")
            print(f"  Total:    R$ {float(v.get('total') or 0):>12,.2f}")
            print(f"  Anterior: R$ {float(v.get('total_dia_anterior') or 0):>12,.2f}")
            print(f"  Variação: {float(v.get('variacao_pct') or 0):+.1f}% "
                  f"(R$ {float(v.get('variacao_valor') or 0):+,.2f})")
            print(f"\n  Análise:")
            for linha in explicacao.strip().split("\n"):
                print(f"  {linha}")
            print(f"\n  {'-'*56}")

        print(f"\n{sep}\n")


# ---------------------------------------------------------------------------
# Chat Terminal
# ---------------------------------------------------------------------------

def run_chat_terminal():
    """Chat interativo no terminal com histórico de conversa e function calling."""
    db = FinancialDB()
    print("\n" + "=" * 60)
    print("💬 Chat Financeiro — GPT-4o (com Function Calling)")
    print("Digite 'sair' para encerrar, 'limpar' para reiniciar conversa")
    print("=" * 60 + "\n")

    history = [
        {"role": "system", "content": _build_system_prompt_tools()}
    ]

    while True:
        try:
            user_input = input("Você: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nAté logo!")
            break

        if not user_input:
            continue
        if user_input.lower() == "sair":
            print("Até logo!")
            break
        if user_input.lower() == "limpar":
            history = [{"role": "system", "content": _build_system_prompt_tools()}]
            print("Conversa reiniciada.\n")
            continue

        history.append({"role": "user", "content": user_input})

        # Loop de function calling (máx 3 rodadas)
        MAX_TOOL_ROUNDS = 3
        message = None

        for _ in range(MAX_TOOL_ROUNDS):
            response = openai_client.chat.completions.create(
                model="gpt-4o",
                messages=history,
                tools=CHAT_TOOLS,
                tool_choice="auto",
                temperature=0.4,
                max_tokens=CONFIG["chat_max_tokens"],
            )
            message = response.choices[0].message

            if message.tool_calls:
                history.append(message)
                for tool_call in message.tool_calls:
                    fn_name = tool_call.function.name
                    fn_args = json.loads(tool_call.function.arguments)
                    log.info(f"🔧 Tool call: {fn_name}({fn_args})")

                    result_str = _execute_tool(db, fn_name, fn_args)
                    history.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result_str,
                    })
                continue
            else:
                break

        answer = message.content if message and message.content else "Desculpe, não consegui gerar uma resposta."
        history.append({"role": "assistant", "content": answer})

        # Trim do histórico
        if len(history) > 40:
            history[:] = [history[0]] + history[-30:]

        print(f"\nAssistente: {answer}\n")


# ---------------------------------------------------------------------------
# Interface Web (Flask)
# ---------------------------------------------------------------------------

def run_web():
    """Inicia interface web local com Flask."""
    try:
        from flask import Flask, request, jsonify, render_template_string
    except ImportError:
        print("Flask não instalado. Execute: pip install flask")
        return

    app = Flask(__name__)
    db  = FinancialDB()

    # Histórico por sessão com function calling (sem contexto estático)
    chat_history = [
        {"role": "system", "content": _build_system_prompt_tools()}
    ]

    HTML_PAGE = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Financial AI</title>
  <script src="https://cdn.jsdelivr.net/npm/marked@9.1.6/marked.min.js"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
           background: #f0f2f5; height: 100vh; display: flex; flex-direction: column; }
    header { background: #1a1a2e; color: white; padding: 16px 24px;
             display: flex; align-items: center; gap: 12px; }
    header h1 { font-size: 18px; font-weight: 600; }
    header span { font-size: 24px; }
    #chat { flex: 1; overflow-y: auto; padding: 24px; display: flex;
            flex-direction: column; gap: 16px; }
    .msg { max-width: 78%; padding: 12px 16px; border-radius: 16px;
           line-height: 1.7; font-size: 14px; }
    .user { align-self: flex-end; background: #1a1a2e; color: white;
            border-bottom-right-radius: 4px; white-space: pre-wrap; }
    .assistant { align-self: flex-start; background: white; color: #2c3e50;
                 border-bottom-left-radius: 4px;
                 box-shadow: 0 1px 4px rgba(0,0,0,0.08); }

    /* ── Markdown rendered content inside assistant messages ── */
    .assistant p { margin: 0 0 10px 0; }
    .assistant p:last-child { margin-bottom: 0; }
    .assistant strong { color: #1a1a2e; font-weight: 700; }
    .assistant em { color: #555; }
    .assistant ul, .assistant ol { margin: 6px 0 10px 20px; }
    .assistant li { margin-bottom: 4px; }
    .assistant li::marker { color: #1a1a2e; }
    .assistant h3 { font-size: 14px; font-weight: 700; color: #1a1a2e;
                    margin: 14px 0 6px 0; border-bottom: 1px solid #eee; padding-bottom: 4px; }
    .assistant h4 { font-size: 13px; font-weight: 700; color: #34495e; margin: 10px 0 4px 0; }

    /* ── Tables ── */
    .assistant table { width: 100%; border-collapse: collapse; margin: 10px 0;
                       font-size: 13px; border-radius: 8px; overflow: hidden; }
    .assistant thead th { background: #1a1a2e; color: white; padding: 8px 12px;
                          text-align: left; font-weight: 600; font-size: 12px;
                          text-transform: uppercase; letter-spacing: 0.3px; }
    .assistant tbody td { padding: 7px 12px; border-bottom: 1px solid #f0f0f0; }
    .assistant tbody tr:last-child td { border-bottom: none; }
    .assistant tbody tr:nth-child(even) { background: #f8f9fa; }
    .assistant tbody tr:hover { background: #eef2ff; }

    /* ── Inline code / highlight chips ── */
    .assistant code { background: #e8f4f8; color: #0d6eaa; padding: 1px 6px;
                      border-radius: 4px; font-size: 13px; font-family: 'SF Mono', 'Menlo', monospace; }

    /* ── Horizontal rules ── */
    .assistant hr { border: none; border-top: 1px solid #e0e0e0; margin: 12px 0; }

    /* ── Positive / negative value highlights (applied via GPT markdown) ── */
    .val-pos { color: #27ae60; font-weight: 700; }
    .val-neg { color: #e74c3c; font-weight: 700; }
    .val-warn { color: #f39c12; font-weight: 700; }
    .val-info { color: #2980b9; font-weight: 700; }

    .typing { align-self: flex-start; background: white; padding: 12px 16px;
              border-radius: 16px; border-bottom-left-radius: 4px;
              box-shadow: 0 1px 4px rgba(0,0,0,0.08); display: none; }
    .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
           background: #aaa; margin: 0 2px; animation: bounce 1.2s infinite; }
    .dot:nth-child(2) { animation-delay: 0.2s; }
    .dot:nth-child(3) { animation-delay: 0.4s; }
    @keyframes bounce { 0%,60%,100% { transform: translateY(0); }
                        30% { transform: translateY(-6px); } }
    #input-area { background: white; border-top: 1px solid #e0e0e0;
                  padding: 16px 24px; display: flex; gap: 12px; align-items: flex-end; }
    #input-area textarea { flex: 1; border: 1px solid #ddd; border-radius: 12px;
                           padding: 10px 14px; font-size: 14px; resize: none;
                           outline: none; font-family: inherit; max-height: 120px; }
    #input-area textarea:focus { border-color: #1a1a2e; }
    #input-area button { background: #1a1a2e; color: white; border: none;
                         border-radius: 12px; padding: 10px 20px; cursor: pointer;
                         font-size: 14px; font-weight: 600; white-space: nowrap; }
    #input-area button:hover { background: #16213e; }
    #input-area button:disabled { background: #ccc; cursor: not-allowed; }
    .shortcuts { padding: 8px 24px 0; display: flex; gap: 8px; flex-wrap: wrap; }
    .shortcut { background: white; border: 1px solid #ddd; border-radius: 20px;
                padding: 6px 14px; font-size: 12px; cursor: pointer; color: #555; }
    .shortcut:hover { border-color: #1a1a2e; color: #1a1a2e; }
  </style>
</head>
<body>
  <header>
    <span>💹</span>
    <h1>Financial AI — Assistente Financeiro</h1>
  </header>

  <div class="shortcuts">
    <span class="shortcut" onclick="ask('Qual o resultado financeiro do mês atual?')">Resultado do mês</span>
    <span class="shortcut" onclick="ask('Quais despesas vencem nos próximos 7 dias?')">Vencimentos próximos</span>
    <span class="shortcut" onclick="ask('Quais são as maiores despesas por categoria?')">Maiores despesas</span>
    <span class="shortcut" onclick="ask('Há algum recebimento em atraso?')">Recebimentos em atraso</span>
    <span class="shortcut" onclick="ask('Qual a evolução das receitas nos últimos 3 meses?')">Evolução receitas</span>
    <span class="shortcut" onclick="ask('Qual o resumo de vendas do mês atual?')">Vendas do mês</span>
  </div>

  <div id="chat">
    <div class="msg assistant"><p>Olá! Sou seu <strong>assistente financeiro</strong>. Tenho acesso aos dados de <strong>receitas</strong>, <strong>despesas</strong> e <strong>vendas</strong> da sua empresa. Como posso ajudar?</p></div>
    <div class="typing" id="typing"><span class="dot"></span><span class="dot"></span><span class="dot"></span></div>
  </div>

  <div id="input-area">
    <textarea id="input" rows="1" placeholder="Pergunte sobre suas finanças..."
              onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();send()}"
              oninput="this.style.height='auto';this.style.height=this.scrollHeight+'px'"></textarea>
    <button id="btn" onclick="send()">Enviar</button>
  </div>

  <script>
    // Configurar marked.js
    marked.use({
      breaks: true,
      gfm: true,
    });

    const chat   = document.getElementById('chat');
    const input  = document.getElementById('input');
    const btn    = document.getElementById('btn');
    const typing = document.getElementById('typing');

    function ask(text) { input.value = text; send(); }

    function addMsg(text, role) {
      const div = document.createElement('div');
      div.className = 'msg ' + role;
      if (role === 'assistant') {
        // Renderizar markdown para HTML
        let html = marked.parse(text);
        // Adicionar classes de cor para valores monetários positivos/negativos
        // Padrão: +R$ ou lucro positivo → verde; -R$ ou prejuízo → vermelho
        html = html.replace(/🟢/g, '<span class="val-pos">●</span>');
        html = html.replace(/🔴/g, '<span class="val-neg">●</span>');
        html = html.replace(/🟡/g, '<span class="val-warn">●</span>');
        div.innerHTML = html;
      } else {
        div.textContent = text;
      }
      chat.insertBefore(div, typing);
      chat.scrollTop = chat.scrollHeight;
    }

    async function send() {
      const text = input.value.trim();
      if (!text) return;
      input.value = '';
      input.style.height = 'auto';
      btn.disabled = true;
      addMsg(text, 'user');
      typing.style.display = 'block';
      chat.scrollTop = chat.scrollHeight;
      try {
        const resp = await fetch('/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message: text })
        });
        const data = await resp.json();
        typing.style.display = 'none';
        addMsg(data.response, 'assistant');
      } catch(e) {
        typing.style.display = 'none';
        addMsg('Erro ao conectar com o servidor. Tente novamente.', 'assistant');
      }
      btn.disabled = false;
      input.focus();
    }
  </script>
</body>
</html>
"""

    @app.route("/")
    def index():
        return render_template_string(HTML_PAGE)

    @app.route("/chat", methods=["POST"])
    def chat_endpoint():
        if WEB_TOKEN:
            token = request.headers.get("X-Auth-Token") or request.args.get("token")
            if token != WEB_TOKEN:
                return jsonify({"error": "Não autorizado. Forneça WEB_TOKEN via header X-Auth-Token ou query ?token="}), 401

        data         = request.get_json()
        user_message = data.get("message", "").strip()
        if not user_message:
            return jsonify({"response": "Mensagem vazia."})

        chat_history.append({"role": "user", "content": user_message})

        # Loop de function calling (máx 3 rodadas)
        MAX_TOOL_ROUNDS = 3
        message = None

        for _ in range(MAX_TOOL_ROUNDS):
            response = openai_client.chat.completions.create(
                model="gpt-4o",
                messages=chat_history,
                tools=CHAT_TOOLS,
                tool_choice="auto",
                temperature=0.4,
                max_tokens=CONFIG["chat_max_tokens"],
            )
            message = response.choices[0].message

            if message.tool_calls:
                # Adiciona a mensagem do assistente com as tool_calls ao histórico
                chat_history.append(message)

                for tool_call in message.tool_calls:
                    fn_name = tool_call.function.name
                    fn_args = json.loads(tool_call.function.arguments)
                    log.info(f"🔧 Tool call: {fn_name}({fn_args})")

                    result_str = _execute_tool(db, fn_name, fn_args)

                    chat_history.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result_str,
                    })
                # Continua o loop para enviar resultados de volta ao GPT
                continue
            else:
                # Sem tool calls — resposta final
                break

        answer = message.content if message and message.content else "Desculpe, não consegui gerar uma resposta."
        chat_history.append({"role": "assistant", "content": answer})

        # Trim do histórico para evitar estouro de contexto (mantém system + últimas 30 msgs)
        if len(chat_history) > 40:
            chat_history[:] = [chat_history[0]] + chat_history[-30:]

        return jsonify({"response": answer})

    print("\n" + "=" * 60)
    print("🌐 Interface web iniciada!")
    print("Acesse: http://localhost:5050")
    print("Ctrl+C para encerrar")
    print("=" * 60 + "\n")
    app.run(host=CONFIG["web_host"], port=CONFIG["web_port"], debug=False)


# ═══════════════════════════════════════════════════════════════════════════════
# Report CFO — Funções HTML e orquestração
# ═══════════════════════════════════════════════════════════════════════════════

def _cfo_fmt(v: float) -> str:
    """Formata valor monetário no padrão brasileiro."""
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _cfo_section(titulo: str, corpo: str, icone: str = "📊") -> str:
    """Wrapper padrão de seção do report CFO."""
    return f"""
    <div style="margin-bottom:28px;">
      <h3 style="color:#1E6BA8; font-size:16px; margin-bottom:12px; padding-bottom:6px;
                 border-bottom:2px solid #1E6BA8;">
        {icone} {titulo}
      </h3>
      {corpo}
    </div>"""


def _html_cfo_posicao(dados: dict, mes: int, ano: int) -> str:
    """Seção 1 — Posição de Caixa Hoje."""
    def _card(label, sub, d, destaque=False):
        bg    = "#E8F1F8" if destaque else "#F9F9F9"
        borda = "#1E6BA8" if destaque else "#DDD"
        cor_liq = "#1A7A3C" if d["liquido"] >= 0 else "#C0392B"
        return f"""
        <div style="flex:1; min-width:200px; background:{bg}; border:1px solid {borda};
                    border-top:4px solid {borda}; border-radius:6px; padding:14px 16px;">
          <div style="font-size:12px; color:#777;">{sub}</div>
          <div style="font-size:14px; font-weight:bold; color:#1A1A1A; margin-bottom:10px;">{label}</div>
          <table style="width:100%; border-collapse:collapse; font-size:13px;">
            <tr><td style="color:#555; padding:3px 0;">📈 Receita</td>
                <td style="text-align:right; color:#1A7A3C; font-weight:bold;">{_cfo_fmt(d['receita'])}</td></tr>
            <tr><td style="color:#555; padding:3px 0;">📉 Despesa</td>
                <td style="text-align:right; color:#C0392B; font-weight:bold;">{_cfo_fmt(d['despesa'])}</td></tr>
            <tr style="border-top:1px solid #DDD;">
                <td style="color:#555; padding:5px 0 3px;">💰 Líquido</td>
                <td style="text-align:right; color:{cor_liq}; font-weight:bold;">{_cfo_fmt(d['liquido'])}</td></tr>
          </table>
        </div>"""

    cards = _card("Realizado no Mês", f"{MESES_PT[mes]}/{ano}", dados["realizado"], destaque=True)
    cards += _card("A Realizar", "Até fim do mês", dados["a_realizar"])
    cards += _card(f"Mês Anterior (até dia {dados['dia_ref']})",
                   dados["mes_ant_label"], dados["mes_anterior_mesmo_dia"])

    return _cfo_section(
        f"Posição de Caixa — {MESES_PT[mes]}/{ano}",
        f'<div style="display:flex; gap:12px; flex-wrap:wrap;">{cards}</div>',
        "💰"
    )


def _html_cfo_fluxo_semana(dados: dict) -> str:
    """Seção 2 — Fluxo de Caixa da Semana."""
    linhas = ""
    for d in dados["dias"]:
        dt = datetime.strptime(d["data"], "%Y-%m-%d")
        dia_label = dt.strftime("%a %d/%m")
        cor_saldo = "#1A7A3C" if d["saldo"] >= 0 else "#C0392B"
        linhas += f"""
        <tr style="border-bottom:1px solid #EEE;">
          <td style="padding:6px 10px;">{dia_label}</td>
          <td style="padding:6px 10px; text-align:right; color:#1A7A3C;">{_cfo_fmt(d['receita'])}</td>
          <td style="padding:6px 10px; text-align:right; color:#C0392B;">{_cfo_fmt(d['despesa'])}</td>
          <td style="padding:6px 10px; text-align:right; color:{cor_saldo}; font-weight:bold;">{_cfo_fmt(d['saldo'])}</td>
        </tr>"""

    cor_total = "#1A7A3C" if dados["saldo_projetado"] >= 0 else "#C0392B"
    linhas += f"""
        <tr style="background:#F0F4F8; font-weight:bold; border-top:2px solid #1E6BA8;">
          <td style="padding:8px 10px;">Total Semana</td>
          <td style="padding:8px 10px; text-align:right; color:#1A7A3C;">{_cfo_fmt(dados['entradas'])}</td>
          <td style="padding:8px 10px; text-align:right; color:#C0392B;">{_cfo_fmt(dados['saidas'])}</td>
          <td style="padding:8px 10px; text-align:right; color:{cor_total};">{_cfo_fmt(dados['saldo_projetado'])}</td>
        </tr>"""

    tabela = f"""
    <table style="width:100%; border-collapse:collapse; font-size:13px;">
      <thead>
        <tr style="background:#1E6BA8; color:#FFF;">
          <th style="padding:8px 10px; text-align:left;">Dia</th>
          <th style="padding:8px 10px; text-align:right;">Entradas</th>
          <th style="padding:8px 10px; text-align:right;">Saídas</th>
          <th style="padding:8px 10px; text-align:right;">Saldo</th>
        </tr>
      </thead>
      <tbody>{linhas}</tbody>
    </table>"""

    destaque_html = ""
    if dados["itens_destaque"]:
        itens = ""
        for r in dados["itens_destaque"][:5]:
            tipo_icon = "📈" if r["tipo"] == "receita" else "📉"
            itens += (f'<li style="padding:3px 0;">{tipo_icon} <strong>{r["description"]}</strong>'
                      f' — {_cfo_fmt(float(r.get("value") or 0))}'
                      f' ({r.get("payee_name") or "—"})</li>')
        destaque_html = f"""
        <div style="margin-top:12px; background:#FFF8E1; border-left:4px solid #F9A825;
                    padding:10px 14px; border-radius:4px;">
          <strong style="font-size:13px;">⚠️ Itens &gt; R$ 50k:</strong>
          <ul style="margin:6px 0 0; padding-left:20px; font-size:13px;">{itens}</ul>
        </div>"""

    return _cfo_section("Fluxo de Caixa — Próximos 7 Dias", tabela + destaque_html, "📅")


def _html_cfo_inadimplencia(dados: dict) -> str:
    """Seção 3 — Inadimplência / Vencidos."""
    rv = dados["receitas_vencidas"]
    dv = dados["despesas_vencidas"]

    resumo = f"""
    <div style="display:flex; gap:12px; flex-wrap:wrap; margin-bottom:14px;">
      <div style="flex:1; min-width:200px; background:#FDF5F5; border:1px solid #E8C4C4;
                  border-radius:6px; padding:12px 16px;">
        <div style="font-size:13px; color:#C0392B; font-weight:bold; margin-bottom:6px;">
          Receitas Vencidas</div>
        <div style="font-size:20px; font-weight:bold; color:#C0392B;">{_cfo_fmt(rv['total'])}</div>
        <div style="font-size:12px; color:#777;">{rv['qtd']} título(s)</div>
      </div>
      <div style="flex:1; min-width:200px; background:#FFF8E1; border:1px solid #E8D4A0;
                  border-radius:6px; padding:12px 16px;">
        <div style="font-size:13px; color:#C75B00; font-weight:bold; margin-bottom:6px;">
          Despesas Vencidas</div>
        <div style="font-size:20px; font-weight:bold; color:#C75B00;">{_cfo_fmt(dv['total'])}</div>
        <div style="font-size:12px; color:#777;">{dv['qtd']} título(s)</div>
      </div>
    </div>"""

    # Tabela aging
    aging_html = """
    <table style="width:100%; border-collapse:collapse; font-size:13px; margin-bottom:14px;">
      <thead>
        <tr style="background:#F0F4F8;">
          <th style="padding:6px 10px; text-align:left;">Faixa</th>
          <th style="padding:6px 10px; text-align:right;">Valor</th>
          <th style="padding:6px 10px; text-align:right;">Qtd</th>
        </tr>
      </thead><tbody>"""
    for faixa in ["1-15d", "16-30d", "31-60d", "60+d"]:
        val = rv["aging"][faixa]
        qtd = rv["aging_qtd"][faixa]
        cor = "#C0392B" if faixa == "60+d" else "#555"
        aging_html += f"""
        <tr style="border-bottom:1px solid #EEE;">
          <td style="padding:5px 10px; color:{cor};">{faixa}</td>
          <td style="padding:5px 10px; text-align:right; color:{cor}; font-weight:bold;">{_cfo_fmt(val)}</td>
          <td style="padding:5px 10px; text-align:right;">{qtd}</td>
        </tr>"""
    aging_html += "</tbody></table>"

    # Todos os devedores
    dev_html = ""
    if dados["top_devedores"]:
        total_geral = dados.get("total_devedores", 0.0)
        qtd_devedores = len(dados["top_devedores"])
        dev_rows = ""
        for d in dados["top_devedores"]:
            dev_rows += f"""
            <tr style="border-bottom:1px solid #EEE;">
              <td style="padding:5px 10px;">{d['payee_name']}</td>
              <td style="padding:5px 10px; text-align:right; color:#C0392B; font-weight:bold;">{_cfo_fmt(d['total'])}</td>
              <td style="padding:5px 10px; text-align:right;">{d['qtd']}</td>
              <td style="padding:5px 10px; text-align:right;">{d['dias_medio']}d</td>
            </tr>"""
        dev_html = f"""
        <div style="display:flex; justify-content:space-between; align-items:baseline; margin-bottom:6px;">
          <div style="font-size:13px; font-weight:bold; color:#555;">
            Devedores ({qtd_devedores} cliente{'s' if qtd_devedores != 1 else ''})</div>
          <div style="font-size:15px; font-weight:bold; color:#C0392B;">
            Total: {_cfo_fmt(total_geral)}</div>
        </div>
        <table style="width:100%; border-collapse:collapse; font-size:13px;">
          <thead><tr style="background:#F0F4F8;">
            <th style="padding:6px 10px; text-align:left;">Cliente</th>
            <th style="padding:6px 10px; text-align:right;">Total</th>
            <th style="padding:6px 10px; text-align:right;">Títulos</th>
            <th style="padding:6px 10px; text-align:right;">Atraso Médio</th>
          </tr></thead><tbody>{dev_rows}</tbody>
        </table>"""

    return _cfo_section("Inadimplência / Vencidos", resumo + aging_html + dev_html, "🔴")


def _html_cfo_resumo_mes(dados: dict, mes: int, ano: int) -> str:
    """Seção 4 — Resumo do Mês (realizado vs previsto)."""
    def _barra(pct, cor):
        w = min(pct, 100)
        return (f'<div style="background:#EEE; border-radius:4px; height:14px; width:100%;">'
                f'<div style="background:{cor}; border-radius:4px; height:14px; '
                f'width:{w}%;"></div></div>')

    rec_total = dados["receita_realizada"] + dados["receita_prevista"]
    desp_total = dados["despesa_realizada"] + dados["despesa_prevista"]
    lucro_prev = dados["lucro_projetado"] - dados["lucro_realizado"]
    cor_lucro_r = "#1A7A3C" if dados["lucro_realizado"] >= 0 else "#C0392B"
    cor_lucro_prev = "#1A7A3C" if lucro_prev >= 0 else "#C0392B"
    cor_lucro_p = "#1A7A3C" if dados["lucro_projetado"] >= 0 else "#C0392B"

    def _cor_margem_cfo(m):
        if m >= 20: return "#1A7A3C"
        if m >= 5:  return "#C75B00"
        return "#C0392B"

    cor_margem_r = _cor_margem_cfo(dados["margem_realizada"])
    cor_margem_p = _cor_margem_cfo(dados["margem_projetada"])

    corpo = f"""
    <table style="width:100%; border-collapse:collapse; font-size:13px;">
      <thead><tr style="background:#1E6BA8; color:#FFF;">
        <th style="padding:8px 10px; text-align:left;">Métrica</th>
        <th style="padding:8px 10px; text-align:right;">Realizado</th>
        <th style="padding:8px 10px; text-align:right;">Previsto</th>
        <th style="padding:8px 10px; text-align:right;">Total Projetado</th>
        <th style="padding:8px 10px; text-align:right;">% Execução</th>
      </tr></thead><tbody>
      <tr style="border-bottom:1px solid #EEE;">
        <td style="padding:7px 10px; font-weight:500;">📈 Receita</td>
        <td style="padding:7px 10px; text-align:right; color:#1A7A3C; font-weight:bold;">{_cfo_fmt(dados['receita_realizada'])}</td>
        <td style="padding:7px 10px; text-align:right; color:#777;">{_cfo_fmt(dados['receita_prevista'])}</td>
        <td style="padding:7px 10px; text-align:right; font-weight:bold;">{_cfo_fmt(rec_total)}</td>
        <td style="padding:7px 10px; text-align:right;">
          {_barra(dados['pct_execucao_receita'], '#1A7A3C')}
          <span style="font-size:11px; color:#555;">{dados['pct_execucao_receita']}%</span>
        </td>
      </tr>
      <tr style="border-bottom:1px solid #EEE;">
        <td style="padding:7px 10px; font-weight:500;">📉 Despesa</td>
        <td style="padding:7px 10px; text-align:right; color:#C0392B; font-weight:bold;">{_cfo_fmt(dados['despesa_realizada'])}</td>
        <td style="padding:7px 10px; text-align:right; color:#777;">{_cfo_fmt(dados['despesa_prevista'])}</td>
        <td style="padding:7px 10px; text-align:right; font-weight:bold;">{_cfo_fmt(desp_total)}</td>
        <td style="padding:7px 10px; text-align:right;">
          {_barra(dados['pct_execucao_despesa'], '#C0392B')}
          <span style="font-size:11px; color:#555;">{dados['pct_execucao_despesa']}%</span>
        </td>
      </tr>
      <tr style="background:#F0F4F8; font-weight:bold; border-top:2px solid #1E6BA8;">
        <td style="padding:8px 10px;">💰 Lucro</td>
        <td style="padding:8px 10px; text-align:right; color:{cor_lucro_r};">{_cfo_fmt(dados['lucro_realizado'])}</td>
        <td style="padding:8px 10px; text-align:right; color:{cor_lucro_prev};">{_cfo_fmt(lucro_prev)}</td>
        <td style="padding:8px 10px; text-align:right; color:{cor_lucro_p};">{_cfo_fmt(dados['lucro_projetado'])}</td>
        <td style="padding:8px 10px;"></td>
      </tr>
      <tr style="background:#F5F9FF; font-weight:bold;">
        <td style="padding:8px 10px;">📊 Margem</td>
        <td style="padding:8px 10px; text-align:right; color:{cor_margem_r};">{dados['margem_realizada']:+.1f}%</td>
        <td style="padding:8px 10px; text-align:right;"></td>
        <td style="padding:8px 10px; text-align:right; color:{cor_margem_p};">{dados['margem_projetada']:+.1f}%</td>
        <td style="padding:8px 10px;"></td>
      </tr>
      </tbody>
    </table>"""

    return _cfo_section(f"Resumo do Mês — {MESES_PT[mes]}/{ano}", corpo, "📋")


def _html_cfo_concentracao(dados: dict) -> str:
    """Seção 5 — Concentração de Risco."""
    hoje_str = date.today().isoformat()

    def _top5_html(label, itens, cor, marcar_vencidos=False):
        if not itens:
            return ""
        rows = ""
        for r in itens:
            dt_raw = r.get("due_date") or ""
            dt = datetime.strptime(dt_raw, "%Y-%m-%d").strftime("%d/%m") if dt_raw else "—"
            vencido = marcar_vencidos and dt_raw and dt_raw < hoje_str
            bg_row = "background:#FFF3F3;" if vencido else ""
            badge_venc = (' <span style="color:#C0392B; font-size:10px; font-weight:bold;'
                          ' background:#FDDEDE; padding:1px 5px; border-radius:3px;'
                          ' border:1px solid #F5B7B1;">VENCIDO</span>') if vencido else ""
            rows += f"""
            <tr style="border-bottom:1px solid #EEE; {bg_row}">
              <td style="padding:5px 10px; font-size:12px;">{r['description'][:50]}</td>
              <td style="padding:5px 10px; font-size:12px;">{r.get('payee_name') or '—'}</td>
              <td style="padding:5px 10px; text-align:right; color:{cor}; font-weight:bold;">{_cfo_fmt(r['value'])}</td>
              <td style="padding:5px 10px; text-align:right;">{dt}{badge_venc}</td>
            </tr>"""
        return f"""
        <div style="margin-bottom:14px;">
          <div style="font-size:13px; font-weight:bold; color:#555; margin-bottom:6px;">{label}</div>
          <table style="width:100%; border-collapse:collapse; font-size:13px;">
            <thead><tr style="background:#F0F4F8;">
              <th style="padding:5px 10px; text-align:left;">Descrição</th>
              <th style="padding:5px 10px; text-align:left;">Fornecedor/Cliente</th>
              <th style="padding:5px 10px; text-align:right;">Valor</th>
              <th style="padding:5px 10px; text-align:right;">Venc.</th>
            </tr></thead><tbody>{rows}</tbody>
          </table>
        </div>"""

    def _concentracao_html(label, itens):
        if not itens:
            return ""
        rows = ""
        for c in itens:
            rows += f"""
            <tr style="border-bottom:1px solid #EEE;">
              <td style="padding:4px 10px;">{c['category_name']}</td>
              <td style="padding:4px 10px; text-align:right; font-weight:bold;">{_cfo_fmt(c['total'])}</td>
              <td style="padding:4px 10px; text-align:right;">{c['pct']}%</td>
            </tr>"""
        return f"""
        <div style="margin-bottom:14px;">
          <div style="font-size:13px; font-weight:bold; color:#555; margin-bottom:6px;">{label}</div>
          <table style="width:100%; border-collapse:collapse; font-size:12px;">
            <thead><tr style="background:#F0F4F8;">
              <th style="padding:5px 10px; text-align:left;">Categoria</th>
              <th style="padding:5px 10px; text-align:right;">Total</th>
              <th style="padding:5px 10px; text-align:right;">%</th>
            </tr></thead><tbody>{rows}</tbody>
          </table>
        </div>"""

    corpo = (_top5_html("Top 5 Maiores Despesas Pendentes", dados["top_despesas"], "#C0392B")
             + _top5_html("Top 5 Maiores Receitas Pendentes", dados["top_receitas"], "#1A7A3C",
                          marcar_vencidos=True)
             + _concentracao_html("Concentração Despesa por Categoria", dados["concentracao_despesa"])
             + _concentracao_html("Concentração Receita por Categoria", dados["concentracao_receita"]))

    return _cfo_section("Concentração de Risco", corpo, "⚠️")


def _html_cfo_kpis(dados: dict) -> str:
    """Seção 6 — KPIs."""
    def _kpi(label, valor, sufixo="", cor=None):
        c = cor or "#1A1A1A"
        return f"""
        <div style="flex:1; min-width:140px; background:#F9F9F9; border:1px solid #DDD;
                    border-radius:6px; padding:12px 14px; text-align:center;">
          <div style="font-size:11px; color:#777; margin-bottom:4px;">{label}</div>
          <div style="font-size:20px; font-weight:bold; color:{c};">{valor}{sufixo}</div>
        </div>"""

    cor_margem = "#1A7A3C" if dados["margem_operacional"] >= 5 else "#C0392B"
    cor_inad = "#1A7A3C" if dados["taxa_inadimplencia"] < 5 else "#C0392B"
    cor_runway = "#1A7A3C" if dados["runway_dias"] > 30 else "#C0392B"

    cards = (
        _kpi("Margem Operacional", f"{dados['margem_operacional']:+.1f}", "%", cor_margem)
        + _kpi("Prazo Médio Receb.", f"{dados['prazo_medio_recebimento']:.0f}", " dias")
        + _kpi("Prazo Médio Pagto.", f"{dados['prazo_medio_pagamento']:.0f}", " dias")
        + _kpi("Taxa Inadimplência", f"{dados['taxa_inadimplencia']:.1f}", "%", cor_inad)
        + _kpi("Burn Rate Diário", _cfo_fmt(dados["burn_rate_diario"]))
        + _kpi("Runway", f"{dados['runway_dias']}", " dias", cor_runway)
    )

    return _cfo_section(
        "KPIs Financeiros",
        f'<div style="display:flex; gap:10px; flex-wrap:wrap;">{cards}</div>',
        "📊"
    )


def _html_cfo_alertas(alertas: list) -> str:
    """Seção 7 — Alertas Automáticos."""
    if not alertas:
        return _cfo_section(
            "Alertas",
            '<div style="padding:10px; color:#1A7A3C; font-size:13px;">✅ Nenhum alerta no momento.</div>',
            "🔔"
        )

    itens = ""
    for a in alertas:
        cor = "#C0392B" if a["nivel"] == "critico" else "#C75B00" if a["nivel"] == "atencao" else "#555"
        icone = "🔴" if a["nivel"] == "critico" else "🟡" if a["nivel"] == "atencao" else "ℹ️"
        itens += f'<li style="padding:4px 0; color:{cor}; font-size:13px;">{icone} {a["msg"]}</li>'

    return _cfo_section(
        "Alertas",
        f'<ul style="margin:0; padding-left:20px;">{itens}</ul>',
        "🔔"
    )


def _gerar_alertas_cfo(inadimpl: dict, fluxo: dict, resumo: dict) -> list:
    """Gera lista de alertas automáticos baseados nos dados."""
    alertas = []

    # Receitas >30d vencidas
    aging = inadimpl["receitas_vencidas"]["aging"]
    total_30plus = aging.get("31-60d", 0) + aging.get("60+d", 0)
    if total_30plus > 0:
        alertas.append({"nivel": "critico",
                        "msg": f"Receitas vencidas há mais de 30 dias: {_cfo_fmt(total_30plus)}"})

    # Despesas vencidas
    if inadimpl["despesas_vencidas"]["total"] > 0:
        alertas.append({"nivel": "atencao",
                        "msg": f"Despesas em atraso: {_cfo_fmt(inadimpl['despesas_vencidas']['total'])} "
                               f"({inadimpl['despesas_vencidas']['qtd']} títulos)"})

    # Fluxo da semana negativo
    if fluxo["saldo_projetado"] < 0:
        alertas.append({"nivel": "atencao",
                        "msg": f"Fluxo de caixa negativo nos próximos 7 dias: "
                               f"{_cfo_fmt(fluxo['saldo_projetado'])}"})

    # Execução de receita baixa (< 30%)
    if resumo["pct_execucao_receita"] < 30 and date.today().day > 15:
        alertas.append({"nivel": "atencao",
                        "msg": f"Receita realizada está em apenas {resumo['pct_execucao_receita']}% "
                               f"do projetado (após dia 15)"})

    return alertas


def run_cfo_report(send_mail: bool = True):
    """Gera e envia o report executivo CFO diário."""
    hoje = date.today()
    ano, mes = hoje.year, hoje.month

    log.info(f"Gerando Report CFO — {MESES_PT[mes]}/{ano}...")
    db = FinancialDB()

    log.info("  Calculando posição de caixa...")
    posicao = db.get_posicao_caixa(ano, mes)

    log.info("  Calculando fluxo da semana...")
    fluxo = db.get_fluxo_semana()

    log.info("  Calculando inadimplência...")
    inadimpl = db.get_inadimplencia()

    log.info("  Calculando resumo do mês...")
    resumo_mes = db.get_resumo_mes(ano, mes)

    log.info("  Calculando concentração de risco...")
    concentracao = db.get_concentracao_risco(ano, mes)

    log.info("  Calculando KPIs...")
    kpis = db.get_kpis(ano, mes)

    log.info("  Gerando alertas...")
    alertas = _gerar_alertas_cfo(inadimpl, fluxo, resumo_mes)

    titulo = f"📊 Report CFO — {MESES_PT[mes]}/{ano} — {hoje.strftime('%d/%m/%Y')}"
    corpo = (
        _html_cfo_posicao(posicao, mes, ano)
        + _html_cfo_fluxo_semana(fluxo)
        + _html_cfo_inadimplencia(inadimpl)
        + _html_cfo_resumo_mes(resumo_mes, mes, ano)
        + _html_cfo_concentracao(concentracao)
        + _html_cfo_kpis(kpis)
        + _html_cfo_alertas(alertas)
    )
    html = _html_wrap(titulo, corpo)

    if send_mail:
        send_email(titulo, html)
    else:
        print(html)

    log.info(f"✓ Report CFO concluído — {len(alertas)} alerta(s)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import argparse

    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    if cmd == "chat":
        run_chat_terminal()

    elif cmd == "web":
        run_web()

    elif cmd == "insights":
        semanal  = "--semanal" in sys.argv
        no_email = "--no-email" in sys.argv
        run_insights(semanal=semanal, send_mail=not no_email)

    elif cmd == "anomalias":
        no_email = "--no-email" in sys.argv
        run_anomalias(send_mail=not no_email)

    elif cmd == "auditoria":
        no_email = "--no-email" in sys.argv
        ano = date.today().year
        for arg in sys.argv:
            if arg.startswith("--ano="):
                try:
                    ano = int(arg.split("=")[1])
                except ValueError:
                    print("❌ Formato inválido. Use --ano=2025")
                    sys.exit(1)
        run_auditoria(ano=ano, send_mail=not no_email)

    elif cmd == "variacoes":
        no_email  = "--no-email" in sys.argv
        threshold = 10.0
        for arg in sys.argv:
            if arg.startswith("--threshold="):
                try:
                    threshold = float(arg.split("=")[1])
                except ValueError:
                    pass
        run_daily_variations(send_mail=not no_email, variacao_min_pct=threshold)

    elif cmd == "cfo":
        no_email = "--no-email" in sys.argv
        run_cfo_report(send_mail=not no_email)

    else:
        print("""
Uso: python3 financial_ai.py [comando] [opções]

Comandos:
  chat                           Chat interativo no terminal
  web                            Interface web no browser (http://localhost:5050)
  insights                       Gera e envia insights diários por e-mail
  insights --semanal             Resumo semanal executivo (rodar às sextas)
  anomalias                      Detecta anomalias por Z-score e envia alerta
  variacoes                      Detecta variações diárias do mês e explica via IA
  variacoes --threshold=15       Threshold customizado (padrão: 10%)
  auditoria                      Auditoria de mudanças mês a mês (ano corrente)
  auditoria --ano=2025           Auditoria de um ano específico
  cfo                            Report executivo CFO — visão diária de caixa

Opções:
  --no-email                     Imprime no terminal em vez de enviar e-mail

Exemplos:
  python3 financial_ai.py auditoria --ano=2025 --no-email
  python3 financial_ai.py auditoria --ano=2026
  python3 financial_ai.py variacoes --no-email
  python3 financial_ai.py insights --semanal
  python3 financial_ai.py cfo --no-email

Agendamento sugerido (crontab -e):
  # Sync diário às 7h
  0 7 * * * cd /seu/projeto && source venv/bin/activate && python3 conta_azul_supabase.py

  # Insights diários às 7h30 (após sync)
  30 7 * * * cd /seu/projeto && source venv/bin/activate && python3 financial_ai.py insights

  # Variações diárias às 7h45
  45 7 * * * cd /seu/projeto && source venv/bin/activate && python3 financial_ai.py variacoes

  # Detecção de anomalias às 8h
  0 8 * * * cd /seu/projeto && source venv/bin/activate && python3 financial_ai.py anomalias

  # Resumo semanal às sextas 9h
  0 9 * * 5 cd /seu/projeto && source venv/bin/activate && python3 financial_ai.py insights --semanal
""")
