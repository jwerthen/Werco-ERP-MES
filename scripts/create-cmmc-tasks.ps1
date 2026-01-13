<#
.SYNOPSIS
    Creates CMMC Level 2 compliance tasks in Notion
.DESCRIPTION
    Creates all tasks from the CMMC compliance roadmap in Notion taskboard.
    Requires NOTION_TOKEN environment variable to be set.
.EXAMPLE
    $env:NOTION_TOKEN = "secret_your_token_here"
    .\create-cmmc-tasks.ps1
#>

# Check for token
$NOTION_TOKEN = $env:NOTION_TOKEN
if (-not $NOTION_TOKEN) {
    Write-Host "Error: NOTION_TOKEN environment variable not set" -ForegroundColor Red
    Write-Host "Set it with: `$env:NOTION_TOKEN = 'your-token-here'" -ForegroundColor Yellow
    exit 1
}

# Get database ID from file
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$DbIdFile = Join-Path $ProjectRoot "notion_taskboard_id.txt"
$TASKBOARD_DB_ID = (Get-Content $DbIdFile -Raw).Trim() -replace '[^a-zA-Z0-9-]', ''

Write-Host "Using Notion Database: $TASKBOARD_DB_ID" -ForegroundColor Cyan

$headers = @{
    "Authorization" = "Bearer $NOTION_TOKEN"
    "Content-Type" = "application/json; charset=utf-8"
    "Notion-Version" = "2022-06-28"
}

function Create-Task {
    param(
        [string]$Title,
        [string]$Priority = "Medium",
        [string]$Status = "Not Started",
        [string]$CurrentStatus = ""
    )
    
    $body = @{
        parent = @{ database_id = $TASKBOARD_DB_ID }
        properties = @{
            "Task" = @{ title = @(@{ text = @{ content = $Title } }) }
            "Status" = @{ select = @{ name = $Status } }
            "Priority" = @{ select = @{ name = $Priority } }
            "Assignee" = @{ select = @{ name = "Droid" } }
        }
    }
    
    if ($CurrentStatus) {
        $body.properties["Current Status"] = @{ rich_text = @(@{ text = @{ content = $CurrentStatus } }) }
    }
    
    $jsonBody = $body | ConvertTo-Json -Depth 6
    $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($jsonBody)
    
    try {
        $result = Invoke-RestMethod -Uri "https://api.notion.com/v1/pages" -Headers $headers -Method Post -Body $bodyBytes
        Write-Host "  Created: $Title" -ForegroundColor Green
        return $result.id
    }
    catch {
        Write-Host "  Failed: $Title - $_" -ForegroundColor Red
        return $null
    }
}

Write-Host "`n=== Creating CMMC Level 2 Compliance Tasks ===" -ForegroundColor Cyan
Write-Host ""

# Phase 1: Critical (P1)
Write-Host "Phase 1: Critical Priority" -ForegroundColor Yellow
Create-Task -Title "[CMMC P1] Multi-Factor Authentication (TOTP) - IA-3.5.3" -Priority "High" -Status "Not Started" -CurrentStatus "2-3 weeks effort"
Create-Task -Title "[CMMC P1] Password Policy Enforcement - IA-3.5.7/8/9" -Priority "High" -Status "Not Started" -CurrentStatus "1 week effort"
Create-Task -Title "[CMMC P1] Data Encryption at Rest - SC-3.13.16" -Priority "High" -Status "Not Started" -CurrentStatus "2-4 weeks effort"
Create-Task -Title "[CMMC P1] System Security Plan (SSP) - PL-3.12.1" -Priority "High" -Status "Not Started" -CurrentStatus "2-4 weeks effort - Documentation"

# Phase 2: High Priority (P2)
Write-Host "`nPhase 2: High Priority" -ForegroundColor Yellow
Create-Task -Title "[CMMC P2] Session Inactivity Timeout - AC-3.1.10" -Priority "High" -Status "Not Started" -CurrentStatus "3-5 days effort"
Create-Task -Title "[CMMC P2] Audit Log Protection - AU-3.3.8" -Priority "High" -Status "Done" -CurrentStatus "COMPLETE - Hash chain integrity implemented"
Create-Task -Title "[CMMC P2] Incident Response Procedures - IR-3.6.1" -Priority "Medium" -Status "Not Started" -CurrentStatus "1-2 weeks effort - Documentation"
Create-Task -Title "[CMMC P2] Automated Security Alerting - IR-3.6.2" -Priority "Medium" -Status "Not Started" -CurrentStatus "2-3 weeks effort"
Create-Task -Title "[CMMC P2] Vulnerability Scanning Setup - RA-3.11.2" -Priority "Medium" -Status "Not Started" -CurrentStatus "1-2 weeks effort"

# Phase 3: Medium Priority (P3)
Write-Host "`nPhase 3: Medium Priority" -ForegroundColor Yellow
Create-Task -Title "[CMMC P3] Media Protection (Encrypted Uploads) - MP-3.8.1" -Priority "Medium" -Status "Not Started" -CurrentStatus "1-2 weeks effort"
Create-Task -Title "[CMMC P3] Security Training Tracking - AT-3.2.1" -Priority "Low" -Status "Not Started" -CurrentStatus "1 week effort"
Create-Task -Title "[CMMC P3] Continuous Monitoring Dashboard - CA-3.12.3" -Priority "Medium" -Status "Not Started" -CurrentStatus "2-3 weeks effort"
Create-Task -Title "[CMMC P3] Configuration Change Tracking - CM-3.4.3" -Priority "Low" -Status "Not Started" -CurrentStatus "1-2 weeks effort"

# Phase 4: Documentation & Process
Write-Host "`nPhase 4: Documentation & Process" -ForegroundColor Yellow
Create-Task -Title "[CMMC Doc] Incident Response Plan" -Priority "Medium" -Status "Not Started" -CurrentStatus "Documentation"
Create-Task -Title "[CMMC Doc] Personnel Termination Procedures - PS-3.9.2" -Priority "Low" -Status "Not Started" -CurrentStatus "Documentation"
Create-Task -Title "[CMMC Doc] Media Sanitization Procedures - MP-3.8.3" -Priority "Low" -Status "Not Started" -CurrentStatus "Documentation"
Create-Task -Title "[CMMC Doc] Risk Assessment Process - RA-3.11.1" -Priority "Medium" -Status "Not Started" -CurrentStatus "Process documentation"
Create-Task -Title "[CMMC Doc] Railway SOC 2 Documentation" -Priority "Low" -Status "Not Started" -CurrentStatus "Obtain from Railway"

Write-Host "`n=== Task Creation Complete ===" -ForegroundColor Cyan
Write-Host "Total tasks created for CMMC Level 2 compliance roadmap" -ForegroundColor Green
