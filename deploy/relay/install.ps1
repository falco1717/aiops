<#
.SYNOPSIS
    Install the AIOps relay node agent as a Windows service.

.DESCRIPTION
    Same agent as the Linux and Docker installers - one Python file. That is a
    deliberate choice over a second implementation in PowerShell: the relay is
    a protocol and a byte pump, and two implementations means two of them to
    get right, only one of which the test suite covers. The cost is a Python 3
    runtime on this machine, which winget or the Store provides in a minute;
    the cost of the alternative is an untested reimplementation carrying
    somebody's SSH sessions.

    Windows has no supervisor that will run a script as a service directly, so
    this compiles a small service host with the C# compiler that ships with the
    .NET Framework - already on every supported Windows - and that host runs
    the agent and restarts it if it stops. Nothing is downloaded.

    The service runs under its own virtual account, NT SERVICE\AIOpsRelayNode,
    which has no password and no rights beyond the state directory.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Url https://aiops.example.com -Token <enrolment token>

.NOTES
    Remove it with:

        powershell -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1

    Both commands are spelled the long way on purpose. This file and
    uninstall.ps1 arrive inside a zip fetched with a browser, so each carries
    Mark-of-the-Web and is treated as internet-sourced; that plus the
    Restricted policy Windows client SKUs ship makes the bare .\script.ps1
    form fail with "is not digitally signed", even on RemoteSigned.
    -ExecutionPolicy Bypass clears both for that one process and changes
    nothing on the machine. If it still refuses, the policy is coming from
    Group Policy: check Get-ExecutionPolicy -List for a MachinePolicy or
    UserPolicy entry.
#>

# House style everywhere else in this repo is a real em-dash in prose. Not
# here, and not in uninstall.ps1: these two files are ASCII-only, and are
# stored as UTF-8 *with* a byte order mark. Windows PowerShell 5.1 - still the
# default shell on every Windows this installs on - decodes a .ps1 with no BOM
# as ANSI (cp1252), so a UTF-8 em-dash arrives as three mojibake characters,
# the last of which (U+201D) closes a double-quoted string early. That took
# uninstall.ps1 from "works" to "does not parse", which is a silent way to lose
# the documented removal path. Keep both properties: no non-ASCII bytes, and
# keep the BOM if you re-save this file.

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
    if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { return }
    # A refusal, not a crash: the stack trace of a `throw` told the reader
    # nothing they did not already know and buried the one line that mattered.
    Write-Host ""
    Write-Host "This installer needs an elevated PowerShell: it creates a Windows service." -ForegroundColor Yellow
    Write-Host "Start PowerShell with 'Run as administrator' and run the same command again."
    Write-Host ""
    exit 1
}

function Find-Python {
    # `py` first: it is the launcher Windows installs and it knows about every
    # Python on the machine, including ones not on PATH.
    foreach ($candidate in @("py", "python3", "python")) {
        $found = Get-Command $candidate -ErrorAction SilentlyContinue
        if (-not $found) { continue }
        # Not $args: that is an automatic variable, and assigning to it works by
        # accident rather than by design.
        $probeArgs = if ($candidate -eq "py") { @("-3", "-c", "import sys;print(sys.executable)") }
                     else { @("-c", "import sys;print(sys.executable)") }
        $path = & $found.Source @probeArgs 2>$null
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

function Invoke-Sc {
    <#
        Every one of these calls is part of the security posture the installer
        advertises, and sc.exe reports failure only in its exit code. When
        `sc config obj=` failed the service kept running as LocalSystem - full
        SYSTEM, not the no-rights virtual account - and the installer still
        printed "Installed." A partial install that claims success is worse
        than no install, so each call is checked and each failure is fatal.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Purpose,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    $output = & sc.exe @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "$Purpose failed: sc.exe exited $LASTEXITCODE. $($output -join ' ')"
    }
}

function Set-StateDirAcl {
    <#
        %ProgramData% grants BUILTIN\Users (OI)(CI)(RX) and (CI)(WD,AD), and a
        child directory inherits both. Adding an ACE for the service account, as
        this used to, left every one of those inherited rights in place: the
        node credential was readable by every local user, and any of them could
        drop files into the state directory. Inheritance has to be broken, not
        added to.

        Well-known SIDs rather than names because BUILTIN\Administrators is
        localised and NT AUTHORITY\SYSTEM is not spelled the same everywhere.

        The directory only, with no /T. (OI)(CI) mean nothing on a file, and
        icacls applying these ACEs down a tree strips the file's inherited ACEs
        and then declines to write the ones it was given, leaving a credential
        with an empty DACL that not even SYSTEM can open. The inheritable ACEs
        set here reach every child that still inherits; a child that does not -
        the credential, which the agent protects itself - is granted directly.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string[]]$Also = @()
    )
    $aces = @("*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F") + $Also
    $arguments = @($Path, "/inheritance:r", "/grant:r") + $aces + @("/Q")
    & icacls @arguments | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not restrict the permissions on $Path." }
}

