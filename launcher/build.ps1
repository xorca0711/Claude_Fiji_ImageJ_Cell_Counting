<#
    build.ps1 -- IF Quant Launcher v1.9.5

    Diff against the v1.7.2 build script, in full:

      1. THREE source files are compiled instead of one. csc takes a list, so
         nothing about the shape of the build changes; the launcher is still a
         single self-contained .exe with no external dependencies.
      2. TWO extra resources are embedded: qupath_wsi_tile_export.groovy and
         aggregate_tiles_to_slide.py. Route 2 needs them on an analysis machine
         with no repository checkout, for exactly the reason v1.7.2 embedded the
         pipeline. Both are optional at run time, so a build made without them
         still starts and simply has no working route 2.
      3. The compiler search adds FrameworkArm64. v1.7.2 looked only in
         Framework64 and Framework; on a win-arm64 machine Framework64 exists
         and works under emulation, so v1.7.2 builds -- but that is luck.
      4. --self-test is RUN as part of the build and the binary is discarded on
         failure. v1.7.2 shipped a self-test and never ran it.

    Unchanged: AnyCPU, /target:winexe, the app.manifest, the versioned output
    name parsed out of AssemblyFileVersion, and the .sha256.txt sidecar written
    beside the exe in the repository root.
#>
[CmdletBinding()]
param(
    [string]$Configuration = "Release",
    [string]$OutputName = "",
    [switch]$SkipSelfTest
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot

$sources = @(
    (Join-Path $PSScriptRoot "IFQuantLauncher.cs"),
    (Join-Path $PSScriptRoot "IFQuantLauncher.Routing.cs"),
    (Join-Path $PSScriptRoot "MainForm.Routes.partial.cs")
)
$manifest = Join-Path $PSScriptRoot "app.manifest"

# name -> (path on disk, resource id inside the assembly)
$resources = [ordered]@{
    "pipeline" = @{ Path = (Join-Path $repo "IF_Quant_Pipeline.groovy")
                    Id   = "IFQuant.IF_Quant_Pipeline.groovy" }
    "registry" = @{ Path = (Join-Path $repo "config\lung_marker_registry.json")
                    Id   = "IFQuant.lung_marker_registry.json" }
    "stage1"   = @{ Path = (Join-Path $repo "qupath_wsi_tile_export.groovy")
                    Id   = "IFQuant.qupath_wsi_tile_export.groovy" }
    "stage3"   = @{ Path = (Join-Path $repo "aggregate_tiles_to_slide.py")
                    Id   = "IFQuant.aggregate_tiles_to_slide.py" }
}

foreach ($required in ($sources + @($manifest))) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required build input is missing: $required"
    }
}
foreach ($key in $resources.Keys) {
    $path = $resources[$key].Path
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required embedded resource is missing: $path"
    }
}

