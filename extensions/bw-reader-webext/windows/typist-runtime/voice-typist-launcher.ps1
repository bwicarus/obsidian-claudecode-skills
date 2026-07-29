[CmdletBinding()]
param(
    [ValidateSet('Start', 'Stop', 'Status', 'Pause', 'Resume', 'EStop', 'ClearStop',
                 'Doctor', 'Calibrate', 'Panel', 'Send', 'ResolveUncertain')]
    [string]$Action = 'Status',

    # Start options
    [switch]$DryRun,

    # Stop ownership fence. The bridge always supplies its exact lease PID.
    [ValidateRange(0, 2147483647)]
    [int]$ExpectedPid = 0,

    [long]$ExpectedStartFileTimeUtc = 0,

    # Direct-v3 owner watchdog. Both values are required together.
    [ValidateRange(0, 2147483647)]
    [int]$OwnerPid = 0,

    [long]$OwnerStartFileTimeUtc = 0,

    # Send options
    [string]$Text,
    [string]$File,

    # Delivery-uncertain manual resolution.
    [string]$SessionId,
    [string]$EventId,
    [long]$Sequence = 0,
    [string]$DeliveryResolution
)

$ErrorActionPreference = 'Stop'

# Owns only the typist process it starts.  It never registers a startup entry,
# never touches the existing reader-context-injector, and never talks to the Pi.
$install = $PSScriptRoot
$python = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
$script = Join-Path $install 'voice_typist.py'
$config = Join-Path $install 'voice-typist.config.json'
$logs = Join-Path $install 'logs'
$log = Join-Path $logs 'voice-typist.jsonl'
$stateDir = Join-Path $install 'state'
$pidFile = Join-Path $install 'voice-typist.pid'
$panelPidFile = Join-Path $install 'voice-typist-panel.pid'
$currentUserSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$lifecycleMutexName = "Local\BWReaderVoiceTypistLifecycle-v3-$currentUserSid"

function Invoke-WithTypistLifecycleLock {
    param(
        [Parameter(Mandatory)]
        [scriptblock]$Body
    )
    $mutex = [System.Threading.Mutex]::new($false, $lifecycleMutexName)
    $acquired = $false
    try {
        try {
            $acquired = $mutex.WaitOne([TimeSpan]::FromSeconds(15))
        }
        catch [System.Threading.AbandonedMutexException] {
            # The previous owner died while mutating lifecycle state.  The
            # mutex is now owned by this process, so continue with strict
            # lease/process validation instead of leaving it permanently wedged.
            $acquired = $true
        }
        if (!$acquired) {
            throw 'Timed out waiting for the voice-typist lifecycle lock'
        }
        & $Body
    }
    finally {
        if ($acquired) {
            $mutex.ReleaseMutex()
        }
        $mutex.Dispose()
    }
}

function Read-TypistLease {
    param(
        [string]$Path = $pidFile,
        [switch]$Strict
    )
    if (!(Test-Path -LiteralPath $Path)) { return $null }
    try {
        $lease = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
        $names = @($lease.PSObject.Properties.Name | Sort-Object)
        if (($names -join ',') -ne
            'ownerPid,ownerStartFileTimeUtc,pid,script,startedAtFileTimeUtc') {
            throw 'lease fields mismatch'
        }
        if ($lease.pid -isnot [long] -and $lease.pid -isnot [int]) {
            throw 'lease pid type mismatch'
        }
        if ([long]$lease.pid -le 0 -or [long]$lease.pid -gt 2147483647) {
            throw 'lease pid invalid'
        }
        if ($lease.startedAtFileTimeUtc -isnot [long] -and
            $lease.startedAtFileTimeUtc -isnot [int]) {
            throw 'lease start time type mismatch'
        }
        if ([long]$lease.startedAtFileTimeUtc -le 0) {
            throw 'lease start time invalid'
        }
        if ([string]$lease.script -cne [string]$script) {
            throw 'lease script mismatch'
        }
        $hasOwnerPid = $null -ne $lease.ownerPid
        $hasOwnerStart = $null -ne $lease.ownerStartFileTimeUtc
        if ($hasOwnerPid -ne $hasOwnerStart) {
            throw 'lease owner generation is incomplete'
        }
        if ($hasOwnerPid -and (
            ($lease.ownerPid -isnot [long] -and
             $lease.ownerPid -isnot [int]) -or
            [long]$lease.ownerPid -le 0 -or
            [long]$lease.ownerPid -gt 2147483647 -or
            ($lease.ownerStartFileTimeUtc -isnot [long] -and
             $lease.ownerStartFileTimeUtc -isnot [int]) -or
            [long]$lease.ownerStartFileTimeUtc -le 0
        )) {
            throw 'lease owner generation is invalid'
        }
        return $lease
    }
    catch {
        if ($Strict) { throw "Typist lease invalid: $($_.Exception.Message)" }
        return $null
    }
}

