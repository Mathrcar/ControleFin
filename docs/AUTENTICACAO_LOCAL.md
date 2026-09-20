# ControleFin — autenticação local

## Fluxo de abertura

O HTML não mostra mais o antigo bloco de carregamento de dados.

Ao abrir o ControleFin:

```text
loading
  ↓
/api/auth/status
  ↓
auth.json existe?
  ├─ não → criar primeiro usuário + senha
  └─ sim
       ↓
sessão válida?
  ├─ sim → loading do SQLite → dashboard
  └─ não → login → loading do SQLite → dashboard
```

## Onde as credenciais ficam

O arquivo é:

```text
user_data/auth.json
```

A senha **não é gravada em texto**.

O arquivo contém:
- nome do usuário;
- salt aleatório;
- hash PBKDF2-HMAC-SHA256;
- quantidade de iterações;
- data de criação.

## Sessão

Depois do login, o backend gera um token aleatório em memória e envia um cookie:

- `HttpOnly`;
- `SameSite=Strict`;
- duração de 12 horas;
- válido somente para o servidor local.

O token não é armazenado em `localStorage`.

Ao reiniciar o servidor, as sessões em memória são descartadas e o usuário
poderá precisar entrar novamente com o usuário e a senha já cadastrados.

## Proteção das APIs

Todo endpoint sob `/api/` exige autenticação, exceto `/api/auth/*`.

Assim, sem login não é possível acessar:
- `/api/db/status`;
- `/api/db/catalog`;
- `/api/db/table/...`;
- `/api/settings`;
- futuros endpoints adicionados sob `/api/`.

## Primeiro acesso

A criação do usuário só é permitida quando `user_data/auth.json` não existe.

Se o arquivo existir mas estiver corrompido, o servidor não o trata como
"primeiro acesso", evitando recriar silenciosamente a conta.

## Logout

O botão **Sair** revoga a sessão atual e retorna para a tela de login.

## Recuperação de senha

Esta versão não cria uma porta de recuperação automática. Isso é intencional:
uma recuperação sem conhecimento da senha enfraqueceria a proteção local.

Se for necessário implementar recuperação futuramente, ela deve ser tratada
como uma função administrativa local explícita.
