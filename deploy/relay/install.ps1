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

    The interpreter is chosen for the service, not for whoever runs this. It
    must be one the service account can execute, which a Python installed for a
    single user is not: it lives inside that user's profile, which Windows
    grants to SYSTEM, administrators and its owner and to nobody else. This
    installer refuses such an interpreter rather than writing it into the
    service's configuration and letting the node fail silently afterwards.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Url https://aiops.example.com -Token <enrolment token>

.EXAMPLE
    Through a corporate HTTP proxy, trusting the gateway that re-signs TLS:

    powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Url https://aiops.example.com -Token <enrolment token> -Proxy http://proxy.corp.example:8080 -CaBundle C:\corp-ca.pem

.EXAMPLE
    Repair a node that is installed and not connecting. No token is needed: the
    credential it already has is kept, and the interpreter is re-resolved.

    powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Url https://aiops.example.com

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
    # An HTTP CONNECT proxy to reach AIOps through, e.g.
    # http://proxy.corp.example:8080 - with credentials in the URL if it wants
    # them. Persisted, so the service uses it on every restart and not only on
    # the run that installed it.
    [string]$Proxy = "",
    # A PEM file of extra certificate authorities to trust. This is the answer
    # to a corporate gateway that re-signs TLS, and it is the answer to prefer
    # over -Insecure, which switches verification off for the handshake the
    # enrolment credential travels in.
    [string]$CaBundle = "",
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

$script:NoMachinePython = @"
No machine-wide Python 3 was found.

The relay runs as a Windows *service*, under an account that is not you. A
Python installed for one user cannot be used by a machine-wide service: the
interpreter sits inside that user's profile, which Windows grants to SYSTEM,
to administrators and to its owner and to nobody else. The service is none of
those, so it is refused the moment it tries to start the agent - and what you
see is a service reporting Running while the node never connects.

That is not a theory. It is the failure this check exists to stop, and it is
why the installer will not simply use whatever Python answers on your PATH.

Install Python for the whole machine:

    winget install --id Python.Python.3.12 --scope machine

then run this script again. An existing broken install is repaired by re-running
it: the interpreter is re-resolved and rewritten, and the node keeps the
credential it already has.
"@

function Test-ServiceCanExecute {
    <#
        Whether the account this service will run as could execute $Path.

        Read off the DACL rather than guessed from the path. A virtual service
        account's token carries Everyone, Authenticated Users, BUILTIN\Users and
        NT AUTHORITY\SERVICE and nothing else that matters here, so the question
        is whether one of those is granted traverse/execute on the file and on
        every directory above it. Measured on a real machine, a per-user Python
        grants exactly three principals - SYSTEM, Administrators and the owning
        user - and the service account is none of them.

        Honest about what it is: this reads permissions, it does not run the
        interpreter as the service account. There is no supported way to do that
        before the service exists. The real execution test happens after the
        service is started, at the bottom of this script, under the account that
        will actually be doing it.
    #>
    param([Parameter(Mandatory = $true)][string]$Path)

    $wanted = @("S-1-1-0", "S-1-5-11", "S-1-5-32-545", "S-1-5-6")
    $execute = [Security.AccessControl.FileSystemRights]::ExecuteFile  # == Traverse on a directory
    $current = $Path
    while ($current) {
        try { $acl = Get-Acl -LiteralPath $current -ErrorAction Stop } catch { return $false }
        $allowed = $false
        foreach ($ace in $acl.Access) {
            $sid = $null
            try {
                $sid = $ace.IdentityReference.Translate(
                    [Security.Principal.SecurityIdentifier]).Value
            } catch { continue }
            if ($wanted -notcontains $sid) { continue }
            if (([int]$ace.FileSystemRights -band [int]$execute) -eq 0) { continue }
            # A Deny anywhere on the path settles it, whatever else is granted.
            if ($ace.AccessControlType -eq "Deny") { return $false }
            $allowed = $true
        }
        if (-not $allowed) { return $false }
        $parent = Split-Path -Parent $current
        if (-not $parent -or $parent -eq $current) { break }
        $current = $parent
    }
    return $true
}

