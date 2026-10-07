#requires -Version 7.0

[CmdletBinding()]
param(
    [string]$Python = "python",
    [string]$InstallRoot = (Join-Path $HOME ".local/share/loopx"),
    [string]$BinDir = (Join-Path $HOME ".local/bin"),
    [string]$SkillsDir = "",
    [switch]$SkipSkills,
    [switch]$AddToUserPath
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$previousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = if ([string]::IsNullOrWhiteSpace($previousPythonPath)) {
    $repoRoot
} else {
    "$repoRoot$([IO.Path]::PathSeparator)$previousPythonPath"
}
try {
    if ([string]::IsNullOrWhiteSpace($SkillsDir)) {
        $codexHome = if ([string]::IsNullOrWhiteSpace($env:CODEX_HOME)) {
            $resolvedHome = & $Python -c 'import sys; from pathlib import Path; from loopx.paths import home_codex_root; print(home_codex_root(Path(sys.argv[1])))' $HOME
            if ($LASTEXITCODE -ne 0) {
                throw "Could not resolve the default Codex home using the selected Python"
            }
            $resolvedHome
        } else {
            $env:CODEX_HOME
        }
        $SkillsDir = Join-Path $codexHome "skills"
    }
    $installerArgs = @(
        "-m", "loopx.windows_install",
        "--source-root", $repoRoot,
        "--install-root", $InstallRoot,
        "--bin-dir", $BinDir,
        "--skills-dir", $SkillsDir,
        "--python", $Python
    )
    if ($SkipSkills) {
        $installerArgs += "--skip-skills"
    }
    & $Python @installerArgs
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
} finally {
    $env:PYTHONPATH = $previousPythonPath
}

$loopxLauncher = Join-Path $BinDir "loopx.ps1"
& $loopxLauncher --format json extension doctor --all-enabled --execute *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Warning "One or more enabled extensions failed post-install doctor revalidation. Run 'loopx extension doctor --all-enabled --execute' after repairing the provider."
}

if ($AddToUserPath) {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $entries = @($userPath -split ";" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    $alreadyPresent = $entries | Where-Object {
        [string]::Equals($_.TrimEnd("\\"), $BinDir.TrimEnd("\\"), [StringComparison]::OrdinalIgnoreCase)
    }
    if (-not $alreadyPresent) {
        $updatedPath = (@($entries) + $BinDir) -join ";"
        [Environment]::SetEnvironmentVariable("Path", $updatedPath, "User")
    }
    if (-not (($env:Path -split ";") -contains $BinDir)) {
        $env:Path = "$BinDir;$env:Path"
    }
    [Console]::Error.WriteLine("LoopX Windows user PATH includes: $BinDir")
}
