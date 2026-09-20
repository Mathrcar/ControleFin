$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

Write-Host "1/4 - Instalando dependências..."
python -m pip install -r .\requirements.txt

Write-Host "2/4 - Executando testes..."
python .\run_tests.py

Write-Host "3/4 - Gerando ControleFin.exe..."
python -m PyInstaller `
  --clean `
  --onedir `
  --name ControleFin `
  --collect-all googleapiclient `
  --collect-all google_auth_oauthlib `
  --collect-submodules google.auth `
  --collect-submodules google.oauth2 `
  --collect-all cryptography `
  ".\controlefin_server.py"

Write-Host "4/4 - Copiando arquivos do aplicativo..."
Copy-Item `
  ".\controlefin_dashboard.html" `
  ".\dist\ControleFin\controlefin_dashboard.html" `
  -Force

Copy-Item `
  ".\GOOGLE_DRIVE_OAUTH.md" `
  ".\dist\ControleFin\GOOGLE_DRIVE_OAUTH.md" `
  -Force

Copy-Item `
  ".\GOOGLE_DRIVE_SYNC.md" `
  ".\dist\ControleFin\GOOGLE_DRIVE_SYNC.md" `
  -Force

Copy-Item `
  ".\MULTIUSUARIO_LOCAL.md" `
  ".\dist\ControleFin\MULTIUSUARIO_LOCAL.md" `
  -Force

Copy-Item `
  ".\GOOGLE_DRIVE_VISIBLE_FOLDER.md" `
  ".\dist\ControleFin\GOOGLE_DRIVE_VISIBLE_FOLDER.md" `
  -Force

Copy-Item `
  ".\PRIMEIRO_ACESSO_RESTAURAR_DRIVE.md" `
  ".\dist\ControleFin\PRIMEIRO_ACESSO_RESTAURAR_DRIVE.md" `
  -Force

Copy-Item `
  ".\AUTENTICACAO_GOOGLE_PRIMARIA.md" `
  ".\dist\ControleFin\AUTENTICACAO_GOOGLE_PRIMARIA.md" `
  -Force

$GoogleOAuthSource = $null

if (Test-Path ".\google_oauth_client.json") {
  $GoogleOAuthSource = ".\google_oauth_client.json"
} elseif (Test-Path ".\credentials.json") {
  $GoogleOAuthSource = ".\credentials.json"
} elseif ($env:LOCALAPPDATA -and (Test-Path "$env:LOCALAPPDATA\ControleFin\config\google_oauth_client.json")) {
  $GoogleOAuthSource = "$env:LOCALAPPDATA\ControleFin\config\google_oauth_client.json"
}

if ($GoogleOAuthSource) {
  Write-Host "Incluindo OAuth Client compartilhado do ControleFin: $GoogleOAuthSource"
  Copy-Item `
    $GoogleOAuthSource `
    ".\dist\ControleFin\google_oauth_client.bundled.json" `
    -Force
} else {
  Write-Warning "OAuth Client não encontrado. Coloque google_oauth_client.json ou credentials.json na raiz do projeto. O build funcionará, mas exigirá configuração administrativa manual do OAuth."
}

Write-Host "Todos os usuários usam o mesmo OAuth Client do aplicativo e autorizam suas próprias contas Google."

Write-Host ""
Write-Host "Build concluído."
Write-Host "Entregue a pasta: dist\ControleFin\"
Write-Host "Dados pessoais continuam em %LOCALAPPDATA%\ControleFin\."
