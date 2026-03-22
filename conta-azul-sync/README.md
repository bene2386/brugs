# conta-azul-sync — Sync Conta Azul → Supabase

Sincronização automática de dados financeiros entre o ERP **Conta Azul** e o banco de dados **Supabase**, com módulo de inteligência financeira via GPT-4o.

---

## Estrutura

```
conta-azul-sync/
├── conta_azul_supabase.py   # Sync receitas e despesas → Supabase
├── conta_azul_vendas.py     # Sync vendas → Supabase
├── financial_ai.py          # IA financeira: chat, insights, anomalias, report CFO
├── run_conta_azul_sync.sh   # Orquestrador bash (roda os 4 passos em sequência)
├── README.md                # Esta documentação
└── docs/
    ├── variaveis-ambiente.md  # Referência completa das variáveis de ambiente
    ├── auto-sync.sh           # Monitor de arquivos com inotifywait (auto-commit/push)
    └── cron-sync.sh           # Script para cron: commit + push de alterações pendentes
```

---

## Pré-requisitos

- Python 3.10+
- `pip` (para instalar dependências)
- Credenciais do Conta Azul (OAuth2), Supabase e OpenAI

---

## Configuração

### 1. Clonar o repositório

```bash
git clone https://github.com/bene2386/brugs.git
cd brugs/conta-azul-sync
```

### 2. Criar e ativar o ambiente virtual

```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Instalar dependências

```bash
pip install requests supabase openai python-dotenv flask scipy numpy
```

### 4. Configurar variáveis de ambiente

Crie um arquivo `.env` na pasta `conta-azul-sync/` (nunca commitado):

```env
# Conta Azul — OAuth2
CONTA_AZUL_CLIENT_ID=
CONTA_AZUL_CLIENT_SECRET=
CONTA_AZUL_REDIRECT_URI=
CONTA_AZUL_REFRESH_TOKEN=

# Supabase
SUPABASE_URL=
SUPABASE_KEY=

# OpenAI
OPENAI_API_KEY=

# E-mail (para alertas e reports — financial_ai.py)
EMAIL_SMTP_HOST=smtp.gmail.com
EMAIL_SMTP_PORT=587
EMAIL_USER=
EMAIL_PASSWORD=
EMAIL_DEST=
```

Para referência detalhada de cada variável, veja [`docs/variaveis-ambiente.md`](docs/variaveis-ambiente.md).

---

## Scripts

### `conta_azul_supabase.py` — Sync Financeiro

Extrai receitas e despesas do Conta Azul e armazena no Supabase com embeddings vetoriais.

**Comportamento:**
- Primeira execução: carga completa desde 2024
- Execuções seguintes: sync incremental (últimas 62h)
- Registros alterados recebem nova linha (histórico preservado)

```bash
source venv/bin/activate
python3 conta_azul_supabase.py sync
```

---

### `conta_azul_vendas.py` — Sync de Vendas

Extrai vendas do Conta Azul e sincroniza com o Supabase.

**Comportamento:**
- Primeira execução: carga completa desde 2024
- Execuções seguintes: sync incremental
- Histórico de alterações preservado

```bash
source venv/bin/activate
python3 conta_azul_vendas.py sync
```

---

### `financial_ai.py` — Inteligência Financeira (GPT-4o)

Módulo de IA financeira com quatro modos de operação:

| Comando | Descrição |
|---|---|
| `python3 financial_ai.py chat` | Chat interativo no terminal |
| `python3 financial_ai.py insights` | Insights diários (envia por e-mail) |
| `python3 financial_ai.py insights --semanal` | Resumo semanal (ideal para sextas) |
| `python3 financial_ai.py anomalias` | Detecta anomalias e alerta por e-mail |
| `python3 financial_ai.py auditoria` | Auditoria financeira completa |
| `python3 financial_ai.py cfo` | Report executivo para o CFO |
| `python3 financial_ai.py web` | Interface web local no browser |

```bash
source venv/bin/activate
python3 financial_ai.py cfo
```

---

### `run_conta_azul_sync.sh` — Orquestrador Completo

Executa os 4 passos em sequência: sync financeiro → sync vendas → auditoria → report CFO. Ideal para rodar via cron.

```bash
bash run_conta_azul_sync.sh
```

**Log gerado em:** `conta_azul_sync.log` (não commitado)

**Exemplo de cron (diário às 7h):**
```cron
0 7 * * * /data/.openclaw/workspace/cfo/scripts/conta-azul-sync/run_conta_azul_sync.sh
```

---

## Sincronização Automática com GitHub

O diretório `docs/` contém dois scripts para manter o repositório sincronizado automaticamente:

- **`docs/auto-sync.sh`** — usa `inotifywait` para monitorar alterações em tempo real e fazer commit/push automático
- **`docs/cron-sync.sh`** — script simples para rodar via cron a cada N minutos

Para verificar logs:
```bash
cat /tmp/brugs-autosync.log     # auto-sync em tempo real
cat /tmp/brugs-cron-sync.log    # cron sync
```

---

*Responsável técnico: agente Dev da equipe Henrique Brugugnoli*
