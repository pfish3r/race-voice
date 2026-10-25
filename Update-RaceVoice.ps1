[CmdletBinding()]
param(
    [string]$InstallPath = 'C:\apps\RaceVoice',
    [string]$ArchivePath,
    [switch]$SkipDependencies
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Get-RaceVoiceArchiveFiles {
    param([string]$ZipPath, [string]$Destination)
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
        $files = New-Object 'System.Collections.Generic.List[object]'
        $archiveRoot = $null
        [long]$total = 0
        foreach ($entry in $zip.Entries) {
            $name = $entry.FullName
            if ($name.Contains('\') -or $name.StartsWith('/') -or $name.Contains(':')) {
                throw "Unsafe archive path: $name"
            }
            $parts = @($name.TrimEnd('/').Split('/'))
            foreach ($part in $parts) {
                if ($part -in @('', '.', '..') -or $part -match '[<>:"|?*]' -or
                    $part.EndsWith('.') -or $part.EndsWith(' ') -or
                    $part -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)') {
                    throw "Unsafe archive path: $name"
                }
            }
            if ($null -eq $archiveRoot) { $archiveRoot = $parts[0] }
            if ($parts[0] -ne $archiveRoot) { throw 'Expected one project folder in the ZIP.' }
            if ((($entry.ExternalAttributes -shr 16) -band 0xF000) -eq 0xA000) {
                throw 'Archive links are not supported.'
            }
            if ($parts.Count -eq 1) {
                if (-not $name.EndsWith('/')) { throw 'Expected a project folder in the ZIP.' }
                continue
            }
            $relative = ($parts[1..($parts.Count - 1)] -join '\')
            if (-not $seen.Add($relative)) { throw "Duplicate archive path: $relative" }
            if ($name.EndsWith('/')) { continue }
            $total += $entry.Length
            if ($total -gt 104857600 -or $files.Count -ge 5000) {
                throw 'Archive exceeds the update size limit.'
            }
            # Local configuration, credentials, environments and backups are never replaced.
            if ($parts[1] -in @('.git', '.venv', 'venv', 'update-backups', '__pycache__',
                                'build', 'dist', 'caddy_data', 'caddy_config', 'certificates') -or
                $parts[-1] -like '.env*' -or $parts[-1] -in @('settings.json', 'client.log', 'last-announcement.wav') -or
                $parts[-1] -match '(?i)\.(?:pem|key|pfx|p12)$') {
                continue
            }
            $output = Join-Path $Destination $relative
            [void][IO.Directory]::CreateDirectory((Split-Path -Parent $output))
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $output, $false)
            $files.Add([pscustomobject]@{ Relative = $relative; Source = $output })
        }
        foreach ($required in @('app.py', 'speech.py', 'playback.py', 'modern_widgets.py',
                                 'requirements.txt', 'Start-Race-Voice.cmd', 'Update-RaceVoice.cmd',
                                 'Update-RaceVoice.ps1')) {
            if (-not ($files.Relative -contains $required)) {
                throw "The download is not a complete RaceVoice update (missing $required)."
            }
        }
        return $files.ToArray()
    }
    finally { $zip.Dispose() }
}

