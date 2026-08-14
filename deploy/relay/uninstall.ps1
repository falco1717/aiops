<#
.SYNOPSIS
    Remove the AIOps relay node agent installed by install.ps1.

.DESCRIPTION
    Stops and deletes the service, kills any agent process it left running,
    then removes the program directory and the state directory - including the
    node's credential. Nothing is left behind to restart it.

    Keep the log by passing -KeepLog: it is the record of what this node was
    asked to connect to, which is usually the reason anyone is uninstalling.
#>

# ASCII only, and saved as UTF-8 *with* a byte order mark. See the same note in
# install.ps1: without the BOM, Windows PowerShell 5.1 reads this file as ANSI,
# and a single em-dash in a double-quoted string was enough to make the whole
# script fail to parse - which is a silent way to lose the only documented way
# to remove a relay node.

[CmdletBinding()]
param(
    [string]$ServiceName = "AIOpsRelayNode",
    [string]$InstallDir = "$env:ProgramFiles\AIOps Relay Node",
    [string]$StateDir = "$env:ProgramData\AIOps Relay Node",
    [switch]$KeepLog
)

$ErrorActionPreference = "Stop"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host ""
    Write-Host "This needs an elevated PowerShell: it deletes a Windows service." -ForegroundColor Yellow
    Write-Host "Start PowerShell with 'Run as administrator' and run it again."
    Write-Host ""
    exit 1
}

function Remove-Tree {
    <#
        install.ps1 waits two seconds after deleting a service before touching
        its files; this did not, and ran straight into a directory the exiting
        host still had open. With $ErrorActionPreference = "Stop" that threw
        after the service was already gone, leaving an install that nothing
        managed and this script could not finish removing. Retry instead.
    #>
    param([Parameter(Mandatory = $true)][string]$Path)
    for ($attempt = 1; $attempt -le 20; $attempt++) {
        try {
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
            return $true
        } catch {
            if ($attempt -eq 20) {
                Write-Warning "could not remove $Path : $($_.Exception.Message)"
                return $false
            }
            Start-Sleep -Milliseconds 500
        }
    }
}

Write-Host "Removing the AIOps relay node."

if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    & sc.exe delete $ServiceName | Out-Null
    # sc.exe returns as soon as the SCM has marked the service for deletion,
    # not when the host process has gone.
    for ($waited = 0; $waited -lt 30; $waited++) {
        if (-not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 500
    }
    Write-Host "  removed service $ServiceName"
}

# Agents the service host left behind. It used to track only its most recent
# child, so a host that had been restarting the agent could leave several
# running, and they survived the uninstall and kept dialling AIOps from a
# machine the operator believed was clean. The fixed host does not orphan them
# any more; this stays because an uninstall should not depend on that.
$agentPath = Join-Path $InstallDir "aiops_relay_node.py"
$stray = @(
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name='py.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine.IndexOf($agentPath, [StringComparison]::OrdinalIgnoreCase) -ge 0
        }
)
foreach ($process in $stray) {
    try {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
        Write-Host "  stopped leftover agent process $($process.ProcessId)"
    } catch {
        Write-Warning "could not stop agent process $($process.ProcessId): $($_.Exception.Message)"
    }
}

# The log lives in the state directory, which is the only directory the service
# account can write. It used to be looked for in $InstallDir, where the service
# could never have created it, so -KeepLog reliably kept nothing.
$kept = @()
foreach ($name in @("aiops-relay.log", "aiops-relay.log.old")) {
    $log = Join-Path $StateDir $name
    if ($KeepLog -and (Test-Path $log)) {
        $destination = Join-Path $env:ProgramData $name
        Copy-Item $log $destination -Force
        $kept += $destination
    }
}
foreach ($path in $kept) { Write-Host "  kept the log at $path" }
if ($KeepLog -and -not $kept) { Write-Host "  no log to keep" }

$failed = @()
foreach ($path in @($InstallDir, $StateDir)) {
    if (Test-Path $path) {
        if (Remove-Tree -Path $path) { Write-Host "  removed $path" } else { $failed += $path }
    }
}

try {
    if ([System.Diagnostics.EventLog]::SourceExists($ServiceName)) {
        [System.Diagnostics.EventLog]::DeleteEventSource($ServiceName)
        Write-Host "  removed the event log source"
    }
} catch {
    Write-Warning "could not remove the event log source: $($_.Exception.Message)"
}

if ($failed) {
    Write-Host ""
    Write-Host "The service is gone, but these could not be removed:" -ForegroundColor Yellow
    foreach ($path in $failed) { Write-Host "  $path" }
    Write-Host "Nothing there runs any more. Delete them by hand, or reboot and run this again."
    exit 1
}

Write-Host ""
Write-Host "Done. Revoke the node in AIOps as well - this machine no longer answers,"
Write-Host "but the node record is still there until you do."
