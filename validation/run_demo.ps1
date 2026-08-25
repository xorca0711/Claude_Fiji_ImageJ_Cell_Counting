# run_demo.ps1 -- one-command synthetic validation of the blackBackground bug.
#
# Generates the deterministic synthetic DAPI field, then runs the production
# nucleus-candidate sequence twice (fixed vs buggy Binary Options call) in
# headless Fiji, and prints a PASS/FAIL verdict.
#
#   PASS = world A (fixed call) recovers the generated interior blob count
#          within 25%, AND world B (buggy call) counts < 20% of world A,
#          AND the Prefs.blackBackground flip is observed directly.
#
# Fiji discovery: $env:IFQ_FIJI_DIR if set, else X:\Fiji. The Fiji launcher
# exe is broken on ARM64, so the JVM is invoked directly (same pattern as
# scripts/run_confocal_260808.ps1). PowerShell 5.1 compatible. -Xmx1g, one
# JVM at a time (limited-RAM machine).

$ErrorActionPreference = "Continue"

function Get-IfqEnvSnapshot {
    $snapshot = @{}
    Get-ChildItem Env: | Where-Object { $_.Name -like 'IFQ_*' } | ForEach-Object {
        $snapshot[$_.Name] = $_.Value
    }
    return $snapshot
}

function Restore-IfqEnvSnapshot {
    param(
        [Parameter(Mandatory)]
        [hashtable]$Snapshot
    )

    $current = Get-ChildItem Env: | Where-Object { $_.Name -like 'IFQ_*' }
    foreach ($item in $current) {
        if (-not $Snapshot.ContainsKey($item.Name)) {
            Remove-Item Env:$($item.Name) -ErrorAction SilentlyContinue
        }
    }

    foreach ($name in $Snapshot.Keys) {
        Set-Item Env:$name $Snapshot[$name]
    }
}

$ifqEnvBefore = Get-IfqEnvSnapshot

try {
    $valDir = $PSScriptRoot
    if (-not $valDir) { $valDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
    $outDir = Join-Path $valDir "out"
    New-Item -ItemType Directory -Force -Path $outDir | Out-Null

    # ---- locate Fiji ------------------------------------------------------------
    $fj = $env:IFQ_FIJI_DIR
    if (-not $fj) { $fj = "X:\Fiji" }
    if (-not (Test-Path (Join-Path $fj "jars"))) {
      Write-Host "ERROR: Fiji not found at '$fj' (no 'jars' subdirectory)."
      Write-Host "Set IFQ_FIJI_DIR to your Fiji installation directory (the folder"
      Write-Host "containing 'jars', 'plugins' and 'java') and re-run this script."
      exit 1
    }
    $jre = Get-ChildItem (Join-Path $fj "java") -Recurse -Filter java.exe -ErrorAction SilentlyContinue |
           Select-Object -First 1
    if (-not $jre) {
      Write-Host "ERROR: no java.exe found under '$fj\java'. This script invokes the"
      Write-Host "JVM bundled with Fiji directly (the launcher exe is broken on ARM64)."
      exit 1
    }
    $jre = $jre.FullName
    $patcher = Get-ChildItem (Join-Path $fj "jars") -Filter "ij1-patcher*.jar" |
               Select-Object -First 1
    if (-not $patcher) {
      Write-Host "ERROR: no ij1-patcher*.jar under '$fj\jars'; is '$fj' really a Fiji install?"
      exit 1
    }
    $patcher = $patcher.FullName

    $env:IFQ_VALIDATION_OUT = $outDir
    $cp = "$fj\jars\*;$fj\plugins\*"
    $genLog  = Join-Path $outDir "generate.log"
    $demoLog = Join-Path $outDir "demo.log"

    # ---- 1/2: generate the fixture ---------------------------------------------
    Write-Host "[1/2] Generating synthetic fixture (headless Fiji, -Xmx1g)..."
    & $jre '--add-opens=java.base/java.lang=ALL-UNNAMED' `
      "-javaagent:$patcher=init" `
      '-Djava.awt.headless=true' "-Dplugins.dir=$fj" '-Xmx1g' `
      -cp $cp net.imagej.Main --headless `
      --run "$valDir\generate_fixture.groovy" 2>&1 |
      Out-File $genLog -Encoding utf8
    if ($LASTEXITCODE -ne 0) {
      Write-Host "ERROR: fixture generation failed (exit $LASTEXITCODE). Log tail ($genLog):"
      Get-Content $genLog | Select-Object -Last 25 | ForEach-Object { Write-Host "  $_" }
      exit 1
    }
    Get-Content $genLog | Select-String -Pattern "^FIXTURE" | ForEach-Object { Write-Host $_.Line }

    # ---- 2/2: run the two-world demo -------------------------------------------
    Write-Host "[2/2] Running bug demo (fixed vs buggy Options call, same JVM)..."
    & $jre '--add-opens=java.base/java.lang=ALL-UNNAMED' `
      "-javaagent:$patcher=init" `
      '-Djava.awt.headless=true' "-Dplugins.dir=$fj" '-Xmx1g' `
      -cp $cp net.imagej.Main --headless `
      --run "$valDir\demo_blackbackground_bug.groovy" 2>&1 |
      Out-File $demoLog -Encoding utf8
    if ($LASTEXITCODE -ne 0) {
      Write-Host "ERROR: demo failed (exit $LASTEXITCODE). Log tail ($demoLog):"
      Get-Content $demoLog | Select-Object -Last 30 | ForEach-Object { Write-Host "  $_" }
      exit 1
    }

    Get-Content $demoLog |
      Select-String -Pattern "^(===|TRUTH|WORLD|Prefs|RESULT|VERDICT|WARNING)" |
      ForEach-Object { Write-Host $_.Line }

    if (Select-String -Path $demoLog -Pattern "^VERDICT: PASS" -Quiet) {
      exit 0
    } else {
      Write-Host "Demo did not PASS. Full log: $demoLog"
      exit 2
    }
}
finally {
    Restore-IfqEnvSnapshot -Snapshot $ifqEnvBefore
}