function Assert-SafeInstallPath {
    param([string]$Path)
    if ($Path -notmatch '^[A-Za-z]:\\') { throw 'InstallPath must be an absolute local folder.' }
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    if ($full -eq [IO.Path]::GetPathRoot($full).TrimEnd('\') -or $full.StartsWith('\\')) {
        throw 'Choose a local application folder, not a drive root or network share.'
    }
    return $full
}

function Assert-NoReparsePath {
    param([string]$Path)
    $item = $Path
    while ($item) {
        if (Test-Path -LiteralPath $item) {
            if ((Get-Item -Force -LiteralPath $item).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Update path contains a junction or symbolic link: $item"
            }
        }
        $parent = Split-Path -Parent $item
        if ($parent -eq $item) { break }
        $item = $parent
    }
}

function Assert-RaceVoiceClosed {
    param([string]$Path)
    $root = $Path.TrimEnd('\') + '\'
    foreach ($process in @(Get-CimInstance Win32_Process -ErrorAction Stop |
            Where-Object { $_.Name -match '^(?i:pythonw?|RaceVoice)\.exe$' })) {
        $exe = [string]$process.ExecutablePath
        $command = [string]$process.CommandLine
        $inFolder = $exe.StartsWith($root, [StringComparison]::OrdinalIgnoreCase) -or
                    $command.IndexOf($root, [StringComparison]::OrdinalIgnoreCase) -ge 0
        $looksLikeRaceVoice = ($exe + ' ' + $command) -match '(?i)race-?voice'
        if ($process.Name -ieq 'RaceVoice.exe' -or
            (($inFolder -or $looksLikeRaceVoice) -and $command -match '(?i)\bapp\.py\b')) {
            throw 'Close RaceVoice, then run the updater again. It will not stop a running app for you.'
        }
    }
}

function Update-RaceVoice {
    $target = Assert-SafeInstallPath $InstallPath
    Assert-NoReparsePath $target
    Assert-RaceVoiceClosed $target
    $pythonLauncher = $null
    if (-not $SkipDependencies) {
        $pythonLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
        if (-not $pythonLauncher) {
            throw 'Python 3.12 x64 is required. Install it from https://www.python.org/downloads/windows/ with the Python launcher enabled, then run this updater again.'
        }
        & $pythonLauncher.Source -3.12 -c 'import sys; assert sys.maxsize > 2**32'
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 x64 is required. Install it, then rerun the updater.' }
    }
    $temporary = Join-Path ([IO.Path]::GetTempPath()) ('RaceVoice-update-' + [guid]::NewGuid().ToString('N'))
    [void][IO.Directory]::CreateDirectory($temporary)
    $backup = $null
    $changed = New-Object 'System.Collections.Generic.List[object]'
    $copying = $false
    try {
        $zip = Join-Path $temporary 'RaceVoice.zip'
        if ($ArchivePath) {
            Copy-Item -LiteralPath $ArchivePath -Destination $zip
        }
        else {
            Write-Host 'Downloading the latest RaceVoice source from GitHub...'
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/pfish3r/race-voice/archive/refs/heads/main.zip' -OutFile $zip -TimeoutSec 120 -Headers @{ 'User-Agent' = 'RaceVoice-Windows-Updater' }
        }
        Write-Host 'Validating and extracting the update...'
        $files = @(Get-RaceVoiceArchiveFiles $zip (Join-Path $temporary 'source'))
        foreach ($file in $files) {
            Assert-NoReparsePath (Join-Path $target $file.Relative)
        }
        Assert-RaceVoiceClosed $target
        [void][IO.Directory]::CreateDirectory($target)
        $backup = Join-Path $target ('update-backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0,8))
        Assert-NoReparsePath $backup
        [void][IO.Directory]::CreateDirectory($backup)
        $python = Join-Path $target '.venv\Scripts\python.exe'
        Assert-NoReparsePath $python
        $requirements = Join-Path $target 'requirements.txt'
        $needDependencies = -not (Test-Path -LiteralPath $python)
        if (-not (Test-Path -LiteralPath $requirements) -or
            (Get-FileHash -LiteralPath $requirements).Hash -ne
            (Get-FileHash -LiteralPath (Join-Path $temporary 'source\requirements.txt')).Hash) {
            $needDependencies = $true
        }
        foreach ($file in $files) {
            $destination = Join-Path $target $file.Relative
            $existed = Test-Path -LiteralPath $destination
            if ($existed) {
                $saved = Join-Path $backup $file.Relative
                [void][IO.Directory]::CreateDirectory((Split-Path -Parent $saved))
                Copy-Item -LiteralPath $destination -Destination $saved
            }
            $changed.Add([pscustomobject]@{ Relative = $file.Relative; Existed = $existed })
        }
        $changed.ToArray() | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $backup 'manifest.json') -Encoding UTF8
        $copying = $true
        foreach ($file in $files) {
            $destination = Join-Path $target $file.Relative
            [void][IO.Directory]::CreateDirectory((Split-Path -Parent $destination))
            Copy-Item -LiteralPath $file.Source -Destination $destination -Force
        }
        if (-not $SkipDependencies -and $needDependencies) {
            Write-Host 'Setting up Python dependencies...'
            if (-not (Test-Path -LiteralPath $python)) {
                & $pythonLauncher.Source -3.12 -m venv (Join-Path $target '.venv')
                if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment.' }
            }
            & $python -m pip install -r $requirements
            if ($LASTEXITCODE -ne 0) {
                throw 'Dependency setup failed. Dependencies may be partially changed; rerun this updater after fixing the error above.'
            }
        }
        Write-Host ''
        Write-Host "RaceVoice updated in $target"
        Write-Host "Previous source files: $backup"
        Write-Host 'Your saved settings and access keys are unchanged.'
        Write-Host 'Start the app using Start-Race-Voice.cmd.'
        if ((Test-Path -LiteralPath (Join-Path $target 'RaceVoice.exe')) -or
            (Test-Path -LiteralPath (Join-Path $target 'dist\RaceVoice\RaceVoice.exe'))) {
            Write-Host 'An EXE build was found. This updater updates source only; run Build-EXE.cmd to rebuild that EXE.'
        }
    }
    catch {
        if ($copying) {
            Write-Warning 'Update failed. Restoring the previous source files...'
            foreach ($file in $changed) {
                $destination = Join-Path $target $file.Relative
                try {
                    if ($file.Existed) {
                        Copy-Item -LiteralPath (Join-Path $backup $file.Relative) -Destination $destination -Force
                    }
                    elseif (Test-Path -LiteralPath $destination) {
                        Remove-Item -LiteralPath $destination -Force
                    }
                }
                catch { Write-Warning "Could not restore $destination. Previous files are in $backup." }
            }
        }
        throw
    }
    finally {
        # Delete only the unique temporary folder created above, within its checked parent.
        $resolved = [IO.Path]::GetFullPath($temporary)
        $tempParent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
        if ((Split-Path -Parent $resolved) -eq $tempParent -and
            (Split-Path -Leaf $resolved) -like 'RaceVoice-update-*') {
            Remove-Item -LiteralPath $resolved -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    try { Update-RaceVoice; exit 0 }
    catch {
        Write-Host ''
        Write-Host ('Update failed: ' + $_.Exception.Message) -ForegroundColor Yellow
        Write-Host 'If access is denied for C:\apps, right-click Update-RaceVoice.cmd and choose Run as administrator.'
        exit 1
    }
}
