# Referência de Variáveis de Ambiente

Todas as variáveis abaixo devem ser definidas no arquivo `.env` na raiz do projeto.
O arquivo `.env` **nunca é commitado** (está no `.gitignore`).

---

## Conta Azul — OAuth2

| Variável | Descrição |
|---|---|
| `CONTA_AZUL_CLIENT_ID` | Client ID do aplicativo registrado no Conta Azul |
| `CONTA_AZUL_CLIENT_SECRET` | Client Secret do aplicativo |
| `CONTA_AZUL_REDIRECT_URI` | URI de redirect configurada no app (ex: `http://localhost:8080/callback`) |
| `CONTA_AZUL_REFRESH_TOKEN` | Refresh Token OAuth2 (obtido na primeira autenticação) |

**Como obter:**
1. Acesse o portal de desenvolvedores do Conta Azul
2. Crie um aplicativo OAuth2
3. Execute o fluxo de autenticação uma vez para gerar o Refresh Token
4. Salve o Refresh Token — ele é de longa duração e renova automaticamente

---

## Supabase

| Variável | Descrição |
|---|---|
| `SUPABASE_URL` | URL do projeto Supabase (ex: `https://xxxx.supabase.co`) |
| `SUPABASE_KEY` | Service Role Key (acesso completo, não o anon key) |

**Como obter:**
- Dashboard Supabase → Settings → API

---

## OpenAI

| Variável | Descrição |
|---|---|
| `OPENAI_API_KEY` | Chave de API da OpenAI (usada pelo GPT-4o nos módulos de IA) |

---

## E-mail (SMTP)

Usado pelo `financial_ai.py` para enviar insights, alertas e reports por e-mail.

| Variável | Descrição | Exemplo |
|---|---|---|
| `EMAIL_SMTP_HOST` | Servidor SMTP | `smtp.gmail.com` |
| `EMAIL_SMTP_PORT` | Porta SMTP | `587` |
| `EMAIL_USER` | Endereço de e-mail remetente | `seu@gmail.com` |
| `EMAIL_PASSWORD` | Senha de aplicativo (não a senha normal) | — |
| `EMAIL_DEST` | Endereço destinatário dos alertas | `henrique@appoena.io` |

**Atenção para Gmail:** use uma [senha de aplicativo](https://myaccount.google.com/apppasswords), não a senha da conta.

---

## Exemplo de `.env` completo

```env
# Conta Azul
CONTA_AZUL_CLIENT_ID=seu_client_id
CONTA_AZUL_CLIENT_SECRET=seu_client_secret
CONTA_AZUL_REDIRECT_URI=http://localhost:8080/callback
CONTA_AZUL_REFRESH_TOKEN=seu_refresh_token

# Supabase
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_KEY=sua_service_role_key

# OpenAI
OPENAI_API_KEY=sk-...

# E-mail
EMAIL_SMTP_HOST=smtp.gmail.com
EMAIL_SMTP_PORT=587
EMAIL_USER=seu@gmail.com
EMAIL_PASSWORD=senha_de_aplicativo
EMAIL_DEST=henrique@appoena.io
```