function Test-InsideUserProfile {
    <#
        Whether $Path lives inside somebody's profile. A second, independent
        signal from the permission check above, and it catches the case that
        check cannot: a machine where the profile happens to be readable, where
        an interpreter under it is still the wrong thing for a service - it
        disappears when that user is removed, and it is per-user by definition.

        This is the general condition, not a list of layouts. Store app
        execution aliases (%LocalAppData%\Microsoft\WindowsApps), the Python
        install manager's runtimes (%LocalAppData%\Python\pythoncore-*) and a
        plain per-user installer (%LocalAppData%\Programs\Python) are all just
        instances of "under C:\Users".
    #>
    param([Parameter(Mandatory = $true)][string]$Path)
    $full = ""
    try { $full = [IO.Path]::GetFullPath($Path) } catch { $full = $Path }
    $roots = @()
    foreach ($root in @($env:USERPROFILE, $env:LOCALAPPDATA, $env:APPDATA,
                        (Join-Path $env:SystemDrive "Users"))) {
        if ($root) { $roots += $root.TrimEnd('\') + '\' }
    }
    foreach ($root in $roots) {
        if ($full.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) { return $true }
    }
    return $false
}

function Get-PythonCandidates {
    <#
        Every interpreter this machine can offer, as absolute paths.

        Deliberately wider than "what answers on PATH". PATH is the installing
        administrator's PATH, and the whole failure this guards against is the
        difference between their view of the machine and the service account's.
    #>
    $found = New-Object System.Collections.Generic.List[string]

    # The launcher knows about installs that are on no PATH at all.
    $listing = $null
    try { $listing = & py.exe --list-paths 2>$null } catch { }
    if ($listing) {
        foreach ($line in $listing) {
            if ($line -match '(?<path>[A-Za-z]:\\[^\s].*python\.exe)') {
                $found.Add($Matches['path'].Trim())
            }
        }
    }

    # The usual machine-wide locations, which is where the answer should be.
    $globs = @()
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:SystemDrive)) {
        if ($base) {
            $globs += (Join-Path $base "Python*\python.exe")
            $globs += (Join-Path $base "Python\Python*\python.exe")
        }
    }
    foreach ($glob in $globs) {
        foreach ($item in (Get-ChildItem -Path $glob -ErrorAction SilentlyContinue)) {
            $found.Add($item.FullName)
        }
    }

    # And finally whatever is on PATH, so a machine with an unusual but correct
    # layout is not turned away.
    foreach ($name in @("py", "python3", "python")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        $probe = if ($name -eq "py") { @("-3", "-c", "import sys;print(sys.executable)") }
                 else { @("-c", "import sys;print(sys.executable)") }
        $reported = & $command.Source @probe 2>$null
        if ($LASTEXITCODE -eq 0 -and $reported) { $found.Add(([string]$reported).Trim()) }
    }

    $unique = New-Object System.Collections.Generic.List[string]
    foreach ($path in $found) {
        if (-not $path) { continue }
        $full = $path
        try { $full = [IO.Path]::GetFullPath($path) } catch { }
        $already = $false
        foreach ($seen in $unique) { if ($seen -ieq $full) { $already = $true } }
        if (-not $already -and (Test-Path -LiteralPath $full)) { $unique.Add($full) }
    }
    return $unique
}

