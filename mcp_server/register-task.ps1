$action = New-ScheduledTaskAction -Execute 'C:\data\claude_all\mcp_server\start.bat' -WorkingDirectory 'C:\data\claude_all\mcp_server'
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -RunLevel Highest
Register-ScheduledTask -TaskName 'MCP-DataAnalysis' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Host 'Task registered. Starting...'
Start-ScheduledTask -TaskName 'MCP-DataAnalysis'
Write-Host 'Task started.'
