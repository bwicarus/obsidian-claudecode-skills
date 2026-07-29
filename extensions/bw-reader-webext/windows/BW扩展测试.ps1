param([switch]$NoLaunch)

$ErrorActionPreference = 'Stop'
$launcherVersion = 12
$channelUrl = 'https://bwicarus.taile44d0c.ts.net/static/pdf/bw-reader-webext-test-channel.json'
$baseDir = Join-Path $env:LOCALAPPDATA 'BWReaderExtensionTest'
$extensionDir = Join-Path $baseDir 'extension'
$profileDir = Join-Path $baseDir 'browser-profile-v2'
$installedScript = Join-Path $baseDir 'BW-Extension-Test.ps1'
$extensionId = 'jddhhakcblmihidgdobfkcejjinpigak'

function Get-TestBrowser {
  $candidates = @(
    (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'),
    (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe'),
    (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe'),
    (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe'),
    (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe')
  )
  foreach ($candidate in $candidates) {
    if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
  }
  throw 'Chrome or Edge was not found.'
}

function Get-Sha256Hex([string]$Path) {
  $stream = [System.IO.File]::OpenRead($Path)
  $sha = [System.Security.Cryptography.SHA256]::Create()
  try {
    $bytes = $sha.ComputeHash($stream)
    return ([System.BitConverter]::ToString($bytes)).Replace('-', '').ToLowerInvariant()
  } finally {
    $sha.Dispose()
    $stream.Dispose()
  }
}

function Get-TestProfileBrowserProcesses {
  $profileNeedle = [System.IO.Path]::GetFullPath($profileDir).TrimEnd('\').ToLowerInvariant()
  return @(
    Get-CimInstance -ClassName Win32_Process -ErrorAction Stop |
      Where-Object {
        ($_.Name -eq 'chrome.exe' -or $_.Name -eq 'msedge.exe') -and
        $_.CommandLine -and
        $_.CommandLine.ToLowerInvariant().Contains($profileNeedle)
      }
  )
}

function Stop-TestProfileBrowserForUpdate {
  $processes = @(Get-TestProfileBrowserProcesses)
  foreach ($process in $processes) {
    # Scope is already restricted to the dedicated browser-profile-v2 command
    # line. Chrome children can exit while this loop runs, so each stop is
    # deliberately idempotent; the bounded poll below is the real completion
    # proof.
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
  }
  for ($attempt = 0; $attempt -lt 60; $attempt++) {
    if (@(Get-TestProfileBrowserProcesses).Count -eq 0) { return }
    Start-Sleep -Milliseconds 250
  }
  throw 'The dedicated BW test browser did not stop. The extension was not replaced.'
}

function Get-DevToolsTargets {
  $stamp = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
  $response = Invoke-RestMethod -Uri (
    'http://127.0.0.1:9222/json/list?t=' + $stamp
  ) -Headers @{ 'Cache-Control' = 'no-cache' }
  # Windows PowerShell 5.1 does not pipeline-enumerate a top-level JSON array
  # returned by Invoke-RestMethod. foreach makes every target a real object.
  foreach ($target in $response) { Write-Output $target }
}

function Wait-DevToolsTarget([string]$Type, [string]$Url) {
  for ($attempt = 0; $attempt -lt 60; $attempt++) {
    try {
      $target = @(Get-DevToolsTargets | Where-Object {
        $_.type -eq $Type -and $_.url -eq $Url
      } | Select-Object -First 1)
      if ($target.Count -gt 0) { return $target[0] }
    } catch {}
    Start-Sleep -Milliseconds 250
  }
  throw ('The dedicated Chrome DevTools target did not appear: ' + $Url)
}

function New-DevToolsTarget([string]$Url) {
  $encodedUrl = [Uri]::EscapeDataString($Url)
  for ($attempt = 0; $attempt -lt 60; $attempt++) {
    try {
      return Invoke-RestMethod -Method Put -Uri (
        'http://127.0.0.1:9222/json/new?' + $encodedUrl
      )
    } catch {}
    Start-Sleep -Milliseconds 250
  }
  throw ('The dedicated Chrome DevTools endpoint did not open: ' + $Url)
}

function Send-CdpCommand(
  [string]$WebSocketUrl,
  [string]$Method,
  [hashtable]$Parameters,
  [bool]$WaitForResponse
) {
  $socket = [System.Net.WebSockets.ClientWebSocket]::new()
  $cancel = [System.Threading.CancellationTokenSource]::new()
  [void]$cancel.CancelAfter(5000)
  try {
    $socket.Options.Proxy = $null
    [void]$socket.Options.SetRequestHeader(
      'Origin',
      'http://127.0.0.1:9222'
    )
    [void]$socket.ConnectAsync(
      [Uri]$WebSocketUrl,
      $cancel.Token
    ).GetAwaiter().GetResult()
    $payload = @{
      id = 1
      method = $Method
      params = $Parameters
    } | ConvertTo-Json -Compress -Depth 8
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($payload)
    $segment = [System.ArraySegment[byte]]::new($bytes)
    [void]$socket.SendAsync(
      $segment,
      [System.Net.WebSockets.WebSocketMessageType]::Text,
      $true,
      $cancel.Token
    ).GetAwaiter().GetResult()
    if (-not $WaitForResponse) {
      Start-Sleep -Milliseconds 200
      return $null
    }
    do {
      $buffer = New-Object byte[] 65536
      $memory = [System.IO.MemoryStream]::new()
      try {
        do {
          $received = $socket.ReceiveAsync(
            [System.ArraySegment[byte]]::new($buffer),
            $cancel.Token
          ).GetAwaiter().GetResult()
          if ($received.MessageType -eq [System.Net.WebSockets.WebSocketMessageType]::Close) {
            throw 'Chrome DevTools closed before returning a response.'
          }
          [void]$memory.Write($buffer, 0, $received.Count)
        } while (-not $received.EndOfMessage)
        $response = [System.Text.Encoding]::UTF8.GetString(
          $memory.ToArray()
        ) | ConvertFrom-Json
      } finally {
        $memory.Dispose()
      }
    } while ($response.id -ne 1)
    if ($response.error) {
      throw ('Chrome DevTools command failed: ' + $response.error.message)
    }
    return $response
  } finally {
    $cancel.Dispose()
    $socket.Dispose()
  }
}

function Reload-BwExtensionWorker(
  [string]$Browser,
  [object[]]$BaseArguments,
  [string]$StartUrl,
  [string]$ExpectedVersion
) {
  $reloadUrl = 'chrome-extension://' + $extensionId + '/popup.html'
  Start-Process -FilePath $Browser -ArgumentList ($BaseArguments + 'about:blank')
  $reloadTarget = New-DevToolsTarget $reloadUrl
  Send-CdpCommand $reloadTarget.webSocketDebuggerUrl 'Runtime.evaluate' @{
    expression = 'chrome.runtime.reload(); true'
    returnByValue = $true
  } $false | Out-Null
  Start-Sleep -Milliseconds 500
  New-DevToolsTarget $StartUrl | Out-Null
  $workerUrl = 'chrome-extension://' + $extensionId + '/background.js'
  $workerTarget = Wait-DevToolsTarget 'service_worker' $workerUrl
  $response = Send-CdpCommand $workerTarget.webSocketDebuggerUrl 'Runtime.evaluate' @{
    expression = "globalThis.__BW_READER_BACKGROUND_BUILD_VERSION || ''"
    returnByValue = $true
  } $true
  $actualVersion = [string]$response.result.result.value
  if ($actualVersion -ne $ExpectedVersion) {
    throw (
      'The BW background worker is stale after reload: expected ' +
      $ExpectedVersion + ', got ' + $actualVersion
    )
  }
  try {
    Invoke-RestMethod -Method Put -Uri (
      'http://127.0.0.1:9222/json/close/' + $reloadTarget.id
    ) | Out-Null
  } catch {}
}

function Install-DesktopShortcut {
  $desktop = [Environment]::GetFolderPath('Desktop')
  $shortcutPath = Join-Path $desktop 'BW Extension Test.lnk'
  $shell = New-Object -ComObject WScript.Shell
  $shortcut = $shell.CreateShortcut($shortcutPath)
  $shortcut.TargetPath = 'powershell.exe'
  $shortcut.Arguments = '-NoLogo -NoProfile -ExecutionPolicy Bypass -File "' + $installedScript + '"'
  $shortcut.WorkingDirectory = $baseDir
  $shortcut.Description = 'Update and open the latest BW Reader extension test build'
  $shortcut.Save()
}

New-Item -ItemType Directory -Force -Path $baseDir | Out-Null
if ($PSCommandPath -ne $installedScript) {
  Copy-Item -LiteralPath $PSCommandPath -Destination $installedScript -Force
}
Install-DesktopShortcut

$stamp = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$channel = Invoke-RestMethod -Uri ($channelUrl + '?t=' + $stamp) -Headers @{ 'Cache-Control' = 'no-cache' }
if (-not $channel.version -or -not $channel.url -or -not $channel.sha256) {
  throw 'The BW Reader test channel manifest is incomplete.'
}

# Keep the installed desktop launcher current. The replacement takes effect on the next run.
if ($channel.launcherVersion -and ([int]$channel.launcherVersion -gt $launcherVersion) -and $channel.launcherUrl -and $channel.launcherSha256) {
  $launcherDownload = Join-Path $baseDir 'BW-Extension-Test.ps1.new'
  Invoke-WebRequest -Uri ([string]$channel.launcherUrl + '?t=' + $stamp) -OutFile $launcherDownload -UseBasicParsing
  $launcherHash = Get-Sha256Hex $launcherDownload
  if ($launcherHash -ne ([string]$channel.launcherSha256).ToLowerInvariant()) { throw 'Launcher checksum verification failed.' }
  Copy-Item -LiteralPath $launcherDownload -Destination $installedScript -Force
  Remove-Item -LiteralPath $launcherDownload -Force
}

$installedVersionFile = Join-Path $baseDir 'installed-version.txt'
$installedVersion = if (Test-Path -LiteralPath $installedVersionFile) { (Get-Content -LiteralPath $installedVersionFile -Raw).Trim() } else { '' }
if ($installedVersion -ne [string]$channel.version -or -not (Test-Path -LiteralPath (Join-Path $extensionDir 'manifest.json'))) {
  $downloadPath = Join-Path $baseDir ('download-' + $channel.version + '.zip')
  $stagingDir = Join-Path $baseDir ('staging-' + [Guid]::NewGuid().ToString('N'))
  $newDir = Join-Path $baseDir ('extension-new-' + [Guid]::NewGuid().ToString('N'))
  $backupDir = Join-Path $baseDir ('extension-old-' + [Guid]::NewGuid().ToString('N'))
  $installedVersionFileExisted = Test-Path -LiteralPath $installedVersionFile
  $swapped = $false
  try {
    Invoke-WebRequest -Uri ([string]$channel.url + '?t=' + $stamp) -OutFile $downloadPath -UseBasicParsing
    $actualHash = Get-Sha256Hex $downloadPath
    if ($actualHash -ne ([string]$channel.sha256).ToLowerInvariant()) { throw 'Package checksum verification failed.' }
    Expand-Archive -LiteralPath $downloadPath -DestinationPath $stagingDir -Force
    $manifest = Get-ChildItem -LiteralPath $stagingDir -Filter manifest.json -File -Recurse | Select-Object -First 1
    if (-not $manifest) { throw 'The downloaded package does not contain manifest.json.' }
    New-Item -ItemType Directory -Force -Path $newDir | Out-Null
    Get-ChildItem -LiteralPath $manifest.DirectoryName -Force | Copy-Item -Destination $newDir -Recurse -Force
    Stop-TestProfileBrowserForUpdate
    if (Test-Path -LiteralPath $extensionDir) { Move-Item -LiteralPath $extensionDir -Destination $backupDir }
    Move-Item -LiteralPath $newDir -Destination $extensionDir
    $swapped = $true
    Set-Content -LiteralPath $installedVersionFile -Value ([string]$channel.version) -Encoding ascii
    if (Test-Path -LiteralPath $backupDir) { Remove-Item -LiteralPath $backupDir -Recurse -Force }
  } catch {
    if ($swapped -and (Test-Path -LiteralPath $extensionDir)) {
      Remove-Item -LiteralPath $extensionDir -Recurse -Force
    }
    if (Test-Path -LiteralPath $backupDir) {
      Move-Item -LiteralPath $backupDir -Destination $extensionDir
    }
    if ($installedVersionFileExisted) {
      Set-Content -LiteralPath $installedVersionFile -Value $installedVersion -Encoding ascii
    } elseif (Test-Path -LiteralPath $installedVersionFile) {
      Remove-Item -LiteralPath $installedVersionFile -Force
    }
    throw
  } finally {
    if (Test-Path -LiteralPath $downloadPath) { Remove-Item -LiteralPath $downloadPath -Force }
    if (Test-Path -LiteralPath $stagingDir) { Remove-Item -LiteralPath $stagingDir -Recurse -Force }
    if (Test-Path -LiteralPath $newDir) { Remove-Item -LiteralPath $newDir -Recurse -Force }
  }
}

if (-not $NoLaunch) {
  $browser = Get-TestBrowser
  $startUrl = if ($channel.startUrl) {
    [string]$channel.startUrl
  } else {
    'https://en.wikipedia.org/wiki/Reading'
  }
  $baseArguments = @(
    '--new-window',
    '--no-first-run',
    '--no-default-browser-check',
    '--remote-debugging-address=127.0.0.1',
    '--remote-debugging-port=9222',
    '--remote-allow-origins=http://127.0.0.1:9222',
    ('--user-data-dir="' + $profileDir + '"')
  )
  $registeredMarker = Join-Path $baseDir 'extension-registered-v2.txt'
  $workerVersionFile = Join-Path $baseDir 'worker-runtime-version.txt'
  $workerVersion = if (Test-Path -LiteralPath $workerVersionFile) {
    (Get-Content -LiteralPath $workerVersionFile -Raw).Trim()
  } else {
    ''
  }
  $workerReloadRequired = $workerVersion -ne [string]$channel.version
  if (Test-Path -LiteralPath $registeredMarker) {
    if ($workerReloadRequired) {
      Stop-TestProfileBrowserForUpdate
      Reload-BwExtensionWorker $browser $baseArguments $startUrl ([string]$channel.version)
      Set-Content -LiteralPath $workerVersionFile -Value ([string]$channel.version) -Encoding ascii
    } else {
      Start-Process -FilePath $browser -ArgumentList ($baseArguments + $startUrl)
    }
  } else {
    Start-Process -FilePath $browser -ArgumentList ($baseArguments + 'chrome://extensions')
    Start-Sleep -Seconds 2
    Set-Clipboard -Value $extensionDir
    Start-Process -FilePath 'explorer.exe' -ArgumentList ('/select,"' + (Join-Path $extensionDir 'manifest.json') + '"')
    Add-Type -AssemblyName System.Windows.Forms
    $message = "Chrome 137 and newer block silent extension installation.`r`n`r`nIn the Extensions page:`r`n1. Turn on Developer mode.`r`n2. Click Load unpacked.`r`n3. Press Ctrl+L in the folder picker, paste the path already copied to your clipboard, press Enter, then Select Folder.`r`n`r`nClick Yes only after BW Reader appears in the extensions list. This is required once."
    $answer = [System.Windows.Forms.MessageBox]::Show(
      $message,
      'BW Extension Test - one-time setup',
      [System.Windows.Forms.MessageBoxButtons]::YesNo,
      [System.Windows.Forms.MessageBoxIcon]::Information
    )
    if ($answer -eq [System.Windows.Forms.DialogResult]::Yes) {
      Set-Content -LiteralPath $registeredMarker -Value ([string]$channel.version) -Encoding ascii
      Stop-TestProfileBrowserForUpdate
      Reload-BwExtensionWorker $browser $baseArguments $startUrl ([string]$channel.version)
      Set-Content -LiteralPath $workerVersionFile -Value ([string]$channel.version) -Encoding ascii
    }
  }
} else {
  $workerVersionFile = Join-Path $baseDir 'worker-runtime-version.txt'
  $workerVersion = if (Test-Path -LiteralPath $workerVersionFile) {
    (Get-Content -LiteralPath $workerVersionFile -Raw).Trim()
  } else {
    ''
  }
  if ($workerVersion -ne [string]$channel.version) {
    throw 'The package is installed but its worker reload is pending. Run the launcher without -NoLaunch.'
  }
}

Write-Host ('BW Reader extension test build ' + $channel.version + ' is ready.') -ForegroundColor Cyan
