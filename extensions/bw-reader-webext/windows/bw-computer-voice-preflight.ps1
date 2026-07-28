<#
Read-only preflight for the optional BW "电脑客户端" voice bridge.

Run this from the Windows user's *interactive desktop* (not an SSH, service,
Task Scheduler background, or elevated Session 0 shell):

  powershell -NoProfile -ExecutionPolicy Bypass -File .\bw-computer-voice-preflight.ps1 -Json

It deliberately does NOT capture microphone/app audio, send a shortcut,
inspect window titles/chat text, create a port, write a pairing key, or modify
Windows settings.  It only tells the future bridge whether the OS and the
target OpenAI desktop process are eligible for the next opt-in step.
#>
[CmdletBinding()]
param(
    [switch] $Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-PreflightResult([hashtable] $Result) {
    if ($Json) {
        $Result | ConvertTo-Json -Depth 6 -Compress
        return
    }
    $Result | ConvertTo-Json -Depth 6
}

try {
    $self = Get-Process -Id $PID
    $sessionId = [int] $self.SessionId
    $build = [Environment]::OSVersion.Version.Build
    $interactive = $sessionId -ne 0
    $explorer = @(
        Get-Process -Name explorer -ErrorAction SilentlyContinue |
            Where-Object { $_.SessionId -eq $sessionId }
    )

    # The current OpenAI Windows package exposes the desktop shell as
    # ChatGPT.exe even when the user is using the Codex surface.  Process
    # loopback includes this root process and its children, so choose only a
    # root of the package-owned ChatGPT.exe tree in the current user session.
    $candidates = @(
        Get-CimInstance -ClassName Win32_Process |
            Where-Object {
                $_.Name -eq 'ChatGPT.exe' -and
                $_.SessionId -eq $sessionId -and
                $_.ExecutablePath -like '*\OpenAI.Codex_*\app\ChatGPT.exe'
            }
    )
    $candidateIds = [System.Collections.Generic.HashSet[uint32]]::new()
    foreach ($candidate in $candidates) {
        [void] $candidateIds.Add([uint32] $candidate.ProcessId)
    }
    $roots = @(
        $candidates |
            Where-Object { -not $candidateIds.Contains([uint32] $_.ParentProcessId) } |
            Sort-Object ProcessId
    )
    $target = if ($roots.Count -eq 1) { $roots[0] } else { $null }
    $apiDll = Join-Path $env:WINDIR 'System32\mmdevapi.dll'
    $processLoopbackEligible = ($build -ge 20348) -and (Test-Path -LiteralPath $apiDll)

    $reason = if (-not $interactive) {
        'non-interactive-session'
    } elseif ($explorer.Count -eq 0) {
        'desktop-shell-not-present'
    } elseif (-not $processLoopbackEligible) {
        'process-loopback-api-unavailable'
    } elseif ($candidates.Count -eq 0) {
        'openai-desktop-process-not-found'
    } elseif ($roots.Count -ne 1) {
        'ambiguous-openai-process-tree'
    } else {
        'preflight-passed-not-yet-bridge-ready'
    }

    $result = [ordered]@{
        contract = 'reader-computer-voice-preflight/1'
        readOnly = $true
        timestampUtc = [DateTime]::UtcNow.ToString('o')
        currentSessionId = $sessionId
        interactiveDesktop = ($interactive -and $explorer.Count -gt 0)
        processLoopbackEligible = $processLoopbackEligible
        app = [ordered]@{
            package = 'OpenAI.Codex'
            executable = 'ChatGPT.exe'
            foundProcessCount = $candidates.Count
            rootProcessId = if ($null -eq $target) { $null } else { [int] $target.ProcessId }
            processTreeEligible = ($null -ne $target)
        }
        appReady = $false
        shortcutConfigured = $false
        bridgeReady = $false
        reason = $reason
        nextAction = 'install-interactive-bridge-configure-hotkey-pair-reader'
    }
    Write-PreflightResult $result
} catch {
    Write-PreflightResult ([ordered]@{
        contract = 'reader-computer-voice-preflight/1'
        readOnly = $true
        appReady = $false
        bridgeReady = $false
        reason = ('preflight-failed:' + $_.Exception.Message)
    })
    exit 1
}
