<#
    run_legacy_equivalence.ps1 -- reproduce the route 4 equivalence claim.

    The claim: the launcher's legacy mode hands Fiji byte-for-byte the same
    environment and the same command line as IFQuantLauncher-v1.7.2.exe, so an
    analysis run before v1.8.0 stays reproducible.

    That claim is checked by EXECUTION, not assertion: the harness builds both
    environments through the real code paths, spawns a probe process with each,
    and diffs what the child actually received.

    Run it with no arguments from anywhere inside the checkout:

        powershell -ExecutionPolicy Bypass -File .\launcher\run_legacy_equivalence.ps1

    Exit code 0 means every check passed. Anything else means at least one did
    not, and the failing lines are printed.

    WHY THIS SCRIPT EXISTS
      The harness used to need six positional paths, one of which -- the v1.7.2
      reference source -- had to be dug out of git history, because commit
      f7dbb02 overwrote launcher/IFQuantLauncher.cs with v1.8.0. Anyone cloning
      this repository could not reproduce the claim. The reference is now
      committed at launcher/reference/IFQuantLauncher-v1.7.2.cs and the harness
      defaults to it.

    ON THE EMBEDDED-ARTEFACT CHECK
      The embedded engine is NO LONGER byte-identical to the one v1.7.2 shipped:
      IF_Quant_Pipeline.groovy carries a fix for a missing `black` token in an
      ImageJ Binary Options macro string that was globally flipping
      Prefs.blackBackground and erasing roughly 89x of the nuclei. The harness
      therefore verifies that this drift is DETECTED AND REPORTED, not that it is
      absent -- asserting absence would force a choice between shipping a
      known-buggy engine and a red suite. To reproduce the original NUMBERS, run
      the archived legacy/launchers/IFQuantLauncher-v1.7.2.exe.
#>
[CmdletBinding()]
param([switch]$KeepBuild)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot

# A Windows environment block may contain both Path and PATH. .NET Framework's
# ProcessStartInfo.EnvironmentVariables used to throw while materializing that
# block. Do not sanitize it here: the shipping EnvironmentApply path must prove
# that it handles the condition itself.
$work = Join-Path $env:TEMP ("ifq_legacy_eq_" + [System.Diagnostics.Process]::GetCurrentProcess().Id)
New-Item -ItemType Directory -Path $work -Force | Out-Null

$csc = Get-ChildItem `
    (Join-Path $env:WINDIR "Microsoft.NET\FrameworkArm64"), `
    (Join-Path $env:WINDIR "Microsoft.NET\Framework64"), `
    (Join-Path $env:WINDIR "Microsoft.NET\Framework") `
    -Filter csc.exe -Recurse -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending | Select-Object -First 1
if (-not $csc) { throw "No C# compiler found under $env:WINDIR\Microsoft.NET." }

$refs = @("/r:System.dll", "/r:System.Drawing.dll", "/r:System.Windows.Forms.dll", "/r:System.Core.dll")

# The environment probe: prints the environment it was started with, so the two
# route-4 builds can be compared at the PROCESS level rather than as dictionaries.
$probe = Join-Path $work "EnvProbe.exe"
& $csc.FullName /nologo /target:exe /platform:anycpu "/out:$probe" @refs `
    (Join-Path $PSScriptRoot "reference\EnvProbe.cs") | Out-Null
if ($LASTEXITCODE -ne 0) { throw "EnvProbe build failed." }

# The harness links the real launcher sources, so it exercises shipping code.
# /main disambiguates: the launcher has its own entry point.
$harness = Join-Path $work "LegacyEquivalence.exe"
& $csc.FullName /nologo /target:exe /platform:anycpu `
    /main:IFQuantLauncher.LegacyCheck.LegacyEquivalenceProgram "/out:$harness" @refs `
    (Join-Path $PSScriptRoot "LegacyEquivalence.cs") `
    (Join-Path $PSScriptRoot "IFQuantLauncher.cs") `
    (Join-Path $PSScriptRoot "IFQuantLauncher.Routing.cs") `
    (Join-Path $PSScriptRoot "MainForm.Routes.partial.cs") `
    (Join-Path $PSScriptRoot "HeReviewForm.cs") | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Harness build failed." }

& $harness `
    (Join-Path $PSScriptRoot "reference\IFQuantLauncher-v1.7.2.cs") `
    (Join-Path $PSScriptRoot "IFQuantLauncher.cs") `
    $probe `
    (Join-Path $repo "IF_Quant_Pipeline.groovy") `
    (Join-Path $repo "config\lung_marker_registry.json") `
    (Join-Path $PSScriptRoot "IFQuantLauncher.Routing.cs")
$code = $LASTEXITCODE

if (-not $KeepBuild) { Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue }
else { Write-Host "build kept at $work" }

exit $code
