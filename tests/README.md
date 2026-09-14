# Suíte permanente de regressão do ControleFin

Esta pasta contém testes que devem permanecer junto do código-fonte do projeto.

## Objetivo

Antes de gerar um novo `.exe` ou entregar uma nova versão do dashboard, execute:

```powershell
python .\run_tests.py
```

A suíte testa o código de produção real e não acessa sua conta Pluggy.

## O que é validado

### Python

- schema de `ajustes.json`;
- transações manuais válidas e inválidas;
- normalização de gasto/entrada manual;
- chaves estáveis do SQLite;
- UPSERT;
- snapshot completo versus parcial;
- `_cf_raw_json`;
- índices SQLite;
- `/api/db/status`;
- ocultação de tabelas/colunas `_cf_*`;
- rejeição de nomes de tabela inválidos;
- contratos estruturais importantes do HTML;
- servidor limitado a `127.0.0.1`.

### JavaScript

Os testes leem `controlefin_dashboard.html` e executam trechos do JavaScript
de produção em sandboxes Node:

- detecção de `CardBankslip`;
- compras parceladas com no máximo uma parcela por mês;
- fluxo de caixa respeitando gastos líquidos e resarcimentos;
- projeções de salário;
- última ocorrência de gasto fixo;
- parcelas futuras;
- entrada líquida projetada.

## Dados

As fixtures em `tests/fixtures` são totalmente sintéticas. Nunca coloque aqui:

- `.env`;
- credenciais Pluggy;
- `data/controlefin.db` real;
- CSVs reais;
- `user_data/ajustes.json` real;
- relatórios contendo suas transações.

## Quando rodar

Rode a suíte:

1. antes de qualquer build PyInstaller;
2. depois de alterar regras financeiras;
3. depois de mexer no schema do SQLite;
4. depois de mexer em settings/persistência;
5. depois de alterar parcelamentos ou projeções;
6. antes de substituir a versão que você usa no dia a dia.

## Resultado esperado

No final:

```text
SUÍTE COMPLETA: OK
```

Se um teste falhar, não gere/substitua o executável até entender a regressão.
