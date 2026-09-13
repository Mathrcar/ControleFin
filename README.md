# ControleFin

Dashboard financeiro pessoal e local, integrado à API da Pluggy.

O projeto foi desenhado para rodar **localmente**, mantendo credenciais, transações, saldos, investimentos e classificações financeiras fora de serviços externos e fora do repositório Git.

O fluxo principal é:

```text
ControleFin.exe / controlefin_server.py
        ↓
lê variáveis do .env
        ↓
consulta a API da Pluggy
        ↓
atualiza os CSVs locais em data/
        ↓
inicia um backend local FastAPI
        ↓
serve o dashboard HTML
        ↓
abre o navegador em http://127.0.0.1:8765
```

---

## Objetivo

O ControleFin consolida dados financeiros de múltiplas contas conectadas à Pluggy e gera uma visão pessoal de:

- gastos;
- cartão de crédito;
- PIX;
- boletos;
- entradas;
- contas bancárias;
- investimentos;
- patrimônio;
- categorias;
- merchants;
- fluxo de caixa;
- movimentações por mês;
- reclassificações manuais de transações.

Todas as contas são tratadas como pertencentes à mesma pessoa.

Por isso, transferências entre contas próprias e movimentações patrimoniais para investimentos podem ser tratadas separadamente dos gastos de consumo.

---

# Arquitetura

O projeto possui três componentes principais.

## 1. `pluggy_finance_export.py`

Responsável pela comunicação com a API da Pluggy.

Ele:

- autentica usando `PLUGGY_CLIENT_ID` e `PLUGGY_CLIENT_SECRET`;
- lê os `ItemIds` configurados;
- identifica os `accountIds`;
- consulta contas;
- consulta transações;
- consulta cartões;
- consulta faturas;
- consulta investimentos;
- consulta empréstimos;
- consulta categorias;
- gera tabelas derivadas para o dashboard;
- grava os resultados em CSV.

Os arquivos são gravados em:

```text
data/
```

Quando a aplicação é executada novamente, os CSVs existentes são sobrescritos com os dados atualizados.

---

## 2. `controlefin_server.py`

Backend local da aplicação.

Ele:

1. identifica a pasta permanente do projeto;
2. lê o `.env`;
3. executa o exportador da Pluggy;
4. atualiza os arquivos em `data/`;
5. inicia um servidor FastAPI local;
6. serve os arquivos do dashboard;
7. abre o navegador automaticamente.

O backend é iniciado somente em:

```text
127.0.0.1
```

por padrão, evitando exposição da aplicação na rede local.

---

## 3. `controlefin_dashboard.html`

Interface visual do ControleFin.

O dashboard lê os arquivos gerados pelo backend e apresenta análises de:

- gastos mensais;
- gastos por categoria;
- gastos por merchant;
- gastos por banco;
- gastos por tipo;
- cartão de crédito;
- PIX;
- boletos;
- entradas;
- patrimônio;
- investimentos;
- contas;
- transações.

O HTML também permite reclassificar transações e criar categorias personalizadas.

---

# Estrutura recomendada

A estrutura local recomendada é:

```text
ControleFin/
│
├── controlefin_server.py
├── pluggy_finance_export.py
├── controlefin_dashboard.html
├── README.md
├── .gitignore
├── .env.example
├── .env
│
├── data/
│   ├── transactions.csv
│   ├── accounts.csv
│   ├── investments.csv
│   └── ...
│
├── user_data/
│   └── ajustes.json
│
├── .venv/
│
├── build/
│
└── dist/
    └── ControleFin/
        ├── ControleFin.exe
        └── _internal/
```

**Importante:** `.env`, `data/`, `user_data/`, `.venv/`, `build/` e `dist/` não devem ser enviados para o GitHub.

---

# Requisitos

Recomendado:

- Windows 10 ou 11;
- Python 3.11+;
- conta Pluggy;
- Client ID da Pluggy;
- Client Secret da Pluggy;
- Item IDs das conexões utilizadas.

Dependências principais:

```text
requests
fastapi
uvicorn
python-dotenv
pyinstaller
```

---

# Instalação local

Clone o projeto:

```powershell
git clone <URL_DO_SEU_REPOSITORIO>
cd ControleFin
```

