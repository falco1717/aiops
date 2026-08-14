<#
.SYNOPSIS
    Install the AIOps relay node agent as a Windows service.

.DESCRIPTION
    Same agent as the Linux and Docker installers — one Python file. That is a
    deliberate choice over a second implementation in PowerShell: the relay is
    a protocol and a byte pump, and two implementations means two of them to
    get right, only one of which the test suite covers. The cost is a Python 3
    runtime on this machine, which winget or the Store provides in a minute;
    the cost of the alternative is an untested reimplementation carrying
    somebody's SSH sessions.

    Windows has no supervisor that will run a script as a service directly, so
    this compiles a small service host with the C# compiler that ships with the
    .NET Framework — already on every supported Windows — and that host runs
    the agent and restarts it if it stops. Nothing is downloaded.

    The service runs under its own virtual account, NT SERVICE\AIOpsRelayNode,
    which has no password and no rights beyond the state directory.

.EXAMPLE
    .\install.ps1 -Url https://aiops.example.com -Token <enrolment token>

.NOTES
    Remove it with .\uninstall.ps1
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Url,
    [string]$Token = "",
    [string]$ServiceName = "AIOpsRelayNode",
    [string]$InstallDir = "$env:ProgramFiles\AIOps Relay Node",
    [string]$StateDir = "$env:ProgramData\AIOps Relay Node",
    [switch]$Insecure
)

$ErrorActionPreference = "Stop"

function Assert-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this from an elevated PowerShell: it creates a service."
    }
}

function Find-Python {
    # `py` first: it is the launcher Windows installs and it knows about every
    # Python on the machine, including ones not on PATH.
    foreach ($candidate in @("py", "python3", "python")) {
        $found = Get-Command $candidate -ErrorAction SilentlyContinue
        if (-not $found) { continue }
        $args = if ($candidate -eq "py") { @("-3", "-c", "import sys;print(sys.executable)") }
                else { @("-c", "import sys;print(sys.executable)") }
        $path = & $found.Source @args 2>$null
        if ($LASTEXITCODE -eq 0 -and $path) { return $path.Trim() }
    }
    throw @"
Python 3 was not found. The relay agent is one stdlib-only Python file, shared
with the Linux and Docker installers. Install it with:

    winget install --id Python.Python.3.12 --scope machine

then run this script again.
"@
}

function Find-Csc {
    $roots = @(
        "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
        "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe"
    )
    foreach ($root in $roots) { if (Test-Path $root) { return $root } }
    throw "The .NET Framework C# compiler was not found, so the service host cannot be built."
}

Assert-Admin
$python = Find-Python
$csc = Find-Csc
$source = Split-Path -Parent $MyInvocation.MyCommand.Path
$agentSource = Join-Path $source "aiops_relay_node.py"
if (-not (Test-Path $agentSource)) { throw "aiops_relay_node.py is not next to this script." }

Write-Host "Installing the AIOps relay node agent."
Write-Host "  AIOps:     $Url"
Write-Host "  Service:   $ServiceName"
Write-Host "  Runs as:   NT SERVICE\$ServiceName"
Write-Host "  Agent:     $InstallDir\aiops_relay_node.py"
Write-Host "  State:     $StateDir"
Write-Host ""

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
Copy-Item $agentSource (Join-Path $InstallDir "aiops_relay_node.py") -Force

# --- the service host --------------------------------------------------
# Windows will not run a script as a service: the Service Control Manager
# expects a process that answers its handshake, which python.exe does not. This
# is the smallest thing that does — it starts the agent, copies its output to a
# log, and stops it on service stop.
$hostSource = Join-Path $InstallDir "RelayServiceHost.cs"
$hostExe = Join-Path $InstallDir "AiopsRelayServiceHost.exe"
@"
using System;
using System.Diagnostics;
using System.IO;
using System.ServiceProcess;
using System.Threading;

public class RelayService : ServiceBase
{
    private Process child;
    private Thread supervisor;
    private volatile bool stopping;
    private readonly string dir;

    public RelayService()
    {
        ServiceName = "$ServiceName";
        CanStop = true;
        CanShutdown = true;
        dir = Path.GetDirectoryName(Process.GetCurrentProcess().MainModule.FileName);
    }

    protected override void OnStart(string[] args)
    {
        stopping = false;
        supervisor = new Thread(Supervise);
        supervisor.IsBackground = true;
        supervisor.Start();
    }

