param(
    [string]$TaskName = "AI Bridge",
    [switch]$RemovePath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$service = New-Object -ComObject "Schedule.Service"
$service.Connect()
$folder = $service.GetFolder("\")

try {
    $task = $folder.GetTask($TaskName)
    $task.Stop(0)
    $folder.DeleteTask($TaskName, 0)
    Write-Output "AI Bridge task removed."
} catch {
    if ($_.Exception.HResult -eq -2147024894) {
        Write-Output "AI Bridge task is not installed."
    } else {
        throw
    }
}

if ($RemovePath) {
    $normalizedRoot = [System.IO.Path]::GetFullPath($root).TrimEnd('\')
    $current = [Environment]::GetEnvironmentVariable("Path", "User")
    $entries = @($current -split ';' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    $updated = @($entries | Where-Object {
        try {
            [System.IO.Path]::GetFullPath($_.Trim()).TrimEnd('\') -ine $normalizedRoot
        } catch {
            $_.Trim().TrimEnd('\') -ine $normalizedRoot
        }
    })
    [Environment]::SetEnvironmentVariable("Path", ($updated -join ';'), "User")
    Write-Output "Removed $root from the user PATH."
}
