<#
.SYNOPSIS
    Notion CLI for Claude Code integration (notion-cc)
    Inspired by Geoffrey Litt's workflow

.DESCRIPTION
    CLI tool to interact with Notion Task Board for AI agent workflow.
    Supports updating task status, setting blocked state, adding comments,
    and waiting for user responses.

.EXAMPLE
    .\notion-cc.ps1 update-status <task-id> "Analyzing codebase..."
    .\notion-cc.ps1 set-blocked <task-id> true "Need clarification on requirements"
    .\notion-cc.ps1 add-comment <task-id> "What database should I use?"
    .\notion-cc.ps1 wait-for-comment <task-id>
    .\notion-cc.ps1 move-to-done <task-id>
    .\notion-cc.ps1 list-tasks
#>

param(
    [Parameter(Position=0)]
    [ValidateSet("update-status", "set-blocked", "add-comment", "wait-for-comment", "move-to-done", "list-tasks", "get-task", "create-task")]
    [string]$Command,
    
    [Parameter(Position=1)]
    [string]$TaskId,
    
    [Parameter(Position=2)]
    [string]$Value,
    
    [Parameter(Position=3)]
    [string]$Message
)

$NOTION_TOKEN = $env:NOTION_TOKEN
if (-not $NOTION_TOKEN) {
    Write-Host "Error: NOTION_TOKEN environment variable not set" -ForegroundColor Red
    Write-Host "Set it with: `$env:NOTION_TOKEN = 'your-token-here'"
    exit 1
}

$TASKBOARD_DB_ID = "2e121c1c-7909-8161-aa3e-c8ef65d599e4"

$headers = @{
    "Authorization" = "Bearer $NOTION_TOKEN"
    "Notion-Version" = "2022-06-28"
    "Content-Type" = "application/json; charset=utf-8"
}

function Get-TaskIdFromUrl {
    param([string]$InputVal)
    # Already in UUID format with dashes
    if ($InputVal -match "^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$") {
        return $InputVal
    }
    # 32-char hex without dashes
    if ($InputVal -match "([a-f0-9]{32})") {
        $id = $Matches[1]
        return "$($id.Substring(0,8))-$($id.Substring(8,4))-$($id.Substring(12,4))-$($id.Substring(16,4))-$($id.Substring(20,12))"
    }
    # URL format
    if ($InputVal -match "([a-f0-9-]{36})") {
        return $Matches[1]
    }
    return $InputVal
}

function Update-TaskStatus {
    param([string]$TaskId, [string]$Status)
    $id = Get-TaskIdFromUrl $TaskId
    $body = @{
        properties = @{
            "Current Status" = @{
                rich_text = @(@{ text = @{ content = $Status } })
            }
        }
    } | ConvertTo-Json -Depth 5
    $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($body)
    $null = Invoke-RestMethod -Uri "https://api.notion.com/v1/pages/$id" -Headers $headers -Method Patch -Body $bodyBytes
    Write-Host "Updated status: $Status"
}

function Set-Blocked {
    param([string]$TaskId, [bool]$Blocked, [string]$Question)
    $id = Get-TaskIdFromUrl $TaskId
    $body = @{
        properties = @{
            "Blocked" = @{ checkbox = $Blocked }
        }
    } | ConvertTo-Json -Depth 5
    $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($body)
    $null = Invoke-RestMethod -Uri "https://api.notion.com/v1/pages/$id" -Headers $headers -Method Patch -Body $bodyBytes
    
    if ($Blocked -and $Question) {
        Add-Comment -TaskId $id -Comment $Question
    }
    Write-Host "Blocked: $Blocked"
}

function Add-Comment {
    param([string]$TaskId, [string]$Comment)
    $id = Get-TaskIdFromUrl $TaskId
    $body = @{
        parent = @{ page_id = $id }
        rich_text = @(@{ text = @{ content = "[DROID] $Comment" } })
    } | ConvertTo-Json -Depth 5
    $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($body)
    $null = Invoke-RestMethod -Uri "https://api.notion.com/v1/comments" -Headers $headers -Method Post -Body $bodyBytes
    Write-Host "Comment added"
}

function Get-Comments {
    param([string]$TaskId)
    $id = Get-TaskIdFromUrl $TaskId
    $result = Invoke-RestMethod -Uri "https://api.notion.com/v1/comments?block_id=$id" -Headers $headers -Method Get
    return $result.results
}