function Find-Python {
    <#
        The interpreter the service will be given, chosen for the service rather
        than for whoever is running this.

        Every candidate is actually executed before it is believed - a path that
        exists proves nothing, and a zero-byte Store alias is a path that exists.
        Machine-wide candidates are preferred, and among equals the highest
        version wins, so the choice is the same on two identical machines rather
        than whatever the filesystem happened to enumerate first.
    #>
    $usable = @()
    foreach ($candidate in (Get-PythonCandidates)) {
        $sentinel = "AIOPS-RELAY-INTERPRETER-OK"
        $reported = $null
        try {
            $reported = & $candidate -c "import sys;print('$sentinel');print(sys.executable);print('%d.%d' % sys.version_info[:2])" 2>$null
        } catch { continue }
        if ($LASTEXITCODE -ne 0 -or -not $reported) { continue }
        $lines = @($reported)
        if ($lines.Count -lt 3 -or $lines[0].Trim() -ne $sentinel) { continue }
        $version = [version]"0.0"
        try { $version = [version]$lines[2].Trim() } catch { }
        if ($version -lt [version]"3.8") { continue }
        $usable += [pscustomobject]@{
            Path       = $candidate
            Version    = $version
            InProfile  = (Test-InsideUserProfile -Path $candidate)
            Executable = (Test-ServiceCanExecute -Path $candidate)
        }
    }

    if ($usable.Count -eq 0) { throw $script:NoMachinePython }

    $serviceable = @($usable | Where-Object { -not $_.InProfile -and $_.Executable })
    if ($serviceable.Count -eq 0) {
        Write-Host ""
        Write-Host "Python 3 is installed here, and not in a way a service can use it:" -ForegroundColor Yellow
        foreach ($one in $usable) {
            $why = if ($one.InProfile) { "inside a user profile" }
                   elseif (-not $one.Executable) { "not executable by the service account" }
                   else { "unusable" }
            Write-Host ("    {0}  ({1})" -f $one.Path, $why)
        }
        throw $script:NoMachinePython
    }

    $chosen = ($serviceable | Sort-Object -Property @{Expression = "Version"; Descending = $true},
                                                   @{Expression = "Path"; Descending = $false})[0]
    $script:PythonChoice = $chosen
    $script:PythonOptions = $serviceable.Count
    return $chosen.Path
}

