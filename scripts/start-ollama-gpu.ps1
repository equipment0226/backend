param(
    [ValidateRange(1, 65535)][int]$Port = 11434,
    [string]$Executable,
    [ValidateRange(1, 999)][int]$GpuLayers = 16,
    [ValidatePattern('^[A-Za-z0-9._:/-]+$')][string]$Model = 'llama3:latest',
    [string]$PythonExecutable
)

# Reuse a healthy resident model without issuing another inference request.
# Never stop an existing server; a new server must pass synthetic extraction.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$baseUrl = "http://127.0.0.1:$Port"
$logDirectory = Join-Path $projectRoot 'data\runtime'
$savedEnvironment = @{}
$startupMutex = $null
$ownsMutex = $false
$exitCode = 0

function Get-LocalStatus([string]$ApiPath, [int]$TimeoutMilliseconds = 2000) {
    $request = [System.Net.HttpWebRequest]::Create("$baseUrl$ApiPath")
    $request.Proxy = $null
    $request.AllowAutoRedirect = $false
    $request.Timeout = $TimeoutMilliseconds
    $request.ReadWriteTimeout = $TimeoutMilliseconds
    $response = $null
    $reader = $null
    try {
        $response = $request.GetResponse()
        $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
        return ($reader.ReadToEnd() | ConvertFrom-Json)
    } finally {
        if ($reader) { $reader.Dispose() }
        if ($response) { $response.Dispose() }
    }
}