function Wait-ForComment {
    param([string]$TaskId, [int]$TimeoutSeconds = 3600, [int]$PollIntervalSeconds = 10)
    $id = Get-TaskIdFromUrl $TaskId
    
    $initialComments = Get-Comments -TaskId $id
    $initialCount = $initialComments.Count
    $lastDroidCommentTime = $null
    
    foreach ($c in $initialComments) {
        $text = ($c.rich_text | ForEach-Object { $_.plain_text }) -join ""
        if ($text -match "^\[DROID\]") {
            $lastDroidCommentTime = $c.created_time
        }
    }
    
    Write-Host "Waiting for user response... (timeout: $TimeoutSeconds seconds)"
    $elapsed = 0
    
    while ($elapsed -lt $TimeoutSeconds) {
        Start-Sleep -Seconds $PollIntervalSeconds
        $elapsed += $PollIntervalSeconds
        
        $currentComments = Get-Comments -TaskId $id
        
        foreach ($c in $currentComments) {
            $text = ($c.rich_text | ForEach-Object { $_.plain_text }) -join ""
            $createdTime = $c.created_time
            
            if (-not ($text -match "^\[DROID\]")) {
                if ($lastDroidCommentTime -and $createdTime -gt $lastDroidCommentTime) {
                    Write-Host "User responded: $text"
                    return $text
                }
                elseif (-not $lastDroidCommentTime -and $currentComments.Count -gt $initialCount) {
                    Write-Host "User responded: $text"
                    return $text
                }
            }
        }
        
        Write-Host "." -NoNewline
    }
    
    Write-Host "`nTimeout waiting for response"
    return $null
}

function Move-ToDone {
    param([string]$TaskId)
    $id = Get-TaskIdFromUrl $TaskId
    $body = @{
        properties = @{
            "Status" = @{ select = @{ name = "Done" } }
            "Blocked" = @{ checkbox = $false }
            "Current Status" = @{ rich_text = @(@{ text = @{ content = "Completed" } }) }
        }
    } | ConvertTo-Json -Depth 5
    $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($body)
    $null = Invoke-RestMethod -Uri "https://api.notion.com/v1/pages/$id" -Headers $headers -Method Patch -Body $bodyBytes
    Write-Host "Task moved to Done"
}

function Get-Tasks {
    $body = @{ page_size = 100 } | ConvertTo-Json
    $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($body)
    $result = Invoke-RestMethod -Uri "https://api.notion.com/v1/databases/$TASKBOARD_DB_ID/query" -Headers $headers -Method Post -Body $bodyBytes
    
    Write-Host "`n=== Task Board ===" -ForegroundColor Cyan
    foreach ($task in $result.results) {
        $title = ($task.properties.Task.title | ForEach-Object { $_.plain_text }) -join ""
        $status = $task.properties.Status.select.name
        $blocked = $task.properties.Blocked.checkbox
        $currentStatus = ($task.properties."Current Status".rich_text | ForEach-Object { $_.plain_text }) -join ""
        $priority = $task.properties.Priority.select.name
        $assignee = $task.properties.Assignee.select.name
        
        $color = "White"
        if ($blocked) { $color = "Red" }
        elseif ($status -eq "Done") { $color = "Green" }
        elseif ($status -eq "In Progress") { $color = "Yellow" }
        
        Write-Host "`n[$status]" -ForegroundColor $color -NoNewline
        Write-Host " $title" -ForegroundColor $color
        Write-Host "  ID: $($task.id)" -ForegroundColor DarkGray
        if ($priority) { Write-Host "  Priority: $priority" }
        if ($assignee) { Write-Host "  Assignee: $assignee" }
        if ($currentStatus) { Write-Host "  Current: $currentStatus" -ForegroundColor Cyan }
        if ($blocked) { Write-Host "  BLOCKED - Check comments for questions" -ForegroundColor Red }
    }
}

