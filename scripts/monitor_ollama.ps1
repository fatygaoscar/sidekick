param(
  [int]$IntervalSeconds = 2,
  [switch]$ShowGpu,
  [string]$SidekickLogPath = "",
  [string]$SidekickBaseUrl = "http://127.0.0.1:8000"
)

function Get-Bar([float]$Pct, [int]$Width = 20) {
  $filled = [int]([Math]::Floor($Pct * $Width))
  $bar = ('#' * $filled) + ('-' * ($Width - $filled))
  "[$bar] {0:0}%" -f ($Pct * 100)
}

function Get-LatestJobId([string]$LogPath) {
  if (-not $LogPath -or -not (Test-Path $LogPath)) { return $null }
  $matches = Get-Content $LogPath |
    Select-String '/api/export-jobs/([0-9a-f-]{36})' |
    ForEach-Object { $_.Matches[0].Groups[1].Value }
  if ($matches) { return @($matches)[-1] } else { return $null }
}

while ($true) {
  $lines = @()
  $lines += "$(Get-Date -Format 'HH:mm:ss')  Sidekick Monitor"
  $lines += "-------------------------------------"

  $lines += "OLLAMA"
  $ollamaOut = (ollama ps 2>&1 | Out-String).TrimEnd() -split "`n"
  foreach ($l in $ollamaOut) { $lines += "  $l" }

  if ($ShowGpu) {
    $lines += ""
    $lines += "GPU"
    foreach ($row in (nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total --format=csv,noheader 2>&1 | Out-String).TrimEnd() -split "`n") {
      $parts = $row -split ", "
      if ($parts.Count -ge 4) {
        $name   = $parts[0].Trim()
        $util   = $parts[1].Trim() -replace ' %', ''
        $memUsed  = $parts[2].Trim() -replace ' MiB', ''
        $memTotal = $parts[3].Trim() -replace ' MiB', ''
        $utilPct  = try { [float]$util / 100 } catch { 0 }
        $memPct   = try { [float]$memUsed / [float]$memTotal } catch { 0 }
        $lines += "  $name"
        $lines += "    Util  $util%   $(Get-Bar $utilPct)"
        $lines += "    VRAM  $memUsed / $memTotal MiB   $(Get-Bar $memPct)"
      } else {
        $lines += "  $row"
      }
    }
  }

  # Export job section
  if ($SidekickLogPath) {
    $jobId = Get-LatestJobId $SidekickLogPath
    if ($jobId) {
      $job = $null
      try {
        $job = Invoke-RestMethod -Uri "$SidekickBaseUrl/api/export-jobs/$jobId" -TimeoutSec 2 -ErrorAction Stop
      } catch { }

      if ($job) {
        $shortId = $jobId.Substring(0, 8)
        $lines += ""
        $lines += "EXPORT ($shortId...)"
        $lines += "  Status    $($job.status)"
        if ($job.stage) {
          $lines += "  Stage     $($job.stage)"
        }
        $tPct = if ($job.transcription_progress -ne $null) {
          [float]$job.transcription_progress
        } elseif ($job.transcript_progress -ne $null) {
          [float]$job.transcript_progress
        } else {
          0
        }
        $sPct = if ($job.summarization_progress -ne $null) {
          [float]$job.summarization_progress
        } elseif ($job.summary_progress -ne $null) {
          [float]$job.summary_progress
        } else {
          0
        }
        $oPct = if ($job.overall_progress -ne $null) { [float]$job.overall_progress } else { 0 }
        $lines += "  Transcript  $(Get-Bar $tPct)"
        $lines += "  Summary     $(Get-Bar $sPct)"
        $lines += "  Overall     $(Get-Bar $oPct)"
      }
    }
  }

  $lines += "-------------------------------------"

  Clear-Host
  $lines | ForEach-Object { Write-Host $_ }

  Start-Sleep -Seconds $IntervalSeconds
}