Assert-Admin
$python = Find-Python
$csc = Find-Csc
$source = Split-Path -Parent $MyInvocation.MyCommand.Path
$agentSource = Join-Path $source "aiops_relay_node.py"
if (-not (Test-Path $agentSource)) { throw "aiops_relay_node.py is not next to this script." }

$logPath = Join-Path $StateDir "aiops-relay.log"

Write-Host "Installing the AIOps relay node agent."
Write-Host "  AIOps:     $Url"
Write-Host "  Service:   $ServiceName"
Write-Host "  Runs as:   NT SERVICE\$ServiceName"
Write-Host "  Agent:     $InstallDir\aiops_relay_node.py"
Write-Host "  State:     $StateDir"
Write-Host "  Log:       $logPath"
Write-Host ""

# An existing service goes first, before anything in $InstallDir is touched.
# Stopping it after copying the agent over and recompiling the host meant
# writing files a running service had open, so re-running the installer to
# upgrade a node failed on the copy or the compile.
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Stopping the service already installed here."
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    & sc.exe delete $ServiceName | Out-Null
    # sc.exe returns when the SCM has marked it for deletion, not when the host
    # process has gone and let go of the files below.
    for ($waited = 0; $waited -lt 60; $waited++) {
        if (-not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 500
    }
    if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
        throw "The existing $ServiceName service is still there 30s after being deleted. Reboot and run this again."
    }
    Start-Sleep -Seconds 2
}

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

# Locked down before enrolment, not after: the credential is written into this
# directory a few lines below, and a directory that is restricted only once the
# secret is already in it was world-readable for the part that mattered. The
# service account cannot be named yet - a virtual account does not resolve
# until its service exists - so it is granted after the service is created.
Set-StateDirAcl -Path $StateDir

Copy-Item $agentSource (Join-Path $InstallDir "aiops_relay_node.py") -Force

# --- the service host --------------------------------------------------
# Windows will not run a script as a service: the Service Control Manager
# expects a process that answers its handshake, which python.exe does not. This
# is the smallest thing that does - it starts the agent, copies its output to a
# log, and stops it on service stop.
$hostSource = Join-Path $InstallDir "RelayServiceHost.cs"
$hostExe = Join-Path $InstallDir "AiopsRelayServiceHost.exe"
@"
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.ServiceProcess;
using System.Threading;

public class RelayService : ServiceBase
{
    private readonly List<Process> children = new List<Process>();
    private Thread supervisor;
    private volatile bool stopping;
    private readonly string dir;
    private string logPath;

    // Baked in by the installer so there is somewhere to write a diagnostic
    // even when agent.cfg - which is where the log path normally comes from -
    // is the thing that could not be read.
    private const string StateDirectory = @"$StateDir";

    public RelayService()
    {
        ServiceName = "$ServiceName";
        CanStop = true;
        CanShutdown = true;
        AutoLog = true;
        dir = Path.GetDirectoryName(Process.GetCurrentProcess().MainModule.FileName);
        logPath = Path.Combine(StateDirectory, "aiops-relay.log");
    }

    protected override void OnStart(string[] startArguments)
    {
        stopping = false;
        supervisor = new Thread(SuperviseGuarded);
        supervisor.IsBackground = true;
        supervisor.Start();
    }

    // Nothing thrown on this thread may escape. An unhandled exception on a
    // background thread takes the whole host down without a word, and the SCM
    // then restarts it into the same failure.
    private void SuperviseGuarded()
    {
        try { Supervise(); }
        catch (Exception error)
        {
            Note("the supervisor stopped unexpectedly: " + error, EventLogEntryType.Error);
            RequestStop();
        }
    }

    private void RequestStop()
    {
        stopping = true;
        try { Stop(); }
        catch (Exception error) { Note("could not stop cleanly: " + error.Message, EventLogEntryType.Error); }
    }

    protected override void OnShutdown() { OnStop(); }

    // Diagnostics have to survive the log file being unwritable, because that
    // is exactly the case that used to go unreported: the log could not be
    // opened, the fallback AppendAllText could not open it either, and the
    // service spun in silence. The Application event log is writable by any
    // account; the installer registers the source.
    private void Note(string message, EventLogEntryType kind)
    {
        try { EventLog.WriteEntry(message, kind); }
        catch { }
        try
        {
            if (logPath != null)
            {
                File.AppendAllText(logPath, DateTime.Now + " service host: " + message + Environment.NewLine);
            }
        }
        catch { }
    }

