[CmdletBinding()]
param(
    [ValidateSet('Start', 'Stop', 'Status', 'Pause', 'Resume', 'EStop', 'ClearStop',
                 'Doctor', 'Calibrate', 'Panel', 'Send')]
    [string]$Action = 'Status',

    # Start options
    [switch]$DryRun,
    [string]$JournalUrl = 'https://bwicarus.taile44d0c.ts.net/pdf/api/outgoing/journal',
    [switch]$NoJournal,

    # Send options
    [string]$Text,
    [string]$File
)

$ErrorActionPreference = 'Stop'

# Owns only the typist process it starts.  It never registers a startup entry,
# never touches the existing reader-context-injector, and never talks to the Pi.
$install = Join-Path 'C:\Users\bwica\bw-reader-context' 'reader-bridge'
$python = 'C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe'
$script = Join-Path $install 'voice_typist.py'
$config = Join-Path $install 'voice-typist.config.json'
$logs = Join-Path $install 'logs'
$log = Join-Path $logs 'voice-typist.jsonl'
$stateDir = Join-Path $install 'state'
$pidFile = Join-Path $install 'voice-typist.pid'
$panelPidFile = Join-Path $install 'voice-typist-panel.pid'
$credentialTarget = 'BWReaderJournal'

function Get-TypistProcess {
    param([string]$Path = $pidFile)
    if (!(Test-Path -LiteralPath $Path)) { return $null }
    $raw = (Get-Content -LiteralPath $Path -Raw).Trim()
    if ($raw -notmatch '^\d+$') { return $null }
    return Get-Process -Id ([int]$raw) -ErrorAction SilentlyContinue
}

function Invoke-Typist {
    param([string[]]$TypistArgs)
    $all = @($script, '--config', $config, '--log', $log, '--state-dir', $stateDir) + $TypistArgs
    & $python @all
}

switch ($Action) {
    'Status' {
        $process = Get-TypistProcess
        $status = $null
        $statusFile = Join-Path $stateDir 'status.json'
        if (Test-Path -LiteralPath $statusFile) {
            $status = Get-Content -LiteralPath $statusFile -Raw | ConvertFrom-Json
        }
        [pscustomobject]@{
            running       = [bool]$process
            pid           = if ($process) { $process.Id } else { $null }
            panelRunning  = [bool](Get-TypistProcess -Path $panelPidFile)
            paused        = Test-Path -LiteralPath (Join-Path $stateDir 'PAUSED')
            emergencyStop = Test-Path -LiteralPath (Join-Path $stateDir 'EMERGENCY_STOP')
            queueDepth    = if ($status) { $status.queue_depth } else { $null }
            lastOutcome   = if ($status) { $status.last_outcome } else { $null }
            lastVerified  = if ($status) { $status.last_verified } else { $null }
            statusAt      = if ($status) { $status.at } else { $null }
            config        = $config
            log           = $log
        } | ConvertTo-Json -Compress
        break
    }
    'Start' {
        if (!(Test-Path -LiteralPath $python)) { throw "Python runtime missing: $python" }
        if (!(Test-Path -LiteralPath $script)) { throw "Typist not installed: $script" }
        if (!(Test-Path -LiteralPath $config)) { throw "Config missing: $config (run: & '$python' '$script' init-config)" }
        $existing = Get-TypistProcess
        if ($existing) { throw "Typist already running (pid $($existing.Id)); use Stop first" }
        New-Item -ItemType Directory -Path $logs -Force | Out-Null

        $arguments = @(
            $script,
            '--config', $config,
            '--log', $log,
            '--state-dir', $stateDir,
            'run',
            '--clear-stop'
        )
        if (!$NoJournal) {
            $arguments += @('--journal-url', $JournalUrl,
                            '--journal-credential-target', $credentialTarget)
        }
        if ($DryRun) { $arguments += '--dry-run' }

        $process = Start-Process -FilePath $python -ArgumentList $arguments -WindowStyle Hidden -PassThru
        Set-Content -LiteralPath $pidFile -Value $process.Id -NoNewline
        Start-Sleep -Milliseconds 900
        if ($process.HasExited) {
            Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
            throw "Typist exited during start (code $($process.ExitCode)); inspect $log"
        }
        [pscustomobject]@{ running = $true; pid = $process.Id; dryRun = [bool]$DryRun; log = $log } | ConvertTo-Json -Compress
        break
    }
    'Stop' {
        $process = Get-TypistProcess
        if ($process) { Stop-Process -Id $process.Id -Force }
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
        'stopped'
        break
    }
    'Pause'     { Invoke-Typist @('state', 'pause'); break }
    'Resume'    { Invoke-Typist @('state', 'resume'); break }
    'EStop'     { Invoke-Typist @('state', 'stop'); break }
    'ClearStop' { Invoke-Typist @('state', 'clear-stop'); break }
    'Doctor'    { Invoke-Typist @('doctor', '--live'); break }
    'Calibrate' { Invoke-Typist @('calibrate'); break }
    'Panel' {
        $existing = Get-TypistProcess -Path $panelPidFile
        if ($existing) { throw "Panel already running (pid $($existing.Id))" }
        $arguments = @($script, '--config', $config, '--log', $log,
                       '--state-dir', $stateDir, 'panel')
        $process = Start-Process -FilePath $python -ArgumentList $arguments -PassThru -WindowStyle Hidden
        Set-Content -LiteralPath $panelPidFile -Value $process.Id -NoNewline
        [pscustomobject]@{ panelRunning = $true; pid = $process.Id } | ConvertTo-Json -Compress
        break
    }
    'Send' {
        $sendArgs = @('send', '--wrap')
        if ($Text) { $sendArgs += @('--text', $Text) }
        elseif ($File) { $sendArgs += @('--file', $File) }
        else { throw 'Send requires -Text or -File' }
        if ($DryRun) { $sendArgs += '--dry-run' }
        Invoke-Typist $sendArgs
        break
    }
}
