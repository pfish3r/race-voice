$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$repo = Split-Path -Parent $PSScriptRoot
$work = Join-Path $PSScriptRoot ('updater-test-' + [guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($work)
$oldTemp = $env:TEMP
$oldTmp = $env:TMP
$env:TEMP = $work
$env:TMP = $work
$checks = 0
function Assert-True($Condition, $Message) {
    if (-not $Condition) { throw "FAIL: $Message" }
    $script:checks++
}
function Assert-Fails([scriptblock]$Action, [string]$Message) {
    $failed = $false
    try { & $Action } catch { $failed = $true }
    Assert-True $failed $Message
}
function New-TestZip($Name, [hashtable]$Extra = @{}, [string]$Omit = '') {
    $path = Join-Path $work $Name
    $zip = [IO.Compression.ZipFile]::Open($path, [IO.Compression.ZipArchiveMode]::Create)
    try {
        $entries = @{
            'repo/app.py' = '# new app'
            'repo/speech.py' = '# new speech'
            'repo/playback.py' = '# new playback'
            'repo/modern_widgets.py' = '# new widgets'
            'repo/requirements.txt' = '# test requirements'
            'repo/Start-Race-Voice.cmd' = '@echo new start'
            'repo/Update-RaceVoice.cmd' = '@echo new updater'
            'repo/Update-RaceVoice.ps1' = '# new updater'
        }
        foreach ($item in (@($entries.GetEnumerator()) + @($Extra.GetEnumerator()))) {
            if ($item.Key -eq $Omit) { continue }
            $entry = $zip.CreateEntry($item.Key)
            $writer = New-Object IO.StreamWriter($entry.Open())
            try { $writer.Write($item.Value) } finally { $writer.Dispose() }
        }
    }
    finally { $zip.Dispose() }
    return $path
}
# Test the real functions with local ZIPs and fake process metadata, no network or Python installs.
. (Join-Path $repo 'Update-RaceVoice.ps1') -InstallPath (Join-Path $work 'installed') -SkipDependencies
function Get-CimInstance { param($ClassName, $ErrorAction) return @() }
try {
    $target = Join-Path $work 'installed'
    [void][IO.Directory]::CreateDirectory($target)
    Set-Content -LiteralPath (Join-Path $target 'app.py') -Value '# old app'
    Set-Content -LiteralPath (Join-Path $target '.env') -Value 'keep key'
    Set-Content -LiteralPath (Join-Path $target 'settings.json') -Value 'keep settings'
    [void][IO.Directory]::CreateDirectory((Join-Path $target '.venv'))
    Set-Content -LiteralPath (Join-Path $target '.venv\sentinel') -Value 'keep environment'
    $ArchivePath = New-TestZip 'good.zip' @{
        'repo/.env' = 'replace key'
        'repo/settings.json' = 'replace settings'
        'repo/.venv/sentinel' = 'replace environment'
    }
    Update-RaceVoice
    Assert-True ((Get-Content -Raw (Join-Path $target 'app.py')).Trim() -eq '# new app') 'new source installed'
    Assert-True ((Get-Content -Raw (Join-Path $target '.env')).Trim() -eq 'keep key') '.env preserved'
    Assert-True ((Get-Content -Raw (Join-Path $target 'settings.json')).Trim() -eq 'keep settings') 'settings preserved'
    Assert-True ((Get-Content -Raw (Join-Path $target '.venv\sentinel')).Trim() -eq 'keep environment') 'venv preserved'
    $backups = @(Get-ChildItem -LiteralPath (Join-Path $target 'update-backups') -Directory)
    Assert-True ($backups.Count -eq 1) 'backup created'
    Assert-True ((Get-Content -Raw (Join-Path $backups[0].FullName 'app.py')).Trim() -eq '# old app') 'backup contains old source'
    $ArchivePath = New-TestZip 'missing.zip' @{} 'repo/speech.py'
    Assert-Fails { Update-RaceVoice } 'incomplete update rejected'
    Assert-True ((Get-Content -Raw (Join-Path $target 'app.py')).Trim() -eq '# new app') 'incomplete update leaves source untouched'
    $ArchivePath = ''
    function Invoke-WebRequest { throw 'Simulated network failure' }
    Assert-Fails { Update-RaceVoice } 'download failure reported'
    Assert-True ((Get-Content -Raw (Join-Path $target 'app.py')).Trim() -eq '# new app') 'download failure leaves source untouched'
    Remove-Item Function:\Invoke-WebRequest
    $ArchivePath = New-TestZip 'traversal.zip' @{ 'repo/../outside.txt' = 'bad' }
    Assert-Fails { Update-RaceVoice } 'traversal rejected'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $work 'outside.txt'))) 'traversal wrote no outside file'
    $ArchivePath = New-TestZip 'duplicate.zip' @{ 'repo/APP.PY' = 'bad duplicate' }
    Assert-Fails { Update-RaceVoice } 'case-insensitive duplicate rejected'
    $ArchivePath = New-TestZip 'stream.zip' @{ 'repo/file.txt:stream' = 'bad stream' }
    Assert-Fails { Update-RaceVoice } 'NTFS streams rejected'
    Assert-Fails { Assert-SafeInstallPath 'C:\' } 'drive root rejected'
    Assert-Fails { Assert-SafeInstallPath 'C:relative' } 'drive-relative path rejected'
    Assert-Fails { Assert-SafeInstallPath '\\server\share\RaceVoice' } 'network shares rejected'

    # An update copy failure must roll back the already-copied app and launcher files.
    $ArchivePath = New-TestZip 'rollback.zip'
    Set-Content -LiteralPath (Join-Path $target 'app.py') -Value '# app before failed update'
    $script:failCopy = $true
    function Copy-Item {
        [CmdletBinding()]param($LiteralPath, $Destination, [switch]$Force)
        if ($script:failCopy -and $Destination -eq (Join-Path $target 'speech.py')) {
            $script:failCopy = $false
            throw 'Simulated copy failure'
        }
        Microsoft.PowerShell.Management\Copy-Item @PSBoundParameters
    }
    Assert-Fails { Update-RaceVoice } 'copy error reported'
    Assert-True ((Get-Content -Raw (Join-Path $target 'app.py')).Trim() -eq '# app before failed update') 'source restored after copy error'
    Remove-Item Function:\Copy-Item

    function Get-CimInstance {
        param($ClassName, $ErrorAction)
        return [pscustomobject]@{Name='python.exe';ExecutablePath=(Join-Path $target '.venv\Scripts\python.exe');CommandLine='python.exe app.py'}
    }
    Assert-Fails { Assert-RaceVoiceClosed $target } 'running app blocks update'
    function Get-CimInstance { param($ClassName, $ErrorAction) return @() }
    Assert-NoReparsePath $target
    $InstallPath = Join-Path $work 'fresh-install'
    $ArchivePath = New-TestZip 'fresh.zip'
    Update-RaceVoice
    Assert-True (Test-Path -LiteralPath (Join-Path $InstallPath 'app.py')) 'first installation creates target'
    Assert-True (Test-Path -LiteralPath (Join-Path $InstallPath 'Update-RaceVoice.cmd')) 'first installation includes future updater'
    $temporary = @(Get-ChildItem -LiteralPath $work -Directory -Filter 'RaceVoice-update-*')
    Assert-True ($temporary.Count -eq 0) 'temporary update folders cleaned'
    # Run the actual CMD launcher from a path with spaces/apostrophes. Its helper
    # deliberately overwrites the running CMD before returning an exit status.
    $launcherFolder = Join-Path $work "launcher space's"
    [void][IO.Directory]::CreateDirectory($launcherFolder)
    $launcher = Join-Path $launcherFolder 'Update-RaceVoice.cmd'
    $helper = Join-Path $launcherFolder 'Update-RaceVoice.ps1'
    foreach ($code in @(0,7)) {
        Microsoft.PowerShell.Management\Copy-Item -LiteralPath (Join-Path $repo 'Update-RaceVoice.cmd') -Destination $launcher -Force
        $stub = "Set-Content -LiteralPath (Join-Path "+'$PSScriptRoot'+" 'Update-RaceVoice.cmd') -Value '@echo replaced'; Set-Content -LiteralPath (Join-Path "+'$PSScriptRoot'+" 'ran.txt') -Value 'ran'; exit $code"
        Set-Content -LiteralPath $helper -Value $stub
        & $env:ComSpec /d /c ('"'+$launcher+'" <nul')
        $result = $LASTEXITCODE
        Write-Host "Launcher case $code returned $result"
        Assert-True ($result -eq $code) 'launcher preserves success/failure after self-update'
        Assert-True (Test-Path -LiteralPath (Join-Path $launcherFolder 'ran.txt')) 'launcher executes helper from path with spaces and apostrophe'
    }
    Write-Host "PASS: $checks Windows updater checks"
}
finally {
    $env:TEMP = $oldTemp
    $env:TMP = $oldTmp
    $resolved = [IO.Path]::GetFullPath($work)
    if ((Split-Path -Parent $resolved) -eq [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\') -and
        (Split-Path -Leaf $resolved) -like 'updater-test-*') {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
