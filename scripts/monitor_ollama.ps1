param(
  [int]$IntervalSeconds = 2,
  [switch]$ShowGpu
)

while ($true) {
  $lines = @()
  $lines += "$(Get-Date -Format 'HH:mm:ss')  Ollama"
  $lines += "-------------------------------------"
  $lines += (ollama ps 2>&1 | Out-String).TrimEnd() -split "`n"

  if ($ShowGpu) {
    $lines += ""
    $lines += "GPU"
    $lines += "-------------------------------------"
    foreach ($row in (nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total --format=csv,noheader 2>&1 | Out-String).TrimEnd() -split "`n") {
      $parts = $row -split ", "
      if ($parts.Count -ge 4) {
        $lines += "  $($parts[0].Trim())  util=$($parts[1].Trim())  mem=$($parts[2].Trim())/$($parts[3].Trim())"
      } else {
        $lines += "  $row"
      }
    }
  }

  Clear-Host
  $lines | ForEach-Object { Write-Host $_ }

  Start-Sleep -Seconds $IntervalSeconds
}
