<#
.SYNOPSIS
    Werco ERP Database Backup Script
    
.DESCRIPTION
    Creates compressed backups of the PostgreSQL database.
    Supports Railway, local Docker, and direct PostgreSQL connections.
    
.PARAMETER DatabaseUrl
    Full PostgreSQL connection URL (e.g., postgresql://user:pass@host:5432/dbname)
    If not provided, will try to get from Railway or .env file
    
.PARAMETER OutputDir
    Directory to store backups. Defaults to ./backups/database
    
.PARAMETER RetentionDays
    Number of days to keep local backups. Default: 30

.EXAMPLE
    .\db-backup.ps1
    .\db-backup.ps1 -DatabaseUrl "postgresql://user:pass@localhost:5432/werco_erp"
    .\db-backup.ps1 -RetentionDays 7
#>

param(
    [string]$DatabaseUrl,
    [string]$OutputDir = "",
    [int]$RetentionDays = 30
)

$ErrorActionPreference = "Stop"

# Script directory
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir

# Default output directory
if (-not $OutputDir) {
    $OutputDir = Join-Path $ProjectRoot "backups\database"
}

# Ensure output directory exists
if (-not (Test-Path $OutputDir)) {
    New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
}

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Werco ERP Database Backup" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# Function to get DATABASE_URL from various sources
function Get-DatabaseUrl {
    # 1. Try command line parameter
    if ($DatabaseUrl) {
        Write-Host "Using DATABASE_URL from command line" -ForegroundColor Green
        return $DatabaseUrl
    }
    
    # 2. Try environment variable
    if ($env:DATABASE_URL) {
        Write-Host "Using DATABASE_URL from environment" -ForegroundColor Green
        return $env:DATABASE_URL
    }
    
    # 3. Try Railway CLI
    try {
        $railwayUrl = & railway variables get DATABASE_URL 2>$null
        if ($railwayUrl -and $railwayUrl -match "^postgresql://") {
            Write-Host "Using DATABASE_URL from Railway" -ForegroundColor Green
            return $railwayUrl
        }
    } catch {}
    
    # 4. Try .env file
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
    
    # 5. Try backend .env
    $backendEnvFile = Join-Path $ProjectRoot "backend\.env"
    if (Test-Path $backendEnvFile) {
        $envContent = Get-Content $backendEnvFile
        foreach ($line in $envContent) {
            if ($line -match "^DATABASE_URL=(.+)$") {
                Write-Host "Using DATABASE_URL from backend/.env" -ForegroundColor Green
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
            Database = $Matches[5].Split('?')[0]  # Remove query params
        }
    }
    return $null
}

# Function to check if pg_dump is available
function Test-PgDump {
    try {
        $null = & pg_dump --version 2>&1
        return $true
    } catch {
        return $false
    }
}

# Get database URL
$dbUrl = Get-DatabaseUrl
if (-not $dbUrl) {
    Write-Host "ERROR: Could not find DATABASE_URL" -ForegroundColor Red
    Write-Host "Please provide it via:" -ForegroundColor Yellow
    Write-Host "  - Command line: -DatabaseUrl 'postgresql://...'" -ForegroundColor Yellow
    Write-Host "  - Environment variable: `$env:DATABASE_URL" -ForegroundColor Yellow
    Write-Host "  - Railway CLI: railway link (if using Railway)" -ForegroundColor Yellow
    Write-Host "  - .env file in project root" -ForegroundColor Yellow
    exit 1
}

# Parse the URL
$dbConfig = Parse-DatabaseUrl $dbUrl
if (-not $dbConfig) {
    Write-Host "ERROR: Could not parse DATABASE_URL" -ForegroundColor Red
    Write-Host "Expected format: postgresql://user:password@host:port/database" -ForegroundColor Yellow
    exit 1
}

Write-Host "Database: $($dbConfig.Database)@$($dbConfig.Host):$($dbConfig.Port)" -ForegroundColor Gray
Write-Host ""

# Check for pg_dump
if (-not (Test-PgDump)) {
    Write-Host "ERROR: pg_dump not found in PATH" -ForegroundColor Red
    Write-Host "Please install PostgreSQL client tools:" -ForegroundColor Yellow
    Write-Host "  - Windows: https://www.postgresql.org/download/windows/" -ForegroundColor Yellow
    Write-Host "  - Or use: winget install PostgreSQL.PostgreSQL" -ForegroundColor Yellow
    exit 1
}

# Generate backup filename
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$backupFile = Join-Path $OutputDir "werco_erp_backup_$timestamp.sql"
$compressedFile = "$backupFile.gz"

Write-Host "Creating backup..." -ForegroundColor Yellow

# Set PGPASSWORD environment variable
$env:PGPASSWORD = $dbConfig.Password

try {
    # Run pg_dump
    $pgDumpArgs = @(
        "-h", $dbConfig.Host,
        "-p", $dbConfig.Port,
        "-U", $dbConfig.User,
        "-d", $dbConfig.Database,
        "--no-owner",
        "--no-acl",
        "--clean",
        "--if-exists",
        "-f", $backupFile
    )
    
    Write-Host "Running pg_dump..." -ForegroundColor Gray
    $result = & pg_dump @pgDumpArgs 2>&1
    
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: pg_dump failed" -ForegroundColor Red
        Write-Host $result -ForegroundColor Red
        exit 1
    }
    
    # Check if backup file was created
    if (-not (Test-Path $backupFile)) {
        Write-Host "ERROR: Backup file was not created" -ForegroundColor Red
        exit 1
    }
    
    $backupSize = (Get-Item $backupFile).Length
    Write-Host "Backup created: $([math]::Round($backupSize / 1MB, 2)) MB" -ForegroundColor Green
    
    # Compress the backup using PowerShell (gzip alternative)
    Write-Host "Compressing backup..." -ForegroundColor Yellow
    
    # Try using gzip if available, otherwise use .NET compression
    try {
        $null = & gzip --version 2>&1
        & gzip -f $backupFile
    } catch {
        # Use .NET compression
        $inputStream = [System.IO.File]::OpenRead($backupFile)
        $outputStream = [System.IO.File]::Create($compressedFile)
        $gzipStream = New-Object System.IO.Compression.GZipStream($outputStream, [System.IO.Compression.CompressionMode]::Compress)
        
        $inputStream.CopyTo($gzipStream)
        
        $gzipStream.Close()
        $outputStream.Close()
        $inputStream.Close()
        
        # Remove uncompressed file
        Remove-Item $backupFile -Force
    }
    
    if (Test-Path $compressedFile) {
        $compressedSize = (Get-Item $compressedFile).Length
        Write-Host "Compressed: $([math]::Round($compressedSize / 1MB, 2)) MB" -ForegroundColor Green
    }
    
    # Clean up old backups
    Write-Host "Cleaning up old backups (older than $RetentionDays days)..." -ForegroundColor Yellow
    $cutoffDate = (Get-Date).AddDays(-$RetentionDays)
    $oldBackups = Get-ChildItem -Path $OutputDir -Filter "werco_erp_backup_*.sql.gz" | 
                  Where-Object { $_.LastWriteTime -lt $cutoffDate }
    
    $deletedCount = 0
    foreach ($oldBackup in $oldBackups) {
        Remove-Item $oldBackup.FullName -Force
        $deletedCount++
    }
    
    if ($deletedCount -gt 0) {
        Write-Host "Deleted $deletedCount old backup(s)" -ForegroundColor Gray
    }
    
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host "  Backup completed successfully!" -ForegroundColor Green
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "Backup file: $compressedFile" -ForegroundColor Cyan
    Write-Host ""
    
    # List recent backups
    Write-Host "Recent backups:" -ForegroundColor Yellow
    Get-ChildItem -Path $OutputDir -Filter "werco_erp_backup_*.sql.gz" | 
        Sort-Object LastWriteTime -Descending | 
        Select-Object -First 5 |
        ForEach-Object {
            $size = [math]::Round($_.Length / 1MB, 2)
            Write-Host "  $($_.Name) ($size MB) - $($_.LastWriteTime)" -ForegroundColor Gray
        }
    
} finally {
    # Clear password from environment
    $env:PGPASSWORD = $null
}