function Hide-ProxySecret {
    <#
        A proxy URL with its password taken out, for anything that is printed.
        A proxy credential is somebody else's, it is routinely a shared one, and
        an installer transcript gets pasted into tickets.
    #>
    param([string]$Value)
    if (-not $Value) { return $Value }
    return [Text.RegularExpressions.Regex]::Replace(
        $Value, '://([^:@/]+):[^@/]*@', '://$1:***@')
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
if ($CaBundle -and -not (Test-Path -LiteralPath $CaBundle -PathType Leaf)) {
    throw "-CaBundle $CaBundle is not a file."
}

$logPath = Join-Path $StateDir "aiops-relay.log"

Write-Host "Installing the AIOps relay node agent."
Write-Host "  AIOps:     $Url"
Write-Host "  Service:   $ServiceName"
Write-Host "  Runs as:   NT SERVICE\$ServiceName"
Write-Host "  Agent:     $InstallDir\aiops_relay_node.py"
Write-Host "  State:     $StateDir"
Write-Host "  Log:       $logPath"
# Said out loud, and said here, because which interpreter the service is given
# is the single setting most likely to be the reason a node never connects.
Write-Host ("  Python:    {0} (Python {1})" -f $python, $script:PythonChoice.Version)
if ($script:PythonOptions -gt 1) {
    Write-Host ("             newest of {0} machine-wide interpreters the service can run." -f $script:PythonOptions)
}
if ($Proxy) { Write-Host "  Proxy:     $(Hide-ProxySecret $Proxy)" }
if ($CaBundle) { Write-Host "  Extra CAs: $CaBundle" }
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

# --- can this machine reach AIOps at all? ------------------------------
# Before the service host is compiled, before a service is registered, before
# an enrolment token is spent. The agent's own --diagnose is what runs, so a
# network this node cannot get out of is a refusal here with a printed reason
# rather than a service flapping quietly afterwards. It cannot be run as the
# service account - that account does not exist until the service does - so it
# proves the network and not the account; the check after Start-Service below
# is the one that proves the account.
Write-Host "Checking that this machine can reach AIOps..."
$preflight = @((Join-Path $InstallDir "aiops_relay_node.py"), "--url", $Url,
               "--state-dir", $StateDir, "--diagnose")
if ($Insecure) { $preflight += "--insecure" }
if ($Proxy) { $preflight += @("--proxy", $Proxy) }
if ($CaBundle) { $preflight += @("--ca-bundle", $CaBundle) }
& $python @preflight
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "This machine cannot reach AIOps. No service has been created and no" -ForegroundColor Red
    Write-Host "enrolment token has been spent; the reason is above."
    exit 1
}

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
    private volatile bool sawAgentOutput;
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

        // The interpreter, named once at startup, in the file people actually
        // read. A node whose service account cannot run the Python it was given
        // dies before Python prints anything, so without this line the log
        // never mentions the one setting that is wrong.
        Note("starting the agent with interpreter: " + cfg[0], EventLogEntryType.Information);

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
        int code = -1;
        // Held back on purpose; see the comment where it is finally said.
        string deferred = null;
        sawAgentOutput = false;
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
                          + (cfg.Length > 4 && cfg[4].Trim() == "1" ? " --insecure" : "")
                          // Lines 5 and 6 are the proxy and the extra CA bundle,
                          // written by the installer so the service uses them on
                          // every restart rather than only on the run that set
                          // them. Absent in a config written by an older
                          // installer, which is why each is length-guarded.
                          + (cfg.Length > 5 && cfg[5].Trim().Length > 0
                             ? " --proxy \"" + cfg[5].Trim() + "\"" : "")
                          + (cfg.Length > 6 && cfg[6].Trim().Length > 0
                             ? " --ca-bundle \"" + cfg[6].Trim() + "\"" : "");
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
            code = started.ExitCode;

            // Both streams were being copied and neither said a word. That is
            // not a Python that failed; it is a Python that never ran - a
            // launcher stub, a runtime whose DLLs are missing, an executable
            // this account may start but not read. Saying "exited with code N"
            // and stopping there is what made this take a week to find.
            if (writer != null && !sawAgentOutput)
            {
                deferred = "the agent exited with code " + code + " having written nothing at "
                         + "all to stdout or stderr, so it never got as far as running Python. "
                         + "The interpreter this service was configured with is: " + cfg[0]
                         + " - check that the account this service runs as can execute it. A "
                         + "Python installed for one user cannot be run by a machine-wide "
                         + "service. Fix it with: winget install --id Python.Python.3.12 "
                         + "--scope machine, then re-run install.ps1.";
            }
        }
        catch (Exception error)
        {
            code = -1;
            deferred = "could not start the agent at all: " + error.Message
                     + " The interpreter this service was configured with is: " + cfg[0]
                     + " - exit code -1 with no agent output means exactly this, that the "
                     + "process was never created. A Python installed for one user cannot be "
                     + "run by a machine-wide service. Fix it with: winget install --id "
                     + "Python.Python.3.12 --scope machine, then re-run install.ps1.";
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

        // Said only now, after the finally above has let go of the log file,
        // and that ordering is the whole point. Note() appends to the same path
        // the StreamWriter holds open with FileShare.Read, so a diagnostic
        // written while it is still open loses a sharing violation to an empty
        // catch and survives only in the event log. Measured: a node logged
        // "the agent exited with code -1" every few minutes for days while the
        // line saying why went somewhere nobody was looking.
        if (deferred != null) { Note(deferred, EventLogEntryType.Error); }
        return code;
    }

    private void Copy(StreamReader from, StreamWriter to)
    {
        try
        {
            string line;
            while ((line = from.ReadLine()) != null)
            {
                sawAgentOutput = true;
                lock (to) { to.WriteLine(line); }
            }
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

# The CA bundle is copied in beside the agent rather than referenced where it
# was found. The path somebody passes is usually their Downloads folder, which
# the service account cannot read and which they will empty.
$installedCaBundle = ""
if ($CaBundle) {
    $installedCaBundle = Join-Path $InstallDir "ca-bundle.pem"
    Copy-Item -LiteralPath $CaBundle -Destination $installedCaBundle -Force
}

# Rewritten in full on every run, which is what makes re-running this installer
# the repair for a node whose interpreter was wrong: the path is re-resolved
# above, re-checked against what the service account can execute, and replaced
# here. The credential in the state directory is untouched, so a repaired node
# keeps its identity and needs no new enrolment token.
$configPath = Join-Path $InstallDir "agent.cfg"
@(
    $python
    (Join-Path $InstallDir "aiops_relay_node.py")
    $Url
    $StateDir
    $(if ($Insecure) { "1" } else { "0" })
    $Proxy
    $installedCaBundle
) | Set-Content -Path $configPath -Encoding UTF8

# A proxy URL can carry a password, and $InstallDir grants BUILTIN\Users read.
# The service account is granted its read below, once it resolves.
& icacls "$configPath" /inheritance:r /grant:r "*S-1-5-18:(F)" "*S-1-5-32-544:(F)" /Q | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not restrict the permissions on $configPath." }

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
    # The same egress the service will use. Enrolment dials out too, and one
    # that ignored the proxy failed at install time on exactly the machines the
    # proxy was there for - so the operator never reached the part that worked.
    if ($Proxy) { $enrolArgs += @("--proxy", $Proxy) }
    if ($installedCaBundle) { $enrolArgs += @("--ca-bundle", $installedCaBundle) }
    # aiops_relay_node.py already tells apart the two ways this can fail - a
    # network that never got the request to AIOps at all, or AIOps itself
    # refusing the token - and logs whichever one actually happened. A generic
    # "the token may already have been used" here, regardless of which one it
    # was, sent every network failure (a proxy, a TLS-inspecting gateway, a
    # firewalled VDI) to an operator who then regenerated tokens forever while
    # the real problem went undiagnosed. Captured rather than left to stream,
    # so the one line that says what actually happened can be quoted instead
    # of re-guessed - the same thing Invoke-Sc does for sc.exe above.
    $enrolOutput = & $python @enrolArgs 2>&1 | ForEach-Object { $_.ToString() }
    foreach ($line in $enrolOutput) { Write-Host $line }
    if ($LASTEXITCODE -ne 0) {
        $reason = $enrolOutput | Where-Object { $_ -match '\bERROR\b' } | Select-Object -Last 1
        if (-not $reason) { $reason = $enrolOutput | Select-Object -Last 1 }
        if (-not $reason) { $reason = "aiops_relay_node.py exited $LASTEXITCODE with no output." }
        Write-Host ""
        Write-Host "Find out exactly what this machine can and cannot reach:"
        Write-Host ""
        Write-Host "    & '$python' '$InstallDir\aiops_relay_node.py' --url $Url --state-dir '$StateDir' --diagnose"
        Write-Host ""
        throw "Enrolment failed: $reason"
    }
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

# agent.cfg had its inheritance broken above, so the grant to $InstallDir did
# not reach it. Read, not modify: the service reads its configuration and has
# no business rewriting it.
& icacls "$configPath" /grant "NT SERVICE\${ServiceName}:(R)" /Q | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not grant the service account access to $configPath." }

# Both are cleared first, so what is read below is this start's answer and not
# a convincing-looking one left over from the last.
$statusPath = Join-Path $StateDir "status"
Remove-Item -LiteralPath $statusPath -Force -ErrorAction SilentlyContinue
if (Test-Path -LiteralPath $logPath) {
    Set-Content -LiteralPath $logPath -Value "" -Encoding UTF8 -ErrorAction SilentlyContinue
}

Start-Service -Name $ServiceName

# --- the only test that runs as the account that matters ---------------
# Everything checked before this point was checked as an administrator, and the
# failure this guards against is precisely the gap between an administrator's
# view of the machine and the service account's: an interpreter that runs
# perfectly when you type it and cannot be started by the service. There is no
# way to settle that except to start the service and ask the node itself
# whether it got there. It answers in the status file; the log is read only to
# explain a node that never answers.
Write-Host -NoNewline "Waiting for the node to reach AIOps"
$deadline = (Get-Date).AddSeconds(60)
$reached = ""
$hostSaid = @()
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    if (Test-Path -LiteralPath $statusPath) {
        $state = ((Get-Content -LiteralPath $statusPath -ErrorAction SilentlyContinue |
                   Select-Object -First 1) -split ' ')[0]
        if ($state) { $reached = $state; break }
    }
    if ((Get-Service -Name $ServiceName -ErrorAction SilentlyContinue).Status -ne "Running") { break }
    $hostSaid = @(Get-Content -LiteralPath $logPath -ErrorAction SilentlyContinue |
                  Where-Object { $_ -match 'service host:' })
    # Two service-host lines with nothing from the agent means it has already
    # failed and been retried. Waiting out the rest of the minute proves nothing.
    if ($hostSaid.Count -ge 2) { break }
    Write-Host -NoNewline "."
}
Write-Host ""

if ($reached -eq "connected") {
    Write-Host "The node is connected to AIOps." -ForegroundColor Green
} elseif ($reached -eq "pending") {
    Write-Host "The node reached AIOps and is waiting to be approved (Nodes -> Approve)."
} else {
    Write-Host ""
    Write-Host "The service is installed and the node has NOT reached AIOps." -ForegroundColor Red
    Write-Host ""
    if ($reached) {
        Write-Host "AIOps answered, and its answer was: $reached"
    } else {
        Write-Host "The agent process produced nothing at all. The interpreter it was given is:"
        Write-Host "    $python"
    }
    Write-Host ""
    if ($hostSaid) {
        Write-Host "What the service host said:"
        foreach ($line in ($hostSaid | Select-Object -Last 4)) { Write-Host "    $line" }
        Write-Host ""
    }
    Write-Host "Ask the node itself why:"
    Write-Host ""
    Write-Host "    & '$python' '$InstallDir\aiops_relay_node.py' --url $Url --state-dir '$StateDir' --diagnose"
    Write-Host ""
    Write-Host "A process that could not be started at all is recorded only here:"
    Write-Host ""
    Write-Host "    Get-WinEvent -FilterHashtable @{LogName='Application'; ProviderName='$ServiceName'} | Select-Object -First 5 | Format-List"
    Write-Host ""
    Write-Host "If that says access denied or file not found, the service account cannot use"
    Write-Host "that interpreter. Install Python for the whole machine and run this again:"
    Write-Host ""
    Write-Host "    winget install --id Python.Python.3.12 --scope machine"
    Write-Host ""
    Write-Host "The node's credential is kept, so re-running needs no new enrolment token."
    exit 1
}

Write-Host ""
Write-Host "Installed. The node carries no traffic until an AIOps administrator"
Write-Host "approves it (Nodes -> Approve)."
Write-Host ""
Write-Host "  status:  Get-Service $ServiceName"
Write-Host "  logs:    Get-Content '$logPath' -Wait"
Write-Host "  errors:  Get-WinEvent -FilterHashtable @{LogName='Application'; ProviderName='$ServiceName'}"
$checkArgs = "--url $Url --state-dir '$StateDir' --diagnose"
if ($installedCaBundle) { $checkArgs = "$checkArgs --ca-bundle '$installedCaBundle'" }
Write-Host "  check:   & '$python' '$InstallDir\aiops_relay_node.py' $checkArgs"
if ($Proxy) {
    Write-Host "           set `$env:AIOPS_RELAY_PROXY to the proxy URL before running that check."
}
Write-Host "  remove:  powershell -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1"
