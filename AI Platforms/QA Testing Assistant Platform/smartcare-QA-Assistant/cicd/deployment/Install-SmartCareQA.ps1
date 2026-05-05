param(
    [string]$PythonExe = "python",
    [string]$VenvPath = ".venv",
    [switch]$InstallLocalPresidioModel
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot

Push-Location $repoRoot
try {
    $pythonCommand = Get-Command $PythonExe -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        throw "Python executable '$PythonExe' was not found. Install Python 3.11 and retry."
    }

    & $PythonExe -m venv $VenvPath

    $venvPython = Join-Path $repoRoot $VenvPath
    $venvPython = Join-Path $venvPython "Scripts\python.exe"
    if (-not (Test-Path $venvPython)) {
        throw "Virtual environment python was not created at $venvPython"
    }

    & $venvPython -m pip install --upgrade pip setuptools wheel
    & $venvPython -m pip install -r requirements.txt

    if ($InstallLocalPresidioModel) {
        & $venvPython -m spacy download en_core_web_sm
    }

    Write-Host "Installation complete. Virtual environment: $VenvPath"
    Write-Host "Next: copy deployment\SmartCareQA.env.template to .env and update the values."
    Write-Host "Then start the app with deployment\Start-SmartCareQA.ps1"
}
finally {
    Pop-Location
}