    private void Supervise()
    {
        string[] cfg = File.ReadAllLines(Path.Combine(dir, "agent.cfg"));
        string logPath = Path.Combine(dir, "aiops-relay.log");
        while (!stopping)
        {
            try
            {
                // Kept small on purpose: a relay that fills a disk with its own
                // log is a relay that takes the machine down with it.
                if (File.Exists(logPath) && new FileInfo(logPath).Length > 8 * 1024 * 1024)
                {
                    File.Copy(logPath, logPath + ".old", true);
                    File.WriteAllText(logPath, "");
                }
                ProcessStartInfo psi = new ProcessStartInfo(cfg[0]);
                psi.Arguments = "\"" + cfg[1] + "\" --url \"" + cfg[2] + "\" --state-dir \"" + cfg[3] + "\""
                              + (cfg.Length > 4 && cfg[4].Trim() == "1" ? " --insecure" : "");
                psi.UseShellExecute = false;
                psi.CreateNoWindow = true;
                psi.RedirectStandardOutput = true;
                child = Process.Start(psi);
                using (StreamWriter writer = new StreamWriter(logPath, true))
                {
                    writer.AutoFlush = true;
                    string line;
                    while ((line = child.StandardOutput.ReadLine()) != null) { writer.WriteLine(line); }
                }
                child.WaitForExit();
                // A clean exit is the agent reporting that AIOps has revoked
                // this node. Restarting it would reconnect forever to be told
                // the same thing, so the service stops instead.
                if (child.ExitCode == 0) { stopping = true; Stop(); return; }
            }
            catch (Exception error)
            {
                try { File.AppendAllText(logPath, DateTime.Now + " service host: " + error.Message + Environment.NewLine); }
                catch { }
            }
            if (stopping) { return; }
            Thread.Sleep(5000);
        }
    }

    protected override void OnStop()
    {
        stopping = true;
        try { if (child != null && !child.HasExited) { child.Kill(); } } catch { }
    }

    public static void Main() { ServiceBase.Run(new RelayService()); }
}
"@ | Set-Content -Path $hostSource -Encoding UTF8

& $csc /nologo /target:exe /platform:anycpu /out:"$hostExe" /reference:System.dll /reference:System.ServiceProcess.dll "$hostSource"
if ($LASTEXITCODE -ne 0) { throw "The service host did not compile." }

@(
    $python
    (Join-Path $InstallDir "aiops_relay_node.py")
    $Url
    $StateDir
    $(if ($Insecure) { "1" } else { "0" })
) | Set-Content -Path (Join-Path $InstallDir "agent.cfg") -Encoding UTF8

# --- enrol before the service starts, so a bad token fails here ---------
$credential = Join-Path $StateDir "credential"
if ($Token -and -not (Test-Path $credential)) {
    Write-Host "Enrolling with AIOps..."
    $enrolArgs = @((Join-Path $InstallDir "aiops_relay_node.py"), "--url", $Url,
                   "--token", $Token, "--state-dir", $StateDir, "--enrol-only")
    if ($Insecure) { $enrolArgs += "--insecure" }
    & $python @enrolArgs
    if ($LASTEXITCODE -ne 0) { throw "Enrolment failed. The token may already have been used." }
} elseif (-not (Test-Path $credential)) {
    Write-Warning "No -Token given and no credential stored. The service will idle until you re-run with a token."
}

# --- the service -------------------------------------------------------
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    & sc.exe delete $ServiceName | Out-Null
    Start-Sleep -Seconds 2
}

& sc.exe create $ServiceName binPath= "`"$hostExe`"" start= auto DisplayName= "AIOps Relay Node" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "The service could not be created." }
& sc.exe description $ServiceName "Holds one outbound connection to AIOps and opens TCP connections on this network when AIOps asks. Removed with uninstall.ps1." | Out-Null
# A virtual service account: its own identity, no password to store or rotate,
# and no rights anywhere except what is granted below.
& sc.exe sidtype $ServiceName unrestricted | Out-Null
& sc.exe config $ServiceName obj= "NT SERVICE\$ServiceName" | Out-Null
& sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/5000/restart/60000 | Out-Null

& icacls "$StateDir" /grant "NT SERVICE\${ServiceName}:(OI)(CI)M" /T | Out-Null
& icacls "$InstallDir" /grant "NT SERVICE\${ServiceName}:(OI)(CI)RX" | Out-Null
& icacls "$InstallDir\aiops-relay.log" /grant "NT SERVICE\${ServiceName}:M" 2>$null | Out-Null

Start-Service -Name $ServiceName

Write-Host ""
Write-Host "Installed. The node is enrolled but carries no traffic until an AIOps"
Write-Host "administrator approves it (Nodes -> Approve)."
Write-Host ""
Write-Host "  status:  Get-Service $ServiceName"
Write-Host "  logs:    Get-Content '$InstallDir\aiops-relay.log' -Wait"
Write-Host "  remove:  .\uninstall.ps1"
