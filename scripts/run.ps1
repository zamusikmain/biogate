# One-command local start: venv on Python 3.11-3.13, dependencies, .env, server.
#   powershell -ExecutionPolicy Bypass -File scripts\run.ps1 [-Dev] [-OpenBrowser] [-Port 8000]
param(
    [switch]$Dev,
    [switch]$OpenBrowser,
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

$url = "http://127.0.0.1:$Port/"
Write-Host "BioGate: $url (admin console: ${url}admin). Press CTRL+C to stop."
if ($OpenBrowser) {
    Start-Job -ScriptBlock { param($u) Start-Sleep -Seconds 5; Start-Process $u } -ArgumentList $url | Out-Null
}
& $venvPython -m uvicorn app.main:app --host 127.0.0.1 --port $Port