    private void Supervise()
    {
        string[] cfg;
        try
        {
            cfg = File.ReadAllLines(Path.Combine(dir, "agent.cfg"));
            if (cfg.Length < 4) { throw new InvalidOperationException("agent.cfg has fewer than four lines"); }
            // The log lives in the state directory. It used to live next to the
            // executable in Program Files, which the service account is granted
            // read and execute on and nothing more, so it could never be
            // written and the documented way to read it pointed at a file that
            // could not exist.
            logPath = Path.Combine(cfg[3], "aiops-relay.log");
        }
        catch (Exception error)
        {
            // Read outside the try, this threw on the supervisor thread and
            // killed it while the service still reported Running.
            Note("cannot read agent.cfg, so the agent was never started: " + error.Message, EventLogEntryType.Error);
            RequestStop();
            return;
        }

        int backoff = 5000;
        while (!stopping)
        {
            DateTime began = DateTime.UtcNow;
            int code = RunOnce(cfg);
            if (stopping) { return; }

            // A clean exit is the agent reporting that AIOps has revoked this
            // node. Restarting it would reconnect forever to be told the same
            // thing, so the service stops instead.
            if (code == 0)
            {
                Note("the agent exited cleanly (AIOps has revoked this node). Stopping.", EventLogEntryType.Information);
                RequestStop(); return;
            }
            // Exit 2 is the agent refusing its own configuration - no URL, or no
            // credential and no token. Respawning that every five seconds for
            // ever only hides it.
            if (code == 2)
            {
                Note("the agent rejected its configuration (exit 2). Stopping; re-run install.ps1.", EventLogEntryType.Error);
                RequestStop(); return;
            }
            // Anything else is a crash or a network failure. Those are worth
            // retrying, but not at a fixed five seconds for ever.
            if (DateTime.UtcNow - began > TimeSpan.FromMinutes(1)) { backoff = 5000; }
            Note("the agent exited with code " + code + "; retrying in " + (backoff / 1000) + "s.", EventLogEntryType.Warning);
            for (int waited = 0; waited < backoff && !stopping; waited += 250) { Thread.Sleep(250); }
            backoff = Math.Min(backoff * 2, 300000);
        }
    }

    private int RunOnce(string[] cfg)
    {
        StreamWriter writer = null;
        Process started = null;
        try
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
                writer = new StreamWriter(logPath, true);
                writer.AutoFlush = true;
            }
            catch (Exception logError)
            {
                // The agent matters more than its log. Start it anyway, with no
                // redirection at all so nothing can block on a pipe no one is
                // draining, and say why somewhere that still works.
                writer = null;
                Note("cannot write " + logPath + " (" + logError.Message + "); the agent will run without a log.", EventLogEntryType.Warning);
            }

            ProcessStartInfo psi = new ProcessStartInfo(cfg[0]);
            psi.Arguments = "\"" + cfg[1] + "\" --url \"" + cfg[2] + "\" --state-dir \"" + cfg[3] + "\""
                          + (cfg.Length > 4 && cfg[4].Trim() == "1" ? " --insecure" : "");
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.RedirectStandardOutput = writer != null;
            psi.RedirectStandardError = writer != null;

            // Started after the log is open, not before. The other way round,
            // every failure to open the log spawned an agent, abandoned it, and
            // came back five seconds later to spawn another.
            started = Process.Start(psi);
            lock (children) { children.Add(started); }

