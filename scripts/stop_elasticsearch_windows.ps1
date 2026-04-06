$ErrorActionPreference = 'Stop'

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$PidDir = Join-Path $ProjectRoot '.tmp'
$PidFile = Join-Path $PidDir 'elasticsearch.pid'
$EsUrl = if ($env:ES_URL) { $env:ES_URL } else { 'http://localhost:9200' }

function Stop-ProcessTree {
  param(
    [Parameter(Mandatory = $true)]
    [int]$ProcessId,

    [ValidateSet('Stop', 'Kill')]
    [string]$Mode = 'Stop'
  )

  $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$ProcessId" -ErrorAction SilentlyContinue
  foreach ($child in $children) {
    Stop-ProcessTree -ProcessId $child.ProcessId -Mode $Mode
  }

  try {
    $process = Get-Process -Id $ProcessId -ErrorAction Stop
    if ($Mode -eq 'Kill') {
      $process.Kill()
    } else {
      Stop-Process -Id $ProcessId -ErrorAction Stop
    }
  } catch {
  }
}

function Wait-ForExit {
  param(
    [Parameter(Mandatory = $true)]
    [int]$ProcessId,

    [int]$Attempts = 20
  )

  for ($i = 0; $i -lt $Attempts; $i++) {
    try {
      Get-Process -Id $ProcessId -ErrorAction Stop | Out-Null
      Start-Sleep -Seconds 1
    } catch {
      return $true
    }
  }

  return $false
}

if (Test-Path $PidFile) {
  $pidText = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
  if ($pidText) {
    try {
      $pid = [int]$pidText
      Get-Process -Id $pid -ErrorAction Stop | Out-Null
      Write-Output "Stopping Elasticsearch process tree rooted at PID $pid"
      Stop-ProcessTree -ProcessId $pid -Mode 'Stop'

      if (Wait-ForExit -ProcessId $pid -Attempts 20) {
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        Write-Output 'Elasticsearch stopped'
        exit 0
      }

      Write-Output 'Process did not exit cleanly, forcing termination'
      Stop-ProcessTree -ProcessId $pid -Mode 'Kill'
      Wait-ForExit -ProcessId $pid -Attempts 10 | Out-Null
      Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
      Write-Output 'Elasticsearch forced to stop'
      exit 0
    } catch {
      Write-Output "Removing stale PID file: $PidFile"
      Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    }
  }
}

$listeners = Get-NetTCPConnection -LocalPort 9200 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique
if ($listeners) {
  Write-Output "No PID file found, but a process is still listening on $EsUrl"
  foreach ($listenerPid in $listeners) {
    try {
      Write-Output "Stopping listener PID $listenerPid"
      Stop-Process -Id $listenerPid -ErrorAction Stop
    } catch {
    }
  }

  Start-Sleep -Seconds 3
  $listeners = Get-NetTCPConnection -LocalPort 9200 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique
  if ($listeners) {
    foreach ($listenerPid in $listeners) {
      try {
        Write-Output "Force stopping listener PID $listenerPid"
        (Get-Process -Id $listenerPid -ErrorAction Stop).Kill()
      } catch {
      }
    }
  }

  Write-Output 'Elasticsearch stopped via port listener cleanup'
  exit 0
}

Write-Output 'Elasticsearch is not running'
exit 0