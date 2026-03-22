#!/usr/bin/env python3
"""
briefing_diario.py — Briefing financeiro diário do CFO
Executa todo dia às 6h (America/Sao_Paulo) via cron do OpenClaw.

Coleta:
  1. Financeiro Appoena (Supabase)
  2. Financeiro Pessoal (Cashflow Live via Lovable API)

Entrega:
  - E-mail para hsouzab2308@gmail.com (via Gmail API, conta pessoal)
  - Mensagem no Telegram (chat 601606463)
"""

import json
import os
import sys
import requests
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

# ─── Configuração ──────────────────────────────────────────────────────────────

WORKSPACE = "/data/.openclaw/workspace"
ENV_FILE  = f"{WORKSPACE}/.env"

def load_env():
    """Carrega variáveis do .env como dicionário."""
    env = {}
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    except Exception as e:
        print(f"[ERRO] Não foi possível ler .env: {e}", file=sys.stderr)
    return env

ENV = load_env()

SUPABASE_URL = ENV.get("SUPABASE_URL", "")
SUPABASE_KEY = ENV.get("SUPABASE_KEY", "")
LOVABLE_URL  = ENV.get("LOVABLE_URL", "")
LOVABLE_KEY  = ENV.get("LOVABLE_KEY", "")

TZ_SP = ZoneInfo("America/Sao_Paulo")

# ─── Helpers ──────────────────────────────────────────────────────────────────

def fmt_brl(value):
    """Formata valor em reais: R$ 1.234.567,89"""
    try:
        v = float(value or 0)
        # Formata com separadores brasileiros
        abs_v = abs(v)
        formatted = f"{abs_v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        sinal = "-" if v < 0 else ""
        return f"R$ {sinal}{formatted}"
    except Exception:
        return "R$ 0,00"

def fmt_pct(numerator, denominator):
    """Percentual com 1 casa decimal."""
    try:
        if not denominator:
            return "—"
        return f"{(numerator / denominator * 100):.1f}%"
    except Exception:
        return "—"