            if (writer != null)
            {
                StreamWriter sink = writer;
                Process reading = started;
                // stderr on its own thread: a Python traceback arrives here and
                // nowhere else, and it is the only thing that explains a crash.
                Thread errors = new Thread(delegate() { Copy(reading.StandardError, sink); });
                errors.IsBackground = true;
                errors.Start();
                Copy(reading.StandardOutput, sink);
                errors.Join(5000);
            }
            started.WaitForExit();
            return started.ExitCode;
        }
        catch (Exception error)
        {
            Note("could not run the agent: " + error.Message, EventLogEntryType.Error);
            return -1;
        }
        finally
        {
            // Whatever went wrong above, this iteration does not leave a child
            // behind. That is the entire reason for the finally.
            if (started != null)
            {
                try { if (!started.HasExited) { started.Kill(); started.WaitForExit(5000); } }
                catch { }
                lock (children) { children.Remove(started); }
                try { started.Close(); } catch { }
            }
            if (writer != null) { try { writer.Dispose(); } catch { } }
        }
    }

    private static void Copy(StreamReader from, StreamWriter to)
    {
        try
        {
            string line;
            while ((line = from.ReadLine()) != null) { lock (to) { to.WriteLine(line); } }
        }
        catch { }
    }

    protected override void OnStop()
    {
        stopping = true;
        Process[] running;
        lock (children) { running = children.ToArray(); }
        foreach (Process one in running)
        {
            try { if (!one.HasExited) { one.Kill(); one.WaitForExit(5000); } }
            catch { }
        }
        lock (children) { children.Clear(); }
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

# The service host's only channel when the log is unwritable. Registering the
# source needs an administrator, which is why it happens here and not there.
try {
    if (-not [System.Diagnostics.EventLog]::SourceExists($ServiceName)) {
        [System.Diagnostics.EventLog]::CreateEventSource($ServiceName, "Application")
    }
} catch {
    Write-Warning "Could not register the event log source: $($_.Exception.Message)"
}

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
# New-Service rather than `sc.exe create`. The quotes in an argument written as
# "`"$hostExe`"" are consumed by Windows PowerShell 5.1 before sc.exe ever sees
# them - pwsh 7 keeps them - so installing from 5.1 registered an ImagePath of
# C:\Program Files\AIOps Relay Node\AiopsRelayServiceHost.exe with no quotes
# and a space in it: a standard audit finding, and a privilege escalation route
# for anyone who can write C:\Program.exe. A cmdlet parameter is not subject to
# native-argument quoting, so both shells now register the same string.
$imagePath = '"{0}"' -f $hostExe
New-Service -Name $ServiceName -BinaryPathName $imagePath `
            -DisplayName "AIOps Relay Node" -StartupType Automatic `
            -Description "Holds one outbound connection to AIOps and opens TCP connections on this network when AIOps asks. Removed with uninstall.ps1." | Out-Null

# Asserted, not assumed. If a future shell mangles this again, the install
# fails here rather than shipping the finding.
$registered = (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName" -Name ImagePath).ImagePath
if ($registered -ne $imagePath) {
    throw "The service registered its executable as [$registered], not the quoted [$imagePath] this installer asked for."
}

# A virtual service account: its own identity, no password to store or rotate,
# and no rights anywhere except what is granted below.
Invoke-Sc -Purpose "Giving the service its own SID" -Arguments @("sidtype", $ServiceName, "unrestricted")
Invoke-Sc -Purpose "Setting the service to run as NT SERVICE\$ServiceName" -Arguments @("config", $ServiceName, "obj=", "NT SERVICE\$ServiceName")
Invoke-Sc -Purpose "Setting the service restart policy" -Arguments @("failure", $ServiceName, "reset=", "86400", "actions=", "restart/5000/restart/5000/restart/60000")

# The one that silently left the service as LocalSystem. Checked twice: once on
# sc.exe's exit code, once on what the SCM actually stored.
$account = (Get-CimInstance Win32_Service -Filter "Name='$ServiceName'").StartName
if ($account -ne "NT SERVICE\$ServiceName") {
    throw "The service runs as [$account], not the no-rights virtual account NT SERVICE\$ServiceName this installer advertises."
}

# Re-applied, now that NT SERVICE\$ServiceName resolves. Modify, not full
# control: the account should not be able to rewrite its own permissions. This
# is also what lets the service write its log, which lives in here.
Set-StateDirAcl -Path $StateDir -Also @("NT SERVICE\${ServiceName}:(OI)(CI)M")

# The credential is the one file in here that does not inherit: the agent gives
# it a DACL of its own the moment it is created, naming whoever ran this
# installer. The service runs as somebody else and still has to read it.
if (Test-Path $credential) {
    & icacls "$credential" /grant "NT SERVICE\${ServiceName}:(R)" /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not grant the service account access to the stored credential." }
}

& icacls "$InstallDir" /grant "NT SERVICE\${ServiceName}:(OI)(CI)RX" /Q | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not grant the service account read access to $InstallDir." }

Start-Service -Name $ServiceName

Write-Host ""
Write-Host "Installed. The node is enrolled but carries no traffic until an AIOps"
Write-Host "administrator approves it (Nodes -> Approve)."
Write-Host ""
Write-Host "  status:  Get-Service $ServiceName"
Write-Host "  logs:    Get-Content '$logPath' -Wait"
Write-Host "  errors:  Get-WinEvent -FilterHashtable @{LogName='Application'; ProviderName='$ServiceName'}"
Write-Host "  remove:  powershell -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1"
