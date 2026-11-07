# One-click practice mode for Windows: no Docker, no database, pretend money.
# Double-click "Start tradeflow.bat" / "Stop tradeflow.bat" in the project folder.
#
# Always PAPER mode with simulated prices and in-memory storage (STORAGE=memory),
# which live mode refuses. Robots may trade at any time of day, since prices are
# simulated anyway. Everything this script creates lives in .practice\ (gitignored).
# Keep this file ASCII-only: Windows PowerShell 5.1 reads it in the ANSI code page.

param(
    [ValidateSet("start", "stop")][string]$Action = "start",
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$State = Join-Path $Root ".practice"
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"
$ApiPort = 8000
$WebPort = 5180
$WebUrl = "http://localhost:$WebPort"

function Say([string]$Text, [string]$Color = "Gray") { Write-Host $Text -ForegroundColor $Color }

function Finish([int]$Code) {
    if (-not $NoPause) { Write-Host ""; Read-Host "Press Enter to close this window" | Out-Null }
    exit $Code
}

function Fail([string]$Text) {
    Say ""
    Say "PROBLEM: $Text" Red
    Finish 1
}

function Stop-Practice {
    foreach ($name in "backend", "web") {
        $pidFile = Join-Path $State "$name.pid"
        if (Test-Path $pidFile) {
            $procId = (Get-Content $pidFile -Raw).Trim()
            # /T also ends child processes (npx starts node underneath).
            & taskkill.exe /PID $procId /T /F 2>&1 | Out-Null
            Remove-Item $pidFile -Force
        }
    }
}

function Port-InUse([int]$Port) {
    $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return $null -ne $c
}

function Wait-Url([string]$Url, [int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
            if ($r.StatusCode -lt 500) { return $true }
        } catch { Start-Sleep -Seconds 2 }
    }
    return $false
}

function Hash-Of([string[]]$Paths) {
    ($Paths | ForEach-Object { (Get-FileHash $_ -Algorithm SHA256).Hash }) -join ""
}

New-Item -ItemType Directory -Force $State | Out-Null

if ($Action -eq "stop") {
    Stop-Practice
    Say "tradeflow has stopped." Green
    Finish 0
}

Say ""
Say "=== Starting tradeflow (practice mode, pretend money) ===" Cyan
Say ""

# --- 1. Python 3.12 and Node 22 ----------------------------------------------
$py = $null
try { & py -3.12 --version *> $null; if ($LASTEXITCODE -eq 0) { $py = @("py", "-3.12") } } catch { }
if (-not $py) {
    try {
        $v = & python --version 2>&1
        if ("$v" -match "Python 3\.12\.") { $py = @("python") }
    } catch { }
}
if (-not $py) {
    Fail "Python 3.12 is not installed. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then double-click Start tradeflow again."
}

$nodeDir = "C:\Program Files\nodejs"
if (Test-Path (Join-Path $nodeDir "node.exe")) { $env:PATH = "$nodeDir;$env:PATH" }
$nodeVersion = ""
try { $nodeVersion = (& node --version 2>&1) } catch { }
if ("$nodeVersion" -notmatch "^v22\.") {
    Fail "Node.js 22 is not installed. Install the 22 LTS version from https://nodejs.org/, then double-click Start tradeflow again."
}
Say "[1/4] Python and Node.js found." Green

# --- 2. Install (first time, or after an update) -----------------------------
$venvPy = Join-Path $Backend ".venv\Scripts\python.exe"
$reqHash = Hash-Of @((Join-Path $Backend "requirements.txt"))
$reqMarker = Join-Path $State "backend-installed.txt"
if (-not (Test-Path $venvPy) -or -not (Test-Path $reqMarker) -or (Get-Content $reqMarker -Raw).Trim() -ne $reqHash) {
    Say "[2/4] Installing the trading engine. The first time this takes a few minutes..." Yellow
    if (-not (Test-Path $venvPy)) {
        & $py[0] $py[1..($py.Length)] -m venv (Join-Path $Backend ".venv")
        if ($LASTEXITCODE -ne 0) { Fail "Could not create the Python environment." }
    }
    & $venvPy -m pip install --disable-pip-version-check -q -r (Join-Path $Backend "requirements.txt")
    if ($LASTEXITCODE -ne 0) { Fail "Installing the trading engine failed. Check your internet connection and try again." }
    Set-Content -Path $reqMarker -Value $reqHash -Encoding ascii
}
$lockHash = Hash-Of @((Join-Path $Frontend "package-lock.json"))
$lockMarker = Join-Path $State "frontend-installed.txt"
$modules = Join-Path $Frontend "node_modules"
if ((Test-Path (Join-Path $modules ".package-lock.json")) -and -not (Test-Path $lockMarker)) {
    # Installed before this script existed (e.g. by a developer): use it as is.
    Set-Content -Path $lockMarker -Value $lockHash -Encoding ascii
}
if (-not (Test-Path $modules) -or (Get-Content $lockMarker -Raw -ErrorAction SilentlyContinue).Trim() -ne $lockHash) {
    Say "[2/4] Installing the dashboard. The first time this takes a few minutes..." Yellow
    Push-Location $Frontend
    try { & npx.cmd -y npm@11.21.1 ci --no-audit --no-fund } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) {
        Fail "Installing the dashboard failed. Close any other tradeflow windows (or restart the computer), check your internet connection, and try again."
    }
    Set-Content -Path $lockMarker -Value $lockHash -Encoding ascii
}
Say "[2/4] Everything is installed." Green