function Get-TypistProcess {
    param(
        [string]$Path = $pidFile,
        [switch]$Strict
    )
    $lease = Read-TypistLease -Path $Path -Strict:$Strict
    if (!$lease) { return $null }
    $process = Get-Process -Id ([int]$lease.pid) -ErrorAction SilentlyContinue
    if (!$process) { return $null }
    try {
        $started = $process.StartTime.ToUniversalTime().ToFileTimeUtc()
        $row = Get-CimInstance Win32_Process -Filter "ProcessId = $($process.Id)"
        $commandLine = [string]$row.CommandLine
        $scriptPattern = '(?i)(?:^|\s)(?:"' +
            [Regex]::Escape($script) + '"|' +
            [Regex]::Escape($script) + ')(?=\s|$)'
        $ownerPidPattern = '(?i)(?:^|\s)--owner-process-id\s+' +
            [Regex]::Escape([string]$lease.ownerPid) + '(?=\s|$)'
        $ownerStartPattern =
            '(?i)(?:^|\s)--owner-process-start-file-time-utc\s+' +
            [Regex]::Escape([string]$lease.ownerStartFileTimeUtc) +
            '(?=\s|$)'
        $ownerArgumentsMatch = if ($null -ne $lease.ownerPid) {
            $commandLine -match $ownerPidPattern -and
            $commandLine -match $ownerStartPattern
        } else {
            $commandLine -notmatch
                '(?i)(?:^|\s)--owner-process-(?:id|start-file-time-utc)(?=\s|$)'
        }
        if ($started -ne [long]$lease.startedAtFileTimeUtc -or
            ![String]::Equals(
                [string]$row.ExecutablePath,
                [string]$python,
                [StringComparison]::OrdinalIgnoreCase
            ) -or
            $commandLine -notmatch $scriptPattern -or
            !$ownerArgumentsMatch) {
            throw 'PID identity mismatch'
        }
        # Force System.Diagnostics.Process to own a handle to this exact process.
        # Kill() below then cannot follow a later PID reuse.
        $null = $process.Handle
        return $process
    }
    catch {
        if ($Strict) { throw "Typist process identity invalid: $($_.Exception.Message)" }
        return $null
    }
}

function Get-PanelProcess {
    if (!(Test-Path -LiteralPath $panelPidFile)) { return $null }
    $raw = (Get-Content -LiteralPath $panelPidFile -Raw).Trim()
    if ($raw -notmatch '^\d+$') { return $null }
    return Get-Process -Id ([int]$raw) -ErrorAction SilentlyContinue
}

