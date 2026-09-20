ControleFin — autenticação Google primária + senha adicional opcional

Base esperada:
    controlefin_onboarding_restore_drive

Novo fluxo:
    Continuar com Google
      -> escolher a própria conta Google
      -> ControleFin identifica/abre o perfil dessa conta
      -> senha adicional ativa?
           não -> continua
           sim -> pede a senha local adicional
      -> conta nova: Restaurar do Drive OU Configurar Pluggy
      -> dashboard

Computador compartilhado:
- Google A abre perfil A;
- Google B abre perfil B;
- SQLite, ajustes, Pluggy, token Google e Cloud Sync são separados;
- uma única conta/perfil fica ativa por vez no processo local.

Senha adicional:
- opcional;
- criada depois do login em Segurança;
- PBKDF2-HMAC-SHA256 / 600.000 iterações;
- nunca substitui o Google;
- não é sincronizada no Drive.

OAuth:
- o mesmo OAuth Client é usado por todos;
- cada pessoa escolhe a própria conta;
- o fluxo usa seletor de conta no Google;
- o token temporário pré-perfil é protegido por DPAPI e depois movido para o perfil correto.

Depois de aplicar:
    python -m pip install -r .\requirements.txt
    python .\run_tests.py
    python .\controlefin_server.py

Para gerar EXE:
    .\BUILD_WINDOWS.ps1

Para distribuição, prefira incluir o OAuth Client no build usando:
    credentials.json
ou:
    google_oauth_client.json

O build gera:
    dist\ControleFin\google_oauth_client.bundled.json

Nunca distribua %LOCALAPPDATA%\ControleFin, bancos, tokens, pluggy.json, .env ou dados financeiros.
