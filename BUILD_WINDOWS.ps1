$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

function Require-File {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    if (-not (Test-Path $Path -PathType Leaf)) {
        throw "$Description não encontrado: $Path"
    }
}

function Copy-OptionalFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Source,
        [Parameter(Mandatory = $true)]
        [string]$Destination
    )

    if (Test-Path $Source -PathType Leaf) {
        Copy-Item $Source $Destination -Force
        Write-Host "  OK  $(Split-Path $Source -Leaf)"
    } else {
        Write-Host "  SKIP $(Split-Path $Source -Leaf) (opcional)"
    }
}

Write-Host ""
Write-Host "============================================================"
Write-Host " ControleFin - Build Windows"
Write-Host "============================================================"
Write-Host ""

# ---------------------------------------------------------------------------
# 0) Validação dos arquivos obrigatórios do projeto
# ---------------------------------------------------------------------------

Require-File ".\requirements.txt" "requirements.txt"
Require-File ".\run_tests.py" "run_tests.py"
Require-File ".\controlefin_server.py" "controlefin_server.py"
Require-File ".\controlefin_dashboard.html" "controlefin_dashboard.html"

# ---------------------------------------------------------------------------
# 1) Localiza e valida o OAuth Client real
# ---------------------------------------------------------------------------

$GoogleOAuthSource = $null

if (Test-Path ".\google_oauth_client.json" -PathType Leaf) {
    $GoogleOAuthSource = ".\google_oauth_client.json"
}
elseif (Test-Path ".\credentials.json" -PathType Leaf) {
    $GoogleOAuthSource = ".\credentials.json"
}
elseif (
    $env:LOCALAPPDATA -and
    (Test-Path "$env:LOCALAPPDATA\ControleFin\config\google_oauth_client.json" -PathType Leaf)
) {
    $GoogleOAuthSource =
        "$env:LOCALAPPDATA\ControleFin\config\google_oauth_client.json"
}

if (-not $GoogleOAuthSource) {
    throw @"
OAuth Client REAL não encontrado.

Coloque na raiz do projeto UM destes arquivos:

    credentials.json

ou

    google_oauth_client.json

O arquivo google_oauth_client.example.json é apenas um exemplo e NÃO é usado.

Você também pode importar o OAuth no ControleFin deste computador; nesse caso
o build tentará reaproveitar:

    %LOCALAPPDATA%\ControleFin\config\google_oauth_client.json
"@
}

Write-Host "OAuth Client encontrado:"
Write-Host "  $GoogleOAuthSource"

try {
    $OAuthJson =
        Get-Content `
            $GoogleOAuthSource `
            -Raw `
            -Encoding UTF8 |
        ConvertFrom-Json
}
catch {
    throw "O OAuth Client não contém JSON válido: $GoogleOAuthSource"
}

if (-not $OAuthJson.installed) {
    throw @"
O arquivo OAuth não é do tipo Desktop/Installed.

No Google Cloud, crie um OAuth Client ID do tipo:

    Desktop app

e baixe novamente o JSON.
"@
}

if (-not $OAuthJson.installed.client_id) {
    throw "client_id não encontrado no OAuth Client."
}

if (-not $OAuthJson.installed.auth_uri) {
    throw "auth_uri não encontrado no OAuth Client."
}

if (-not $OAuthJson.installed.token_uri) {
    throw "token_uri não encontrado no OAuth Client."
}

Write-Host "  OK  OAuth Desktop válido"

$BundledOAuthStage =
    Join-Path `
        $PSScriptRoot `
        "google_oauth_client.bundled.json"

Copy-Item `
    $GoogleOAuthSource `
    $BundledOAuthStage `
    -Force

Write-Host "  OK  OAuth preparado para empacotamento"
Write-Host ""

# ---------------------------------------------------------------------------
# 2) Dependências
# ---------------------------------------------------------------------------

Write-Host "1/5 - Instalando dependências..."
python -m pip install -r .\requirements.txt

if ($LASTEXITCODE -ne 0) {
    throw "Falha ao instalar as dependências."
}

Write-Host ""

# ---------------------------------------------------------------------------
# 3) Testes
# ---------------------------------------------------------------------------

Write-Host "2/5 - Executando testes..."
python .\run_tests.py

if ($LASTEXITCODE -ne 0) {
    throw "Os testes falharam. O build foi interrompido."
}

Write-Host ""

# ---------------------------------------------------------------------------
# 4) Limpeza para impedir prompts do PyInstaller
# ---------------------------------------------------------------------------

Write-Host "3/5 - Limpando build anterior..."

if (Test-Path ".\build") {
    Remove-Item ".\build" -Recurse -Force
}

if (Test-Path ".\dist\ControleFin") {
    Remove-Item ".\dist\ControleFin" -Recurse -Force
}

if (Test-Path ".\dist\ControleFin-Windows.zip") {
    Remove-Item ".\dist\ControleFin-Windows.zip" -Force
}

Write-Host "  OK  limpeza concluída"
Write-Host ""

# ---------------------------------------------------------------------------
# 5) PyInstaller
# ---------------------------------------------------------------------------

Write-Host "4/5 - Gerando ControleFin.exe..."

$PyInstallerArgs = @(
    "-m",
    "PyInstaller",
    "--clean",
    "--noconfirm",
    "--onedir",
    "--name",
    "ControleFin",
    "--collect-all",
    "googleapiclient",
    "--collect-all",
    "google_auth_oauthlib",
    "--collect-submodules",
    "google.auth",
    "--collect-submodules",
    "google.oauth2",
    "--collect-all",
    "cryptography"
)

if (Test-Path ".\FinAPP.ico" -PathType Leaf) {
    $PyInstallerArgs += @(
        "--icon",
        ".\FinAPP.ico"
    )
}

$PyInstallerArgs += @(
    "--add-data",
    "$BundledOAuthStage;."
)

$PyInstallerArgs += ".\controlefin_server.py"

python @PyInstallerArgs

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller falhou."
}

