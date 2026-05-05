param(
    [string]$OutputRoot = "$(Join-Path $PSScriptRoot 'out')",
    [string]$BuildLabel = "local"
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$artifactName = "smartcare-qa-assistant"
$packageRoot = Join-Path $OutputRoot $artifactName
$deploymentRoot = Join-Path $packageRoot "deployment"
$infrastructureRoot = Join-Path $packageRoot "infrastructure"

if (Test-Path $packageRoot) {
    Remove-Item -Path $packageRoot -Recurse -Force
}

New-Item -ItemType Directory -Path $packageRoot | Out-Null
New-Item -ItemType Directory -Path $deploymentRoot | Out-Null

$copyTargets = @(
    "requirements.txt",
    "pyproject.toml",
    "src"
)

foreach ($relativePath in $copyTargets) {
    $sourcePath = Join-Path $repoRoot $relativePath
    if (Test-Path $sourcePath) {
        Copy-Item -Path $sourcePath -Destination $packageRoot -Recurse -Force
    }
}

$infraSource = Join-Path $repoRoot "infra"
if (Test-Path $infraSource) {
    Copy-Item -Path $infraSource -Destination $infrastructureRoot -Recurse -Force
}

$deploymentSource = Join-Path $PSScriptRoot "deployment"
if (Test-Path $deploymentSource) {
    Copy-Item -Path (Join-Path $deploymentSource "*") -Destination $deploymentRoot -Recurse -Force
}

$manifestPath = Join-Path $deploymentRoot "ARTIFACT-MANIFEST.txt"
$manifestLines = @(
    "SmartCare QA Assistant deployment artifact",
    "Build label: $BuildLabel",
    "Generated (UTC): $([DateTime]::UtcNow.ToString('yyyy-MM-dd HH:mm:ss'))",
    "",
    "Package contents:",
    "- src\\",
    "- requirements.txt",
    "- pyproject.toml",
    "- deployment\\Install-SmartCareQA.ps1",
    "- deployment\\Start-SmartCareQA.ps1",
    "- deployment\\SmartCareQA.env.template",
    "- deployment\\DEPLOYMENT-INSTRUCTIONS.txt",
    "- infrastructure\\ (reference only)",
    "",
    "Recommended BRT handoff files:",
    "- smartcare-qa-assistant-<build>.zip",
    "- deployment\\DEPLOYMENT-INSTRUCTIONS.txt",
    "- deployment\\SmartCareQA.env.template"
)
$manifestLines | Set-Content -Path $manifestPath -Encoding ASCII

Write-Host "Deployment artifact created at $packageRoot"