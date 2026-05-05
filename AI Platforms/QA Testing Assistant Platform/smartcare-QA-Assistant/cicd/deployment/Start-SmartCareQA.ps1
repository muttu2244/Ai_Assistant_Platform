param(
    [string]$VenvPath = ".venv",
    [string]$EnvFile = ".env",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot

function Import-DotEnvFile {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        return
    }

    Get-Content -Path $Path | ForEach-Object {
        $line = $_.Trim()
        if (-not $line) {
            return
        }
        if ($line.StartsWith("#")) {
            return
        }
        $parts = $line -split '=', 2
        if ($parts.Count -ne 2) {
            return
        }
        $name = $parts[0].Trim()
        $value = $parts[1].Trim().Trim('"')
        Set-Item -Path ("Env:" + $name) -Value $value
    }
}

Push-Location $repoRoot
try {
    $venvPython = Join-Path $repoRoot $VenvPath
    $venvPython = Join-Path $venvPython "Scripts\python.exe"
    if (-not (Test-Path $venvPython)) {
        throw "Virtual environment not found. Run deployment\Install-SmartCareQA.ps1 first."
    }

    Import-DotEnvFile -Path (Join-Path $repoRoot $EnvFile)
    $env:PYTHONPATH = $repoRoot

    & $venvPython -m uvicorn src.chat_ui.app:app --host 0.0.0.0 --port $Port
}
finally {
    Pop-Location
}