function Create-Task {
    param([string]$Title, [string]$Priority = "Medium", [string]$Status = "Done")
    $body = @{
        parent = @{ database_id = $TASKBOARD_DB_ID }
        properties = @{
            "Task" = @{ title = @(@{ text = @{ content = $Title } }) }
            "Status" = @{ select = @{ name = $Status } }
            "Priority" = @{ select = @{ name = $Priority } }
            "Assignee" = @{ select = @{ name = "Droid" } }
            "Current Status" = @{ rich_text = @(@{ text = @{ content = "Completed" } }) }
        }
    } | ConvertTo-Json -Depth 6
    $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($body)
    $result = Invoke-RestMethod -Uri "https://api.notion.com/v1/pages" -Headers $headers -Method Post -Body $bodyBytes
    Write-Host "Task created: $Title"
    Write-Host "ID: $($result.id)"
    return $result.id
}

function Get-Task {
    param([string]$TaskId)
    $id = Get-TaskIdFromUrl $TaskId
    $task = Invoke-RestMethod -Uri "https://api.notion.com/v1/pages/$id" -Headers $headers -Method Get
    
    $title = ($task.properties.Task.title | ForEach-Object { $_.plain_text }) -join ""
    $status = $task.properties.Status.select.name
    $blocked = $task.properties.Blocked.checkbox
    $currentStatus = ($task.properties."Current Status".rich_text | ForEach-Object { $_.plain_text }) -join ""
    
    Write-Host "`nTask: $title"
    Write-Host "Status: $status"
    Write-Host "Blocked: $blocked"
    Write-Host "Current Status: $currentStatus"
    Write-Host "ID: $($task.id)"
    Write-Host "URL: $($task.url)"
    
    $comments = Get-Comments -TaskId $id
    if ($comments.Count -gt 0) {
        Write-Host "`nComments:"
        foreach ($c in $comments) {
            $text = ($c.rich_text | ForEach-Object { $_.plain_text }) -join ""
            Write-Host "  - $text"
        }
    }
}

# Main command router
switch ($Command) {
    "update-status" {
        if (-not $TaskId -or -not $Value) {
            Write-Host "Usage: notion-cc update-status <task-id> <status-text>"
            exit 1
        }
        Update-TaskStatus -TaskId $TaskId -Status $Value
    }
    "set-blocked" {
        if (-not $TaskId -or -not $Value) {
            Write-Host "Usage: notion-cc set-blocked <task-id> <true|false> [question]"
            exit 1
        }
        $blocked = $Value -eq "true"
        Set-Blocked -TaskId $TaskId -Blocked $blocked -Question $Message
    }
    "add-comment" {
        if (-not $TaskId -or -not $Value) {
            Write-Host "Usage: notion-cc add-comment <task-id> <comment>"
            exit 1
        }
        Add-Comment -TaskId $TaskId -Comment $Value
    }
    "wait-for-comment" {
        if (-not $TaskId) {
            Write-Host "Usage: notion-cc wait-for-comment <task-id>"
            exit 1
        }
        Wait-ForComment -TaskId $TaskId
    }
    "move-to-done" {
        if (-not $TaskId) {
            Write-Host "Usage: notion-cc move-to-done <task-id>"
            exit 1
        }
        Move-ToDone -TaskId $TaskId
    }
    "list-tasks" {
        Get-Tasks
    }
    "get-task" {
        if (-not $TaskId) {
            Write-Host "Usage: notion-cc get-task <task-id>"
            exit 1
        }
        Get-Task -TaskId $TaskId
    }
    "create-task" {
        if (-not $TaskId) {
            Write-Host "Usage: notion-cc create-task <title> [priority] [status]"
            exit 1
        }
        $priority = if ($Value) { $Value } else { "Medium" }
        $status = if ($Message) { $Message } else { "Done" }
        Create-Task -Title $TaskId -Priority $priority -Status $status
    }
    default {
        Write-Host @"
notion-cc - Notion CLI for Claude Code integration

Commands:
  list-tasks                          List all tasks on the board
  get-task <id>                       Get details of a specific task
  update-status <id> <text>           Update the 'Current Status' field
  set-blocked <id> <true|false> [q]   Set blocked status (optionally add question)
  add-comment <id> <text>             Add a comment to the task
  wait-for-comment <id>               Poll until user responds to a comment
  move-to-done <id>                   Move task to Done status

Examples:
  .\notion-cc.ps1 list-tasks
  .\notion-cc.ps1 update-status 2e121c1c-7909-8161-aa3e-c8ef65d599e4 "Analyzing code..."
  .\notion-cc.ps1 set-blocked <id> true "What database credentials should I use?"
  .\notion-cc.ps1 wait-for-comment <id>
"@
    }
}
