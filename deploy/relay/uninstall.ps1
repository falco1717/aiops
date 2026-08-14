<#
.SYNOPSIS
    Remove the AIOps relay node agent installed by install.ps1.

.DESCRIPTION
    Stops and deletes the service, then removes the program directory and the
    state directory — including the node's credential. Nothing is left behind
    to restart it.

    Keep the log by passing -KeepLog: it is the record of what this node was
    asked to connect to, which is usually the reason anyone is uninstalling.
#>
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
    throw "Run this from an elevated PowerShell."
}

Write-Host "Removing the AIOps relay node."

if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    & sc.exe delete $ServiceName | Out-Null
    Write-Host "  removed service $ServiceName"
}

$log = Join-Path $InstallDir "aiops-relay.log"
if ($KeepLog -and (Test-Path $log)) {
    $kept = Join-Path $env:ProgramData "aiops-relay.log"
    Copy-Item $log $kept -Force
    Write-Host "  kept the log at $kept"
}

foreach ($path in @($InstallDir, $StateDir)) {
    if (Test-Path $path) {
        Remove-Item -Recurse -Force $path
        Write-Host "  removed $path"
    }
}

Write-Host ""
Write-Host "Done. Revoke the node in AIOps as well — this machine no longer answers,"
Write-Host "but the node record is still there until you do."