Crie o ambiente virtual:

```powershell
py -m venv .venv
```

Ative:

```powershell
.\.venv\Scripts\Activate.ps1
```

Atualize o pip:

```powershell
python -m pip install --upgrade pip
```

Instale as dependências:

```powershell
pip install requests fastapi uvicorn python-dotenv pyinstaller
```

---

# Configuração das credenciais

Copie:

```text
.env.example
```

para:

```text
.env
```

No PowerShell:

```powershell
Copy-Item ".env.example" ".env"
```

Edite o `.env`:

```env
PLUGGY_CLIENT_ID=seu_client_id
PLUGGY_CLIENT_SECRET=seu_client_secret
PLUGGY_ITEM_IDS=item_id_1,item_id_2,item_id_3
```

Nunca envie o arquivo `.env` para o GitHub.

O `.gitignore` deste projeto já bloqueia esse arquivo.

---

# Variáveis de ambiente

## `PLUGGY_CLIENT_ID`

Client ID fornecido pela Pluggy.

```env
PLUGGY_CLIENT_ID=...
```

## `PLUGGY_CLIENT_SECRET`

Client Secret fornecido pela Pluggy.

```env
PLUGGY_CLIENT_SECRET=...
```

## `PLUGGY_ITEM_IDS`

Lista dos Item IDs conhecidos, separados por vírgula.

```env
PLUGGY_ITEM_IDS=id_1,id_2,id_3
```

`ItemId` representa uma conexão Pluggy.

`accountId` representa uma conta ou cartão pertencente a uma conexão.

---

# Executando pelo Python

Com o ambiente virtual ativo:

```powershell
python ".\controlefin_server.py"
```

A aplicação deverá:

1. autenticar na Pluggy;
2. atualizar os CSVs;
3. iniciar o backend;
4. abrir automaticamente:

```text
http://127.0.0.1:8765
```

---

# Dados financeiros locais

Todos os dados extraídos ficam em:

```text
data/
```

Exemplos:

```text
data/
├── accounts.csv
├── bank_accounts.csv
├── credit_cards.csv
├── transactions.csv
├── investments.csv
├── investment_transactions.csv
├── credit_card_bills.csv
├── dashboard_kpis.csv
├── monthly_cashflow.csv
├── monthly_spending_by_category.csv
├── monthly_spending_by_merchant.csv
└── ...
```

Esses arquivos podem conter informações financeiras pessoais.

Por esse motivo:

```text
data/
```

está inteiramente ignorado pelo Git.

---

# Dados pessoais e ajustes

O diretório:

```text
user_data/
```

é reservado para configurações pessoais, categorias customizadas e reclassificações de transações.

Exemplo:

```text
user_data/
└── ajustes.json
```

Esse diretório também não deve ser versionado.

---

# Compilando o executável

Com o ambiente virtual ativo:

```powershell
python -m PyInstaller --clean --onedir --name ControleFin ".\controlefin_server.py"
```

O resultado será criado em:

```text
dist/
└── ControleFin/
    ├── ControleFin.exe
    └── _internal/
```

Execute:

```text
dist\ControleFin\ControleFin.exe
```

---

# Importante sobre `dist/`

A pasta `dist/` é gerada pelo PyInstaller e deve ser considerada **descartável**.

Nunca coloque manualmente dentro de `dist/`:

- `.env`;
- `data/`;
- `user_data/`;
- credenciais;
- dados pessoais.

Ao executar:

```powershell
python -m PyInstaller --clean ...
```

o PyInstaller pode apagar e recriar o conteúdo de `dist/`.

Por isso os arquivos permanentes ficam na raiz do projeto.

---

# Estrutura em produção local

Exemplo:

```text
C:\ControleFin\
│
├── .env
├── controlefin_dashboard.html
├── controlefin_server.py
├── pluggy_finance_export.py
│
├── data\
├── user_data\
│
└── dist\
    └── ControleFin\
        └── ControleFin.exe
```

Ao executar:

```text
C:\ControleFin\dist\ControleFin\ControleFin.exe
```

o backend identifica:

```text
BASE_DIR = C:\ControleFin
```

e utiliza:

