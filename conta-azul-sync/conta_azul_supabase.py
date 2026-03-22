"""
Conta Azul → Supabase Vector Integration
=========================================
Extrai receitas e despesas do ERP Conta Azul e armazena no Supabase Vector.

Comportamento:
  - Primeira execução: busca tudo desde 2024 (carga inicial)
  - Execuções seguintes: busca só o que foi alterado nas últimas 62h
  - Registros alterados: nova linha é inserida, histórico anterior é mantido
  - Baixas: consultadas para registros inseridos na execução atual

Requisitos:
    pip install requests supabase openai python-dotenv

Variáveis de ambiente (.env):
    CONTA_AZUL_CLIENT_ID
    CONTA_AZUL_CLIENT_SECRET
    CONTA_AZUL_REDIRECT_URI
    CONTA_AZUL_REFRESH_TOKEN
    SUPABASE_URL
    SUPABASE_KEY
    OPENAI_API_KEY
"""

import os
import json
import logging
import base64
from datetime import datetime, date, timedelta
from typing import Optional
from urllib.parse import urlencode

import time
import random

import requests
from dotenv import load_dotenv
from supabase import create_client, Client
from openai import OpenAI

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("conta_azul_sync.log")],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configurações
# ---------------------------------------------------------------------------

CONTA_AZUL_BASE_URL = "https://api-v2.contaazul.com"
AUTH_URL  = "https://auth.contaazul.com/oauth2/authorize"
TOKEN_URL = "https://auth.contaazul.com/oauth2/token"

CLIENT_ID      = os.getenv("CONTA_AZUL_CLIENT_ID")
CLIENT_SECRET  = os.getenv("CONTA_AZUL_CLIENT_SECRET")
REDIRECT_URI   = os.getenv("CONTA_AZUL_REDIRECT_URI")

SUPABASE_URL   = os.getenv("SUPABASE_URL")
SUPABASE_KEY   = os.getenv("SUPABASE_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Data de início para carga inicial
START_DATE = "2024-01-01"
PAGE_SIZE  = 100

# Arquivo de controle que indica se a carga inicial já foi feita
CONTROL_FILE = ".sync_initialized"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class ContaAzulAuth:

    def __init__(self):
        self.access_token: Optional[str] = None
        self.token_expiry: Optional[datetime] = None

    @staticmethod
    def _basic_header() -> dict:
        credentials = f"{CLIENT_ID}:{CLIENT_SECRET}"
        encoded = base64.b64encode(credentials.encode()).decode()
        return {
            "Authorization": f"Basic {encoded}",
            "Content-Type": "application/x-www-form-urlencoded",
        }

    @staticmethod
    def _save_refresh_token(new_token: str):
        """Salva o novo refresh_token diretamente no arquivo .env."""
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if not os.path.exists(env_path):
            log.warning(f"Arquivo .env não encontrado em {env_path}")
            return
        with open(env_path, "r") as f:
            lines = f.readlines()
        new_lines = []
        found = False
        for line in lines:
            if line.startswith("CONTA_AZUL_REFRESH_TOKEN="):
                new_lines.append(f"CONTA_AZUL_REFRESH_TOKEN={new_token}\n")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"\nCONTA_AZUL_REFRESH_TOKEN={new_token}\n")
        with open(env_path, "w") as f:
            f.writelines(new_lines)
        log.info("✓ refresh_token atualizado automaticamente no .env")

    @staticmethod
    def get_authorization_url() -> str:
        params = {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "state": "contaazul_sync",
            "scope": "openid profile aws.cognito.signin.user.admin",
        }
        return f"https://auth.contaazul.com/login?{urlencode(params)}"

    @classmethod
    def exchange_code_for_tokens(cls, authorization_code: str) -> dict:
        resp = requests.post(
            TOKEN_URL,
            headers=cls._basic_header(),
            data={
                "grant_type": "authorization_code",
                "code": authorization_code,
                "redirect_uri": REDIRECT_URI,
            },
            timeout=30,
        )
        resp.raise_for_status()
        tokens = resp.json()
        print("\n✓ Tokens obtidos com sucesso!")
        print(f"\nAdicione no .env:\nCONTA_AZUL_REFRESH_TOKEN={tokens.get('refresh_token')}\n")
        return tokens

    def refresh_access_token(self) -> str:
        refresh = os.getenv("CONTA_AZUL_REFRESH_TOKEN")
        resp = requests.post(
            TOKEN_URL,
            headers=self._basic_header(),
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        self.access_token = data["access_token"]
        expires_in = data.get("expires_in", 3600)
        self.token_expiry = datetime.now() + timedelta(seconds=expires_in - 60)
        log.info("✓ Access token renovado.")
        if "refresh_token" in data:
            new_refresh = data["refresh_token"]
            os.environ["CONTA_AZUL_REFRESH_TOKEN"] = new_refresh
            self._save_refresh_token(new_refresh)
        return self.access_token

    def get_token(self) -> str:
        if not self.access_token or datetime.now() >= self.token_expiry:
            self.refresh_access_token()
        return self.access_token

    def headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.get_token()}",
            "Content-Type": "application/json",
        }


# ---------------------------------------------------------------------------
# API Client
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Rate limit: 600 chamadas/min = 10/s — controlamos para ficar abaixo
# ---------------------------------------------------------------------------
RATE_LIMIT_DELAY   = 0.12   # 120ms entre chamadas (~8/s, seguro abaixo de 10/s)
MAX_RETRIES        = 5      # Tentativas máximas em caso de 429
BACKOFF_BASE       = 2      # Base do backoff exponencial (segundos)


