# Compartilhar o ControleFin com 1–2 pessoas

## O que é compartilhado

Todos usam o mesmo executável, o mesmo projeto Google Cloud e o mesmo OAuth
Client do ControleFin. Cada pessoa entra com a própria conta Google e usa o
próprio Google Drive, banco, Pluggy e ajustes.

```text
ControleFin / OAuth Client único
├── pessoa A → Google A → perfil A → Drive A
└── pessoa B → Google B → perfil B → Drive B
```

## Distribuição

Entregue somente a pasta gerada pelo build:

```text
dist\ControleFin\
├── ControleFin.exe
├── google_oauth_client.bundled.json   # quando configurado no build
└── _internal\
```

Nunca distribua `%LOCALAPPDATA%\ControleFin`, bancos SQLite, tokens, arquivos
`pluggy.json`, `.env` ou dados financeiros.

## Primeiro uso

1. Abrir `ControleFin.exe`.
2. Clicar **Continuar com Google**.
3. Escolher a própria conta Google.
4. Se a conta for nova no ControleFin, escolher entre restaurar um backup do
   Drive ou configurar a própria Pluggy.
5. Opcionalmente ativar uma senha adicional em **Segurança**.

## Mesmo computador

Duas pessoas podem usar a mesma instalação desde que cada uma use sua própria
conta Google. O ControleFin mantém um perfil separado para cada identidade e
opera somente um perfil por vez.

## Google Cloud em modo Testing

Enquanto o OAuth estiver em modo de teste no Google Cloud, inclua as contas das
pessoas autorizadas na lista de usuários de teste do projeto.

## Build

```powershell
python -m pip install -r .\requirements.txt
python .\run_tests.py
.\BUILD_WINDOWS.ps1
```