def supabase_get(path, params=None):
    """GET no Supabase REST API."""
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    r = requests.get(url, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def lovable_get(path, params=None):
    """GET no Lovable (Cashflow Live) REST API."""
    headers = {
        "apikey": LOVABLE_KEY,
        "Authorization": f"Bearer {LOVABLE_KEY}",
        "Content-Type": "application/json",
    }
    url = f"{LOVABLE_URL}{path}"
    r = requests.get(url, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def google_headers(conta="pessoal"):
    """Retorna headers de autenticação para a conta Google especificada."""
    token_file = f"{WORKSPACE}/google-auth/token_{conta}.json"
    creds_file = f"{WORKSPACE}/google-auth/credentials.json"

    with open(token_file) as f:
        token = json.load(f)
    with open(creds_file) as f:
        creds = json.load(f)["installed"]

    if "refresh_token" in token:
        r = requests.post("https://oauth2.googleapis.com/token", data={
            "refresh_token": token["refresh_token"],
            "client_id":     creds["client_id"],
            "client_secret": creds["client_secret"],
            "grant_type":    "refresh_token",
        }, timeout=15)
        new_token = r.json()
        token["access_token"] = new_token["access_token"]
        with open(token_file, "w") as f:
            json.dump(token, f, indent=2)

    return {"Authorization": f"Bearer {token['access_token']}"}

# ─── 1. Financeiro Appoena ─────────────────────────────────────────────────────

def get_snapshot_ids():
    """Retorna set de installment_ids do snapshot mais recente."""
    data = supabase_get(
        "daily_snapshot",
        params={
            "select": "installment_id",
            "snapshot_date": f"eq.{get_max_snapshot_date()}",
            "limit": 10000,
        }
    )
    return {row["installment_id"] for row in data}

def get_max_snapshot_date():
    """Retorna a data do snapshot mais recente."""
    data = supabase_get(
        "daily_snapshot",
        params={"select": "snapshot_date", "order": "snapshot_date.desc", "limit": 1}
    )
    return data[0]["snapshot_date"] if data else date.today().isoformat()

def get_all_valid_transactions():
    """
    Retorna todas as transações válidas (cruzadas com daily_snapshot).
    Puxa em batches de 1000 para cobrir toda a base.
    """
    snapshot_date = get_max_snapshot_date()
    snapshot_ids  = get_snapshot_ids()

    all_rows = []
    offset = 0
    batch  = 1000
    while True:
        rows = supabase_get(
            "financial_transactions_latest",
            params={
                "select": "tipo,status,value,due_date,payment_date,description,payee_name",
                "limit":  batch,
                "offset": offset,
            }
        )
        all_rows.extend(rows)
        if len(rows) < batch:
            break
        offset += batch

    # Filtro: apenas os que existem no snapshot mais recente
    valid = [r for r in all_rows if r.get("installment_id") or True]

    # Vamos refazer cruzando via snapshot (usando a query via RPC não disponível,
    # então fazemos localmente com os IDs)
    # Na verdade precisamos de installment_id — refaz a query com ele
    return valid, snapshot_ids, snapshot_date

def get_all_valid_transactions_v2():
    """
    Versão corrigida: busca transactions com installment_id para cruzar com snapshot.
    """
    snapshot_date = get_max_snapshot_date()
    snapshot_ids  = get_snapshot_ids()

    all_rows = []
    offset = 0
    batch  = 1000
    while True:
        rows = supabase_get(
            "financial_transactions_latest",
            params={
                "select": "installment_id,tipo,status,value,due_date,payment_date,description,payee_name",
                "limit":  batch,
                "offset": offset,
            }
        )
        all_rows.extend(rows)
        if len(rows) < batch:
            break
        offset += batch

    # Apenas registros presentes no snapshot (não deletados)
    valid = [r for r in all_rows if r.get("installment_id") in snapshot_ids]
    return valid, snapshot_date

def calcular_mes(row, ano, mes):
    """
    Retorna (valor_realizado, valor_previsto) de um row para o mês dado.
    Regras do MEMORY.md:
      - Realizado: status=ACQUITTED e COALESCE(payment_date,due_date) no mês
      - Previsto:  status!=ACQUITTED e due_date no mês
    """
    from datetime import date as dt_date

    mes_inicio = dt_date(ano, mes, 1)
    if mes == 12:
        mes_fim = dt_date(ano + 1, 1, 1)
    else:
        mes_fim = dt_date(ano, mes + 1, 1)

    def parse_date(s):
        if not s:
            return None
        try:
            return dt_date.fromisoformat(s[:10])
        except Exception:
            return None

    due     = parse_date(row.get("due_date"))
    payment = parse_date(row.get("payment_date"))
    status  = row.get("status", "")
    value   = float(row.get("value") or 0)

    # Verifica se o registro entra no filtro do mês (due_date OU payment_date)
    due_no_mes     = due     and (mes_inicio <= due     < mes_fim)
    payment_no_mes = payment and (mes_inicio <= payment < mes_fim)

    if not (due_no_mes or payment_no_mes):
        return 0.0, 0.0

    data_ref = payment if payment else due

    realizado = 0.0
    previsto  = 0.0

    if status == "ACQUITTED":
        # Realizado: data_ref deve estar no mês
        if data_ref and (mes_inicio <= data_ref < mes_fim):
            realizado = value
    else:
        # Previsto: due_date deve estar no mês
        if due_no_mes:
            previsto = value

    return realizado, previsto

def appoena_financeiro():
    """Coleta e processa dados financeiros da Appoena."""
    today = datetime.now(TZ_SP).date()
    ano   = today.year

    rows, snapshot_date = get_all_valid_transactions_v2()

    # ── Ano completo ──────────────────────────────────────────────
    rec_real_ano = desp_real_ano = rec_prev_ano = desp_prev_ano = 0.0

    # ── Mês a mês ─────────────────────────────────────────────────
    meses = {}
    for m in range(1, 13):
        meses[m] = dict(rec_real=0, desp_real=0, rec_prev=0, desp_prev=0)

    for row in rows:
        tipo = row.get("tipo", "")
        for m in range(1, 13):
            real, prev = calcular_mes(row, ano, m)
            if tipo == "receita":
                meses[m]["rec_real"]  += real
                meses[m]["rec_prev"]  += prev
                rec_real_ano += real
                rec_prev_ano += prev
            elif tipo == "despesa":
                meses[m]["desp_real"] += real
                meses[m]["desp_prev"] += prev
                desp_real_ano += real
                desp_prev_ano += prev

    lucro_real_ano = rec_real_ano - desp_real_ano
    lucro_proj_ano = (rec_real_ano + rec_prev_ano) - (desp_real_ano + desp_prev_ano)

    # ── Receitas vencidas não recebidas ───────────────────────────
    vencidas = []
    for row in rows:
        if row.get("tipo") != "receita":
            continue
        if row.get("status") == "ACQUITTED":
            continue
        due = row.get("due_date")
        if not due:
            continue
        due_dt = date.fromisoformat(due[:10])
        if due_dt < today:
            vencidas.append({
                "desc":    row.get("payee_name") or row.get("description") or "—",
                "valor":   float(row.get("value") or 0),
                "due":     due_dt,
            })

    vencidas.sort(key=lambda x: x["due"])

    return {
        "snapshot_date": snapshot_date,
        "ano": ano,
        "rec_real_ano":   rec_real_ano,
        "desp_real_ano":  desp_real_ano,
        "lucro_real_ano": lucro_real_ano,
        "rec_proj_ano":   rec_real_ano + rec_prev_ano,
        "desp_proj_ano":  desp_real_ano + desp_prev_ano,
        "lucro_proj_ano": lucro_proj_ano,
        "meses":          meses,
        "vencidas":       vencidas,
    }

# ─── 2. Financeiro Pessoal ────────────────────────────────────────────────────

def pessoal_financeiro():
    """
    Coleta despesas pessoais do Cashflow Live (Lovable API).
    A tabela relevante é `despesas` com `despesa_recurring_items` e `despesa_one_off_items`.

    Status no Cashflow Live (inferido da UI):
      - 'A Pagar' → não pago, dentro do vencimento
      - 'Vencido'  → não pago, vencimento passado

    Como o banco usa `despesas` (lista mestre) + `despesa_recurring_items` (itens),
    precisamos calcular as ocorrências do mês corrente.
    """
    today     = datetime.now(TZ_SP).date()
    ano       = today.year
    mes       = today.month
    mes_str   = f"{ano}-{mes:02d}"
    today_str = today.isoformat()

    result = {
        "hoje":       [],   # vence hoje e não pago
        "vencidas":   [],   # já vencidas (antes de hoje) e não pagas
        "total_aberto_mes": 0.0,
        "count_aberto_mes": 0,
        "erro": None,
    }

    try:
        # Tenta buscar via Lovable API
        # Estrutura: despesas (lista) + despesa_recurring_items (itens recorrentes por mês)
        # e despesa_one_off_items (itens únicos por mês)

        # Busca despesas ativas
        despesas_raw = lovable_get(
            "despesas",
            params={"select": "id,name,status,item_type,payment_data", "is_active": "eq.true", "limit": 500}
        )

        if not despesas_raw:
            result["erro"] = "Sem dados na API Lovable (tabela vazia)"
            return result

        # Processa cada despesa
        for d in despesas_raw:
            status = d.get("status", "")
            nome   = d.get("name", "—")
            pdata  = d.get("payment_data") or {}

            # Extrai due_date e amount do payment_data (estrutura varia)
            if isinstance(pdata, str):
                try:
                    pdata = json.loads(pdata)
                except Exception:
                    pdata = {}

            due_str = pdata.get("due_date") or pdata.get("vencimento", "")
            amount  = float(pdata.get("amount") or pdata.get("valor") or 0)

            if not due_str:
                continue

            try:
                due = date.fromisoformat(due_str[:10])
            except Exception:
                continue

            # Apenas mês corrente
            if due.year != ano or due.month != mes:
                continue

            # Classifica
            if status not in ("A Pagar", "Vencido", "pending", "PENDING", "overdue"):
                continue

            if due < today:
                result["vencidas"].append({"nome": nome, "valor": amount, "due": due})
            elif due == today:
                result["hoje"].append({"nome": nome, "valor": amount})

            result["total_aberto_mes"] += amount
            result["count_aberto_mes"] += 1

    except Exception as e:
        result["erro"] = f"Erro ao consultar Lovable API: {e}"

    return result

# ─── 3. Formatar Briefing ─────────────────────────────────────────────────────

MESES_PT = {
    1: "Jan", 2: "Fev", 3: "Mar", 4: "Abr", 5: "Mai", 6: "Jun",
    7: "Jul", 8: "Ago", 9: "Set", 10: "Out", 11: "Nov", 12: "Dez"
}

def formatar_briefing(ap, pe):
    """Monta o texto do briefing."""
    today    = datetime.now(TZ_SP)
    data_str = today.strftime("%d/%m/%Y")
    hora_str = today.strftime("%H:%M")
    ano      = ap["ano"]

    linhas = []
    linhas.append(f"📊 Briefing Diário — {data_str} às {hora_str}")
    linhas.append(f"Snapshot Conta Azul: {ap['snapshot_date']}")
    linhas.append("")

    # ── APPOENA ──────────────────────────────────────────────────
    linhas.append("═══ APPOENA — FINANCEIRO ═══")
    linhas.append(f"Ano {ano} — Realizado:")
    linhas.append(f"  Receita:  {fmt_brl(ap['rec_real_ano'])}")
    linhas.append(f"  Despesa:  {fmt_brl(ap['desp_real_ano'])}")
    linhas.append(f"  Lucro:    {fmt_brl(ap['lucro_real_ano'])} ({fmt_pct(ap['lucro_real_ano'], ap['rec_real_ano'])})")
    linhas.append("")
    linhas.append(f"Ano {ano} — Projetado (realizado + previsto):")
    linhas.append(f"  Receita:  {fmt_brl(ap['rec_proj_ano'])}")
    linhas.append(f"  Despesa:  {fmt_brl(ap['desp_proj_ano'])}")
    linhas.append(f"  Lucro:    {fmt_brl(ap['lucro_proj_ano'])} ({fmt_pct(ap['lucro_proj_ano'], ap['rec_proj_ano'])})")
    linhas.append("")

    # Mês a mês
    linhas.append(f"Mês a mês {ano}:")
    today_mes = datetime.now(TZ_SP).month
    for m in range(1, 13):
        d = ap["meses"][m]
        rec   = d["rec_real"]  + d["rec_prev"]
        desp  = d["desp_real"] + d["desp_prev"]
        lucro = rec - desp

        # Indicador: passado=realizado, futuro=projetado
        sufixo = "✓" if m < today_mes else ("◉" if m == today_mes else "·")
        linhas.append(
            f"  {MESES_PT[m]} {sufixo}  Rec {fmt_brl(rec):>18}  "
            f"Desp {fmt_brl(desp):>18}  "
            f"Lucro {fmt_brl(lucro):>18}"
        )
    linhas.append("")

    # Receitas vencidas
    if ap["vencidas"]:
        linhas.append(f"⚠️  Receitas vencidas não recebidas ({len(ap['vencidas'])}):")
        total_venc = sum(v["valor"] for v in ap["vencidas"])
        for v in ap["vencidas"]:
            dias = (datetime.now(TZ_SP).date() - v["due"]).days
            linhas.append(f"  • {v['desc'][:45]:<45}  {fmt_brl(v['valor'])}  — venceu {v['due'].strftime('%d/%m')} ({dias}d)")
        linhas.append(f"  TOTAL EM ABERTO: {fmt_brl(total_venc)}")
    else:
        linhas.append("✅ Nenhuma receita vencida em aberto.")
    linhas.append("")

    # ── PESSOAL ──────────────────────────────────────────────────
    linhas.append("═══ PESSOAL — FINANCEIRO ═══")

    if pe.get("erro"):
        linhas.append(f"⚠️  {pe['erro']}")
        linhas.append("    (Dados indisponíveis — verifique o Cashflow Live)")
    else:
        # Vence hoje
        if pe["hoje"]:
            linhas.append(f"🔴 Vence HOJE ({len(pe['hoje'])} despesa(s)):")
            for item in pe["hoje"]:
                linhas.append(f"  • {item['nome'][:45]:<45}  {fmt_brl(item['valor'])}")
        else:
            linhas.append("✅ Nenhuma despesa pessoal vence hoje.")

        linhas.append("")

        # Já vencidas
        if pe["vencidas"]:
            linhas.append(f"🚨 Já vencidas ({len(pe['vencidas'])} despesa(s)):")
            for item in pe["vencidas"]:
                dias = (datetime.now(TZ_SP).date() - item["due"]).days
                linhas.append(f"  • {item['nome'][:45]:<45}  {fmt_brl(item['valor'])}  — {dias}d atraso")
        else:
            linhas.append("✅ Nenhuma despesa pessoal vencida em aberto.")

        linhas.append("")

        mes_nome = MESES_PT[datetime.now(TZ_SP).month]
        linhas.append(f"Total em aberto no mês ({mes_nome}): "
                      f"{fmt_brl(pe['total_aberto_mes'])} "
                      f"({pe['count_aberto_mes']} despesas)")

    linhas.append("")
    linhas.append("—")
    linhas.append("Brugs CFO")

    return "\n".join(linhas)

# ─── 4. Enviar Gmail ──────────────────────────────────────────────────────────

def enviar_email(assunto, corpo):
    """Envia e-mail via Gmail API (conta pessoal)."""
    import base64
    from email.mime.text import MIMEText

    headers = google_headers("pessoal")
    msg = MIMEText(corpo, "plain", "utf-8")
    msg["to"]      = "hsouzab2308@gmail.com"
    msg["from"]    = "hsouzab2308@gmail.com"
    msg["subject"] = assunto

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    r = requests.post(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        headers={**headers, "Content-Type": "application/json"},
        json={"raw": raw},
        timeout=30,
    )
    if r.status_code not in (200, 201):
        raise Exception(f"Gmail erro {r.status_code}: {r.text[:200]}")
    return r.json()

# ─── 5. Enviar Telegram ───────────────────────────────────────────────────────

def enviar_telegram(texto):
    """
    Envia via OpenClaw message tool (executa openclaw CLI).
    Alternativa: usa a API do Telegram diretamente se houver token configurado.
    """
    import subprocess

    # Telegram tem limite de 4096 chars por mensagem — divide se necessário
    MAX = 4000
    partes = [texto[i:i+MAX] for i in range(0, len(texto), MAX)]

    for parte in partes:
        # Usa openclaw CLI para enviar
        result = subprocess.run(
            ["openclaw", "message", "send",
             "--channel", "telegram",
             "--target",  "601606463",
             "--message", parte],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            raise Exception(f"Telegram erro: {result.stderr[:200]}")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    today     = datetime.now(TZ_SP)
    data_str  = today.strftime("%d/%m/%Y")
    log_prefix = f"[{today.strftime('%Y-%m-%d %H:%M')}] [briefing_diario]"

    print(f"{log_prefix} Iniciando coleta de dados...")

    erros = []

    # 1. Appoena
    try:
        print(f"{log_prefix} Coletando Appoena (Supabase)...")
        ap = appoena_financeiro()
        print(f"{log_prefix} Appoena OK — snapshot {ap['snapshot_date']}, "
              f"{len(ap['vencidas'])} receitas vencidas")
    except Exception as e:
        ap = {
            "snapshot_date": "erro", "ano": today.year,
            "rec_real_ano": 0, "desp_real_ano": 0, "lucro_real_ano": 0,
            "rec_proj_ano": 0, "desp_proj_ano": 0, "lucro_proj_ano": 0,
            "meses": {m: dict(rec_real=0,desp_real=0,rec_prev=0,desp_prev=0) for m in range(1,13)},
            "vencidas": [],
        }
        erros.append(f"Appoena: {e}")
        print(f"{log_prefix} ERRO Appoena: {e}", file=sys.stderr)

    # 2. Pessoal
    try:
        print(f"{log_prefix} Coletando Pessoal (Cashflow Live)...")
        pe = pessoal_financeiro()
        print(f"{log_prefix} Pessoal OK — "
              f"{pe['count_aberto_mes']} despesas em aberto no mês")
    except Exception as e:
        pe = {"hoje": [], "vencidas": [], "total_aberto_mes": 0,
              "count_aberto_mes": 0, "erro": str(e)}
        erros.append(f"Pessoal: {e}")
        print(f"{log_prefix} ERRO Pessoal: {e}", file=sys.stderr)

    # 3. Formata
    briefing = formatar_briefing(ap, pe)

    # 4. E-mail
    assunto = f"📊 Briefing Diário — {data_str}"
    try:
        print(f"{log_prefix} Enviando e-mail...")
        enviar_email(assunto, briefing)
        print(f"{log_prefix} E-mail enviado.")
    except Exception as e:
        erros.append(f"Email: {e}")
        print(f"{log_prefix} ERRO email: {e}", file=sys.stderr)

    # 5. Telegram
    try:
        print(f"{log_prefix} Enviando Telegram...")
        enviar_telegram(briefing)
        print(f"{log_prefix} Telegram enviado.")
    except Exception as e:
        erros.append(f"Telegram: {e}")
        print(f"{log_prefix} ERRO Telegram: {e}", file=sys.stderr)

    # Resultado
    if erros:
        print(f"{log_prefix} Concluído com {len(erros)} erro(s): {'; '.join(erros)}")
        sys.exit(1)
    else:
        print(f"{log_prefix} ✅ Briefing enviado com sucesso.")
        sys.exit(0)

if __name__ == "__main__":
    main()