class ContaAzulClient:
    def __init__(self, auth: ContaAzulAuth):
        self.auth = auth
        self._last_request_time = 0.0

    def _throttle(self):
        """Garante intervalo mínimo entre requisições para respeitar rate limit."""
        elapsed = time.time() - self._last_request_time
        if elapsed < RATE_LIMIT_DELAY:
            time.sleep(RATE_LIMIT_DELAY - elapsed)
        self._last_request_time = time.time()

    def _get(self, path: str, params: dict = None) -> dict:
        """GET com retry exponencial em caso de 429 ou erros transitórios."""
        for attempt in range(1, MAX_RETRIES + 1):
            self._throttle()
            try:
                resp = requests.get(
                    f"{CONTA_AZUL_BASE_URL}{path}",
                    headers=self.auth.headers(),
                    params=params,
                    timeout=30,
                )

                # Respeita Retry-After se vier no header
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 0))
                    wait = retry_after if retry_after > 0 else BACKOFF_BASE ** attempt + random.uniform(0, 1)
                    log.warning(f"429 Too Many Requests — aguardando {wait:.1f}s (tentativa {attempt}/{MAX_RETRIES})")
                    time.sleep(wait)
                    continue

                resp.raise_for_status()
                return resp.json()

            except requests.HTTPError as e:
                # Erros 5xx são transitórios — tenta novamente
                if e.response.status_code >= 500 and attempt < MAX_RETRIES:
                    wait = BACKOFF_BASE ** attempt + random.uniform(0, 1)
                    log.warning(f"Erro {e.response.status_code} — aguardando {wait:.1f}s (tentativa {attempt}/{MAX_RETRIES})")
                    time.sleep(wait)
                    continue
                raise

            except requests.ConnectionError as e:
                if attempt < MAX_RETRIES:
                    wait = BACKOFF_BASE ** attempt
                    log.warning(f"Erro de conexão — aguardando {wait:.1f}s (tentativa {attempt}/{MAX_RETRIES}): {e}")
                    time.sleep(wait)
                    continue
                raise

        raise RuntimeError(f"Falha após {MAX_RETRIES} tentativas: GET {path}")

    def _fetch_all(self, path: str, params: dict) -> list[dict]:
        """Busca todas as páginas de um endpoint paginado."""
        records, pagina = [], 1
        while True:
            params["pagina"] = pagina
            params["tamanho_pagina"] = PAGE_SIZE
            try:
                data = self._get(path, params)
            except requests.HTTPError as e:
                log.error(f"Erro ao buscar {path} página {pagina}: {e}")
                break

            itens = data.get("itens", [])
            if not itens:
                break
            records.extend(itens)
            log.info(f"{path} — página {pagina}: {len(itens)} registros")

            total = data.get("itens_totais", 0)
            if pagina * PAGE_SIZE >= total:
                break
            pagina += 1

        return records

    def fetch_despesas(self, start_date: str, end_date: str,
                       alteracao_de: str = None, alteracao_ate: str = None) -> list[dict]:
        """
        Busca despesas por vencimento ou por data de alteração.
        Se alteracao_de/ate forem informados, usa filtro de alteração (modo diário).
        """
        params = {}
        if alteracao_de and alteracao_ate:
            params["data_alteracao_de"] = alteracao_de
            params["data_alteracao_ate"] = alteracao_ate
            # Filtra por vencimento mínimo para evitar registros pré-2024.
            # Usa data máxima distante para não excluir registros com vencimento futuro.
            params["data_vencimento_de"] = START_DATE
            params["data_vencimento_ate"] = "2099-12-31"
        else:
            params["data_vencimento_de"] = start_date
            params["data_vencimento_ate"] = end_date

        return self._fetch_all(
            "/v1/financeiro/eventos-financeiros/contas-a-pagar/buscar", params
        )

    def fetch_receitas(self, start_date: str, end_date: str,
                       alteracao_de: str = None, alteracao_ate: str = None) -> list[dict]:
        """
        Busca receitas por vencimento ou por data de alteração.
        """
        params = {}
        if alteracao_de and alteracao_ate:
            params["data_alteracao_de"] = alteracao_de
            params["data_alteracao_ate"] = alteracao_ate
            # Filtra por vencimento mínimo para evitar registros pré-2024.
            # Usa data máxima distante para não excluir registros com vencimento futuro.
            params["data_vencimento_de"] = START_DATE
            params["data_vencimento_ate"] = "2099-12-31"
        else:
            params["data_vencimento_de"] = start_date
            params["data_vencimento_ate"] = end_date

        return self._fetch_all(
            "/v1/financeiro/eventos-financeiros/contas-a-receber/buscar", params
        )

    def fetch_baixas_da_parcela(self, parcela_id: str) -> list[dict]:
        """
        GET /v1/financeiro/eventos-financeiros/parcelas/{parcela_id}/baixa
        Retorna lista de baixas com data_pagamento e conta_financeira.
        """
        try:
            data = self._get(
                f"/v1/financeiro/eventos-financeiros/parcelas/{parcela_id}/baixa"
            )
            return data if isinstance(data, list) else []
        except requests.HTTPError as e:
            if e.response.status_code == 404:
                return []
            log.warning(f"Erro ao buscar baixas da parcela {parcela_id}: {e}")
            return []


# ---------------------------------------------------------------------------
# Controle de inicialização
# ---------------------------------------------------------------------------

def is_initialized() -> bool:
    """Verifica se a carga inicial já foi executada."""
    return os.path.exists(CONTROL_FILE)


def mark_initialized():
    """Cria arquivo de controle indicando que a carga inicial foi concluída."""
    with open(CONTROL_FILE, "w") as f:
        f.write(datetime.now().isoformat())
    log.info("✓ Carga inicial marcada como concluída.")


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------

class EmbeddingService:
    def __init__(self):
        self.client = OpenAI(api_key=OPENAI_API_KEY)

    def generate(self, text: str) -> list[float]:
        resp = self.client.embeddings.create(
            model="text-embedding-3-small",
            input=text[:8000],
        )
        return resp.data[0].embedding


