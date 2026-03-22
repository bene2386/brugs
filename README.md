# brugs — Automações do Henrique Brugugnoli

Repositório privado de scripts e automações. Cada projeto vive na sua própria pasta com documentação independente.

---

## Projetos

| Pasta | Descrição |
|---|---|
| [`conta-azul-sync/`](conta-azul-sync/README.md) | Sincronização Conta Azul → Supabase + IA financeira (GPT-4o) |

---

## Estrutura

```
brugs/
├── README.md                  ← este arquivo: índice geral
├── .gitignore
└── conta-azul-sync/           ← sync financeiro Conta Azul → Supabase
    ├── README.md
    ├── conta_azul_supabase.py
    ├── conta_azul_vendas.py
    ├── financial_ai.py
    ├── run_conta_azul_sync.sh
    └── docs/
        ├── variaveis-ambiente.md
        ├── auto-sync.sh
        └── cron-sync.sh
```

---

## Como adicionar um novo projeto

1. Crie uma pasta com nome descritivo em `kebab-case` (ex: `meu-projeto/`)
2. Coloque todos os arquivos do projeto dentro dela
3. Adicione um `README.md` dentro da pasta documentando o projeto
4. Adicione uma linha na tabela de **Projetos** acima
5. Faça commit e push

---

## Responsável Técnico

Mantido pelo **agente Dev** da equipe do Henrique Brugugnoli.

*Última atualização: 2026-03-22*
