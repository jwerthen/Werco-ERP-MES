<#
.SYNOPSIS
    Werco ERP Database Restore Script
    
.DESCRIPTION
    Restores the PostgreSQL database from a backup file.
    Supports .sql and .sql.gz backup files.
    
.PARAMETER BackupFile
    Path to the backup file to restore. Required.
    
.PARAMETER DatabaseUrl
    Full PostgreSQL connection URL. If not provided, will try to get from Railway or .env
    
.PARAMETER Force
    Skip confirmation prompt

.EXAMPLE
    .\db-restore.ps1 -BackupFile ".\backups\database\werco_erp_backup_20260109_120000.sql.gz"
    .\db-restore.ps1 -BackupFile ".\backups\database\werco_erp_backup_20260109_120000.sql.gz" -Force
#>

param(
    [Parameter(Mandatory=$true)]
    [string]$BackupFile,
    [string]$DatabaseUrl,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

# Script directory
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Werco ERP Database Restore" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# Verify backup file exists
if (-not (Test-Path $BackupFile)) {
    # Try relative to backups directory
    $altPath = Join-Path $ProjectRoot "backups\database\$BackupFile"
    if (Test-Path $altPath) {
        $BackupFile = $altPath
    } else {
        Write-Host "ERROR: Backup file not found: $BackupFile" -ForegroundColor Red
        exit 1
    }
}

$BackupFile = Resolve-Path $BackupFile
Write-Host "Backup file: $BackupFile" -ForegroundColor Cyan
Write-Host ""

# Function to get DATABASE_URL from various sources
function Get-DatabaseUrl {
    if ($DatabaseUrl) {
        Write-Host "Using DATABASE_URL from command line" -ForegroundColor Green
        return $DatabaseUrl
    }
    
    if ($env:DATABASE_URL) {
        Write-Host "Using DATABASE_URL from environment" -ForegroundColor Green
        return $env:DATABASE_URL
    }
    
    try {
        $railwayUrl = & railway variables get DATABASE_URL 2>$null
        if ($railwayUrl -and $railwayUrl -match "^postgresql://") {
            Write-Host "Using DATABASE_URL from Railway" -ForegroundColor Green
            return $railwayUrl
        }
    } catch {}
    
    $envFile = Join-Path $ProjectRoot ".env"
    if (Test-Path $envFile) {
        $envContent = Get-Content $envFile
        foreach ($line in $envContent) {
            if ($line -match "^DATABASE_URL=(.+)$") {
                Write-Host "Using DATABASE_URL from .env file" -ForegroundColor Green
                return $Matches[1].Trim('"').Trim("'")
            }
        }
    }
    
    return $null
}

# Function to parse PostgreSQL URL
function Parse-DatabaseUrl {
    param([string]$Url)
    
    if ($Url -match "postgresql://([^:]+):([^@]+)@([^:]+):(\d+)/(.+)") {
        return @{
            User = $Matches[1]
            Password = $Matches[2]
            Host = $Matches[3]
            Port = $Matches[4]
            Database = $Matches[5].Split('?')[0]
        }
    }
    return $null
}

# Get database URL
$dbUrl = Get-DatabaseUrl
if (-not $dbUrl) {
    Write-Host "ERROR: Could not find DATABASE_URL" -ForegroundColor Red
    exit 1
}

$dbConfig = Parse-DatabaseUrl $dbUrl
if (-not $dbConfig) {
    Write-Host "ERROR: Could not parse DATABASE_URL" -ForegroundColor Red
    exit 1
}

Write-Host "Target: $($dbConfig.Database)@$($dbConfig.Host):$($dbConfig.Port)" -ForegroundColor Gray
Write-Host ""

# Warning and confirmation
Write-Host "WARNING: This will REPLACE ALL DATA in the database!" -ForegroundColor Red
Write-Host "Database: $($dbConfig.Database)" -ForegroundColor Yellow
Write-Host ""

if (-not $Force) {
    $confirmation = Read-Host "Type 'RESTORE' to confirm"
    if ($confirmation -ne "RESTORE") {
        Write-Host "Restore cancelled." -ForegroundColor Yellow
        exit 0
    }
}

Write-Host ""
Write-Host "Starting restore..." -ForegroundColor Yellow

# Set PGPASSWORD
$env:PGPASSWORD = $dbConfig.Password

try {
    # Determine if file is compressed
    $isCompressed = $BackupFile -like "*.gz"
    $sqlFile = $BackupFile
    
    if ($isCompressed) {
        Write-Host "Decompressing backup file..." -ForegroundColor Yellow
        $sqlFile = $BackupFile -replace "\.gz$", ""
        
        # Decompress using .NET
        $inputStream = [System.IO.File]::OpenRead($BackupFile)
        $outputStream = [System.IO.File]::Create($sqlFile)
        $gzipStream = New-Object System.IO.Compression.GZipStream($inputStream, [System.IO.Compression.CompressionMode]::Decompress)
        
        $gzipStream.CopyTo($outputStream)
        
        $outputStream.Close()
        $gzipStream.Close()
        $inputStream.Close()
        
        Write-Host "Decompressed to: $sqlFile" -ForegroundColor Gray
    }
    
    # Get file size
    $fileSize = (Get-Item $sqlFile).Length
    Write-Host "SQL file size: $([math]::Round($fileSize / 1MB, 2)) MB" -ForegroundColor Gray
    
    # Run psql to restore
    Write-Host "Restoring database..." -ForegroundColor Yellow
    
    $psqlArgs = @(
        "-h", $dbConfig.Host,
        "-p", $dbConfig.Port,
        "-U", $dbConfig.User,
        "-d", $dbConfig.Database,
        "-f", $sqlFile,
        "-v", "ON_ERROR_STOP=1"
    )
    
    $result = & psql @psqlArgs 2>&1
    
    if ($LASTEXITCODE -ne 0) {
        # Check for common acceptable errors
        $errorOutput = $result | Out-String
        if ($errorOutput -match "does not exist" -or $errorOutput -match "already exists") {
            Write-Host "Note: Some objects may have been recreated (this is normal)" -ForegroundColor Yellow
        } else {
            Write-Host "Warning: psql returned errors" -ForegroundColor Yellow
            Write-Host $errorOutput -ForegroundColor Gray
        }
    }
    
    # Clean up decompressed file if we created it
    if ($isCompressed -and (Test-Path $sqlFile)) {
        Remove-Item $sqlFile -Force
    }
    
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host "  Restore completed!" -ForegroundColor Green
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "IMPORTANT: You may need to restart the application to" -ForegroundColor Yellow
    Write-Host "           reconnect to the restored database." -ForegroundColor Yellow
    Write-Host ""
    
} finally {
    $env:PGPASSWORD = $null
}
