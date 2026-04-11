$ErrorActionPreference = 'Stop'

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$BundledEsHome = Join-Path $ProjectRoot 'elasticsearch-9.1.4'
$LogDir = Join-Path $ProjectRoot 'logs'
$PidDir = Join-Path $ProjectRoot '.tmp'
$PidFile = Join-Path $PidDir 'elasticsearch.pid'
$LogFile = Join-Path $LogDir 'elasticsearch.log'
$LocalConfDir = Join-Path $PidDir 'elasticsearch-config'
$EsUrl = if ($env:ES_URL) { $env:ES_URL } else { 'http://localhost:9200' }
$StartupTimeout = if ($env:ES_STARTUP_TIMEOUT) { [int]$env:ES_STARTUP_TIMEOUT } else { 180 }
$StartupArgs = @(
  '-E', 'discovery.type=single-node'
  '-E', 'xpack.security.enabled=false'
  '-E', 'xpack.security.enrollment.enabled=false'
  '-E', 'xpack.security.http.ssl.enabled=false'
  '-E', 'xpack.security.transport.ssl.enabled=false'
)

New-Item -ItemType Directory -Force -Path $LogDir, $PidDir | Out-Null

function Test-ElasticsearchRunning {
  try {
    Invoke-WebRequest -Uri $EsUrl -TimeoutSec 5 -ErrorAction Stop | Out-Null
    return $true
  } catch {
    return $false
  }
}

function Initialize-LocalConfig {
  $sourceConfDir = Join-Path $env:ES_HOME 'config'
  if (-not (Test-Path $sourceConfDir)) {
    throw "Unable to find Elasticsearch config directory: $sourceConfDir"
  }

  Remove-Item -Recurse -Force $LocalConfDir -ErrorAction SilentlyContinue
  New-Item -ItemType Directory -Force -Path $LocalConfDir | Out-Null
  Copy-Item -Path (Join-Path $sourceConfDir '*') -Destination $LocalConfDir -Recurse -Force

  @'
discovery.type: single-node
xpack.security.enabled: false
xpack.security.enrollment.enabled: false
xpack.security.http.ssl.enabled: false
xpack.security.transport.ssl.enabled: false
'@ | Set-Content -Path (Join-Path $LocalConfDir 'elasticsearch.yml') -Encoding utf8
}

function Find-ElasticsearchBinary {
  if ($env:ELASTICSEARCH_BIN -and (Test-Path $env:ELASTICSEARCH_BIN)) {
    return (Resolve-Path $env:ELASTICSEARCH_BIN).Path
  }

  if ($env:ES_HOME) {
    $homeBinary = Join-Path $env:ES_HOME 'bin\elasticsearch.bat'
    if (Test-Path $homeBinary) {
      return (Resolve-Path $homeBinary).Path
    }
  }

  $pathCommand = Get-Command 'elasticsearch.bat' -ErrorAction SilentlyContinue | Select-Object -First 1
  if (-not $pathCommand) {
    $pathCommand = Get-Command 'elasticsearch' -ErrorAction SilentlyContinue | Select-Object -First 1
  }
  if ($pathCommand) {
    return $pathCommand.Source
  }

  $bundledBinary = Join-Path $BundledEsHome 'bin\elasticsearch.bat'
  if (Test-Path $bundledBinary) {
    return (Resolve-Path $bundledBinary).Path
  }

  $localInstallRoot = Join-Path $env:USERPROFILE '.local\elasticsearch'
  if (Test-Path $localInstallRoot) {
    $candidate = Get-ChildItem -Path (Join-Path $localInstallRoot 'elasticsearch-*\bin\elasticsearch.bat') -File -ErrorAction SilentlyContinue |
      Select-Object -First 1
    if ($candidate) {
      return $candidate.FullName
    }
  }

  return $null
}

if (Test-ElasticsearchRunning) {
  Write-Output "Elasticsearch is already running at $EsUrl"
  exit 0
}

if (Test-Path $PidFile) {
  $existingPid = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
  if ($existingPid) {
    try {
      Get-Process -Id [int]$existingPid -ErrorAction Stop | Out-Null
      Write-Output "Elasticsearch process already running with PID $existingPid"
      exit 0
    } catch {
      Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    }
  }
}

$EsBinary = Find-ElasticsearchBinary
if (-not $EsBinary) {
  Write-Output @'
Unable to find the Elasticsearch executable.

Set one of the following before running this script:
  - ES_HOME=C:\path\to\elasticsearch
  - ELASTICSEARCH_BIN=C:\path\to\bin\elasticsearch.bat

The bundled repository copy is expected at .\elasticsearch-9.1.4.
'@
  exit 1
}

if (-not $env:ES_HOME) {
  $env:ES_HOME = Split-Path (Split-Path $EsBinary -Parent) -Parent
}

Initialize-LocalConfig

Write-Output "Starting Elasticsearch with: $EsBinary"
Write-Output "Logs: $LogFile"

$commandLine = 'set "ES_PATH_CONF=' + $LocalConfDir + '" && call "' + $EsBinary + '"'
$commandLine += ' ' + ($StartupArgs -join ' ')
$commandLine += ' >> "' + $LogFile + '" 2>&1'

$process = Start-Process -FilePath 'cmd.exe' -ArgumentList @('/c', $commandLine) -PassThru -WindowStyle Hidden
Set-Content -Path $PidFile -Value $process.Id -Encoding ascii

for ($i = 0; $i -lt $StartupTimeout; $i++) {
  if (Test-ElasticsearchRunning) {
    Write-Output "Elasticsearch is up at $EsUrl"
    exit 0
  }

  Start-Sleep -Seconds 1
}

Write-Output "Elasticsearch did not become ready in time. Check $LogFile"
exit 1