def build_embedding_text(record: dict, tipo: str) -> str:
    categorias = record.get("categorias") or []
    cat_nome = categorias[0].get("nome", "") if categorias else ""
    conta = record.get("conta_financeira") or {}
    fornecedor = record.get("fornecedor") or record.get("cliente") or {}

    parts = [
        f"Tipo: {tipo}",
        f"Descrição: {record.get('descricao', '')}",
        f"Valor: {record.get('total', record.get('valor_bruto', ''))}",
        f"Vencimento: {record.get('data_vencimento', '')}",
        f"Competência: {record.get('data_competencia', '')}",
        f"Status: {record.get('status', '')}",
        f"Categoria: {cat_nome}",
        f"Conta: {conta.get('nome', '') if isinstance(conta, dict) else ''}",
        f"Fornecedor/Cliente: {fornecedor.get('nome', '') if isinstance(fornecedor, dict) else ''}",
    ]
    return " | ".join(p for p in parts if p.split(": ", 1)[1])


# ---------------------------------------------------------------------------
# SQL Schema
# ---------------------------------------------------------------------------

SUPABASE_SCHEMA_SQL = """
-- ============================================================
-- Pré-requisito: extensão pgvector
-- ============================================================
CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================
-- Limpar objetos existentes (para recriar do zero)
-- ============================================================
DROP TABLE IF EXISTS financial_transactions CASCADE;
DROP VIEW  IF EXISTS financial_transactions_latest CASCADE;
DROP FUNCTION IF EXISTS search_transactions CASCADE;

-- ============================================================
-- Tabela principal
-- ============================================================
CREATE TABLE financial_transactions (
    -- Chave primária
    id                  BIGSERIAL PRIMARY KEY,

    -- Controle de extração
    extraction_date     DATE        NOT NULL DEFAULT CURRENT_DATE,
    is_initial_load     BOOLEAN     NOT NULL DEFAULT FALSE,  -- TRUE somente na carga inicial

    -- Identificação
    tipo                TEXT        NOT NULL CHECK (tipo IN ('receita', 'despesa')),
    installment_id      TEXT        NOT NULL,  -- ID original da parcela no Conta Azul

    -- Dados brutos (JSON completo retornado pela API)
    raw_data            JSONB       NOT NULL,

    -- Campos normalizados para consulta
    description         TEXT,                  -- descricao
    value               NUMERIC,               -- total (campo principal da API)
    due_date            DATE,                  -- data_vencimento
    competence_date     DATE,                  -- data_competencia
    status              TEXT,                  -- status (ACQUITTED, PENDING, etc.)
    category_name       TEXT,                  -- categorias[0].nome (exceto impostos retidos)
    bank_account_name   TEXT,                  -- conta_financeira.nome (atualizado pela baixa)
    payee_name          TEXT,                  -- fornecedor.nome ou cliente.nome

    -- Dados de pagamento (preenchidos ao encontrar baixa)
    payment_date        DATE,                  -- data_pagamento da baixa
    baixa_data          JSONB,                 -- JSON completo das baixas

    -- Embedding vetorial para busca semântica com IA
    embedding           VECTOR(1536),          -- text-embedding-3-small (OpenAI)

    -- Auditoria
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- Índices
-- ============================================================

-- Busca vetorial (HNSW — melhor performance para pgvector)
CREATE INDEX idx_financial_embedding
    ON financial_transactions USING hnsw (embedding vector_cosine_ops);

-- Filtros comuns
CREATE INDEX idx_financial_extraction_date
    ON financial_transactions (extraction_date);
CREATE INDEX idx_financial_tipo
    ON financial_transactions (tipo);
CREATE INDEX idx_financial_installment
    ON financial_transactions (installment_id);

-- Busca da versão mais recente por parcela
CREATE INDEX idx_financial_installment_date
    ON financial_transactions (installment_id, extraction_date DESC, created_at DESC);

-- Filtros por período e status (úteis para dashboards)
CREATE INDEX idx_financial_due_date
    ON financial_transactions (due_date);
CREATE INDEX idx_financial_status
    ON financial_transactions (status);
CREATE INDEX idx_financial_payment_date
    ON financial_transactions (payment_date);

-- ============================================================
-- View: versão mais recente de cada parcela
-- Usar esta view em dashboards e relatórios
-- ============================================================
CREATE OR REPLACE VIEW financial_transactions_latest
WITH (security_invoker = true) AS
SELECT DISTINCT ON (installment_id)
    id, extraction_date, is_initial_load, tipo, installment_id,
    raw_data, description, value, due_date, competence_date,
    status, category_name, bank_account_name, payee_name,
    payment_date, baixa_data, created_at
FROM financial_transactions
ORDER BY installment_id, extraction_date DESC, created_at DESC;

-- ============================================================
-- Tabela: snapshot diário de registros ativos
-- Populada após cada sync — base para detectar removidos
-- Cada linha = estado de um registro em um dia específico
-- ============================================================
CREATE TABLE IF NOT EXISTS daily_snapshot (
    snapshot_date   DATE    NOT NULL,
    installment_id  TEXT    NOT NULL,
    tipo            TEXT    NOT NULL,
    description     TEXT,
    value           NUMERIC,
    status          TEXT,
    category_name   TEXT,
    payee_name      TEXT,
    due_date        DATE,
    PRIMARY KEY (snapshot_date, installment_id)
);

CREATE INDEX IF NOT EXISTS idx_snapshot_date
    ON daily_snapshot (snapshot_date);
CREATE INDEX IF NOT EXISTS idx_snapshot_installment
    ON daily_snapshot (installment_id);
CREATE INDEX IF NOT EXISTS idx_snapshot_due_date
    ON daily_snapshot (due_date);

-- ============================================================
-- Tabela: histórico de execuções de sync
-- Criada automaticamente ao rodar o schema — base para a
-- janela dinâmica de alteracao_de/ate de cada sync
-- ============================================================
CREATE TABLE IF NOT EXISTS sync_log (
    id               BIGSERIAL PRIMARY KEY,
    sync_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    alteracao_de     TEXT,         -- NULL na carga inicial
    alteracao_ate    TEXT,         -- NULL na carga inicial
    vencimento_de    TEXT NOT NULL,
    vencimento_ate   TEXT NOT NULL,
    despesas_found   INT,
    receitas_found   INT,
    records_inserted INT,
    status           TEXT NOT NULL DEFAULT 'ok',  -- 'ok' | 'error'
    error_message    TEXT
);

CREATE INDEX IF NOT EXISTS idx_sync_log_at
    ON sync_log (sync_at DESC);

-- ============================================================
-- View: diff entre versões consecutivas de cada parcela
-- Usada para detecção de anomalias e auditoria de mudanças
-- ============================================================
CREATE OR REPLACE VIEW financial_transactions_diff
WITH (security_invoker = true) AS
SELECT
    curr.installment_id,
    curr.tipo,
    curr.description,
    curr.payee_name,
    curr.due_date,

    -- Datas das versões
    prev.extraction_date                              AS data_anterior,
    curr.extraction_date                              AS data_atual,

    -- Variação de valor
    prev.value                                        AS valor_anterior,
    curr.value                                        AS valor_atual,
    ROUND(((curr.value - prev.value)
        / NULLIF(prev.value, 0)) * 100, 2)            AS variacao_pct,

    -- Mudança de status
    prev.status                                       AS status_anterior,
    curr.status                                       AS status_atual,
    curr.status != prev.status                        AS status_mudou,

    -- Mudança de categoria
    prev.category_name                                AS categoria_anterior,
    curr.category_name                                AS categoria_atual,
    curr.category_name != prev.category_name          AS categoria_mudou,

    -- Pagamento
    curr.payment_date,
    curr.payment_date - curr.due_date                 AS dias_para_pagamento

FROM financial_transactions curr
JOIN financial_transactions prev
  ON curr.installment_id = prev.installment_id
 AND prev.extraction_date = (
     SELECT MAX(t2.extraction_date)
     FROM financial_transactions t2
     WHERE t2.installment_id = curr.installment_id
       AND t2.extraction_date < curr.extraction_date
 );

-- ============================================================
-- View: resumo financeiro diário do mês corrente
-- Mostra total de receitas e despesas por dia com variação
-- vs dia anterior — base para o dashboard analítico diário
-- ============================================================
CREATE OR REPLACE VIEW daily_financial_summary
WITH (security_invoker = true) AS
WITH daily AS (
    SELECT
        due_date                                      AS dia,
        tipo,
        COUNT(*)                                      AS qtd_registros,
        SUM(value)                                    AS total
    FROM financial_transactions_latest
    WHERE due_date >= DATE_TRUNC('month', CURRENT_DATE)
      AND due_date <= CURRENT_DATE
    GROUP BY due_date, tipo
)
SELECT
    dia,
    tipo,
    qtd_registros,
    ROUND(total::NUMERIC, 2)                          AS total,

    -- Total do dia anterior (mesmo tipo)
    ROUND(LAG(total) OVER (
        PARTITION BY tipo ORDER BY dia
    )::NUMERIC, 2)                                    AS total_dia_anterior,

    -- Variação absoluta
    ROUND((total - LAG(total) OVER (
        PARTITION BY tipo ORDER BY dia
    ))::NUMERIC, 2)                                   AS variacao_valor,

    -- Variação percentual
    ROUND(((total - LAG(total) OVER (
        PARTITION BY tipo ORDER BY dia
    )) / NULLIF(LAG(total) OVER (
        PARTITION BY tipo ORDER BY dia
    ), 0) * 100)::NUMERIC, 2)                         AS variacao_pct

FROM daily
ORDER BY dia DESC, tipo;

-- ============================================================
-- Função: busca semântica com pgvector
-- ============================================================
CREATE OR REPLACE FUNCTION search_transactions(
    query_embedding VECTOR(1536),
    tipo_filter     TEXT    DEFAULT NULL,
    match_count     INT     DEFAULT 10,
    apenas_latest   BOOLEAN DEFAULT TRUE
)
RETURNS TABLE (
    id                BIGINT,
    extraction_date   DATE,
    tipo              TEXT,
    description       TEXT,
    value             NUMERIC,
    due_date          DATE,
    competence_date   DATE,
    status            TEXT,
    category_name     TEXT,
    bank_account_name TEXT,
    payee_name        TEXT,
    payment_date      DATE,
    similarity        FLOAT
)
LANGUAGE sql STABLE AS $$
    SELECT
        id, extraction_date, tipo, description, value, due_date,
        competence_date, status, category_name, bank_account_name,
        payee_name, payment_date,
        1 - (embedding <=> query_embedding) AS similarity
    FROM (
        SELECT DISTINCT ON (installment_id) *
        FROM financial_transactions
        ORDER BY installment_id, extraction_date DESC, created_at DESC
    ) latest
    WHERE (tipo_filter IS NULL OR tipo = tipo_filter)
    ORDER BY embedding <=> query_embedding
    LIMIT match_count;
$$;
"""


