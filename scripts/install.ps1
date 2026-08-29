<#
.SYNOPSIS
    One-time setup for gen-leads: installs Python 3.13 (if not already
    present), creates a virtual environment, and installs all packages
    from requirements.txt into it.

.DESCRIPTION
    Run this once after copying/cloning this project onto a new computer.
    It will:
      1. Check whether Python 3.13 is already installed system-wide; if
         not, install it via winget (Windows Package Manager -- built into
         Windows 10/11). This system Python is only ever used to create
         the virtual environment below -- nothing is installed into it.
      2. Create a virtual environment at <project root>\.venv, if one
         doesn't already exist there.
      3. Make sure pip is present and up to date inside that venv.
      4. Run `pip install -r requirements.txt` inside that venv.
    Safe to re-run: each step is skipped automatically if already done. In
    particular, once the venv exists, re-running this script only ever
    installs/upgrades packages inside .venv -- it never touches the system
    Python or its packages again.

.NOTES
    How to run this:
      1. Right-click this file (install.ps1) and choose "Run with PowerShell".
      2. If Windows shows a blue "Windows protected your PC" popup, click
         "More info", then "Run anyway".
      3. If PowerShell instead shows a red error mentioning "execution of
         scripts is disabled", open PowerShell, "cd" into this scripts
         folder, and run:
              powershell -ExecutionPolicy Bypass -File install.ps1
    No administrator rights are required -- Python is installed for the
    current user only.
#>

$ErrorActionPreference = "Stop"

# The version this project is built and tested against (see CLAUDE.md) --
# pinned rather than "whatever winget calls latest" so a fresh install
# behaves the same as the one this app was developed on.
$PythonVersion = "3.13"
$WingetPackageId = "Python.Python.3.13"

# requirements.txt (and .venv) live at the project root, one level up from
# this scripts/ folder, regardless of where this script is run from.
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RequirementsPath = Join-Path $ProjectRoot "requirements.txt"
$VenvPath = Join-Path $ProjectRoot ".venv"
$VenvPython = Join-Path $VenvPath "Scripts\python.exe"

function Write-Step($message) {
    Write-Host ""
    Write-Host "==> $message" -ForegroundColor Cyan
}

function Write-Ok($message) {
    Write-Host "    $message" -ForegroundColor Green
}

function Update-SessionPath {
    # Installing Python updates the User/Machine PATH in the registry, but
    # this already-running PowerShell process doesn't pick that up on its
    # own -- without this, the rest of the script would fail to find
    # python.exe until you closed and reopened the window.
    $machinePath = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machinePath;$userPath"
}

function Get-PythonCommand {
    # Prefers the Python launcher pinned to the exact version this project
    # wants, so this keeps working correctly even if other Python versions
    # are also installed. Falls back to a bare "python" only if the
    # launcher isn't available.
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $null = & py "-$PythonVersion" --version 2>$null
        if ($LASTEXITCODE -eq 0) {
            return @("py", "-$PythonVersion")
        }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $version = & python --version 2>&1
        if ($version -match [regex]::Escape($PythonVersion)) {
            return @("python")
        }
    }
    return $null
}

Write-Step "Checking for Python $PythonVersion"
$pythonCmd = Get-PythonCommand

if ($null -eq $pythonCmd) {
    Write-Host "    Not found -- installing via winget..." -ForegroundColor Yellow

    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host ""
        Write-Host "ERROR: winget (Windows Package Manager) isn't available on this computer." -ForegroundColor Red
        Write-Host "Install 'App Installer' from the Microsoft Store, then run this script again:" -ForegroundColor Red
        Write-Host "  https://apps.microsoft.com/detail/9nblggh4nns1" -ForegroundColor Red
        exit 1
    }

    winget install --id $WingetPackageId --exact --source winget `
        --accept-package-agreements --accept-source-agreements --silent
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "ERROR: winget could not install Python $PythonVersion (exit code $LASTEXITCODE)." -ForegroundColor Red
        exit 1
    }

    Update-SessionPath
    $pythonCmd = Get-PythonCommand

    if ($null -eq $pythonCmd) {
        Write-Host ""
        Write-Host "ERROR: Python $PythonVersion was installed, but this window can't see it yet." -ForegroundColor Red
        Write-Host "Close this PowerShell window, open a new one, and run this script again." -ForegroundColor Red
        exit 1
    }

    Write-Ok "Python $PythonVersion installed."
} else {
    Write-Ok "Already installed."
}

$pythonVersionOutput = & $pythonCmd[0] $pythonCmd[1..($pythonCmd.Length - 1)] --version
Write-Ok "Using system $pythonVersionOutput (only to create the virtual environment below)"

Write-Step "Setting up the virtual environment"
if (Test-Path $VenvPython) {
    Write-Ok "Already exists at $VenvPath"
} else {
    & $pythonCmd[0] @($pythonCmd[1..($pythonCmd.Length - 1)]) -m venv $VenvPath
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: creating the virtual environment failed (exit code $LASTEXITCODE)." -ForegroundColor Red
        exit 1
    }
    if (-not (Test-Path $VenvPython)) {
        Write-Host "ERROR: venv creation reported success, but $VenvPython doesn't exist." -ForegroundColor Red
        exit 1
    }
    Write-Ok "Created at $VenvPath"
}

# From here on, every step uses the venv's own python.exe exclusively --
# never the system one above -- so nothing outside .venv is ever touched.
Write-Step "Ensuring pip is present and up to date (inside the venv)"
& $VenvPython -m ensurepip --upgrade
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: ensurepip failed (exit code $LASTEXITCODE)." -ForegroundColor Red
    exit 1
}
& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: upgrading pip failed (exit code $LASTEXITCODE)." -ForegroundColor Red
    exit 1
}
Write-Ok "pip is ready."

Write-Step "Installing packages from requirements.txt (inside the venv)"
if (-not (Test-Path $RequirementsPath)) {
    Write-Host "ERROR: requirements.txt not found at $RequirementsPath" -ForegroundColor Red
    exit 1
}
& $VenvPython -m pip install -r $RequirementsPath
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: pip install failed (exit code $LASTEXITCODE)." -ForegroundColor Red
    exit 1
}
Write-Ok "All packages installed."

Write-Step "Done!"
Write-Host "    You can now run the app, e.g.:" -ForegroundColor Green
Write-Host "        cd `"$ProjectRoot`"" -ForegroundColor Green
Write-Host "        .venv\Scripts\python.exe main_app.py" -ForegroundColor Green
Write-Host ""
Write-Host "    Or activate the venv first so plain 'python' picks it up:" -ForegroundColor Green
Write-Host "        .venv\Scripts\Activate.ps1" -ForegroundColor Green
Write-Host "        python main_app.py" -ForegroundColor Green
Write-Host ""
