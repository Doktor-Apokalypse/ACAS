#requires -Version 5.1
<#
.SYNOPSIS
Start the analysis engine with .venv313, independently of PyCharm.
.DESCRIPTION
Copy this file to your desktop, then right-click it and choose Run with PowerShell.
Keep the console open while using the WebUI. Ollama must already be running.
Defaults use low GPT-OSS reasoning and one function per request after measured
high-reasoning batch failures. Existing process environment settings take precedence.
.PARAMETER ProjectDirectory
The installed project directory, regardless of where this launcher is saved.
.PARAMETER CheckOnly
Check paths and display settings without starting Python or opening the database.
.EXAMPLE
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Start-AnalysisEngine.ps1
.EXAMPLE
.\Start-AnalysisEngine.ps1 -CheckOnly
#>
[CmdletBinding()]
param(
    [string]$ProjectDirectory = $PSScriptRoot,
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$launcherExitCode = 0
$launcherLocationPushed = $false
$launcherPreviousEnvironment = @{}

try {
    if (-not (Test-Path -LiteralPath $ProjectDirectory -PathType Container)) {
        throw "Project directory not found: $ProjectDirectory. Edit the ProjectDirectory parameter in this script."
    }
    $launcherProject = (Resolve-Path -LiteralPath $ProjectDirectory).ProviderPath
    $launcherVenv = Join-Path $launcherProject '.venv313'
    $launcherScripts = Join-Path $launcherVenv 'Scripts'
    $launcherPython = Join-Path $launcherScripts 'python.exe'
    $launcherMain = Join-Path $launcherProject 'main.py'
    foreach ($launcherRequiredFile in @($launcherPython, $launcherMain)) {
        if (-not (Test-Path -LiteralPath $launcherRequiredFile -PathType Leaf)) {
            throw "Required file not found: $launcherRequiredFile"
        }
    }

    # No IDE settings or credentials are read at runtime. Use the measured
    # troubleshooting profile: less reasoning and no speculative batch pass.
    # Adaptive function-analysis context/output tiers still come from app_config.py.
    $launcherDefaults = [ordered]@{
        OLLAMA_MODEL = 'gpt-oss:20b'
        OLLAMA_CONTEXT_SIZE = '32768'
        OLLAMA_GPT_OSS_REASONING = 'low'
        OLLAMA_MAX_OUTPUT_TOKENS = '6144'
        FUNCTION_ANALYSIS_BATCH_SIZE = '1'
        FUNCTION_ANALYSIS_REQUEST_TIMEOUT = '300'
    }
    $launcherEnvironment = @{
        VIRTUAL_ENV = $launcherVenv
        PATH = $launcherScripts + [IO.Path]::PathSeparator + $env:PATH
        PYTHONUNBUFFERED = '1'
        PYTHONUTF8 = '1'
    }
    foreach ($launcherSetting in $launcherDefaults.Keys) {
        if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($launcherSetting, 'Process'))) {
            $launcherEnvironment[$launcherSetting] = $launcherDefaults[$launcherSetting]
        }
    }
    foreach ($launcherSetting in $launcherEnvironment.Keys) {
        $launcherPreviousEnvironment[$launcherSetting] = [Environment]::GetEnvironmentVariable($launcherSetting, 'Process')
        [Environment]::SetEnvironmentVariable($launcherSetting, $launcherEnvironment[$launcherSetting], 'Process')
    }

    # Keep relative configuration/data paths anchored to the project, even when
    # this script is launched from the desktop. Calling the venv executable directly
    # also avoids requiring Activate.ps1 or a system Python installation on PATH.
    Push-Location -LiteralPath $launcherProject
    $launcherLocationPushed = $true
    Write-Host ''
    Write-Host 'Apokalypse code analysis engine' -ForegroundColor Cyan
    Write-Host "Project: $launcherProject"
    Write-Host "Python:  $launcherPython"
    foreach ($launcherSetting in $launcherDefaults.Keys) {
        Write-Host ('{0}: {1}' -f $launcherSetting, [Environment]::GetEnvironmentVariable($launcherSetting, 'Process'))
    }
    Write-Host ''

    if ($CheckOnly) {
        Write-Host 'Path and launcher checks passed. The engine was not started.' -ForegroundColor Green
    }
    else {
        Write-Host 'Ollama must be running. The engine will print the WebUI address below.'
        Write-Host 'Keep this window open. Pause analysis in the WebUI before stopping with Ctrl+C.'
        Write-Host ''
        # Python logging uses stderr. In Windows PowerShell 5.1, avoid treating
        # normal native stderr output as a terminating PowerShell error.
        $ErrorActionPreference = 'Continue'
        try {
            & $launcherPython -u $launcherMain
            $launcherExitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = 'Stop'
        }
        if ($launcherExitCode -ne 0) {
            Write-Host "Engine exited with code $launcherExitCode. See the messages above." -ForegroundColor Red
        }
    }
}
catch {
    $launcherExitCode = 1
    Write-Host ('Unable to start the engine: ' + $_.Exception.Message) -ForegroundColor Red
}
finally {
    if ($launcherLocationPushed) {
        Pop-Location
    }
    foreach ($launcherSetting in $launcherPreviousEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($launcherSetting, $launcherPreviousEnvironment[$launcherSetting], 'Process')
    }
}

if (-not $CheckOnly) {
    [void](Read-Host 'Press Enter to close this launcher')
}
exit $launcherExitCode