```text
C:\ControleFin\.env
C:\ControleFin\data\
C:\ControleFin\user_data\
C:\ControleFin\controlefin_dashboard.html
```

Assim, recompilar o executável não apaga os dados permanentes.

---

# Segurança

Este projeto manipula dados financeiros pessoais.

Recomendações:

- nunca versionar `.env`;
- nunca versionar `data/`;
- nunca versionar `user_data/`;
- nunca colocar Client Secret no código;
- nunca enviar CSVs reais para Issues ou Pull Requests;
- não publicar prints que contenham saldos, documentos ou IDs sensíveis;
- manter o backend em `127.0.0.1`;
- não alterar o backend para `0.0.0.0` sem entender as consequências;
- não colocar o repositório em modo público se arquivos sensíveis já tiverem sido commitados anteriormente.

---

# O que pode ir para o GitHub

Arquivos recomendados:

```text
controlefin_server.py
pluggy_finance_export.py
controlefin_dashboard.html
README.md
.gitignore
.env.example
requirements.txt
```

Opcionalmente:

```text
LICENSE
docs/
tests/
```

---

# O que NÃO pode ir para o GitHub

Não versionar:

```text
.env
data/
user_data/
.venv/
dist/
build/
__pycache__/
*.spec
```

Também não versionar arquivos de backup contendo dados reais.

---

# Antes do primeiro `git push`

Confira o que será versionado:

```powershell
git status
```

O resultado **não deve mostrar**:

```text
.env
data/
user_data/
.venv/
dist/
```

Você também pode conferir:

```powershell
git status --ignored
```

para verificar se esses arquivos estão sendo ignorados.

---

# Inicializando o repositório

Caso ainda não tenha iniciado o Git:

```powershell
git init
```

Adicione os arquivos permitidos:

```powershell
git add .
```

Confira:

```powershell
git status
```

Crie o commit:

```powershell
git commit -m "Initial ControleFin project"
```

Adicione o remoto:

```powershell
git remote add origin <URL_DO_REPOSITORIO>
```

Envie:

```powershell
git branch -M main
git push -u origin main
```

---

# Se um segredo já foi commitado

Adicionar o arquivo ao `.gitignore` **não remove o segredo do histórico do Git**.

Se `.env`, Client Secret ou dados financeiros já tiverem sido commitados:

1. não faça apenas `git rm`;
2. considere o segredo comprometido;
3. gere novas credenciais na Pluggy;
4. remova o arquivo do histórico do repositório antes de publicar.

Se isso acontecer, não publique o repositório até limpar o histórico.

---

# Verificando o `.gitignore`

Teste:

```powershell
git check-ignore -v ".env"
git check-ignore -v "data\transactions.csv"
git check-ignore -v "user_data\ajustes.json"
```

O Git deve informar qual regra está ignorando cada arquivo.

---

# Atualização dos dados

Toda vez que o ControleFin é iniciado pelo backend:

```text
Pluggy API
    ↓
pluggy_finance_export.py
    ↓
data/*.csv
```

Os CSVs anteriores são sobrescritos com os dados mais recentes.

Esses dados continuam somente no ambiente local e não são enviados ao Git.

---

# Desenvolvimento

Durante o desenvolvimento, execute:

```powershell
python ".\controlefin_server.py"
```

Para recompilar:

```powershell
python -m PyInstaller --clean --onedir --name ControleFin ".\controlefin_server.py"
```

Não é necessário versionar o `.exe`.

O executável deve ser considerado um artefato de build.

---

# Backup

Os arquivos mais importantes para backup pessoal são:

```text
.env
user_data/
```

e, opcionalmente:

```text
data/
```

Esses backups devem ser armazenados de forma privada.

Não utilize o próprio repositório Git como backup desses arquivos.

---

# Privacidade

O ControleFin foi estruturado para funcionar localmente.

Dados financeiros extraídos da Pluggy são armazenados localmente em CSV.

O dashboard é servido por um backend local em:

```text
127.0.0.1
```

Nenhum arquivo em `data/` deve ser publicado no GitHub.

---

# Licença

Defina uma licença antes de tornar o projeto público.

Se o projeto for exclusivamente pessoal, você também pode optar por manter o repositório privado e não adicionar uma licença.
