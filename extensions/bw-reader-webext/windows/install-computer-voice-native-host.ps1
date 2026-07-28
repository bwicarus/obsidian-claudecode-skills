[CmdletBinding()]
param(
    [ValidateSet("Status", "Enable", "Disable")]
    [string]$Action = "Status",
    [string]$ExtensionId = "",
    [string]$HostExecutable = "",
    [string]$TypistHelper = ""
)

$ErrorActionPreference = "Stop"
$contract = "reader-computer-voice-native-host-installer/1"
$hostName = "space.bwicarus.computer_voice"
$registryPath = "HKCU:\Software\Google\Chrome\NativeMessagingHosts\$hostName"
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $HostExecutable) {
    $HostExecutable = Join-Path $scriptRoot `
        "native-host\bw-computer-voice-audio.exe"
}
if (-not $TypistHelper) {
    $TypistHelper = Join-Path $scriptRoot "bw_computer_voice_typist_helper.py"
}
$manifestPath = Join-Path $scriptRoot "space.bwicarus.computer_voice.json"
$configPath = Join-Path (Split-Path -Parent $HostExecutable) `
    "computer-voice-native.config.json"

function Write-Receipt([hashtable]$Value) {
    $receipt = [ordered]@{
        contract = $contract
        action = $Action.ToLowerInvariant()
        timestampUtc = [DateTime]::UtcNow.ToString("o")
    }
    foreach ($key in $Value.Keys) { $receipt[$key] = $Value[$key] }
    $receipt | ConvertTo-Json -Depth 8 -Compress
}

function Test-ExtensionId([string]$Value) {
    return $Value -cmatch "^[a-p]{32}$"
}

function Get-CaptureEndpoints {
    $root = "Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\" +
        "CurrentVersion\MMDevices\Audio\Capture"
    if (-not (Test-Path -LiteralPath $root)) { return @() }
    return @(Get-ChildItem -LiteralPath $root | ForEach-Object {
        $state = Get-ItemProperty -LiteralPath $_.PSPath
        if ([int]$state.DeviceState -ne 1) { return }
        $properties = Get-ItemProperty -LiteralPath `
            (Join-Path $_.PSPath "Properties")
        $friendly = $properties.PSObject.Properties |
            Where-Object { $_.Name -like "*,6" } |
            Select-Object -First 1 -ExpandProperty Value
        [pscustomobject]@{
            EndpointId = $_.PSChildName
            FriendlyName = if ($friendly) {
                [string]$friendly
            } else {
                [string]$_.PSChildName
            }
        }
    })
}

if ($Action -eq "Status") {
    $config = $null
    if (Test-Path -LiteralPath $configPath) {
        try {
            $config = Get-Content -LiteralPath $configPath -Raw |
                ConvertFrom-Json
        } catch {}
    }
    Write-Receipt @{
        ok = $true
        registered = Test-Path -LiteralPath $registryPath
        localOptIn = [bool]($config -and $config.localOptIn)
        hostPresent = Test-Path -LiteralPath $HostExecutable
        typistHelperPresent = Test-Path -LiteralPath $TypistHelper
        extensionConfigured = [bool](
            $config -and (Test-ExtensionId ([string]$config.allowedExtensionId))
        )
    }
    exit 0
}

if ($Action -eq "Disable") {
    if (Test-Path -LiteralPath $registryPath) {
        Remove-Item -LiteralPath $registryPath -Force
    }
    if (Test-Path -LiteralPath $configPath) {
        $config = Get-Content -LiteralPath $configPath -Raw |
            ConvertFrom-Json
        $config.localOptIn = $false
        [IO.File]::WriteAllText(
            $configPath,
            ($config | ConvertTo-Json -Depth 8),
            [Text.UTF8Encoding]::new($false)
        )
    }
    Write-Receipt @{
        ok = $true
        registered = $false
        localOptIn = $false
        note = "Restart the extension to apply; no voice session was started"
    }
    exit 0
}

if (-not (Test-ExtensionId $ExtensionId)) {
    throw "ExtensionId must be the 32-character a-p ID shown by the extension"
}
if (-not (Test-Path -LiteralPath $HostExecutable -PathType Leaf)) {
    throw "Native host not found: $HostExecutable"
}
if (-not (Test-Path -LiteralPath $TypistHelper -PathType Leaf)) {
    throw "voice-typist helper not found: $TypistHelper"
}
if (
    [System.Diagnostics.Process]::GetCurrentProcess().SessionId -eq 0 -or
    -not [Environment]::UserInteractive
) {
    throw "Enable must run on the interactive Windows desktop, not Session 0"
}

$endpoints = @(Get-CaptureEndpoints)
if ($endpoints.Count -eq 0) {
    throw "No Active microphone capture endpoint was found"
}
Write-Host "Select the microphone explicitly authorized for this bridge:"
for ($index = 0; $index -lt $endpoints.Count; $index++) {
    Write-Host ("  [{0}] {1}" -f ($index + 1), $endpoints[$index].FriendlyName)
}
$selectedText = Read-Host "Enter device number"
$selectedIndex = 0
if (
    -not [int]::TryParse($selectedText, [ref]$selectedIndex) -or
    $selectedIndex -lt 1 -or
    $selectedIndex -gt $endpoints.Count
) {
    throw "Invalid microphone number; no configuration was written"
}
Write-Host ""
Write-Host "Strict scope to enable:"
Write-Host "  - capture only the selected microphone"
Write-Host "  - capture only the OpenAI Codex desktop process tree output"
Write-Host "  - no system-wide output and no inbound Windows control port"
Write-Host "  - Ctrl+Shift+C only after one Reader phone-button request"
$confirmation = Read-Host "Type ENABLE exactly to opt in"
if ($confirmation -cne "ENABLE") {
    throw "Local opt-in was not confirmed; no configuration was written"
}

$selected = $endpoints[$selectedIndex - 1]
$config = [ordered]@{
    contract = "reader-computer-voice-native-host-config/1"
    localOptIn = $true
    microphoneEndpointId = [string]$selected.EndpointId
    allowedExtensionId = $ExtensionId
    typistHelper = [IO.Path]::GetFullPath($TypistHelper)
    voiceStartShortcut = "Ctrl+Shift+C"
    outputScope = "process-only"
    appKind = "codex-desktop"
}
$manifest = [ordered]@{
    name = $hostName
    description = "BW Reader process-scoped Codex computer voice bridge"
    path = [IO.Path]::GetFullPath($HostExecutable)
    type = "stdio"
    allowed_origins = @("chrome-extension://$ExtensionId/")
}

[IO.File]::WriteAllText(
    $configPath,
    ($config | ConvertTo-Json -Depth 8),
    [Text.UTF8Encoding]::new($false)
)
[IO.File]::WriteAllText(
    $manifestPath,
    ($manifest | ConvertTo-Json -Depth 8),
    [Text.UTF8Encoding]::new($false)
)
New-Item -Path $registryPath -Force | Out-Null
Set-Item -LiteralPath $registryPath -Value $manifestPath

Write-Receipt @{
    ok = $true
    registered = $true
    localOptIn = $true
    extensionId = $ExtensionId
    microphoneName = [string]$selected.FriendlyName
    outputScope = "process-only"
    systemOutputFallback = $false
    inboundWindowsPort = $false
    shortcut = "Ctrl+Shift+C"
}
