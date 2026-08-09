[CmdletBinding()]
param(
    [switch]$Once,
    [int]$PollSeconds = 8,
    [ValidateRange(1, 1440)]
    [int]$MaxExecutionMinutes = 180,
    [string]$ApiBase = 'http://127.0.0.1:8000/api/v1',
    [string]$WorkspaceRoot = '',
    [string]$CodexPath = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($WorkspaceRoot)) {
    $WorkspaceRoot = Join-Path $projectRoot 'runtime\execution-workspaces'
}
$stateRoot = Join-Path $projectRoot 'runtime\state\codex-worker'
$logRoot = Join-Path $projectRoot 'runtime\logs\codex-worker'
$pidFile = Join-Path $stateRoot 'worker.pid'
$codexWorkerScript = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'codex-worker.ps1'))
$ownedPidRecordRaw = $null
$workerId = $env:COMPUTERNAME + '-' + [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$registeredWorkspaceRoot = if ($env:WORKSPACE_HOST_ROOT) {
    [IO.Path]::GetFullPath($env:WORKSPACE_HOST_ROOT)
} else {
    Join-Path $projectRoot 'workspaces'
}
$obsidianVaultRoot = if ($env:OBSIDIAN_VAULT_HOST_ROOT) {
    [IO.Path]::GetFullPath($env:OBSIDIAN_VAULT_HOST_ROOT)
} else {
    Join-Path $projectRoot 'vault'
}
$heartbeatSeconds = 20
$heartbeatRequestTimeoutSeconds = 5
$completeRetryCount = 3

New-Item -ItemType Directory -Path $stateRoot, $logRoot, $WorkspaceRoot -Force | Out-Null

function Get-WorkerProcessState {
    $state = [ordered]@{
        IsValid = $false
        Pid = 0
        Process = $null
        Reason = 'pid_file_missing'
        RawRecord = $null
    }
    if (-not (Test-Path -LiteralPath $pidFile -PathType Leaf)) {
        return [pscustomobject]$state
    }

    $rawRecord = Get-Content -LiteralPath $pidFile -Raw
    $state.RawRecord = $rawRecord
    $recordedCreationUtc = ''
    $recordedScriptPath = ''
    $legacyRecord = $true
    $workerPid = 0
    if (-not [int]::TryParse($rawRecord.Trim(), [ref]$workerPid)) {
        $legacyRecord = $false
        try {
            $record = $rawRecord | ConvertFrom-Json -ErrorAction Stop
            [void][int]::TryParse([string]$record.pid, [ref]$workerPid)
            $recordedCreationUtc = [string]$record.creation_utc
            $recordedScriptPath = [string]$record.script_path
        }
        catch {
            $state.Reason = 'pid_record_invalid'
            return [pscustomobject]$state
        }
    }
    if (-not $legacyRecord -and (
        [string]::IsNullOrWhiteSpace($recordedCreationUtc) -or
        [string]::IsNullOrWhiteSpace($recordedScriptPath)
    )) {
        $state.Reason = 'pid_record_identity_incomplete'
        return [pscustomobject]$state
    }
    $state.Pid = $workerPid
    if ($workerPid -le 0) {
        $state.Reason = 'pid_record_invalid'
        return [pscustomobject]$state
    }

    try {
        $process = Get-CimInstance -ClassName Win32_Process `
            -Filter "ProcessId = $workerPid" -ErrorAction Stop |
            Select-Object -First 1
    }
    catch {
        $state.Reason = 'process_lookup_failed'
        return [pscustomobject]$state
    }
    if ($null -eq $process) {
        $state.Reason = 'process_not_found'
        return [pscustomobject]$state
    }

    $executableName = [IO.Path]::GetFileName([string]$process.ExecutablePath)
    if ($executableName -notin @('powershell.exe', 'pwsh.exe')) {
        $state.Reason = 'executable_is_not_powershell'
        return [pscustomobject]$state
    }
    $expectedScript = $codexWorkerScript.Replace('/', '\')
    $commandLine = ([string]$process.CommandLine).Replace('/', '\')
    $scriptPattern = '(?i)(?:^|\s)-File(?:\s+|:)(?:"' +
        [regex]::Escape($expectedScript) + '"|' +
        [regex]::Escape($expectedScript) + ')(?=\s|$)'
    if ([string]::IsNullOrWhiteSpace($commandLine) -or $commandLine -notmatch $scriptPattern) {
        $state.Reason = 'worker_script_command_line_mismatch'
        return [pscustomobject]$state
    }
    if (-not [string]::IsNullOrWhiteSpace($recordedScriptPath)) {
        try {
            $recordedScript = [IO.Path]::GetFullPath($recordedScriptPath).Replace('/', '\')
        }
        catch {
            $state.Reason = 'recorded_script_path_invalid'
            return [pscustomobject]$state
        }
        if (-not $recordedScript.Equals($expectedScript, [StringComparison]::OrdinalIgnoreCase)) {
            $state.Reason = 'recorded_script_path_mismatch'
            return [pscustomobject]$state
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($recordedCreationUtc)) {
        $recordedCreation = [DateTimeOffset]::MinValue
        if (-not [DateTimeOffset]::TryParse($recordedCreationUtc, [ref]$recordedCreation)) {
            $state.Reason = 'recorded_creation_time_invalid'
            return [pscustomobject]$state
        }
        $actualCreation = [DateTimeOffset]([datetime]$process.CreationDate)
        if ([Math]::Abs(($actualCreation.ToUniversalTime() - $recordedCreation.ToUniversalTime()).TotalSeconds) -gt 1) {
            $state.Reason = 'process_creation_time_mismatch'
            return [pscustomobject]$state
        }
    }

    $state.IsValid = $true
    $state.Process = $process
    $state.Reason = 'ok'
    return [pscustomobject]$state
}

function Remove-StaleWorkerPidRecord {
    param([AllowNull()][string]$ExpectedRawRecord)
    if ($null -eq $ExpectedRawRecord -or
        -not (Test-Path -LiteralPath $pidFile -PathType Leaf)) {
        return $false
    }
    $currentRaw = Get-Content -LiteralPath $pidFile -Raw
    if ($currentRaw -cne $ExpectedRawRecord) { return $false }
    Remove-Item -LiteralPath $pidFile -Force
    return $true
}

function Write-WorkerPidRecord {
    $selfProcess = Get-CimInstance -ClassName Win32_Process `
        -Filter "ProcessId = $PID" -ErrorAction Stop |
        Select-Object -First 1
    if ($null -eq $selfProcess) {
        throw 'Cannot inspect the Codex worker process identity.'
    }
    $executableName = [IO.Path]::GetFileName([string]$selfProcess.ExecutablePath)
    $expectedScript = $codexWorkerScript.Replace('/', '\')
    $commandLine = ([string]$selfProcess.CommandLine).Replace('/', '\')
    $scriptPattern = '(?i)(?:^|\s)-File(?:\s+|:)(?:"' +
        [regex]::Escape($expectedScript) + '"|' +
        [regex]::Escape($expectedScript) + ')(?=\s|$)'
    if ($executableName -notin @('powershell.exe', 'pwsh.exe') -or
        [string]::IsNullOrWhiteSpace($commandLine) -or
        $commandLine -notmatch $scriptPattern) {
        throw 'Codex worker must run in PowerShell with this script supplied through -File.'
    }
    $creationUtc = ([DateTimeOffset]([datetime]$selfProcess.CreationDate)).ToUniversalTime().ToString('o')
    $rawRecord = [ordered]@{
        schema_version = 1
        pid = $PID
        creation_utc = $creationUtc
        script_path = $codexWorkerScript
    } | ConvertTo-Json -Compress
    $bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes($rawRecord)
    $stream = New-Object System.IO.FileStream(
        $pidFile,
        [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None
    )
    try {
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    }
    finally {
        $stream.Dispose()
    }
    return $rawRecord
}

function Resolve-CodexPath {
    if (-not [string]::IsNullOrWhiteSpace($CodexPath)) {
        if (-not (Test-Path -LiteralPath $CodexPath)) {
            throw "Configured Codex CLI was not found: $CodexPath"
        }
        return (Resolve-Path -LiteralPath $CodexPath).Path
    }
    $runtimeRoot = Join-Path $env:LOCALAPPDATA 'OpenAI\Codex\bin'
    $candidate = Get-ChildItem -LiteralPath $runtimeRoot -Directory -ErrorAction SilentlyContinue |
        Where-Object {
            (Test-Path -LiteralPath (Join-Path $_.FullName 'codex.exe')) -and
            (Test-Path -LiteralPath (Join-Path $_.FullName 'codex-windows-sandbox-setup.exe'))
        } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($null -eq $candidate) {
        throw 'A complete Codex runtime with its Windows sandbox helper was not found. Open or update the Codex app once.'
    }
    return (Join-Path $candidate.FullName 'codex.exe')
}

$CodexPath = Resolve-CodexPath

if (Test-Path -LiteralPath $pidFile) {
    $existingWorker = Get-WorkerProcessState
    if ($existingWorker.IsValid) {
        Write-Output "Codex worker is already running with verified PID $($existingWorker.Pid)."
        exit 0
    }
    if ($existingWorker.Reason -eq 'process_lookup_failed') {
        throw 'Cannot verify the existing Codex worker process; PID record was preserved.'
    }
    if (-not (Remove-StaleWorkerPidRecord -ExpectedRawRecord $existingWorker.RawRecord)) {
        $existingWorker = Get-WorkerProcessState
        if ($existingWorker.IsValid) {
            Write-Output "Codex worker is already running with verified PID $($existingWorker.Pid)."
            exit 0
        }
        throw "Codex worker PID record changed during validation: $($existingWorker.Reason)"
    }
    Write-Output (
        "Removed stale Codex worker PID record only; no process was stopped " +
        "(PID=$($existingWorker.Pid), reason=$($existingWorker.Reason))."
    )
}
try {
    $ownedPidRecordRaw = Write-WorkerPidRecord
}
catch [System.IO.IOException] {
    $existingWorker = Get-WorkerProcessState
    if ($existingWorker.IsValid) {
        Write-Output "Codex worker is already running with verified PID $($existingWorker.Pid)."
        exit 0
    }
    throw
}

function Write-WorkerLog {
    param([string]$Message)
    $line = (Get-Date -Format 'yyyy-MM-ddTHH:mm:ssK') + ' ' + $Message
    Add-Content -LiteralPath (Join-Path $logRoot 'worker.log') -Value $line -Encoding UTF8
    Write-Output $line
}

function ConvertTo-WindowsProcessArgument {
    param([AllowEmptyString()][string]$Value)
    if ($null -eq $Value -or $Value.Length -eq 0) { return '""' }
    if ($Value -notmatch '[\s"]') { return $Value }

    # ProcessStartInfo on Windows PowerShell 5.1 has no ArgumentList API.
    # Apply the CommandLineToArgvW quoting rules so configured paths containing
    # spaces or quotes remain one literal native argument.
    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append('"')
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') {
            $backslashes++
            continue
        }
        if ($character -eq '"') {
            if ($backslashes -gt 0) {
                [void]$builder.Append(('\' * (2 * $backslashes)))
            }
            [void]$builder.Append('\"')
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            [void]$builder.Append(('\' * $backslashes))
            $backslashes = 0
        }
        [void]$builder.Append($character)
    }
    if ($backslashes -gt 0) {
        [void]$builder.Append(('\' * (2 * $backslashes)))
    }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Stop-ExactProcessTree {
    param([System.Diagnostics.Process]$Process)
    if ($Process.HasExited) { return }

    # Windows PowerShell 5.1 does not expose Process.Kill(entireProcessTree).
    # taskkill /T targets only the tree rooted at this exact PID.
    $taskkillPath = Join-Path $env:SystemRoot 'System32\taskkill.exe'
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $taskkillPath
    $startInfo.Arguments = "/PID $($Process.Id) /T /F"
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $terminator = New-Object System.Diagnostics.Process
    $terminator.StartInfo = $startInfo
    $terminationExitCode = -1
    try {
        [void]$terminator.Start()
        $terminationOutput = $terminator.StandardOutput.ReadToEndAsync()
        $terminationError = $terminator.StandardError.ReadToEndAsync()
        if (-not $terminator.WaitForExit(15000)) {
            $terminator.Kill()
            [void]$terminator.WaitForExit(5000)
        }
        [void]$terminationOutput.GetAwaiter().GetResult()
        [void]$terminationError.GetAwaiter().GetResult()
        if ($terminator.HasExited) { $terminationExitCode = $terminator.ExitCode }
    }
    finally {
        $terminator.Dispose()
    }

    # A successful taskkill can take a moment to signal the original handle.
    # If taskkill itself was denied (for example, inside a restricted test
    # sandbox), fall back immediately instead of burning the full grace period.
    $graceMilliseconds = if ($terminationExitCode -eq 0) { 3000 } else { 100 }
    if (-not $Process.WaitForExit($graceMilliseconds)) {
        # This fallback is only for a taskkill race involving the root process;
        # taskkill has already been asked to terminate all known descendants.
        $Process.Kill()
        if (-not $Process.WaitForExit(5000)) {
            throw "Timed-out Codex process tree did not terminate (PID $($Process.Id))."
        }
    }
}

function Get-LeaseLossEvent {
    param([object]$Job)
    if ($null -eq $Job) { return $null }

    $events = @(Receive-Job -Job $Job -Keep -ErrorAction SilentlyContinue)
    $leaseLoss = $events |
        Where-Object { [bool]$_.lease_lost } |
        Select-Object -First 1
    if ($null -ne $leaseLoss) { return $leaseLoss }

    # A heartbeat job is expected to remain Running until the owner stops it.
    # If its runspace dies without producing the explicit event, fail closed:
    # continuing to write after heartbeats stop can overlap a takeover worker.
    if ($Job.State -in @('Completed', 'Failed', 'Disconnected')) {
        return [pscustomobject]@{
            lease_lost = $true
            status_code = 0
            error = "Lease heartbeat job ended unexpectedly (state=$($Job.State))."
        }
    }
    return $null
}

function Invoke-ProcessWithDeadline {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList,
        [string]$StandardInputPath,
        [object]$CancellationJob = $null,
        [ValidateRange(1, 2147483647)]
        [int]$TimeoutMilliseconds
    )
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $FilePath
    $startInfo.Arguments = (($ArgumentList | ForEach-Object {
        ConvertTo-WindowsProcessArgument -Value ([string]$_)
    }) -join ' ')
    $startInfo.WorkingDirectory = Split-Path -Parent $FilePath
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    $promptStream = $null
    $started = $false
    try {
        [void]$process.Start()
        $started = $true
        $standardOutput = $process.StandardOutput.ReadToEndAsync()
        $standardError = $process.StandardError.ReadToEndAsync()
        $promptStream = [System.IO.File]::OpenRead($StandardInputPath)
        $stdinCopy = $promptStream.CopyToAsync($process.StandardInput.BaseStream)
        $stdinClosed = $false
        $timer = [System.Diagnostics.Stopwatch]::StartNew()
        $timedOut = $false
        $leaseLoss = $null
        $terminationError = $null

        while ($true) {
            if (-not $stdinClosed -and $stdinCopy.IsCompleted) {
                [void]$stdinCopy.GetAwaiter().GetResult()
                $process.StandardInput.Close()
                $promptStream.Dispose()
                $promptStream = $null
                $stdinClosed = $true
            }
            $leaseLoss = Get-LeaseLossEvent -Job $CancellationJob
            if ($null -ne $leaseLoss) {
                try {
                    Stop-ExactProcessTree -Process $process
                }
                catch {
                    $terminationError = $_.Exception.Message
                }
                break
            }
            $remaining = $TimeoutMilliseconds - [int]$timer.ElapsedMilliseconds
            if ($remaining -le 0) {
                $timedOut = $true
                Stop-ExactProcessTree -Process $process
                break
            }
            if ($process.WaitForExit([Math]::Min(250, $remaining))) { break }
        }
        # Close the narrow race where the child exits in the same polling slice
        # in which the heartbeat reports a rejected/stale attempt.
        if ($null -eq $leaseLoss) {
            $leaseLoss = Get-LeaseLossEvent -Job $CancellationJob
        }
        $timer.Stop()
        if (-not $process.HasExited) { [void]$process.WaitForExit(30000) }

        $stdoutText = [string]$standardOutput.GetAwaiter().GetResult()
        $stderrText = [string]$standardError.GetAwaiter().GetResult()
        return [pscustomobject]@{
            ProcessId = $process.Id
            ExitCode = if ($null -ne $leaseLoss) { 125 } elseif ($timedOut) { 124 } else { $process.ExitCode }
            TimedOut = $timedOut
            LeaseLost = $null -ne $leaseLoss
            LeaseLossStatusCode = if ($null -ne $leaseLoss) { [int]$leaseLoss.status_code } else { 0 }
            LeaseLossError = if ($null -ne $leaseLoss) { [string]$leaseLoss.error } else { $null }
            TerminationError = $terminationError
            StandardOutput = $stdoutText
            StandardError = $stderrText
        }
    }
    finally {
        if ($null -ne $promptStream) { $promptStream.Dispose() }
        if ($started -and -not $process.HasExited) {
            try {
                Stop-ExactProcessTree -Process $process
            }
            catch {
                # A lease-lost attempt must never fall through to /complete,
                # even if Windows refuses the final process cleanup attempt.
                if ($null -eq $leaseLoss) { throw }
            }
        }
        $process.Dispose()
    }
}

function Invoke-Api {
    param([string]$Method, [string]$Path, [object]$Body = $null)
    $arguments = @{
        Method = $Method
        Uri = $ApiBase + $Path
        TimeoutSec = 120
    }
    if ($null -ne $Body) {
        $arguments.ContentType = 'application/json'
        $arguments.Body = $Body | ConvertTo-Json -Depth 12 -Compress
    }
    return Invoke-RestMethod @arguments
}

function Get-AttemptRoot {
    param([string]$PlanId, [string]$AttemptId)
    if ($PlanId -notmatch '^[A-Za-z0-9-]{1,120}$' -or $AttemptId -notmatch '^[A-Za-z0-9-]{1,120}$') {
        throw 'Invalid Codex plan or attempt ID.'
    }
    return Join-Path (Join-Path $stateRoot $PlanId) $AttemptId
}

function Get-SavedAttemptResult {
    param([string]$PlanId, [string]$AttemptId)
    $resultPath = Join-Path (Get-AttemptRoot -PlanId $PlanId -AttemptId $AttemptId) 'result.json'
    if (-not (Test-Path -LiteralPath $resultPath -PathType Leaf)) { return $null }
    return Get-Content -LiteralPath $resultPath -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Save-AttemptResult {
    param([string]$PlanId, [string]$AttemptId, [object]$Result)
    $attemptRoot = Get-AttemptRoot -PlanId $PlanId -AttemptId $AttemptId
    New-Item -ItemType Directory -Path $attemptRoot -Force | Out-Null
    $resultPath = Join-Path $attemptRoot 'result.json'
    if (Test-Path -LiteralPath $resultPath -PathType Leaf) { return $resultPath }
    $temporaryPath = Join-Path $attemptRoot ('.result-' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        $document = $Result | ConvertTo-Json -Depth 12
        [System.IO.File]::WriteAllText($temporaryPath, $document, (New-Object System.Text.UTF8Encoding($false)))
        # Rename within one NTFS directory so a restart sees either no result or
        # the complete payload, never a partially written JSON document.
        [System.IO.File]::Move($temporaryPath, $resultPath)
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath) {
            Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
        }
    }
    return $resultPath
}

function Save-LeaseLossMarker {
    param([string]$PlanId, [string]$AttemptId, [object]$Result)
    $attemptRoot = Get-AttemptRoot -PlanId $PlanId -AttemptId $AttemptId
    New-Item -ItemType Directory -Path $attemptRoot -Force | Out-Null
    $markerPath = Join-Path $attemptRoot 'lease-lost.json'
    if (Test-Path -LiteralPath $markerPath -PathType Leaf) { return $markerPath }
    $temporaryPath = Join-Path $attemptRoot ('.lease-lost-' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        $document = $Result | ConvertTo-Json -Depth 12
        [System.IO.File]::WriteAllText($temporaryPath, $document, (New-Object System.Text.UTF8Encoding($false)))
        [System.IO.File]::Move($temporaryPath, $markerPath)
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath) {
            Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
        }
    }
    return $markerPath
}

function Start-LeaseHeartbeat {
    param([string]$PlanId, [string]$AttemptId)
    $parentProcessId = $PID
    return Start-Job -ScriptBlock {
        param($BaseUrl, $CurrentPlanId, $CurrentWorkerId, $CurrentAttemptId, $IntervalSeconds, $RequestTimeoutSeconds, $OwnerPid)
        $ErrorActionPreference = 'Stop'
        $consecutiveFailures = 0
        while ($true) {
            Start-Sleep -Seconds $IntervalSeconds
            if (-not (Get-Process -Id $OwnerPid -ErrorAction SilentlyContinue)) { break }
            try {
                $body = @{
                    worker_id = $CurrentWorkerId
                    attempt_id = $CurrentAttemptId
                } | ConvertTo-Json -Compress
                [void](Invoke-RestMethod -Method Post `
                    -Uri ($BaseUrl + "/orchestrator/plans/$CurrentPlanId/codex/heartbeat") `
                    -ContentType 'application/json' -Body $body `
                    -TimeoutSec $RequestTimeoutSeconds)
                $consecutiveFailures = 0
            }
            catch {
                $consecutiveFailures++
                $statusCode = 0
                try { $statusCode = [int]$_.Exception.Response.StatusCode } catch { $statusCode = 0 }
                if ($statusCode -in @(404, 409) -or $consecutiveFailures -ge 3) {
                    [pscustomobject]@{
                        lease_lost = $true
                        status_code = $statusCode
                        error = $_.Exception.Message
                    }
                    break
                }
            }
        }
    } -ArgumentList $ApiBase, $PlanId, $workerId, $AttemptId, `
        $heartbeatSeconds, $heartbeatRequestTimeoutSeconds, $parentProcessId
}

function Stop-LeaseHeartbeat {
    param([object]$Job, [string]$PlanId, [string]$AttemptId)
    if ($null -eq $Job) { return }
    if ($Job.State -eq 'Running') {
        Stop-Job -Job $Job -ErrorAction SilentlyContinue | Out-Null
    }
    $events = @(Receive-Job -Job $Job -ErrorAction SilentlyContinue)
    Remove-Job -Job $Job -Force -ErrorAction SilentlyContinue | Out-Null
    foreach ($event in $events) {
        if ($event.lease_lost) {
            Write-WorkerLog "Lease heartbeat stopped plan=$PlanId attempt=$AttemptId status=$($event.status_code): $($event.error)"
        }
    }
}

function Complete-Attempt {
    param([string]$PlanId, [string]$AttemptId, [object]$Result)
    for ($attempt = 1; $attempt -le $completeRetryCount; $attempt++) {
        try {
            [void](Invoke-Api -Method Post -Path ("/orchestrator/plans/{0}/codex/complete" -f $PlanId) -Body $Result)
            return $true
        }
        catch {
            Write-WorkerLog "Completion acknowledgement failed plan=$PlanId attempt=$AttemptId try=$attempt/$completeRetryCount`: $($_.Exception.Message)" | Out-Null
            if ($attempt -lt $completeRetryCount) { Start-Sleep -Seconds (3 * $attempt) }
        }
    }
    return $false
}

function Get-ExecutionWorkspace {
    param([object]$Plan)
    $workspaceId = [string]$Plan.local_workspace_id
    if ([string]::IsNullOrWhiteSpace($workspaceId)) {
        $workspaceId = [string]$Plan.project_id
    }
    if ($workspaceId -notmatch '^[a-z][a-z0-9-]{0,62}$') {
        throw 'Codex execution requires a valid project or workspace ID.'
    }
    $root = [System.IO.Path]::GetFullPath($WorkspaceRoot).TrimEnd('\')
    $candidate = [System.IO.Path]::GetFullPath((Join-Path $root $workspaceId))
    if ([System.IO.Directory]::GetParent($candidate).FullName -ne $root) {
        throw 'Execution workspace escaped the configured root.'
    }
    New-Item -ItemType Directory -Path $candidate -Force | Out-Null
    $agentsPath = Join-Path $candidate 'AGENTS.md'
    if (-not (Test-Path -LiteralPath $agentsPath)) {
        $guidance = @"
# Local-First Agent Codex Execution Workspace

- Work only inside this workspace.
- Implement the approved goal completely and run relevant validation.
- Do not publish, deploy, purchase, message third parties, or change external accounts.
- Preserve user files and report changed files, tests, and remaining risks.
"@
        [System.IO.File]::WriteAllText($agentsPath, $guidance, (New-Object System.Text.UTF8Encoding($false)))
    }
    if (-not (Test-Path -LiteralPath (Join-Path $candidate '.git'))) {
        & git -C $candidate init -q
        if ($LASTEXITCODE -ne 0) { throw 'Failed to initialize the execution workspace repository.' }
    }
    return $candidate
}

function Get-RegisteredWorkspacePath {
    param([object]$Plan)
    $workspaceId = [string]$Plan.local_workspace_id
    if ([string]::IsNullOrWhiteSpace($workspaceId)) {
        $workspaceId = [string]$Plan.project_id
    }
    if ($workspaceId -notmatch '^[a-z][a-z0-9-]{0,62}$') { return $null }
    $candidate = Join-Path $registeredWorkspaceRoot $workspaceId
    if (Test-Path -LiteralPath $candidate -PathType Container) {
        return [System.IO.Path]::GetFullPath($candidate)
    }
    return $null
}

function Convert-KnowledgePath {
    param([object]$Value)
    $path = [string]$Value
    if ([string]::IsNullOrWhiteSpace($path)) { return $null }
    if ($path.StartsWith('/vault/')) {
        $relative = $path.Substring('/vault/'.Length).Replace('/', '\')
        return Join-Path $obsidianVaultRoot $relative
    }
    return $path
}

function Invoke-CodexExecution {
    param([object]$Plan, [string]$AttemptId, [object]$HeartbeatJob)
    if (-not (Test-Path -LiteralPath $CodexPath)) {
        throw "Codex CLI was not found: $CodexPath"
    }
    $workspace = Get-ExecutionWorkspace -Plan $Plan
    $runRoot = Get-AttemptRoot -PlanId ([string]$Plan.id) -AttemptId $AttemptId
    New-Item -ItemType Directory -Path $runRoot -Force | Out-Null
    $lastMessage = Join-Path $runRoot 'last-message.txt'
    $transcript = Join-Path $runRoot 'codex.log'
    if (Test-Path -LiteralPath $lastMessage) { Remove-Item -LiteralPath $lastMessage -Force }

    # The Agent handoff exposes the user's explicit project-level decision as
    # `context_shared`.  Keep this gate in the worker as well so a missing or
    # false field can never expose local knowledge/workspace paths by accident.
    $contextConsent = [bool]$Plan.result.context_shared
    $referenceWorkspace = if ($contextConsent) {
        Get-RegisteredWorkspacePath -Plan $Plan
    } else {
        $null
    }
    $context = [ordered]@{
        goal = $Plan.prompt
        project_id = $Plan.project_id
        workspace_id = $Plan.local_workspace_id
        registered_workspace_path = $referenceWorkspace
        execution_workspace_path = $workspace
        local_context_consent = $contextConsent
        handoff_note_path = if ($contextConsent) { (Convert-KnowledgePath -Value $Plan.result.handoff_note) } else { $null }
        local_plan = if ($contextConsent) { $Plan.result.local_plan } else { $null }
        local_model_analysis = if ($contextConsent) { $Plan.result.response } else { $null }
        research_note = if ($contextConsent) { (Convert-KnowledgePath -Value $Plan.result.research_note) } else { $null }
        verified_web_sources = if ($contextConsent) { $Plan.result.web_sources } else { $null }
        knowledge_evidence = if ($contextConsent) { $Plan.result.knowledge_evidence } else { $null }
        prior_stage_results = if ($contextConsent) { $Plan.result.prior_stage_results } else { $null }
    } | ConvertTo-Json -Depth 10
    $prompt = @"
Execute the approved Local-First Agent project task below. Inspect the registered reference workspace and handoff note when their paths are present, but treat them as read-only. Implement all changes in the execution workspace and run relevant validation. Do not merely propose steps. In the final response, summarize the implementation, list validation performed, and state remaining risks.

Approved task context:
$context
"@
    $arguments = @(
        'exec', '--ephemeral', '-s', 'workspace-write',
        '-c', 'approval_policy="never"', '--skip-git-repo-check',
        '-C', $workspace, '-o', $lastMessage, '-'
    )
    # Keep the prompt out of the command line. The per-attempt, random file is
    # copied to stdin and removed immediately after the child finishes or is
    # terminated, preserving Codex's `-` prompt and `-o` last-message behavior.
    $promptPath = Join-Path $runRoot ('.prompt-' + [guid]::NewGuid().ToString('N') + '.tmp')
    [System.IO.File]::WriteAllText($promptPath, $prompt, (New-Object System.Text.UTF8Encoding($false)))
    try {
        $processResult = Invoke-ProcessWithDeadline `
            -FilePath $CodexPath `
            -ArgumentList $arguments `
            -StandardInputPath $promptPath `
            -CancellationJob $HeartbeatJob `
            -TimeoutMilliseconds ([int]($MaxExecutionMinutes * 60 * 1000))
    }
    finally {
        if (Test-Path -LiteralPath $promptPath) {
            Remove-Item -LiteralPath $promptPath -Force -ErrorAction SilentlyContinue
        }
    }
    $exitCode = [int]$processResult.ExitCode
    $outputParts = @()
    if (-not [string]::IsNullOrWhiteSpace([string]$processResult.StandardOutput)) {
        $outputParts += ([string]$processResult.StandardOutput).TrimEnd([char[]]"`r`n")
    }
    if (-not [string]::IsNullOrWhiteSpace([string]$processResult.StandardError)) {
        $outputParts += ([string]$processResult.StandardError).TrimEnd([char[]]"`r`n")
    }
    $allOutput = $outputParts -join [Environment]::NewLine
    [System.IO.File]::WriteAllText($transcript, $allOutput, (New-Object System.Text.UTF8Encoding($false)))
    $summary = if (Test-Path -LiteralPath $lastMessage) {
        Get-Content -LiteralPath $lastMessage -Raw -Encoding UTF8
    } else {
        $allOutput
    }
    $allChangedFiles = @(
        & git -C $workspace status --porcelain 2>$null |
            ForEach-Object { if ($_.Length -gt 3) { $_.Substring(3).Trim('"') } } |
            Where-Object { $_ -and $_ -ne 'AGENTS.md' } |
            Select-Object -Unique
    )
    $changedFiles = @($allChangedFiles | Select-Object -First 500)
    $validation = @("Codex CLI exit code: $exitCode")
    if ($processResult.TimedOut) {
        $validation += "Codex CLI exceeded MaxExecutionMinutes=$MaxExecutionMinutes; its exact process tree was terminated."
    }
    if ($processResult.LeaseLost) {
        $validation += "Codex lease was lost; this stale attempt's exact process tree was terminated and will not be sent to /complete."
    }
    if (-not [string]::IsNullOrWhiteSpace([string]$processResult.TerminationError)) {
        $validation += "Process-tree termination warning: $($processResult.TerminationError)"
    }
    if ($allChangedFiles.Count -gt $changedFiles.Count) {
        $validation += "Changed files truncated to 500 of $($allChangedFiles.Count) for the API completion contract."
    }
    return [ordered]@{
        worker_id = $workerId
        attempt_id = $AttemptId
        lease_lost = [bool]$processResult.LeaseLost
        stale = [bool]$processResult.LeaseLost
        lease_loss_status_code = [int]$processResult.LeaseLossStatusCode
        success = ($exitCode -eq 0 -and -not $processResult.LeaseLost)
        summary = if ($processResult.LeaseLost) {
            "Stale Codex attempt stopped after lease loss; no completion was submitted."
        } else {
            ([string]$summary).Substring(0, [Math]::Min(20000, ([string]$summary).Length))
        }
        output = ($allOutput.Substring(0, [Math]::Min(60000, $allOutput.Length)))
        workspace_path = $workspace
        changed_files = $changedFiles
        validation = $validation
        error = if ($processResult.LeaseLost) {
            "Codex attempt lost its lease and was cancelled: $($processResult.LeaseLossError)"
        } elseif ($processResult.TimedOut) {
            "Codex CLI exceeded the $MaxExecutionMinutes minute execution limit and its process tree was terminated. See $transcript"
        } elseif ($exitCode -eq 0) {
            $null
        } else {
            "Codex CLI exited with code $exitCode. See $transcript"
        }
    }
}

try {
    Write-WorkerLog "Codex worker started PID=$PID root=$WorkspaceRoot max_execution_minutes=$MaxExecutionMinutes"
    do {
        try {
            $handoffs = Invoke-Api -Method Get -Path '/orchestrator/handoffs'
            foreach ($plan in @($handoffs.items)) {
                $heartbeatJob = $null
                $attemptId = $null
                try {
                    $savedResult = $null
                    if (
                        $plan.status -eq 'codex_running' -and
                        [string]$plan.result.worker_id -eq $workerId -and
                        -not [string]::IsNullOrWhiteSpace([string]$plan.result.attempt_id)
                    ) {
                        $existingAttemptId = [string]$plan.result.attempt_id
                        try {
                            $savedResult = Get-SavedAttemptResult -PlanId ([string]$plan.id) -AttemptId $existingAttemptId
                        }
                        catch {
                            Write-WorkerLog "Saved result is unreadable plan=$($plan.id) attempt=$existingAttemptId`: $($_.Exception.Message)"
                            $savedResult = $null
                        }
                        if ($null -ne $savedResult -and [string]$savedResult.attempt_id -eq $existingAttemptId) {
                            try {
                                $plan = Invoke-Api -Method Post `
                                    -Path ("/orchestrator/plans/{0}/codex/heartbeat" -f $plan.id) `
                                    -Body @{ worker_id = $workerId; attempt_id = $existingAttemptId }
                                $attemptId = $existingAttemptId
                                $heartbeatJob = Start-LeaseHeartbeat -PlanId ([string]$plan.id) -AttemptId $attemptId
                                Write-WorkerLog "Retrying durable completion plan=$($plan.id) attempt=$attemptId"
                                $acknowledged = Complete-Attempt -PlanId ([string]$plan.id) -AttemptId $attemptId -Result $savedResult
                                if ($acknowledged) {
                                    Write-WorkerLog "Completion acknowledged plan=$($plan.id) attempt=$attemptId success=$($savedResult.success)"
                                } else {
                                    Write-WorkerLog "Completion payload remains durable for retry plan=$($plan.id) attempt=$attemptId"
                                }
                                if ($Once) { break }
                                continue
                            }
                            catch {
                                Write-WorkerLog "Existing attempt cannot resume plan=$($plan.id) attempt=$existingAttemptId`: $($_.Exception.Message)"
                                $savedResult = $null
                            }
                        }
                    }

                    # A running handoff can only be claimed here after its lease
                    # expires. An unexpired owner (including the same stable
                    # worker identity after a restart) receives 409 and is left
                    # untouched, preventing duplicate execution.
                    $plan = Invoke-Api -Method Post `
                        -Path ("/orchestrator/plans/{0}/codex/start" -f $plan.id) `
                        -Body @{ worker_id = $workerId }
                    if ($plan.status -ne 'codex_running') { continue }
                    if ([string]$plan.result.worker_id -ne $workerId) { continue }
                    $attemptId = [string]$plan.result.attempt_id
                    if ([string]::IsNullOrWhiteSpace($attemptId)) {
                        throw 'The claimed Codex handoff did not include an attempt ID.'
                    }
                    $heartbeatJob = Start-LeaseHeartbeat -PlanId ([string]$plan.id) -AttemptId $attemptId
                    Write-WorkerLog "Executing plan=$($plan.id) attempt=$attemptId project=$($plan.project_id)"
                    try {
                        $result = Invoke-CodexExecution -Plan $plan -AttemptId $attemptId `
                            -HeartbeatJob $heartbeatJob
                    }
                    catch {
                        $executionError = [string]$_.Exception.Message
                        $executionError = $executionError.Substring(0, [Math]::Min(20000, $executionError.Length))
                        $leaseLoss = Get-LeaseLossEvent -Job $heartbeatJob
                        if ($null -ne $leaseLoss) {
                            $result = [ordered]@{
                                worker_id = $workerId
                                attempt_id = $attemptId
                                lease_lost = $true
                                stale = $true
                                lease_loss_status_code = [int]$leaseLoss.status_code
                                success = $false
                                summary = 'Stale Codex attempt stopped after lease loss; no completion was submitted.'
                                output = ''
                                workspace_path = ''
                                changed_files = @()
                                validation = @('Execution aborted after the heartbeat rejected or abandoned this attempt.')
                                error = "Lease lost: $($leaseLoss.error); execution error: $executionError"
                            }
                        }
                        else {
                            $result = [ordered]@{
                                worker_id = $workerId
                                attempt_id = $attemptId
                                success = $false
                                summary = 'Codex worker failed while executing the claimed attempt.'
                                output = ''
                                workspace_path = ''
                                changed_files = @()
                                validation = @('Execution raised an exception before Codex returned a result.')
                                error = $executionError
                            }
                        }
                    }
                    if ([bool]$result.lease_lost) {
                        $markerPath = Save-LeaseLossMarker `
                            -PlanId ([string]$plan.id) -AttemptId $attemptId `
                            -Result $result
                        Write-WorkerLog (
                            "Stale attempt cancelled without /complete plan=$($plan.id) " +
                            "attempt=$attemptId marker=$markerPath"
                        )
                        if ($Once) { break }
                        continue
                    }
                    $resultPath = Save-AttemptResult -PlanId ([string]$plan.id) -AttemptId $attemptId -Result $result
                    $result = Get-SavedAttemptResult -PlanId ([string]$plan.id) -AttemptId $attemptId
                    Write-WorkerLog "Execution result persisted plan=$($plan.id) attempt=$attemptId path=$resultPath"
                    $acknowledged = Complete-Attempt -PlanId ([string]$plan.id) -AttemptId $attemptId -Result $result
                    if ($acknowledged) {
                        Write-WorkerLog "Completion acknowledged plan=$($plan.id) attempt=$attemptId success=$($result.success)"
                    } else {
                        Write-WorkerLog "Completion payload remains durable for retry plan=$($plan.id) attempt=$attemptId"
                    }
                }
                catch {
                    Write-WorkerLog "Plan processing deferred id=$($plan.id): $($_.Exception.Message)"
                }
                finally {
                    Stop-LeaseHeartbeat -Job $heartbeatJob -PlanId ([string]$plan.id) -AttemptId ([string]$attemptId)
                }
                if ($Once) { break }
            }
        }
        catch {
            Write-WorkerLog "Polling failed: $($_.Exception.Message)"
        }
        if (-not $Once) { Start-Sleep -Seconds $PollSeconds }
    } while (-not $Once)
}
finally {
    if ($null -ne $ownedPidRecordRaw -and
        (Test-Path -LiteralPath $pidFile -PathType Leaf)) {
        $currentPidRecordRaw = Get-Content -LiteralPath $pidFile -Raw
        if ($currentPidRecordRaw -ceq $ownedPidRecordRaw) {
            Remove-Item -LiteralPath $pidFile -Force
        }
    }
    Write-WorkerLog "Codex worker stopped PID=$PID"
}
