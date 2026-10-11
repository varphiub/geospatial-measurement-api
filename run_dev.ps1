$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Python = Join-Path $Root "venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    Write-Error "Virtual environment not found. Run: python -m venv venv; .\venv\Scripts\pip install -r requirements.txt"
}

Set-Location $Root
& $Python manage.py migrate
& $Python manage.py runserver