# ---- version ------------------------------------------------------------
$sourceText = [System.IO.File]::ReadAllText($sources[0])
$versionMatch = [regex]::Match(
    $sourceText,
    '\[assembly:\s*AssemblyFileVersion\("(?<version>\d+\.\d+\.\d+)\.\d+"\)\]'
)
if (-not $versionMatch.Success) {
    throw "Could not resolve the launcher version from IFQuantLauncher.cs."
}
$launcherVersion = $versionMatch.Groups["version"].Value
if ([string]::IsNullOrWhiteSpace($OutputName)) {
    $OutputName = "IFQuantLauncher-v$launcherVersion.exe"
}
if ([System.IO.Path]::GetFileName($OutputName) -ne $OutputName -or
    -not $OutputName.EndsWith(".exe", [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "OutputName must be a simple .exe filename."
}
$output = Join-Path $repo $OutputName

# The routing layer states the same version as a compile-time constant, so the
# UI text and the file name cannot disagree. Check it here rather than trusting
# two hand-edited places to stay in step.
$routingText = [System.IO.File]::ReadAllText($sources[1])
$routingVersion = [regex]::Match($routingText, 'public const string Version = "(?<v>[^"]+)"')
if (-not $routingVersion.Success -or $routingVersion.Groups["v"].Value -ne $launcherVersion) {
    throw ("LauncherBuild.Version is '" + $routingVersion.Groups["v"].Value +
           "' but AssemblyFileVersion says '$launcherVersion'.")
}

# ---- compiler -----------------------------------------------------------
$compilerCandidates = @(
    "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
    "$env:WINDIR\Microsoft.NET\FrameworkArm64\v4.0.30319\csc.exe",
    "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe"
)
$compiler = $compilerCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $compiler) {
    throw "The Windows .NET Framework C# compiler was not found."
}

# ---- compile ------------------------------------------------------------
$arguments = @(
    "/nologo",
    "/target:winexe",
    "/platform:anycpu",
    "/optimize+",
    "/win32manifest:$manifest",
    "/out:$output",
    "/reference:System.dll",
    "/reference:System.Core.dll",
    "/reference:System.Drawing.dll",
    "/reference:System.Windows.Forms.dll",
    "/reference:System.Web.Extensions.dll"
)
foreach ($key in $resources.Keys) {
    $arguments += "/resource:$($resources[$key].Path),$($resources[$key].Id)"
}
$arguments += $sources

& $compiler @arguments
if ($LASTEXITCODE -ne 0) {
    throw "C# compilation failed with exit code $LASTEXITCODE."
}

# ---- structural invariant: ONE way into a child environment -------------
#
# The launch choke point (RunSeal.Issue) is only a choke point while there is
# exactly one statement that writes a child process's environment and exactly
# one statement that can construct the seal. Both previous defect rounds were
# a SECOND path into the child environment that skipped validation, so the
# build refuses to produce a binary in which a second one exists. The binary
# is compiled first and discarded here, the same way a failed self-test
# discards it: a build that gets this far and still fails must leave nothing
# runnable behind.
#
# Comments are stripped first: the choke point's own doc comments quote the
# patterns being counted, which is deliberate, and a raw scan would count the
# explanation as a violation.
$scanText = ($sources | ForEach-Object { [System.IO.File]::ReadAllText($_) }) -join "`n"
$scanText = [regex]::Replace($scanText, '/\*.*?\*/', ' ', 'Singleline')
$scanText = [regex]::Replace($scanText, '//[^\r\n]*', ' ')
$structural = @()

$envWrites = [regex]::Matches($scanText, '\.EnvironmentVariables\s*\[').Count
if ($envWrites -ne 1) {
    $structural += "there are $envWrites statements writing a child process environment; exactly 1 is allowed (EnvironmentApply.Apply)"
}
$sealNews = [regex]::Matches($scanText, 'new\s+RunSeal\s*\(').Count
if ($sealNews -ne 1) {
    $structural += "there are $sealNews statements constructing a RunSeal; exactly 1 is allowed (inside RunSeal.Issue)"
}
if (-not [regex]::IsMatch($scanText, 'private\s+RunSeal\s*\(')) {
    $structural += "RunSeal's constructor is not private, so a caller could build a seal without passing the choke point"
}
if (-not [regex]::IsMatch(
        $scanText,
        'public\s+static\s+void\s+Apply\s*\(\s*(\r?\n\s*)?System\.Diagnostics\.ProcessStartInfo\s+psi\s*,\s*RunSeal\s+seal\s*\)')) {
    $structural += "EnvironmentApply.Apply does not take a RunSeal"
}
if ([regex]::IsMatch($scanText, 'RunSeal\.Issue\s*\(') -eq $false) {
    $structural += "nothing issues a RunSeal, so no process could start"
}
if ($structural.Count -gt 0) {
    if (Test-Path -LiteralPath $output) { Remove-Item -LiteralPath $output -Force }
    throw ("The launch choke point is not intact. The build was discarded.`r`n  - " +
           ($structural -join "`r`n  - "))
}
Write-Host "Choke point: one environment write, one seal construction, private ctor."

# ---- self-test ----------------------------------------------------------
# The launcher is /target:winexe, i.e. a GUI-subsystem binary. PowerShell does
# NOT wait for one of those when you call it with `& $exe`: control returns
# immediately and $LASTEXITCODE still holds whatever the previous command left
# behind. A self-test invoked that way reports "passed" without having run.
# Start-Process -Wait -PassThru is what actually waits and returns the code.
function Invoke-LauncherCheck {
    param([string]$Exe, [string]$Mode)
    $process = Start-Process -FilePath $Exe -ArgumentList $Mode -Wait -PassThru
    return $process.ExitCode
}

if (-not $SkipSelfTest) {
    # 0 = every embedded-artefact check, every route-model invariant and every
    # fail-closed rule passed. Codes 10-28 are v1.7.2's; 30-43 are the route
    # model and H1-H5. See RuntimeBundle.SelfTest / RouteSelfTest.
    $selfTest = Invoke-LauncherCheck -Exe $output -Mode "--self-test"
    if ($selfTest -ne 0) {
        Remove-Item -LiteralPath $output -Force
        throw "Self-test failed with exit code $selfTest. The build was discarded."
    }
    Write-Host "Self-test: passed."

    # Builds the real window, walks every route and checks that route 3 cannot
    # be selected. Codes 60-73. Needs an interactive desktop; on a session
    # without one, pass -SkipSelfTest and run it by hand.
    $uiSmoke = Invoke-LauncherCheck -Exe $output -Mode "--ui-smoke"
    if ($uiSmoke -ne 0) {
        Remove-Item -LiteralPath $output -Force
        throw "UI smoke test failed with exit code $uiSmoke. The build was discarded."
    }
    Write-Host "UI smoke test: passed."
}

# ---- hash sidecar -------------------------------------------------------
$hash = (Get-FileHash -LiteralPath $output -Algorithm SHA256).Hash.ToLowerInvariant()
$hashRecord = "$hash  $OutputName`r`n"
$hashFileName = [System.IO.Path]::GetFileNameWithoutExtension($OutputName) + ".sha256.txt"
[System.IO.File]::WriteAllText(
    (Join-Path $repo $hashFileName),
    $hashRecord,
    [System.Text.UTF8Encoding]::new($false)
)

Write-Host "Built: $output"
Write-Host "SHA256: $hash"
foreach ($key in $resources.Keys) {
    $h = (Get-FileHash -LiteralPath $resources[$key].Path -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-Host ("  embedded {0,-9} {1}  {2}" -f $key, $h, (Split-Path $resources[$key].Path -Leaf))
}