function Test-LocalPort {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $connection = $client.ConnectAsync('127.0.0.1', $Port)
        return ($connection.Wait(500) -and $client.Connected)
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Get-CanonicalModel([string]$Name) {
    if (-not $Name) { return '' }
    if (($Name -split '/')[-1].Contains(':')) { return $Name }
    return "${Name}:latest"
}

function Get-ResidentModel {
    $status = Get-LocalStatus '/api/ps'
    if (-not $status.PSObject.Properties['models']) {
        throw 'The local server returned an invalid model status.'
    }
    $loaded = @($status.models | Where-Object { $null -ne $_ })
    $requested = Get-CanonicalModel $Model
    $matching = @($loaded | Where-Object {
        (Get-CanonicalModel $_.model) -ceq $requested -or
        (Get-CanonicalModel $_.name) -ceq $requested
    })
    if ($matching.Count -gt 0) {
        $resident = $matching[0]
        if ([long]$resident.size_vram -le 0) {
            throw 'The requested model is loaded on CPU only. Finish active work, then stop or replace that server manually before retrying. No process was stopped.'
        }
        if ([int]$resident.context_length -ne 8192) {
            throw 'The resident GPU model does not report the required 8192-token context. Finish active work and reload the server manually before retrying. No model was reloaded.'
        }
        return $resident
    }
    if ($loaded.Count -gt 0) {
        throw 'A different model is already loaded. To preserve active work, finish it before warming the requested model. No model was replaced.'
    }
    return $null
}

function Resolve-LocalExecutable([string]$Candidate, [string]$DefaultCommand) {
    if (-not $Candidate) {
        $Candidate = (Get-Command $DefaultCommand -CommandType Application -ErrorAction Stop).Source
    }
    if ($Candidate.StartsWith('\\') -or $Candidate.Contains('://')) {
        throw 'The executable must be a local file, not a network path or URL.'
    }
    $absolute = [System.IO.Path]::IsPathRooted($Candidate)
    if (-not $absolute) { $Candidate = Join-Path $projectRoot $Candidate }
    $resolved = [System.IO.Path]::GetFullPath($Candidate)
    if (-not $absolute -and -not $resolved.StartsWith($projectRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'A relative executable path must stay inside the project directory.'
    }
    $drive = New-Object System.IO.DriveInfo([System.IO.Path]::GetPathRoot($resolved))
    if ($drive.DriveType -eq [System.IO.DriveType]::Network -or [System.IO.Path]::GetExtension($resolved) -ine '.exe' -or -not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw 'The executable must be an existing local .exe file.'
    }
    return $resolved
}

try {
    $configPath = Join-Path $projectRoot '.local\ollama-gpu\config.json'
    if (Test-Path -LiteralPath $configPath -PathType Leaf) {
        $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if (-not ($config -is [System.Management.Automation.PSCustomObject])) {
            throw 'GPU configuration must be a JSON object.'
        }
        if ($config.PSObject.Properties['enabled']) {
            if ($config.enabled -isnot [bool]) { throw 'GPU configuration enabled must be true or false.' }
            if (-not $config.enabled) {
                Write-Output 'Local GPU startup is disabled in the project configuration.'
                exit 0
            }
        }
        if (-not $PSBoundParameters.ContainsKey('Executable') -and $config.PSObject.Properties['executable']) {
            if ($config.executable -isnot [string] -or -not $config.executable.Trim()) { throw 'GPU configuration executable must be a local file path.' }
            $Executable = $config.executable
        }
        if (-not $PSBoundParameters.ContainsKey('GpuLayers') -and $config.PSObject.Properties['gpu_layers']) {
            if (($config.gpu_layers -isnot [int] -and $config.gpu_layers -isnot [long]) -or $config.gpu_layers -lt 1 -or $config.gpu_layers -gt 999) {
                throw 'GPU configuration gpu_layers must be an integer between 1 and 999.'
            }
            $GpuLayers = [int]$config.gpu_layers
        }
    }

    $startupMutex = New-Object System.Threading.Mutex($false, "Local\DebtoffOllamaGPU-$Port")
    try { $ownsMutex = $startupMutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $ownsMutex = $true }
    if (-not $ownsMutex) { throw 'Another GPU startup or health check is already running for this port. Retry after it finishes.' }

    $version = $null
    try { $version = Get-LocalStatus '/api/version' } catch {}
    if ($version -and $version.version) {
        $resident = Get-ResidentModel
        if ($resident) {
            Write-Output "Reusing the existing GPU model on port $Port (context 8192, VRAM $($resident.size_vram) bytes). No inference or reload was performed."
            exit 0
        }
        Write-Output "Existing Ollama server on port $Port has no loaded models. Running synthetic GPU validation."
    } else {
        if (Test-LocalPort) { throw 'The selected port is occupied but does not report a healthy Ollama server. Check the existing service manually; it was not stopped.' }
        $resolvedExecutable = Resolve-LocalExecutable $Executable 'ollama.exe'
        $childEnvironment = @{
            OLLAMA_HOST = "127.0.0.1:$Port"
            OLLAMA_IGPU_ENABLE = '1'
            OLLAMA_VULKAN = 'true'
            OLLAMA_NUM_PARALLEL = '1'
            OLLAMA_MAX_LOADED_MODELS = '1'
            OLLAMA_NO_CLOUD = '1'
            LLAMA_ARG_NO_HOST = '1'
            LLAMA_ARG_N_GPU_LAYERS = [string]$GpuLayers
            LLAMA_ARG_CACHE_RAM = '256'
        }
        foreach ($name in $childEnvironment.Keys) {
            $savedEnvironment[$name] = [System.Environment]::GetEnvironmentVariable($name, 'Process')
            [System.Environment]::SetEnvironmentVariable($name, $childEnvironment[$name], 'Process')
        }
        New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
        $logPrefix = Join-Path $logDirectory ("ollama-{0}-{1}" -f $Port, (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
        $serverProcess = Start-Process -FilePath $resolvedExecutable -ArgumentList 'serve' -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput "$logPrefix.out.log" -RedirectStandardError "$logPrefix.err.log"
        $serverProcess.Id | Set-Content -LiteralPath (Join-Path $logDirectory "ollama-$Port.pid") -Encoding ASCII
        Write-Output "Started local Ollama PID $($serverProcess.Id) on port $Port. Logs: $logPrefix"
        $readiness = [System.Diagnostics.Stopwatch]::StartNew()
        while ($readiness.Elapsed.TotalMilliseconds -lt 30000) {
            $serverProcess.Refresh()
            if ($serverProcess.HasExited) { throw 'The new Ollama server exited before becoming ready. Review its runtime log.' }
            $remaining = [int][Math]::Max(1, 30000 - $readiness.Elapsed.TotalMilliseconds)
            try { $version = Get-LocalStatus '/api/version' ([Math]::Min(2000, $remaining)) } catch { $version = $null }
            if ($version -and $version.version) { break }
            $remaining = [int][Math]::Max(0, 30000 - $readiness.Elapsed.TotalMilliseconds)
            if ($remaining -gt 0) { Start-Sleep -Milliseconds ([Math]::Min(250, $remaining)) }
        }
        if (-not $version -or -not $version.version) { throw 'The new Ollama server did not become ready within 30 seconds. Review its runtime log before retrying.' }
    }

    $resolvedPython = Resolve-LocalExecutable $PythonExecutable 'python.exe'
    & $resolvedPython (Join-Path $PSScriptRoot 'check_local_gpu.py') --base-url $baseUrl --model $Model --timeout 240 --output (Join-Path $projectRoot 'reports\local-gpu-check.json')
    if ($LASTEXITCODE -ne 0) { throw 'Synthetic extraction or GPU validation failed. Review reports/local-gpu-check.json. Application startup was not approved.' }
    $resident = Get-ResidentModel
    if (-not $resident) { throw 'The requested GPU model is no longer loaded after validation. Application startup was not approved.' }
    Write-Output "GPU validation passed on port $Port (context 8192, VRAM $($resident.size_vram) bytes)."
} catch {
    Write-Error -Message $_.Exception.Message -ErrorAction Continue
    $exitCode = 1
} finally {
    foreach ($name in $savedEnvironment.Keys) {
        [System.Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], 'Process')
    }
    if ($ownsMutex) { $startupMutex.ReleaseMutex() }
    if ($startupMutex) { $startupMutex.Dispose() }
}
exit $exitCode
