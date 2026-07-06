<#
Researchy dev launcher - one command brings up the full local stack.

  .\scripts\dev.ps1          start infra (Docker), the API, and the Celery worker
  .\scripts\dev.ps1 -Stop    stop the API/worker windows and the Docker infra

The API and worker open in their own console windows so their logs stay
separate; this window is free again once they're up.
#>
param(
    [switch]$Stop
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$pidFile = Join-Path $root ".dev.pids"
$py = Join-Path $root ".venv\Scripts\python.exe"

if ($Stop) {
    if (Test-Path $pidFile) {
        foreach ($procId in Get-Content $pidFile) {
            # /T kills the whole tree (the window shell + uvicorn/celery children)
            cmd /c "taskkill /PID $procId /T /F >nul 2>&1"
        }
        Remove-Item $pidFile
        Write-Host "[dev] API and worker stopped."
    } else {
        Write-Host "[dev] no $pidFile - nothing to stop on the host."
    }
    docker compose stop
    exit 0
}

# --- preflight ---------------------------------------------------------------
if (-not (Test-Path $py)) {
    Write-Host "[dev] .venv not found. Create it first:"
    Write-Host '    python -m venv .venv; .venv\Scripts\pip install -e ".[dev]"'
    exit 1
}
if (-not (Test-Path (Join-Path $root ".env"))) {
    Write-Host "[dev] warning: no .env - copy .env.example and fill in your keys."
}
if (Test-Path $pidFile) {
    Write-Host "[dev] $pidFile exists - stack may already be running."
    Write-Host "      Run '.\scripts\dev.ps1 -Stop' first."
    exit 1
}

# --- 1. infra (--wait blocks until the Postgres/Redis healthchecks pass) -----
Write-Host "[dev] starting infra: docker compose up -d --wait ..."
docker compose up -d --wait
if ($LASTEXITCODE -ne 0) {
    Write-Host "[dev] docker compose failed - is Docker Desktop running?"
    exit 1
}

# --- 2. schema ----------------------------------------------------------------
Write-Host "[dev] applying migrations: alembic upgrade head ..."
& $py -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { exit 1 }

# --- 3. API + worker, each in its own window ----------------------------------
$api = Start-Process powershell -PassThru -ArgumentList @(
    "-NoExit", "-Command",
    "`$host.UI.RawUI.WindowTitle = 'Researchy API'; Set-Location '$root'; & '$py' -m uvicorn research_assistant.api.app:app --reload"
)
$worker = Start-Process powershell -PassThru -ArgumentList @(
    "-NoExit", "-Command",
    "`$host.UI.RawUI.WindowTitle = 'Researchy worker'; Set-Location '$root'; & '$py' -m celery -A research_assistant.tasks.celery_app worker --loglevel=info --pool=solo"
)
Set-Content $pidFile @($api.Id, $worker.Id)

Write-Host ""
Write-Host "[dev] stack is up:"
Write-Host "  API    http://127.0.0.1:8000    (docs: http://127.0.0.1:8000/docs)"
Write-Host "  key    .venv\Scripts\python.exe -m research_assistant.scripts.issue_api_key u1"
Write-Host "  stop   .\scripts\dev.ps1 -Stop"
