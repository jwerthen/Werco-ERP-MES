<#
.SYNOPSIS
    Werco ERP Database Backup Utilities
    
.DESCRIPTION
    Utility functions for managing database backups:
    - List backups
    - Verify backup integrity
    - Download from Railway
    - Setup scheduled backups
    
.PARAMETER Action
    Action to perform: list, verify, schedule, download-latest

.EXAMPLE
    .\db-backup-utils.ps1 list
    .\db-backup-utils.ps1 verify -BackupFile ".\backups\database\werco_erp_backup_20260109.sql.gz"
    .\db-backup-utils.ps1 schedule -IntervalHours 24
#>

param(
    [Parameter(Position=0)]
    [ValidateSet("list", "verify", "schedule", "unschedule", "download-latest")]
    [string]$Action = "list",
    
    [string]$BackupFile,
    [int]$IntervalHours = 24
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$BackupDir = Join-Path $ProjectRoot "backups\database"

function Show-Backups {
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host "  Available Database Backups" -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host ""
    
    if (-not (Test-Path $BackupDir)) {
        Write-Host "No backups directory found." -ForegroundColor Yellow
        Write-Host "Run db-backup.ps1 to create your first backup." -ForegroundColor Gray
        return
    }
    
    $backups = Get-ChildItem -Path $BackupDir -Filter "werco_erp_backup_*.sql.gz" | 
               Sort-Object LastWriteTime -Descending
    
    if ($backups.Count -eq 0) {
        Write-Host "No backups found." -ForegroundColor Yellow
        Write-Host "Run db-backup.ps1 to create your first backup." -ForegroundColor Gray
        return
    }
    
    $totalSize = 0
    $index = 1
    
    Write-Host "  #   Date/Time            Size      Age" -ForegroundColor Gray
    Write-Host "  --- -------------------- --------- --------" -ForegroundColor Gray
    
    foreach ($backup in $backups) {
        $size = [math]::Round($backup.Length / 1MB, 2)
        $totalSize += $backup.Length
        $age = (Get-Date) - $backup.LastWriteTime
        $ageStr = if ($age.Days -gt 0) { "$($age.Days)d $($age.Hours)h" } else { "$($age.Hours)h $($age.Minutes)m" }
        
        $color = if ($index -eq 1) { "Green" } elseif ($age.Days -gt 7) { "Yellow" } else { "White" }
        
        Write-Host "  $($index.ToString().PadLeft(2))  $($backup.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss'))  $($size.ToString().PadLeft(6)) MB  $ageStr" -ForegroundColor $color
        $index++
    }
    
    Write-Host ""
    Write-Host "Total: $($backups.Count) backup(s), $([math]::Round($totalSize / 1MB, 2)) MB" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "To restore a backup, run:" -ForegroundColor Gray
    Write-Host "  .\db-restore.ps1 -BackupFile `"$($backups[0].FullName)`"" -ForegroundColor White
}

function Test-BackupIntegrity {
    param([string]$File)
    
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host "  Verify Backup Integrity" -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host ""
    
    if (-not $File) {
        # Use most recent backup
        $latestBackup = Get-ChildItem -Path $BackupDir -Filter "werco_erp_backup_*.sql.gz" | 
                        Sort-Object LastWriteTime -Descending | 
                        Select-Object -First 1
        
        if (-not $latestBackup) {
            Write-Host "No backup files found to verify." -ForegroundColor Red
            return
        }
        $File = $latestBackup.FullName
    }
    
    if (-not (Test-Path $File)) {
        Write-Host "ERROR: File not found: $File" -ForegroundColor Red
        return
    }
    
    Write-Host "Verifying: $File" -ForegroundColor Gray
    Write-Host ""
    
    $checks = @{
        "File exists" = $true
        "File readable" = $false
        "Valid gzip format" = $false
        "Contains SQL statements" = $false
        "Contains CREATE statements" = $false
        "Contains INSERT statements" = $false
    }
    
    try {
        # Check if file is readable
        $stream = [System.IO.File]::OpenRead($File)
        $stream.Close()
        $checks["File readable"] = $true
        
        # Check gzip header
        $bytes = [System.IO.File]::ReadAllBytes($File)
        if ($bytes.Length -ge 2 -and $bytes[0] -eq 0x1F -and $bytes[1] -eq 0x8B) {
            $checks["Valid gzip format"] = $true
        }
        
        # Decompress and check content
        $inputStream = [System.IO.File]::OpenRead($File)
        $gzipStream = New-Object System.IO.Compression.GZipStream($inputStream, [System.IO.Compression.CompressionMode]::Decompress)
        $reader = New-Object System.IO.StreamReader($gzipStream)
        
        # Read first 50KB to check content
        $buffer = New-Object char[] 51200
        $charsRead = $reader.Read($buffer, 0, 51200)
        $content = -join $buffer[0..($charsRead-1)]
        
        $reader.Close()
        $gzipStream.Close()
        $inputStream.Close()
        
        if ($content -match "SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER") {
            $checks["Contains SQL statements"] = $true
        }
        if ($content -match "CREATE TABLE|CREATE INDEX|CREATE SEQUENCE") {
            $checks["Contains CREATE statements"] = $true
        }
        if ($content -match "INSERT INTO|COPY .+ FROM stdin") {
            $checks["Contains INSERT statements"] = $true
        }
        
    } catch {
        Write-Host "Error during verification: $_" -ForegroundColor Red
    }
    
    # Display results
    $allPassed = $true
    foreach ($check in $checks.GetEnumerator()) {
        $status = if ($check.Value) { "[PASS]" } else { "[FAIL]" }
        $color = if ($check.Value) { "Green" } else { "Red" }
        Write-Host "  $status $($check.Key)" -ForegroundColor $color
        if (-not $check.Value) { $allPassed = $false }
    }
    
    Write-Host ""
    if ($allPassed) {
        Write-Host "Backup verification: PASSED" -ForegroundColor Green
    } else {
        Write-Host "Backup verification: FAILED" -ForegroundColor Red
        Write-Host "This backup may be corrupted or incomplete." -ForegroundColor Yellow
    }
}

function Set-ScheduledBackup {
    param([int]$Hours)
    
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host "  Schedule Automatic Backups" -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host ""
    
    $taskName = "WercoERP-DatabaseBackup"
    $backupScript = Join-Path $ScriptDir "db-backup.ps1"
    
    # Check if task already exists
    $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existingTask) {
        Write-Host "Removing existing scheduled task..." -ForegroundColor Yellow
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    
    # Create the scheduled task
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-ExecutionPolicy Bypass -File `"$backupScript`"" -WorkingDirectory $ProjectRoot
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Hours $Hours)
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
    
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null
    
    Write-Host "Scheduled task created: $taskName" -ForegroundColor Green
    Write-Host "Backup will run every $Hours hour(s)" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "First backup will run in 5 minutes." -ForegroundColor Gray
    Write-Host ""
    Write-Host "To remove this schedule, run:" -ForegroundColor Gray
    Write-Host "  .\db-backup-utils.ps1 unschedule" -ForegroundColor White
}

function Remove-ScheduledBackup {
    $taskName = "WercoERP-DatabaseBackup"
    
    $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existingTask) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host "Scheduled backup task removed." -ForegroundColor Green
    } else {
        Write-Host "No scheduled backup task found." -ForegroundColor Yellow
    }
}

# Main action router
switch ($Action) {
    "list" { Show-Backups }
    "verify" { Test-BackupIntegrity -File $BackupFile }
    "schedule" { Set-ScheduledBackup -Hours $IntervalHours }
    "unschedule" { Remove-ScheduledBackup }
    "download-latest" {
        Write-Host "This feature requires Railway CLI and is meant for downloading" -ForegroundColor Yellow
        Write-Host "backups from a production Railway database to local storage." -ForegroundColor Yellow
        Write-Host ""
        Write-Host "To download latest backup, just run db-backup.ps1 with your" -ForegroundColor Gray
        Write-Host "production DATABASE_URL from Railway." -ForegroundColor Gray
    }
    default {
        Write-Host "Unknown action: $Action" -ForegroundColor Red
        Write-Host "Valid actions: list, verify, schedule, unschedule" -ForegroundColor Yellow
    }
}
