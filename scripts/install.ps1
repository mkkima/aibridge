param(
    [string]$TaskName = "AI Bridge",
    [string[]]$RemovePath = @()
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

function Resolve-PythonWindowedRuntime {
    param([string]$ProjectRoot)

    $candidates = [System.Collections.Generic.List[string]]::new()
    $candidates.Add((Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"))

    if ($env:LOCALAPPDATA) {
        $pythonInstallRoot = Join-Path $env:LOCALAPPDATA "Programs\Python"
        if (Test-Path -LiteralPath $pythonInstallRoot) {
            Get-ChildItem -LiteralPath $pythonInstallRoot -Directory -Filter "Python3*" |
                Sort-Object Name -Descending |
                ForEach-Object {
                    $candidates.Add((Join-Path $_.FullName "pythonw.exe"))
                }
        }
    }

    $pathRuntime = Get-Command "pythonw.exe" -ErrorAction SilentlyContinue
    if ($pathRuntime) {
        $candidates.Add($pathRuntime.Source)
    }

    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            continue
        }

        $python = Join-Path (Split-Path $candidate -Parent) "python.exe"
        if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
            continue
        }

        Push-Location $ProjectRoot
        try {
            & $python -c "import sys; import websockets; import aibridge; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) {
                return [pscustomobject]@{
                    Python = (Resolve-Path -LiteralPath $python).Path
                    Pythonw = (Resolve-Path -LiteralPath $candidate).Path
                }
            }
        } finally {
            Pop-Location
        }
    }

    throw "Python 3.11+ with the 'websockets' package was not found. Run: python -m pip install -r `"$root\requirements.txt`""
}

function Normalize-PathEntry {
    param([string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        return ""
    }
    try {
        return [System.IO.Path]::GetFullPath($Path.Trim()).TrimEnd('\')
    } catch {
        return $Path.Trim().TrimEnd('\')
    }
}

function Update-UserPath {
    param(
        [string]$Add,
        [string[]]$Remove
    )

    $addNormalized = Normalize-PathEntry $Add
    $removeNormalized = @($Remove | ForEach-Object { Normalize-PathEntry $_ } | Where-Object { $_ })
    $current = [Environment]::GetEnvironmentVariable("Path", "User")
    $entries = @($current -split ';' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    $updated = [System.Collections.Generic.List[string]]::new()

    foreach ($entry in $entries) {
        $normalized = Normalize-PathEntry $entry
        if ($removeNormalized | Where-Object { $_ -ieq $normalized }) {
            continue
        }
        if ($normalized -ieq $addNormalized) {
            continue
        }
        $updated.Add($entry.Trim())
    }

    $updated.Add($addNormalized)
    [Environment]::SetEnvironmentVariable("Path", ($updated -join ';'), "User")
}

$runtime = Resolve-PythonWindowedRuntime -ProjectRoot $root
$service = New-Object -ComObject "Schedule.Service"
$service.Connect()
$folder = $service.GetFolder("\")
$definition = $service.NewTask(0)

try {
    $existingTask = $folder.GetTask($TaskName)
    $existingTask.Stop(0)
} catch {
    # The task is not installed yet.
}

$definition.RegistrationInfo.Description = "Runs AI Bridge on demand when the ai command needs the browser bridge."
$definition.Principal.UserId = $identity
$definition.Principal.LogonType = 3
$definition.Principal.RunLevel = 0

$definition.Settings.Enabled = $true
$definition.Settings.AllowDemandStart = $true
$definition.Settings.StartWhenAvailable = $false
$definition.Settings.DisallowStartIfOnBatteries = $false
$definition.Settings.StopIfGoingOnBatteries = $false
$definition.Settings.ExecutionTimeLimit = "PT0S"
$definition.Settings.MultipleInstances = 2
$definition.Settings.RestartCount = 999
$definition.Settings.RestartInterval = "PT1M"

# Deliberately create no triggers. The ai CLI starts this task with schtasks /Run.
$action = $definition.Actions.Create(0)
$action.Path = $runtime.Pythonw
$action.Arguments = "-m aibridge.task_runner"
$action.WorkingDirectory = $root

$taskCreateOrUpdate = 6
$taskLogonInteractiveToken = 3
$registeredTask = $folder.RegisterTaskDefinition(
    $TaskName,
    $definition,
    $taskCreateOrUpdate,
    $identity,
    $null,
    $taskLogonInteractiveToken,
    $null
)

if ($registeredTask.Definition.Triggers.Count -ne 0) {
    throw "Task '$TaskName' was registered with an unexpected trigger."
}

Update-UserPath -Add $root -Remove $RemovePath

Write-Output "AI Bridge task registered for on-demand use only."
Write-Output "Triggers: 0; state: $($registeredTask.State); command: $($runtime.Pythonw) -m aibridge.task_runner"
Write-Output "Open a new terminal before invoking ai by name."
