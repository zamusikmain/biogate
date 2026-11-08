# One-command local start: venv on Python 3.11-3.13, dependencies, .env, server.
#   powershell -ExecutionPolicy Bypass -File scripts\run.ps1 [-Dev] [-OpenBrowser] [-BehindProxy] [-Port 8000]
# -BehindProxy trusts X-Forwarded-For from a local TLS reverse proxy (docs/NETWORK.md).
param(
    [switch]$Dev,
    [switch]$OpenBrowser,
    [switch]$BehindProxy,
    [int]$Port = 8000
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

function Test-SupportedPython([string[]]$Command) {
    try {
        $exe = $Command[0]
        $rest = @($Command | Select-Object -Skip 1)
        $version = & $exe @rest -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        return ($LASTEXITCODE -eq 0) -and ($version -in @("3.11", "3.12", "3.13"))
    } catch {
        return $false
    }
}

$venvPython = ".\.venv\Scripts\python.exe"
if ((Test-Path $venvPython) -and -not (Test-SupportedPython @($venvPython))) {
    throw ".venv uses an unsupported Python. Remove the .venv folder and run again (needs Python 3.11-3.13)."
}
if (-not (Test-Path $venvPython)) {
    $candidates = @(@("py", "-3.13"), @("py", "-3.12"), @("py", "-3.11"), @("python"), @("python3"))
    $base = $null
    foreach ($candidate in $candidates) {
        if (Test-SupportedPython $candidate) { $base = $candidate; break }
    }
    if (-not $base) { throw "Python 3.11, 3.12 or 3.13 was not found. Install one from python.org and run again." }
    Write-Host "Creating .venv with $($base -join ' ')"
    $baseExe = $base[0]
    $baseArgs = @($base | Select-Object -Skip 1) + @("-m", "venv", ".venv")
    & $baseExe @baseArgs
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
}

# Reinstall only when the dependency definition changed.
$target = if ($Dev) { ".[dev]" } else { "." }
$stamp = ".venv\.biogate-install"
$expected = "$target " + (Get-FileHash pyproject.toml -Algorithm SHA256).Hash
if (-not (Test-Path $stamp) -or (Get-Content $stamp -Raw).Trim() -ne $expected) {
    Write-Host "Installing dependencies ($target)"
    & $venvPython -m pip install --disable-pip-version-check -q -e $target
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
    Set-Content -Path $stamp -Value $expected -Encoding ascii
}

& $venvPython scripts\bootstrap_env.py
if ($LASTEXITCODE -ne 0) { throw ".env setup failed" }

# Fail early instead of letting uvicorn crash or the browser open whatever already owns the port.
$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
try {
    $listener.Start()
} catch {
    throw "Port $Port is already in use (another BioGate window?). Close it or start with -Port <number>."
} finally {
    $listener.Stop()
}

$url = "http://127.0.0.1:$Port/"
Write-Host "BioGate: $url (admin console: ${url}admin). Press CTRL+C to stop."
if ($OpenBrowser) {
    # Open the browser once BioGate itself reports healthy. The job belongs to this PowerShell
    # process, so it ends together with the server instead of polling after a crash.
    Start-Job -ScriptBlock {
        param($health, $page)
        for ($i = 0; $i -lt 300; $i++) {
            try {
                if ((Invoke-RestMethod $health -TimeoutSec 2).status -eq "ok") { Start-Process $page; return }
            } catch {}
            Start-Sleep -Seconds 1
        }
    } -ArgumentList "${url}health/live", $url | Out-Null
}
$serverArgs = @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$Port")
if ($BehindProxy) {
    # Only the proxy on this machine may set the client address.
    $serverArgs += @("--proxy-headers", "--forwarded-allow-ips", "127.0.0.1")
    # Clients reach BioGate only over HTTPS here, so the session cookie must never travel in clear text.
    # Environment variables override .env, so a forgotten setting there cannot weaken this.
    $env:BIOGATE_ADMIN_COOKIE_SECURE = "true"
    Write-Host "Expecting a local HTTPS reverse proxy in front (see docs/NETWORK.md). Session cookies are HTTPS-only."
}
& $venvPython @serverArgs
