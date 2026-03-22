"""
Conta Azul Vendas → Supabase Integration
=========================================
Extrai vendas do ERP Conta Azul e armazena no Supabase com histórico.

Comportamento:
  - Primeira execução: busca todas as vendas desde 2024 (carga inicial)
  - Execuções seguintes: busca só o que foi alterado (sync incremental)
  - Registros alterados: nova linha é inserida, histórico anterior é mantido

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
import sys
import json
import logging
import time
import random
import argparse
from datetime import datetime, date, timedelta

import requests
from dotenv import load_dotenv
from supabase import create_client, Client
from openai import OpenAI

# Reutiliza a autenticação do script principal
from conta_azul_supabase import ContaAzulAuth

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("conta_azul_vendas.log")],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configurações
# ---------------------------------------------------------------------------

CONTA_AZUL_BASE_URL = "https://api-v2.contaazul.com"

SUPABASE_URL   = os.getenv("SUPABASE_URL")
SUPABASE_KEY   = os.getenv("SUPABASE_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

START_DATE   = "2024-01-01"
PAGE_SIZE    = 1000
CONTROL_FILE = ".vendas_initialized"


# ---------------------------------------------------------------------------
# Controle de carga inicial
# ---------------------------------------------------------------------------

def is_initialized() -> bool:
    return os.path.exists(CONTROL_FILE)


def mark_initialized():
    with open(CONTROL_FILE, "w") as f:
        f.write(datetime.now().isoformat())
    log.info("✓ Carga inicial de vendas marcada como concluída.")


# ---------------------------------------------------------------------------
# Cliente API Vendas
# ---------------------------------------------------------------------------

RATE_LIMIT_DELAY = 0.12
MAX_RETRIES      = 5
BACKOFF_BASE     = 2


class ContaAzulVendasClient:
    def __init__(self, auth: ContaAzulAuth):
        self.auth = auth
        self._last_request_time = 0.0

    def _throttle(self):
        elapsed = time.time() - self._last_request_time
        if elapsed < RATE_LIMIT_DELAY:
            time.sleep(RATE_LIMIT_DELAY - elapsed)
        self._last_request_time = time.time()

    def _get(self, path: str, params: dict = None) -> dict:
        for attempt in range(1, MAX_RETRIES + 1):
            self._throttle()
            try:
                resp = requests.get(
                    f"{CONTA_AZUL_BASE_URL}{path}",
                    headers=self.auth.headers(),
                    params=params,
                    timeout=30,
                )
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 0))
                    wait = retry_after if retry_after > 0 else BACKOFF_BASE ** attempt + random.uniform(0, 1)
                    log.warning(f"429 Too Many Requests — aguardando {wait:.1f}s (tentativa {attempt}/{MAX_RETRIES})")
                    time.sleep(wait)
                    continue

                resp.raise_for_status()
                return resp.json()

            except requests.HTTPError as e:
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
        """Busca paginada — retorna todos os itens."""
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
            total = data.get("total_itens", 0)
            log.info(f"{path} — página {pagina}: {len(itens)} registros (total: {total})")

            if pagina * PAGE_SIZE >= total:
                break

            pagina += 1

        return records

    def fetch_vendas(self, start_date: str, end_date: str,
                     alteracao_de: str = None, alteracao_ate: str = None) -> list[dict]:
        """
        Busca vendas na API do Conta Azul.
        - Carga inicial: usa data_inicio/data_fim (emissão)
        - Sync diário: usa data_alteracao_de/ate + data_inicio amplo
        """
        params = {}
        if alteracao_de and alteracao_ate:
            params["data_alteracao_de"] = alteracao_de
            params["data_alteracao_ate"] = alteracao_ate
            params["data_inicio"] = START_DATE
            params["data_fim"] = "2099-12-31"
        else:
            params["data_inicio"] = start_date
            params["data_fim"] = end_date

        return self._fetch_all("/v1/venda/busca", params)

    def fetch_venda_detalhe(self, sale_id: str) -> dict | None:
        """Busca detalhes de uma venda específica (GET /v1/venda/{id}).
        Retorna o objeto completo com venda.id_categoria."""
        try:
            return self._get(f"/v1/venda/{sale_id}")
        except Exception as e:
            log.warning(f"Erro ao buscar detalhe da venda {sale_id}: {e}")
            return None


# ---------------------------------------------------------------------------
# Cache de Categorias
# ---------------------------------------------------------------------------

class CategoriaCache:
    """Cache em memória para evitar chamadas repetidas à API de categorias.
    Carrega todas as categorias de uma vez na primeira chamada."""

    def __init__(self, api_client: 'ContaAzulVendasClient'):
        self._client = api_client
        self._cache: dict[str, str] = {}  # id -> nome
        self._loaded = False

    def _load_all(self):
        """Carrega todas as categorias da API (paginação própria,
        pois o campo de total se chama itens_totais, diferente de vendas)."""
        if self._loaded:
            return
        try:
            pagina = 1
            while True:
                self._client._throttle()
                data = self._client._get("/v1/categorias", {
                    "pagina": pagina,
                    "tamanho_pagina": 1000,
                    "permite_apenas_filhos": "false",
                })
                itens = data.get("itens", [])
                if not itens:
                    break
                for cat in itens:
                    cat_id = cat.get("id")
                    cat_nome = cat.get("nome", "")
                    if cat_id:
                        self._cache[cat_id] = cat_nome
                total = data.get("itens_totais", 0)
                if pagina * 1000 >= total:
                    break
                pagina += 1
            log.info(f"Cache de categorias carregado: {len(self._cache)} categorias")
        except Exception as e:
            log.warning(f"Erro ao carregar categorias: {e}")
        self._loaded = True

    def get_nome(self, categoria_id: str) -> str:
        """Retorna o nome da categoria. Carrega o cache na primeira chamada."""
        if not self._loaded:
            self._load_all()
        return self._cache.get(categoria_id, "")


# ---------------------------------------------------------------------------
# Supabase Store para Vendas
# ---------------------------------------------------------------------------

class SupabaseVendasStore:
    def __init__(self):
        self.client: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        self.table = "sales_transactions"

    def insert(self, row: dict):
        return self.client.table(self.table).insert(row).execute()

    def get_latest_version(self, sale_id: str) -> dict | None:
        resp = (
            self.client.table(self.table)
            .select("total,situacao,cliente_nome,tipo_venda,versao,categoria_id")
            .eq("sale_id", sale_id)
            .order("extraction_date", desc=True)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else None

    def get_last_sync(self) -> dict | None:
        resp = (
            self.client.table("sales_sync_log")
            .select("alteracao_de,alteracao_ate,sync_at")
            .eq("status", "ok")
            .not_.is_("alteracao_ate", "null")
            .order("sync_at", desc=True)
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else None

    def record_sync(self, params: dict):
        self.client.table("sales_sync_log").insert(params).execute()


# ---------------------------------------------------------------------------
# Normalização e comparação
# ---------------------------------------------------------------------------

def normalize_venda(record: dict, is_initial_load: bool) -> dict:
    """Normaliza um registro da API de vendas para inserção no Supabase."""
    cliente = record.get("cliente") or {}
    situacao = record.get("situacao") or {}

    data_criacao = record.get("criado_em")
    if data_criacao:
        data_criacao = str(data_criacao)[:23]  # trunca microsegundos excessivos

    data_alteracao = record.get("data_alteracao")
    if data_alteracao:
        data_alteracao = str(data_alteracao)[:23]

    return {
        "extraction_date": date.today().isoformat(),
        "is_initial_load": is_initial_load,
        "sale_id": str(record.get("id", "")),
        "raw_data": json.dumps(record, ensure_ascii=False),
        "numero": record.get("numero"),
        "data_venda": record.get("data"),
        "total": float(record.get("total") or 0),
        "tipo_venda": record.get("tipo"),
        "situacao": situacao.get("nome", ""),
        "situacao_descricao": situacao.get("descricao", ""),
        "cliente_id": str(cliente.get("id", "")) if cliente.get("id") else None,
        "cliente_nome": cliente.get("nome", ""),
        "cliente_email": cliente.get("email", ""),
        "categoria_id": None,           # preenchido pelo pipeline via GET /v1/venda/{id}
        "categoria_nome": None,         # preenchido pelo pipeline via cache de categorias
        "evento_financeiro_id": None,   # preenchido pelo pipeline via GET /v1/venda/{id}
        "origem": record.get("origem"),
        "versao": record.get("versao"),
        "data_criacao": data_criacao,
        "data_alteracao": data_alteracao,
    }


def _venda_mudou(novo: dict, anterior: dict) -> bool:
    """Compara campos relevantes para decidir se nova versão deve ser inserida."""
    # Se a versão da API subiu, registro mudou
    v_novo = novo.get("versao")
    v_ant = anterior.get("versao")
    if v_novo is not None and v_ant is not None and v_novo != v_ant:
        return True

    # Total com tolerância
    t_novo = float(novo.get("total") or 0)
    t_ant = float(anterior.get("total") or 0)
    if abs(t_novo - t_ant) > 0.01:
        return True

    # Campos de texto
    for campo in ("situacao", "cliente_nome", "tipo_venda"):
        val_novo = novo.get(campo) or ""
        val_ant = anterior.get(campo) or ""
        if val_novo != val_ant:
            return True

    # Categoria — detecta reclassificação
    cat_novo = novo.get("categoria_id") or ""
    cat_ant = anterior.get("categoria_id") or ""
    if cat_novo != cat_ant:
        return True

    return False


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


def build_embedding_text_venda(record: dict) -> str:
    cliente = record.get("cliente") or {}
    situacao = record.get("situacao") or {}
    parts = [
        f"Venda #{record.get('numero', '')}",
        f"Cliente: {cliente.get('nome', '')}",
        f"Valor: R$ {record.get('total', '')}",
        f"Data: {record.get('data', '')}",
        f"Tipo: {record.get('tipo', '')}",
        f"Situação: {situacao.get('nome', '')}",
        f"Origem: {record.get('origem', '')}",
    ]
    return " | ".join(p for p in parts if p.split(": ", 1)[-1])


# ---------------------------------------------------------------------------
# Pipeline de processamento
# ---------------------------------------------------------------------------

def _resolver_detalhe_venda(row: dict, ca: 'ContaAzulVendasClient',
                            cat_cache: CategoriaCache) -> None:
    """Busca o detalhe da venda (GET /v1/venda/{id}) para obter:
    - id_categoria → categoria_id + categoria_nome
    - evento_financeiro.id → evento_financeiro_id (vínculo com financial_transactions)
    Só chama a API se categoria_id ainda está vazio."""
    if row.get("categoria_id"):
        return  # já preenchido (detalhe já foi buscado)

    sale_id = row["sale_id"]
    detalhe = ca.fetch_venda_detalhe(sale_id)
    if not detalhe:
        return

    # Categoria
    venda = detalhe.get("venda") or {}
    cat_id = venda.get("id_categoria")
    if cat_id:
        row["categoria_id"] = str(cat_id)
        nome = cat_cache.get_nome(cat_id)
        row["categoria_nome"] = nome if nome else None

    # Evento financeiro (vínculo com financial_transactions)
    evento = detalhe.get("evento_financeiro") or {}
    ev_id = evento.get("id")
    if ev_id:
        row["evento_financeiro_id"] = str(ev_id)


def process_and_insert_vendas(records: list[dict], store: SupabaseVendasStore,
                              emb: EmbeddingService, ca: 'ContaAzulVendasClient',
                              cat_cache: CategoriaCache,
                              is_initial_load: bool) -> list[dict]:
    """Normaliza, compara, resolve categoria, gera embedding e insere vendas no Supabase."""
    inserted = []
    total = len(records)

    for i, rec in enumerate(records, 1):
        row = normalize_venda(rec, is_initial_load)
        sale_id = row["sale_id"]

        # Busca detalhe da venda para obter categoria e evento financeiro
        _resolver_detalhe_venda(row, ca, cat_cache)

        # Verifica se já existe — se existir e não mudou, pula
        anterior = store.get_latest_version(sale_id)
        if anterior and not _venda_mudou(row, anterior):
            continue

        # Gera embedding
        text = build_embedding_text_venda(rec)
        try:
            row["embedding"] = emb.generate(text)
        except Exception as e:
            log.warning(f"Erro ao gerar embedding para venda {sale_id}: {e}")
            row["embedding"] = None

        # Insere
        result = store.insert(row)
        if result.data:
            inserted.append({
                "row_id": result.data[0]["id"],
                "sale_id": sale_id,
            })
            if i % 10 == 0 or i == total:
                log.info(f"Progresso: {i}/{total} processadas, {len(inserted)} inseridas")

    return inserted


# ---------------------------------------------------------------------------
# Sync principal
# ---------------------------------------------------------------------------

def run_sync_vendas(force_full: bool = False, start_date: str = START_DATE,
                    end_date: str = None):
    """Executa sincronização de vendas — carga inicial ou incremental."""
    auth = ContaAzulAuth()
    ca = ContaAzulVendasClient(auth)
    emb = EmbeddingService()
    store = SupabaseVendasStore()
    cat_cache = CategoriaCache(ca)

    if end_date is None:
        end_date = date.today().isoformat()

    if force_full or not is_initialized():
        # ════════════════════════════════════════
        # CARGA INICIAL
        # ════════════════════════════════════════
        log.info("=" * 60)
        log.info("VENDAS — MODO: Carga inicial completa")
        log.info(f"Período de emissão: {start_date} → {end_date}")
        log.info("=" * 60)

        vendas = ca.fetch_vendas(start_date, end_date)
        log.info(f"Total vendas encontradas: {len(vendas)}")

        inserted = process_and_insert_vendas(vendas, store, emb, ca, cat_cache, is_initial_load=True)
        log.info(f"✓ {len(inserted)} vendas inseridas")

        store.record_sync({
            "alteracao_de": None,
            "alteracao_ate": None,
            "emissao_de": start_date,
            "emissao_ate": end_date,
            "vendas_found": len(vendas),
            "records_inserted": len(inserted),
            "status": "ok",
        })
        mark_initialized()

    else:
        # ════════════════════════════════════════
        # SYNC INCREMENTAL
        # ════════════════════════════════════════
        agora = datetime.now()
        alteracao_ate = agora.strftime("%Y-%m-%dT%H:%M:%S")

        ultimo_sync = store.get_last_sync()
        if ultimo_sync:
            ultimo_ate = datetime.strptime(ultimo_sync["alteracao_ate"],
                                           "%Y-%m-%dT%H:%M:%S")
            alteracao_de = (ultimo_ate + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S")
            log.info(f"Última sync de vendas: {ultimo_sync['sync_at']}")
            log.info(f"Janela dinâmica: {alteracao_de} → {alteracao_ate}")
        else:
            alteracao_de = (agora - timedelta(hours=62)).strftime("%Y-%m-%dT%H:%M:%S")
            log.info(f"Sem histórico — fallback 62h: {alteracao_de} → {alteracao_ate}")

        log.info("=" * 60)
        log.info("VENDAS — MODO: Atualização incremental")
        log.info(f"Janela: {alteracao_de} → {alteracao_ate}")
        log.info("=" * 60)

        vendas = ca.fetch_vendas(
            START_DATE, end_date,
            alteracao_de=alteracao_de, alteracao_ate=alteracao_ate
        )
        log.info(f"Vendas alteradas: {len(vendas)}")

        if not vendas:
            log.info("Nenhuma alteração encontrada em vendas.")
            store.record_sync({
                "alteracao_de": alteracao_de,
                "alteracao_ate": alteracao_ate,
                "emissao_de": START_DATE,
                "emissao_ate": "2099-12-31",
                "vendas_found": 0,
                "records_inserted": 0,
                "status": "ok",
            })
            return

        inserted = process_and_insert_vendas(vendas, store, emb, ca, cat_cache, is_initial_load=False)
        log.info(f"✓ {len(inserted)} novas versões de vendas inseridas")

        store.record_sync({
            "alteracao_de": alteracao_de,
            "alteracao_ate": alteracao_ate,
            "emissao_de": START_DATE,
            "emissao_ate": "2099-12-31",
            "vendas_found": len(vendas),
            "records_inserted": len(inserted),
            "status": "ok",
        })


# ---------------------------------------------------------------------------
# Schema SQL
# ---------------------------------------------------------------------------

SUPABASE_SCHEMA_SQL = """
-- ============================================================
-- Pré-requisito: extensão pgvector (já habilitada se usa financial_transactions)
-- ============================================================
-- CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================
-- Tabela: histórico de todas as versões de cada venda
-- Cada sync insere uma nova linha se o registro mudou
-- ============================================================
CREATE TABLE sales_transactions (
    id                  BIGSERIAL PRIMARY KEY,
    extraction_date     DATE        NOT NULL DEFAULT CURRENT_DATE,
    is_initial_load     BOOLEAN     NOT NULL DEFAULT FALSE,

    -- ID da venda no Conta Azul (UUID)
    sale_id             TEXT        NOT NULL,

    -- Raw JSON completo da API
    raw_data            JSONB       NOT NULL,

    -- Campos normalizados
    numero              INTEGER,
    data_venda          DATE,
    total               NUMERIC,
    tipo_venda          TEXT,
    situacao            TEXT,
    situacao_descricao  TEXT,

    -- Cliente
    cliente_id          TEXT,
    cliente_nome        TEXT,
    cliente_email       TEXT,

    -- Categoria (obtida via GET /v1/venda/{id})
    categoria_id        TEXT,               -- UUID da categoria
    categoria_nome      TEXT,               -- nome da categoria

    -- Evento financeiro (vínculo com financial_transactions)
    evento_financeiro_id TEXT,              -- UUID do evento financeiro

    -- Metadados
    origem              TEXT,
    versao              INTEGER,
    data_criacao        TIMESTAMPTZ,
    data_alteracao      TIMESTAMPTZ,

    -- Embedding para busca semântica
    embedding           VECTOR(1536),

    -- Audit
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes
CREATE INDEX idx_sales_extraction_date
    ON sales_transactions (extraction_date);

CREATE INDEX idx_sales_sale_id
    ON sales_transactions (sale_id, extraction_date DESC, created_at DESC);

CREATE INDEX idx_sales_data_venda
    ON sales_transactions (data_venda);

CREATE INDEX idx_sales_situacao
    ON sales_transactions (situacao);

CREATE INDEX idx_sales_cliente
    ON sales_transactions (cliente_nome);

CREATE INDEX idx_sales_embedding
    ON sales_transactions USING hnsw (embedding vector_cosine_ops);

-- ============================================================
-- View: versão mais recente de cada venda (security_invoker)
-- ============================================================
CREATE OR REPLACE VIEW sales_transactions_latest
WITH (security_invoker = true) AS
SELECT DISTINCT ON (sale_id)
    id, extraction_date, is_initial_load, sale_id, raw_data,
    numero, data_venda, total, tipo_venda, situacao, situacao_descricao,
    cliente_id, cliente_nome, cliente_email,
    categoria_id, categoria_nome, evento_financeiro_id,
    origem, versao, data_criacao, data_alteracao, embedding, created_at
FROM sales_transactions
ORDER BY sale_id, extraction_date DESC, created_at DESC;

-- ============================================================
-- Tabela: log de sincronizações de vendas
-- ============================================================
CREATE TABLE IF NOT EXISTS sales_sync_log (
    id               BIGSERIAL PRIMARY KEY,
    sync_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    alteracao_de     TEXT,
    alteracao_ate    TEXT,
    emissao_de       TEXT NOT NULL,
    emissao_ate      TEXT NOT NULL,
    vendas_found     INT,
    records_inserted INT,
    status           TEXT NOT NULL DEFAULT 'ok',
    error_message    TEXT
);

CREATE INDEX IF NOT EXISTS idx_sales_sync_log_at
    ON sales_sync_log (sync_at DESC);
"""


SUPABASE_RESET_SQL = """
-- ============================================================
-- RESET COMPLETO — apaga todos os dados de vendas
-- Execute no Supabase Dashboard → SQL Editor
-- ATENÇÃO: irreversível
-- ============================================================

DROP VIEW IF EXISTS sales_transactions_latest CASCADE;
DROP TABLE IF EXISTS sales_transactions CASCADE;
DROP TABLE IF EXISTS sales_sync_log CASCADE;

-- Após executar este SQL, rode novamente:
--   python3 conta_azul_vendas.py schema
-- para ver o SQL de criação, e depois:
--   rm .vendas_initialized
--   python3 conta_azul_vendas.py sync
-- para fazer a carga inicial.
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def print_help():
    print("""
Uso: python3 conta_azul_vendas.py [comando] [opções]

Comandos:
  (sem comando)          Executa sync automático (carga inicial ou diário)
  sync                   Mesmo que acima
  sync --full            Força carga completa desde 2024 mesmo se já inicializado
  sync --inicio AAAA-MM-DD --fim AAAA-MM-DD   Sync manual em período específico
  schema                 Imprime SQL para criar tabelas/views no Supabase
  schema-reset           Imprime SQL para apagar tudo
  help                   Exibe esta mensagem

Opções:
  --full                 Força carga completa ignorando controle de inicialização

Exemplos:
  python3 conta_azul_vendas.py sync
  python3 conta_azul_vendas.py sync --full
  python3 conta_azul_vendas.py sync --inicio 2024-01-01 --fim 2025-12-31
  python3 conta_azul_vendas.py schema
""")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "sync"

    if cmd == "schema":
        print(SUPABASE_SCHEMA_SQL)

    elif cmd == "schema-reset":
        print(SUPABASE_RESET_SQL)

    elif cmd == "help":
        print_help()

    else:
        # sync (com ou sem argumentos)
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--full", action="store_true")
        parser.add_argument("--inicio", default=None)
        parser.add_argument("--fim", default=None)

        args_to_parse = sys.argv[2:] if cmd == "sync" else sys.argv[1:]
        args, _ = parser.parse_known_args(args_to_parse)

        if args.inicio or args.fim:
            inicio = args.inicio or START_DATE
            fim = args.fim or date.today().isoformat()
            try:
                datetime.strptime(inicio, "%Y-%m-%d")
                datetime.strptime(fim, "%Y-%m-%d")
            except ValueError:
                print("❌ Formato de data inválido. Use AAAA-MM-DD")
                sys.exit(1)

            log.info(f"Período manual: {inicio} → {fim}")
            run_sync_vendas(force_full=True, start_date=inicio, end_date=fim)

        elif args.full:
            log.info(f"Carga completa forçada: {START_DATE} → {date.today().isoformat()}")
            run_sync_vendas(force_full=True, start_date=START_DATE,
                            end_date=date.today().isoformat())
        else:
            run_sync_vendas(force_full=False)