Write-Host ""

# ---------------------------------------------------------------------------
# 6) Arquivos externos necessários para o aplicativo congelado
# ---------------------------------------------------------------------------

Write-Host "5/5 - Montando distribuição..."

$DistDir = ".\dist\ControleFin"

if (-not (Test-Path $DistDir -PathType Container)) {
    throw "PyInstaller não criou a pasta esperada: $DistDir"
}

Copy-Item `
    ".\controlefin_dashboard.html" `
    "$DistDir\controlefin_dashboard.html" `
    -Force

Write-Host "  OK  controlefin_dashboard.html"

Copy-Item `
    $BundledOAuthStage `
    "$DistDir\google_oauth_client.bundled.json" `
    -Force

Write-Host "  OK  google_oauth_client.bundled.json"

# Documentação é útil, mas NÃO deve quebrar o build caso esteja ausente.
$OptionalDocs = @(
    "README.md",
    "README.txt",
    "COMPARTILHAR_COM_OUTRAS_PESSOAS.md",
    "GOOGLE_DRIVE_OAUTH.md",
    "GOOGLE_DRIVE_SYNC.md",
    "GOOGLE_DRIVE_VISIBLE_FOLDER.md",
    "MULTIUSUARIO_LOCAL.md",
    "PRIMEIRO_ACESSO_RESTAURAR_DRIVE.md",
    "AUTENTICACAO_GOOGLE_PRIMARIA.md",
    "BACKUP_SEM_SENHA_DA_NUVEM.md"
)

foreach ($Doc in $OptionalDocs) {
    Copy-OptionalFile `
        ".\$Doc" `
        "$DistDir\$Doc"
}

# ---------------------------------------------------------------------------
# 7) Validação final
# ---------------------------------------------------------------------------

$RequiredDistFiles = @(
    "$DistDir\ControleFin.exe",
    "$DistDir\controlefin_dashboard.html",
    "$DistDir\google_oauth_client.bundled.json"
)

foreach ($Required in $RequiredDistFiles) {
    if (-not (Test-Path $Required -PathType Leaf)) {
        throw "Build incompleto. Arquivo obrigatório ausente: $Required"
    }
}

if (-not (Test-Path "$DistDir\_internal" -PathType Container)) {
    throw "Build incompleto. A pasta _internal está ausente."
}

$InternalOAuth =
    "$DistDir\_internal\google_oauth_client.bundled.json"

if (-not (Test-Path $InternalOAuth -PathType Leaf)) {
    throw "Build incompleto. OAuth Client não foi incorporado em _internal."
}

Write-Host "  OK  OAuth incorporado em _internal"

# Garante que nenhum arquivo de exemplo seja confundido com credencial real.
if (Test-Path "$DistDir\google_oauth_client.example.json") {
    Remove-Item `
        "$DistDir\google_oauth_client.example.json" `
        -Force
}

# ---------------------------------------------------------------------------
# 8) ZIP pronto para distribuição
# ---------------------------------------------------------------------------

$ZipPath = ".\dist\ControleFin-Windows.zip"

Compress-Archive `
    -Path $DistDir `
    -DestinationPath $ZipPath `
    -CompressionLevel Optimal `
    -Force

if (-not (Test-Path $ZipPath -PathType Leaf)) {
    throw "Não foi possível criar o ZIP de distribuição."
}

Write-Host ""
Write-Host "============================================================"
Write-Host " BUILD CONCLUÍDO COM SUCESSO"
Write-Host "============================================================"
Write-Host ""
Write-Host "Pasta pronta:"
Write-Host "  $((Resolve-Path $DistDir).Path)"
Write-Host ""
Write-Host "ZIP para enviar a outro computador:"
Write-Host "  $((Resolve-Path $ZipPath).Path)"
Write-Host ""
Write-Host "Arquivos obrigatórios confirmados:"
Write-Host "  ControleFin.exe"
Write-Host "  _internal\"
Write-Host "  controlefin_dashboard.html"
Write-Host "  google_oauth_client.bundled.json"
Write-Host ""
if (Test-Path $BundledOAuthStage -PathType Leaf) {
    Remove-Item $BundledOAuthStage -Force
}

Write-Host "IMPORTANTE:"
Write-Host "  - Envie o ZIP inteiro, não apenas ControleFin.exe."
Write-Host "  - Cada pessoa entra com a própria conta Google."
Write-Host "  - Dados pessoais continuam em %LOCALAPPDATA%\ControleFin\."
Write-Host ""