# ---------------------------------------------------------------------------
# Supabase Store
# ---------------------------------------------------------------------------

class SupabaseStore:
    def __init__(self):
        self.client: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        self.table = "financial_transactions"

    def insert(self, row: dict):
        return self.client.table(self.table).insert(row).execute()

    def installment_exists(self, installment_id: str) -> bool:
        """Verifica se já existe algum registro para esse installment_id."""
        resp = (
            self.client.table(self.table)
            .select("id")
            .eq("installment_id", installment_id)
            .limit(1)
            .execute()
        )
        return len(resp.data) > 0

    def populate_snapshot(self, active_ids: set | None = None):
        """
        Popula o snapshot do dia de hoje com o estado atual de todos
        os registros ativos (financial_transactions_latest).
        Chamado ao final de cada sync — upsert para ser idempotente.

        active_ids: conjunto de installment_ids retornados pela API no sync atual.
                    Quando fornecido (carga inicial / full sync), apenas esses IDs
                    são incluídos no snapshot — registros deletados do Conta Azul
                    são automaticamente excluídos.
                    Se None (sync incremental), inclui todos os registros da view
                    (deleções não detectáveis sem uma varredura completa).
        """
        today = date.today().isoformat()
        log.info(f"Populando snapshot do dia {today}...")

        # Busca todos os registros na versão mais recente (paginado)
        registros = []
        _page_size = 1000
        _offset = 0
        while True:
            resp = (
                self.client.table("financial_transactions_latest")
                .select("installment_id,tipo,description,value,status,"
                        "category_name,payee_name,due_date")
                .range(_offset, _offset + _page_size - 1)
                .execute()
            )
            _batch = resp.data or []
            registros.extend(_batch)
            if len(_batch) < _page_size:
                break
            _offset += _page_size

        if not registros:
            log.info("Nenhum registro encontrado para snapshot.")
            return

        # Quando active_ids é fornecido (full sync), restringe ao que a API
        # retornou — registros ausentes foram deletados no Conta Azul
        if active_ids is not None:
            antes = len(registros)
            registros = [r for r in registros if r["installment_id"] in active_ids]
            removidos = antes - len(registros)
            if removidos:
                log.info(f"Snapshot: {removidos} registro(s) excluído(s) do snapshot "
                         f"(deletados no Conta Azul e ausentes do full sync)")

        # Monta linhas do snapshot
        linhas = [
            {
                "snapshot_date":  today,
                "installment_id": r["installment_id"],
                "tipo":           r["tipo"],
                "description":    r.get("description"),
                "value":          r.get("value"),
                "status":         r.get("status"),
                "category_name":  r.get("category_name"),
                "payee_name":     r.get("payee_name"),
                "due_date":       r.get("due_date"),
            }
            for r in registros
        ]

        # Upsert em lotes de 500 (limite do Supabase por request)
        BATCH = 500
        total = 0
        for i in range(0, len(linhas), BATCH):
            lote = linhas[i:i + BATCH]
            self.client.table("daily_snapshot").upsert(
                lote,
                on_conflict="snapshot_date,installment_id"
            ).execute()
            total += len(lote)

        log.info(f"✓ Snapshot populado: {total} registros para {today}")

    def get_latest_version(self, installment_id: str) -> dict | None:
        """Retorna os campos comparáveis da versão mais recente de um registro."""
        resp = (
            self.client.table(self.table)
            .select("value,status,category_name,payee_name,payment_date,bank_account_name")
            .eq("installment_id", installment_id)
            .order("extraction_date", desc=True)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else None

    def get_last_sync(self) -> dict | None:
        """
        Retorna o último sync bem-sucedido que teve janela de alteração
        (ou seja, não é carga inicial). Usado para calcular o alteracao_de
        do próximo sync.
        """
        resp = (
            self.client.table("sync_log")
            .select("alteracao_de,alteracao_ate,sync_at")
            .eq("status", "ok")
            .not_.is_("alteracao_ate", "null")
            .order("sync_at", desc=True)
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else None

    def record_sync(self, params: dict):
        """Registra uma execução de sync na tabela sync_log."""
        self.client.table("sync_log").insert(params).execute()

    def update_baixa(self, row_id: int, payment_date: str,
                     baixa_data: dict, bank_account_name: str = None):
        update_fields = {
            "payment_date": payment_date,
            "baixa_data": json.dumps(baixa_data, ensure_ascii=False),
        }
        if bank_account_name:
            update_fields["bank_account_name"] = bank_account_name
        self.client.table(self.table).update(update_fields).eq("id", row_id).execute()


# ---------------------------------------------------------------------------
# Normalização
# ---------------------------------------------------------------------------

CATEGORIA_IGNORADA = "Impostos retidos em vendas"


def extract_categoria_nome(record: dict) -> str:
    categorias = record.get("categorias") or []
    if categorias and isinstance(categorias, list):
        validas = [c for c in categorias if c.get("nome", "").strip() != CATEGORIA_IGNORADA]
        escolhida = validas[0] if validas else categorias[0]
        nome = escolhida.get("nome", "")
        if nome:
            return nome

    rateio = record.get("rateio") or []
    if rateio and isinstance(rateio, list):
        cat = rateio[0].get("categoria") or {}
        nome = cat.get("nome", "") if isinstance(cat, dict) else ""
        if nome:
            return nome

    cat = record.get("categoria") or {}
    if isinstance(cat, dict):
        return cat.get("nome", cat.get("name", ""))

    return ""


def extract_valor(record: dict):
    if record.get("total") is not None:
        return record["total"]
    composicao = record.get("valor_composicao") or {}
    if isinstance(composicao, dict) and composicao.get("valor_bruto") is not None:
        return composicao["valor_bruto"]
    return record.get("valor_bruto") or record.get("valor") or record.get("value")


def normalize_record(record: dict, tipo: str, is_initial_load: bool = False) -> dict:
    conta      = record.get("conta_financeira") or {}
    contato    = record.get("contato") or {}
    fornecedor = record.get("fornecedor") or record.get("cliente") or contato

    baixas = record.get("baixas", [])
    payment_date = None
    if baixas and isinstance(baixas, list):
        ultima_baixa = baixas[-1]
        payment_date = ultima_baixa.get("data_pagamento")
        if payment_date:
            payment_date = payment_date[:10]

    return {
        "extraction_date":   date.today().isoformat(),
        "is_initial_load":   is_initial_load,
        "tipo":              tipo,
        "installment_id":    str(record.get("id", "")),
        "raw_data":          json.dumps(record, ensure_ascii=False),
        "description":       record.get("descricao") or record.get("description") or "",
        "value":             extract_valor(record),
        "due_date":          record.get("data_vencimento") or record.get("dueDate"),
        "competence_date":   record.get("data_competencia") or record.get("competenceDate"),
        "status":            record.get("status", ""),
        "category_name":     extract_categoria_nome(record),
        "bank_account_name": conta.get("nome", conta.get("name", "")) if isinstance(conta, dict) else "",
        "payee_name":        fornecedor.get("nome", fornecedor.get("name", "")) if isinstance(fornecedor, dict) else "",
        "payment_date":      payment_date,
        "baixa_data":        json.dumps(baixas, ensure_ascii=False) if baixas else None,
    }


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def _registro_mudou(novo: dict, anterior: dict) -> bool:
    """
    Compara os campos relevantes entre a nova versão (já enriquecida com baixas)
    e a última versão salva no banco.
    Retorna True apenas se algo realmente mudou.
    """
    # Campos diretos da API do Conta Azul
    campos_simples = ["value", "status", "category_name", "payee_name",
                      "payment_date", "bank_account_name"]
    for campo in campos_simples:
        val_novo = novo.get(campo)
        val_ant  = anterior.get(campo)
        # Normaliza None e string vazia
        val_novo = None if val_novo == "" else val_novo
        val_ant  = None if val_ant  == "" else val_ant
        # Compara valor numérico com tolerância
        if campo == "value" and val_novo is not None and val_ant is not None:
            if abs(float(val_novo) - float(val_ant)) > 0.01:
                return True
            continue
        if val_novo != val_ant:
            return True
    return False


def _enriquecer_com_baixa(row: dict, ca: "ContaAzulClient") -> dict:
    """
    Consulta a API de baixas e preenche payment_date e bank_account_name
    diretamente no row antes da comparação e inserção.
    Retorna o row enriquecido (modifica in-place também).
    """
    baixas = ca.fetch_baixas_da_parcela(row["installment_id"])
    if not baixas:
        return row

    ultima = baixas[-1]
    payment_date = ultima.get("data_pagamento")
    if payment_date:
        row["payment_date"] = payment_date[:10]

    conta_fin = ultima.get("conta_financeira")
    if isinstance(conta_fin, dict):
        bank = conta_fin.get("banco") or conta_fin.get("nome")
        if bank:
            row["bank_account_name"] = bank

    row["baixa_data"] = json.dumps(baixas, ensure_ascii=False)
    return row


def process_and_insert(records: list[dict], tipo: str, store: SupabaseStore,
                       emb: EmbeddingService, is_initial_load: bool,
                       ca: "ContaAzulClient" = None) -> list[dict]:
    """
    Insere registros no Supabase.

    Carga inicial:
      - Enriquece com baixas antes de inserir
      - Insere todos

    Sync diário:
      - Enriquece com baixas ANTES de comparar (payment_date é fonte da verdade)
      - Compara com última versão no banco (value, status, category, payee, payment_date, bank)
      - Só insere nova versão se algo realmente mudou
    """
    inserted  = []
    ignorados = 0

    for rec in records:
        row            = normalize_record(rec, tipo, is_initial_load)
        installment_id = row["installment_id"]

        # ── Enriquece com baixas antes de qualquer comparação ─────────────
        if ca:
            row = _enriquecer_com_baixa(row, ca)

        # ── Sync diário: só insere se algo mudou de verdade ───────────────
        if not is_initial_load:
            anterior = store.get_latest_version(installment_id)
            if anterior and not _registro_mudou(row, anterior):
                ignorados += 1
                continue

        # ── Gera embedding e insere ───────────────────────────────────────
        text = build_embedding_text(rec, tipo)
        try:
            row["embedding"] = emb.generate(text)
        except Exception as e:
            log.warning(f"Embedding falhou {tipo} {rec.get('id')}: {e}")
            row["embedding"] = None

        try:
            result = store.insert(row)
            if result.data:
                inserted.append({
                    "row_id":         result.data[0]["id"],
                    "installment_id": installment_id,
                    "tipo":           tipo,
                    "tem_baixa":      row["payment_date"] is not None,
                })
        except Exception as e:
            log.error(f"Insert falhou {tipo} {rec.get('id')}: {e}")

    if ignorados:
        log.info(f"  {ignorados} registro(s) ignorados — sem mudança real vs última versão")

    return inserted


def _processar_baixa(item: dict, ca: ContaAzulClient, store: SupabaseStore) -> bool:
    """Consulta e atualiza baixa de um registro. Retorna True se encontrou baixa."""
    if not item["installment_id"]:
        return False

    baixas = ca.fetch_baixas_da_parcela(item["installment_id"])
    if not baixas:
        return False

    ultima = baixas[-1]
    payment_date = ultima.get("data_pagamento")
    if payment_date:
        payment_date = payment_date[:10]

    # Extrai banco da conta financeira da baixa
    bank_account_name = None
    conta_fin = ultima.get("conta_financeira")
    if isinstance(conta_fin, dict):
        bank_account_name = conta_fin.get("banco") or conta_fin.get("nome")
    if not bank_account_name and isinstance(conta_fin, str):
        bank_account_name = ultima.get("banco")

    try:
        store.update_baixa(item["row_id"], payment_date,
                           {"baixas": baixas}, bank_account_name)
        log.debug(
            f"Baixa: parcela {item['installment_id']} → "
            f"{payment_date} | banco: {bank_account_name}"
        )
        return True
    except Exception as e:
        log.error(f"Update baixa falhou {item['installment_id']}: {e}")
        return False


def consultar_e_atualizar_baixas(inserted_ids: list[dict], ca: ContaAzulClient,
                                  store: SupabaseStore, is_initial_load: bool = False):
    """
    Consulta e atualiza baixas.

    - Carga inicial (is_initial_load=True):
        Consulta TODOS os registros inseridos — pois qualquer um pode já ter sido pago.

    - Sync diário (is_initial_load=False):
        Consulta apenas registros que vieram sem baixa da API nessa execução,
        pois foram alterados recentemente e podem ter recebido pagamento.
    """
    if is_initial_load:
        # Carga inicial: consulta todos via API de baixas
        alvo = inserted_ids
        log.info(f"Carga inicial — consultando baixas para TODOS os {len(alvo)} registros...")
    else:
        # Sync diário: apenas os que foram atualizados e ainda não têm baixa
        alvo = [i for i in inserted_ids if not i["tem_baixa"]]
        log.info(f"Sync diário — consultando baixas para {len(alvo)} registros atualizados sem pagamento...")

    baixas_found = sum(
        _processar_baixa(item, ca, store)
        for item in alvo
    )
    log.info(f"✓ Baixas atualizadas: {baixas_found}/{len(alvo)}")


def run_sync(force_full: bool = False, start_date: str = START_DATE,
             end_date: str = None):
    if end_date is None:
        end_date = date.today().isoformat()

    auth  = ContaAzulAuth()
    ca    = ContaAzulClient(auth)
    emb   = EmbeddingService()
    store = SupabaseStore()

    # ------------------------------------------------------------------
    # Decide modo: carga inicial ou atualização diária
    # ------------------------------------------------------------------
    if force_full or not is_initialized():
        # ── CARGA INICIAL ──────────────────────────────────────────────
        log.info("=" * 60)
        log.info("MODO: Carga inicial completa")
        log.info(f"Período: {start_date} → {end_date}")
        log.info("=" * 60)

        log.info(f"Buscando despesas de {start_date} até {end_date}...")
        despesas = ca.fetch_despesas(start_date, end_date)
        log.info(f"Total despesas: {len(despesas)}")

        log.info(f"Buscando receitas de {start_date} até {end_date}...")
        receitas = ca.fetch_receitas(start_date, end_date)
        log.info(f"Total receitas: {len(receitas)}")

        inserted = []
        inserted += process_and_insert(despesas, "despesa", store, emb, is_initial_load=True, ca=ca)
        inserted += process_and_insert(receitas, "receita", store, emb, is_initial_load=True, ca=ca)

        log.info(f"✓ {len(inserted)} registros inseridos na carga inicial")
        # Coleta IDs retornados pela API para detectar deleções no snapshot
        api_ids = {str(r["id"]) for r in despesas + receitas if r.get("id")}
        store.populate_snapshot(active_ids=api_ids)
        store.record_sync({
            "alteracao_de":     None,
            "alteracao_ate":    None,
            "vencimento_de":    start_date,
            "vencimento_ate":   end_date,
            "despesas_found":   len(despesas),
            "receitas_found":   len(receitas),
            "records_inserted": len(inserted),
            "status":           "ok",
        })
        mark_initialized()

    else:
        # ── ATUALIZAÇÃO DIÁRIA ─────────────────────────────────────────
        agora         = datetime.now()
        alteracao_ate = agora.strftime("%Y-%m-%dT%H:%M:%S")

        # Janela dinâmica: começa 1 segundo após o fim do último sync bem-sucedido.
        # Garante cobertura contínua sem gaps, independente do intervalo entre syncs.
        ultimo_sync = store.get_last_sync()
        if ultimo_sync:
            ultimo_ate   = datetime.strptime(ultimo_sync["alteracao_ate"], "%Y-%m-%dT%H:%M:%S")
            alteracao_de = (ultimo_ate + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S")
            log.info(f"Último sync registrado em: {ultimo_sync['sync_at']}")
            log.info(f"Janela dinâmica: {alteracao_de} → {alteracao_ate}")
        else:
            alteracao_de = (agora - timedelta(hours=62)).strftime("%Y-%m-%dT%H:%M:%S")
            log.info(f"Sem histórico de sync — fallback 62h: {alteracao_de} → {alteracao_ate}")

        log.info("=" * 60)
        log.info("MODO: Atualização diária (apenas alterações recentes)")
        log.info(f"Janela: {alteracao_de} → {alteracao_ate}")
        log.info("=" * 60)

        log.info("Buscando despesas alteradas...")
        despesas = ca.fetch_despesas(
            START_DATE, end_date,
            alteracao_de=alteracao_de, alteracao_ate=alteracao_ate
        )
        log.info(f"Despesas alteradas: {len(despesas)}")

        log.info("Buscando receitas alteradas...")
        receitas = ca.fetch_receitas(
            START_DATE, end_date,
            alteracao_de=alteracao_de, alteracao_ate=alteracao_ate
        )
        log.info(f"Receitas alteradas: {len(receitas)}")

        if not despesas and not receitas:
            log.info("Nenhuma alteração encontrada. Nada a fazer.")
            store.record_sync({
                "alteracao_de":     alteracao_de,
                "alteracao_ate":    alteracao_ate,
                "vencimento_de":    START_DATE,
                "vencimento_ate":   "2099-12-31",
                "despesas_found":   0,
                "receitas_found":   0,
                "records_inserted": 0,
                "status":           "ok",
            })
            return

        # Baixas buscadas ANTES da comparação dentro de process_and_insert
        inserted = []
        inserted += process_and_insert(despesas, "despesa", store, emb, is_initial_load=False, ca=ca)
        inserted += process_and_insert(receitas, "receita", store, emb, is_initial_load=False, ca=ca)

        log.info(f"✓ {len(inserted)} registros inseridos (novas versões)")
        store.populate_snapshot()
        store.record_sync({
            "alteracao_de":     alteracao_de,
            "alteracao_ate":    alteracao_ate,
            "vencimento_de":    START_DATE,
            "vencimento_ate":   "2099-12-31",
            "despesas_found":   len(despesas),
            "receitas_found":   len(receitas),
            "records_inserted": len(inserted),
            "status":           "ok",
        })

    log.info("✓ Sincronização concluída!")


# ---------------------------------------------------------------------------
# Busca semântica
# ---------------------------------------------------------------------------

def search_transactions_semantically(query: str, tipo: str = None, top_k: int = 10):
    emb   = EmbeddingService()
    store = SupabaseStore()
    resp  = store.client.rpc("search_transactions", {
        "query_embedding": emb.generate(query),
        "tipo_filter":     tipo,
        "match_count":     top_k,
        "apenas_latest":   True,
    }).execute()
    results = resp.data or []
    for r in results:
        print(f"  [{r['tipo']}] {r['description']} | R$ {r['value']} | {r['status']} | Sim: {r['similarity']:.3f}")
    return results


SUPABASE_RESET_SQL = """
-- ============================================================
-- RESET COMPLETO — apaga todos os dados e objetos do schema
-- Execute no Supabase Dashboard → SQL Editor
-- ATENÇÃO: irreversível — todos os dados serão perdidos
-- ============================================================

-- Remove views (dependem das tabelas)
DROP VIEW IF EXISTS daily_financial_summary CASCADE;
DROP VIEW IF EXISTS financial_transactions_diff CASCADE;
DROP VIEW IF EXISTS financial_transactions_latest CASCADE;

-- Remove funções
DROP FUNCTION IF EXISTS search_transactions CASCADE;

-- Remove tabelas
DROP TABLE IF EXISTS daily_snapshot CASCADE;
DROP TABLE IF EXISTS financial_transactions CASCADE;

-- ============================================================
-- Após executar este SQL, rode novamente:
--   python3 conta_azul_supabase.py schema
-- para recriar toda a estrutura, e depois:
--   rm .sync_initialized
--   python3 conta_azul_supabase.py sync --inicio 2024-01-01 --fim <hoje>
-- para fazer a carga inicial.
-- ============================================================
"""




def print_help():
    print("""
Uso: python3 conta_azul_supabase.py [comando] [opções]

Comandos:
  (sem comando)          Executa sync automático (carga inicial ou diário)
  sync                   Mesmo que acima
  sync --full            Força carga completa desde 2024 mesmo se já inicializado
  sync --inicio AAAA-MM-DD --fim AAAA-MM-DD   Sync manual em período específico
  auth-url               Gera URL de autorização OAuth
  exchange-code <code>   Troca authorization code por tokens
  search <consulta>      Busca semântica nas transações
  schema                 Imprime SQL para criar tabelas/views no Supabase
  schema-reset           Imprime SQL para apagar tudo (tabelas, views, funções)
  help                   Exibe esta mensagem

Exemplos:
  python3 conta_azul_supabase.py
  python3 conta_azul_supabase.py sync --full
  python3 conta_azul_supabase.py sync --inicio 2025-01-01 --fim 2025-06-30
  python3 conta_azul_supabase.py schema-reset
  python3 conta_azul_supabase.py search despesas com aluguel
""")


if __name__ == "__main__":
    import sys
    import argparse

    cmd = sys.argv[1] if len(sys.argv) > 1 else "sync"

    if cmd == "auth-url":
        print("\n🔗 Acesse esta URL no navegador:\n")
        print(ContaAzulAuth.get_authorization_url())
        print("\nApós autorizar, copie o valor de 'code=' na URL de redirect.\n")

    elif cmd == "exchange-code":
        if len(sys.argv) < 3:
            print("Uso: python3 conta_azul_supabase.py exchange-code <code>")
            sys.exit(1)
        tokens = ContaAzulAuth.exchange_code_for_tokens(sys.argv[2])
        print(json.dumps(tokens, indent=2))

    elif cmd == "search":
        query = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "transações recentes"
        search_transactions_semantically(query)

    elif cmd == "schema":
        print(SUPABASE_SCHEMA_SQL)

    elif cmd == "schema-reset":
        print(SUPABASE_RESET_SQL)

    elif cmd == "help":
        print_help()

    else:
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--full", action="store_true",
                            help="Força carga completa ignorando controle de inicialização")
        parser.add_argument("--inicio", default=None)
        parser.add_argument("--fim", default=None)

        args_to_parse = sys.argv[2:] if cmd == "sync" else sys.argv[1:]
        args, _ = parser.parse_known_args(args_to_parse)

        # Validação de datas se informadas manualmente
        if args.inicio or args.fim:
            inicio = args.inicio or START_DATE
            fim    = args.fim or date.today().isoformat()
            try:
                datetime.strptime(inicio, "%Y-%m-%d")
                datetime.strptime(fim, "%Y-%m-%d")
            except ValueError:
                print("❌ Formato de data inválido. Use AAAA-MM-DD")
                sys.exit(1)
            if inicio > fim:
                print("❌ A data de início não pode ser maior que a data de fim.")
                sys.exit(1)
            # Quando datas são passadas manualmente, sempre trata como carga inicial
            # pois o usuário está definindo explicitamente o período a processar
            log.info(f"Período informado manualmente: {inicio} → {fim}")
            run_sync(force_full=True, start_date=inicio, end_date=fim)
        elif args.full:
            # --full sem datas: usa START_DATE até hoje
            log.info(f"Carga completa forçada: {START_DATE} → {date.today().isoformat()}")
            run_sync(force_full=True, start_date=START_DATE, end_date=date.today().isoformat())
        else:
            # Modo automático: decide entre carga inicial ou sync diário
            run_sync(force_full=False)
