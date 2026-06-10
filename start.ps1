# Sets up (first time) and starts the LinkedIn Outbound dashboard on Windows.
# Right-click > Run with PowerShell, or run:  .\start.ps1
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Find-Python {
    foreach ($cmd in @("python", "py", "python3")) {
        $p = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($p) { return $p.Source }
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    Write-Host "Python was not found. Install Python 3.10+ from https://www.python.org/downloads/ (tick 'Add to PATH'), then re-run."
    exit 1
}

& $py "bootstrap.py"
