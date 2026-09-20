$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

Write-Host "1/3 - Instalando dependências..."
python -m pip install -r .\requirements.txt

Write-Host "2/3 - Executando testes..."
python .\run_tests.py

Write-Host "3/3 - Gerando ControleFin.exe..."
python -m PyInstaller `
  --clean `
  --onedir `
  --name ControleFin `
  ".\controlefin_server.py"

Write-Host ""
Write-Host "Build concluído."
Write-Host "Entregue a pasta: dist\ControleFin\"
Write-Host "Não inclua .env, data\, user_data\ ou config\."
