# ControleFin para 1–2 outras pessoas

Esta versão foi preparada para o modelo:

```text
Pessoa A / computador A
    ├─ usuário/senha local A
    ├─ credenciais Pluggy A
    ├─ Item IDs A
    └─ controlefin.db A

Pessoa B / computador B
    ├─ usuário/senha local B
    ├─ credenciais Pluggy B
    ├─ Item IDs B
    └─ controlefin.db B
```

Não existe servidor central e uma pessoa não compartilha dados com outra.

## Primeiro acesso

1. A pessoa abre `ControleFin.exe`.
2. Cria seu usuário e senha local.
3. Informa:
   - Client ID da própria conta Pluggy;
   - Client Secret da própria conta Pluggy;
   - seus Item IDs.
4. O ControleFin testa autenticação e todos os Item IDs.
5. Somente se o teste passar a configuração é salva.
6. O ControleFin faz a primeira sincronização e cria o SQLite local.

## Onde os dados ficam

No Windows:

```text
%LOCALAPPDATA%\ControleFin\
├── data\
│   └── controlefin.db
├── user_data\
│   ├── ajustes.json
│   └── auth.json
└── config\
    └── pluggy.json
```

Isso permite que a pasta do programa seja atualizada/recompilada sem apagar
dados pessoais.

## Proteção do Client Secret

`config\pluggy.json` nunca contém o Client Secret em texto puro.

O segredo é protegido com **Windows DPAPI / Current User**. Na prática:

- outra conta do Windows não consegue simplesmente copiar o arquivo e ler a
  chave;
- copiar `pluggy.json` para outro computador não fornece o segredo utilizável;
- o navegador nunca recebe o Client Secret já salvo.

Na tela de edição, deixar Client Secret vazio significa **manter o segredo
atual**.

## Sincronização

O botão **Sincronizar** chama:

```text
POST /api/sync
```

O backend:
1. lê a configuração local;
2. desbloqueia o Client Secret com DPAPI;
3. autentica na Pluggy;
4. coleta os Item IDs daquela pessoa;
5. constrói o SQLite em staging;
6. valida;
7. publica atomicamente.

## Configuração Pluggy

O botão **Pluggy** no topo permite:
- trocar Client ID;
- trocar Client Secret;
- adicionar/remover Item IDs;
- testar a conexão;
- salvar e sincronizar.

## Migração da sua instalação atual

Se você já usa o ControleFin antigo:

- `data\controlefin.db`;
- `user_data\ajustes.json`;
- `user_data\auth.json`;

são copiados para `%LOCALAPPDATA%\ControleFin` quando necessário.

Se existir um `.env` antigo contendo as três chaves Pluggy, no Windows ele é
importado para `config\pluggy.json` usando DPAPI. Depois que o novo segredo é
validado localmente, o `.env` antigo é removido para não deixar o Client Secret
em texto.

## Como gerar a versão para compartilhar

Na sua máquina:

```powershell
.\BUILD_WINDOWS.ps1
```

Esse script:
1. instala/atualiza dependências;
2. executa toda a suíte de regressão;
3. gera o executável com PyInstaller.

Depois entregue somente:

```text
dist\ControleFin\
├── ControleFin.exe
└── _internal\
```

Não envie:
- `.env`;
- `data\`;
- `user_data\`;
- `config\`;
- seu `controlefin.db`.

Cada pessoa cria tudo isso no próprio computador.

## Atualização futura

Para enviar uma versão nova, basta substituir a pasta do programa. Os dados
continuam em `%LOCALAPPDATA%\ControleFin`, fora de `dist`.