function Write-TypistLease {
    param(
        [System.Diagnostics.Process]$Process,
        [int]$OwnerProcessId = 0,
        [long]$OwnerProcessStartFileTimeUtc = 0
    )
    $lease = [ordered]@{
        pid = $Process.Id
        startedAtFileTimeUtc = $Process.StartTime.ToUniversalTime().ToFileTimeUtc()
        script = $script
        ownerPid = if ($OwnerProcessId -gt 0) {
            $OwnerProcessId
        } else { $null }
        ownerStartFileTimeUtc = if ($OwnerProcessStartFileTimeUtc -gt 0) {
            $OwnerProcessStartFileTimeUtc
        } else { $null }
    }
    $tmp = "$pidFile.tmp"
    $lease | ConvertTo-Json -Compress | Set-Content -LiteralPath $tmp -Encoding UTF8 -NoNewline
    Move-Item -LiteralPath $tmp -Destination $pidFile -Force
    return [pscustomobject]$lease
}

function Remove-TypistLeaseIfMatch {
    param($ExpectedLease)
    $current = Read-TypistLease
    if ($current -and
        [int]$current.pid -eq [int]$ExpectedLease.pid -and
        [long]$current.startedAtFileTimeUtc -eq [long]$ExpectedLease.startedAtFileTimeUtc) {
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-Typist {
    param([string[]]$TypistArgs)
    $all = @($script, '--config', $config, '--log', $log, '--state-dir', $stateDir) + $TypistArgs
    & $python @all
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "voice-typist command failed with exit code $exitCode"
    }
}

switch ($Action) {
    'Status' {
        $process = Get-TypistProcess -Strict
        $lease = if ($process) { Read-TypistLease } else { $null }
        $status = $null
        $statusFile = Join-Path $stateDir 'status.json'
        if (Test-Path -LiteralPath $statusFile) {
            $status = Get-Content -LiteralPath $statusFile -Raw | ConvertFrom-Json
        }
        $queueStatus = $null
        if (!$process) {
            $queueRaw = @(Invoke-Typist @('queue-status')) -join [Environment]::NewLine
            if (!$queueRaw) {
                throw 'voice-typist queue-status returned no JSON'
            }
            $queueEnvelope = $queueRaw | ConvertFrom-Json
            $rootNames = @(
                $queueEnvelope.PSObject.Properties.Name | Sort-Object
            ) -join ','
            if ($rootNames -ne 'contract,ok,payload,queueContract' -or
                [string]$queueEnvelope.contract -cne
                    'reader-voice-typist-queue-status/1' -or
                $queueEnvelope.ok -ne $true -or
                [string]$queueEnvelope.queueContract -cne
                    'reader-voice-typist-queue/3') {
                throw 'voice-typist queue-status envelope is invalid'
            }
            $queueStatus = $queueEnvelope.payload
            $payloadNames = @(
                $queueStatus.PSObject.Properties.Name | Sort-Object
            ) -join ','
            if ($payloadNames -ne (
                'blocked_event_id,blocked_sequence,blocked_session_id,' +
                'queue_blocked_reason,queue_depth'
            ) -or
                ($queueStatus.queue_depth -isnot [int] -and
                 $queueStatus.queue_depth -isnot [long]) -or
                [long]$queueStatus.queue_depth -lt 0) {
                throw 'voice-typist queue-status payload is invalid'
            }
            if ($null -eq $queueStatus.queue_blocked_reason) {
                if ($null -ne $queueStatus.blocked_session_id -or
                    $null -ne $queueStatus.blocked_event_id -or
                    $null -ne $queueStatus.blocked_sequence) {
                    throw 'voice-typist queue-status empty blocker is inconsistent'
                }
            }
            elseif (
                [string]$queueStatus.queue_blocked_reason -cne
                    'delivery_uncertain' -or
                [string]::IsNullOrEmpty(
                    [string]$queueStatus.blocked_session_id
                ) -or
                [string]::IsNullOrEmpty(
                    [string]$queueStatus.blocked_event_id
                ) -or
                ($queueStatus.blocked_sequence -isnot [int] -and
                 $queueStatus.blocked_sequence -isnot [long]) -or
                [long]$queueStatus.blocked_sequence -le 0
            ) {
                throw 'voice-typist queue-status blocker is invalid'
            }
        }
        [pscustomobject]@{
            running       = [bool]$process
            pid           = if ($process) { $process.Id } else { $null }
            processStartFileTimeUtc = if ($lease) {
                [long]$lease.startedAtFileTimeUtc
            } else { $null }
            ownerPid       = if ($lease) { $lease.ownerPid } else { $null }
            ownerStartFileTimeUtc = if ($lease) {
                $lease.ownerStartFileTimeUtc
            } else { $null }
            panelRunning  = [bool](Get-PanelProcess)
            paused        = Test-Path -LiteralPath (Join-Path $stateDir 'PAUSED')
            emergencyStop = Test-Path -LiteralPath (Join-Path $stateDir 'EMERGENCY_STOP')
            queueDepth    = if ($queueStatus) {
                $queueStatus.queue_depth
            } elseif ($status) { $status.queue_depth } else { $null }
            queueBlockedReason = if ($queueStatus) {
                $queueStatus.queue_blocked_reason
            } elseif ($status) {
                $status.queue_blocked_reason
            } else { $null }
            blockedSessionId = if ($queueStatus) {
                $queueStatus.blocked_session_id
            } elseif ($status) {
                $status.blocked_session_id
            } else { $null }
            blockedEventId = if ($queueStatus) {
                $queueStatus.blocked_event_id
            } elseif ($status) {
                $status.blocked_event_id
            } else { $null }
            blockedSequence = if ($queueStatus) {
                $queueStatus.blocked_sequence
            } elseif ($status) {
                $status.blocked_sequence
            } else { $null }
            lastOutcome   = if ($status) { $status.last_outcome } else { $null }
            lastVerified  = if ($status) { $status.last_verified } else { $null }
            statusAt      = if ($status) { $status.at } else { $null }
            config        = $config
            log           = $log
        } | ConvertTo-Json -Compress
        break
    }
    'Start' {
        Invoke-WithTypistLifecycleLock {
            if (($OwnerPid -gt 0) -ne ($OwnerStartFileTimeUtc -gt 0)) {
                throw 'OwnerPid and OwnerStartFileTimeUtc must be supplied together'
            }
            if (!(Test-Path -LiteralPath $python)) { throw "Python runtime missing: $python" }
            if (!(Test-Path -LiteralPath $script)) { throw "Typist not installed: $script" }
            if (Test-Path -LiteralPath (Join-Path $stateDir 'EMERGENCY_STOP')) {
                throw 'Typist emergency stop is engaged; use ClearStop explicitly'
            }
            if (Test-Path -LiteralPath (Join-Path $stateDir 'PAUSED')) {
                throw 'Typist is paused; use Resume explicitly'
            }
            if (!(Test-Path -LiteralPath $config)) {
                Invoke-Typist @('init-config')
                if (!(Test-Path -LiteralPath $config)) {
                    throw "Config initialization failed: $config"
                }
            }
            $existing = Get-TypistProcess -Strict
            if ($existing) { throw "Typist already running (pid $($existing.Id)); use Stop first" }
            New-Item -ItemType Directory -Path $logs -Force | Out-Null
            New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
            Remove-Item -LiteralPath (Join-Path $stateDir 'status.json') -Force -ErrorAction SilentlyContinue

            $arguments = @(
                $script,
                '--config', $config,
                '--log', $log,
                '--state-dir', $stateDir,
                'run',
                '--idle-exit-seconds',
                $(if ($OwnerPid -gt 0) { '0' } else { '600' })
            )
            if ($OwnerPid -gt 0) {
                $arguments += @(
                    '--owner-process-id', [string]$OwnerPid,
                    '--owner-process-start-file-time-utc',
                    [string]$OwnerStartFileTimeUtc
                )
            }
            if ($DryRun) { $arguments += '--dry-run' }

            $process = Start-Process -FilePath $python -ArgumentList $arguments -WindowStyle Hidden -PassThru
            $lease = Write-TypistLease `
                -Process $process `
                -OwnerProcessId $OwnerPid `
                -OwnerProcessStartFileTimeUtc $OwnerStartFileTimeUtc
            $ready = $false
            $deadline = [DateTime]::UtcNow.AddSeconds(5)
            while ([DateTime]::UtcNow -lt $deadline) {
                $process.Refresh()
                if ($process.HasExited) { break }
                $statusFile = Join-Path $stateDir 'status.json'
                if (Test-Path -LiteralPath $statusFile) {
                    try {
                        $startedStatus = Get-Content -LiteralPath $statusFile -Raw | ConvertFrom-Json
                        if ($startedStatus.running -eq $true -and
                            (Get-TypistProcess -Strict).Id -eq $process.Id) {
                            $ready = $true
                            break
                        }
                    } catch {}
                }
                Start-Sleep -Milliseconds 100
            }
            if (!$ready) {
                try {
                    $process.Refresh()
                    if (!$process.HasExited) {
                        $process.Kill()
                        $process.WaitForExit(3000) | Out-Null
                    }
                } finally {
                    Remove-TypistLeaseIfMatch -ExpectedLease $lease
                }
                $detail = if ($process.HasExited) {
                    "code $($process.ExitCode)"
                } else { 'readiness timeout' }
                throw "Typist failed start postcondition ($detail); inspect $log"
            }
            [pscustomobject]@{
                running = $true
                pid = $process.Id
                processStartFileTimeUtc = [long]$lease.startedAtFileTimeUtc
                dryRun = [bool]$DryRun
                log = $log
            } | ConvertTo-Json -Compress
        }
        break
    }
    'Stop' {
        Invoke-WithTypistLifecycleLock {
            $lease = Read-TypistLease -Strict
            if (!$lease) {
                'stopped'
                return
            }
            if ($ExpectedPid -gt 0 -and [int]$lease.pid -ne $ExpectedPid) {
                throw "Typist lease PID mismatch: expected $ExpectedPid, actual $($lease.pid)"
            }
            if ($ExpectedStartFileTimeUtc -gt 0 -and
                [long]$lease.startedAtFileTimeUtc -ne $ExpectedStartFileTimeUtc) {
                throw "Typist lease generation mismatch"
            }
            $process = Get-TypistProcess -Strict
            if ($process) {
                # Get-TypistProcess opened and verified this exact process handle.
                $process.Kill()
                if (!$process.WaitForExit(5000)) {
                    throw "Typist pid $($process.Id) did not exit"
                }
            }
            Remove-TypistLeaseIfMatch -ExpectedLease $lease
            'stopped'
        }
        break
    }
    'Pause'     { Invoke-Typist @('state', 'pause'); break }
    'Resume'    { Invoke-Typist @('state', 'resume'); break }
    'EStop'     { Invoke-Typist @('state', 'stop'); break }
    'ClearStop' { Invoke-Typist @('state', 'clear-stop'); break }
    'Doctor'    { Invoke-Typist @('doctor', '--live'); break }
    'Calibrate' { Invoke-Typist @('calibrate'); break }
    'Panel' {
        $existing = Get-PanelProcess
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
    'ResolveUncertain' {
        Invoke-WithTypistLifecycleLock {
            if (Get-TypistProcess -Strict) {
                throw 'Stop voice-typist before resolving an uncertain delivery'
            }
            if (!$SessionId -or !$EventId -or $Sequence -le 0) {
                throw 'ResolveUncertain requires SessionId, EventId and Sequence'
            }
            if ($DeliveryResolution -notin @('Delivered', 'NotDelivered')) {
                throw 'DeliveryResolution must be Delivered or NotDelivered'
            }
            $resolution = if ($DeliveryResolution -eq 'Delivered') {
                'delivered'
            } else { 'not-delivered' }
            Invoke-Typist @(
                'queue-resolve',
                '--session-id', $SessionId,
                '--event-id', $EventId,
                '--sequence', [string]$Sequence,
                '--resolution', $resolution,
                '--launcher-confirmed-stopped'
            )
        }
        break
    }
}