# --- 3. Start ------------------------------------------------------------------
Stop-Practice  # a previous run that was not stopped
if (Port-InUse $ApiPort) { Fail "Another program is using port $ApiPort. Close it (or restart the computer) and try again." }
if (Port-InUse $WebPort) { Fail "Another program is using port $WebPort. Close it (or restart the computer) and try again." }

$pwFile = Join-Path $State "password.txt"
if (-not (Test-Path $pwFile)) {
    $bytes = New-Object byte[] 18
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $pw = [Convert]::ToBase64String($bytes).Replace("+", "x").Replace("/", "y")
    Set-Content -Path $pwFile -Value $pw -Encoding ascii
}
$password = (Get-Content $pwFile -Raw).Trim()

# Safety: always paper, simulated prices, no database. These override any .env.
$env:MODE = "paper"
$env:LIVE_TRADING_CONFIRMED = "false"
$env:FEED = "sim"
$env:STORAGE = "memory"
$env:ADMIN_TOKEN = $password
$env:KITE_API_KEY = ""
$env:KITE_API_SECRET = ""
$env:LOG_JSON = "false"
$env:ENGINE_AUTOSTART = "true"
$env:EXIT_ON_SESSION_EXPIRY = "false"
$env:FRONTEND_URL = $WebUrl
$env:FRONTEND_ORIGIN = $WebUrl
$env:STRATEGY_OVERRIDES_FILE = (Join-Path $State "strategies.local.yaml")
# Practice any time of day (prices are simulated).
$env:MARKET_OPEN = "00:00"
$env:TRADING_START = "00:00"
$env:TRADING_END = "23:50"
$env:SQUARE_OFF_TIME = "23:55"
$env:MARKET_CLOSE = "23:59"
$env:BACKEND_URL = "http://127.0.0.1:$ApiPort"

Say "[3/4] Starting the trading engine and the dashboard..." Yellow
$api = Start-Process -FilePath $venvPy -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$ApiPort" `
    -WorkingDirectory $Backend -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $State "backend.log") -RedirectStandardError (Join-Path $State "backend-errors.log")
Set-Content -Path (Join-Path $State "backend.pid") -Value $api.Id -Encoding ascii
$web = Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "npx.cmd vite --port $WebPort --strictPort" `
    -WorkingDirectory $Frontend -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $State "web.log") -RedirectStandardError (Join-Path $State "web-errors.log")
Set-Content -Path (Join-Path $State "web.pid") -Value $web.Id -Encoding ascii

if (-not (Wait-Url "http://127.0.0.1:$ApiPort/health" 180)) {
    Stop-Practice
    Fail "The trading engine did not start. Details are in .practice\backend-errors.log"
}
if (-not (Wait-Url $WebUrl 120)) {
    Stop-Practice
    Fail "The dashboard did not start. Details are in .practice\web-errors.log"
}
Say "[4/4] Running." Green

try { Set-Clipboard -Value $password } catch { }
Start-Process $WebUrl

Say ""
Say "=============================================================" Cyan
Say " tradeflow is running in PRACTICE MODE (pretend money)." Cyan
Say ""
Say " Website : $WebUrl  (it opened in your browser)" White
Say " Password: $password" White
Say "           (already copied: click the password box and press Ctrl+V)" Gray
Say ""
Say " The first time, a 'Start here' guide walks you through everything." Gray
Say " Practice data is not kept: closing tradeflow starts fresh next time." Gray
Say " To stop: double-click 'Stop tradeflow' in the project folder." Gray
Say "=============================================================" Cyan
Finish 0
