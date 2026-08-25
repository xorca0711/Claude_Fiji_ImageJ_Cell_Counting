param([switch]$KeepFixture)

$ErrorActionPreference = "Stop"

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
    $repo = Split-Path -Parent $valDir
    $fj = $env:IFQ_FIJI_DIR
    if (-not $fj) { $fj = "X:\Fiji" }
    if (-not (Test-Path -LiteralPath (Join-Path $fj "jars") -PathType Container)) {
      throw "Fiji not found at '$fj'. Set IFQ_FIJI_DIR."
    }
    $jre = Get-ChildItem (Join-Path $fj "java") -Recurse -Filter java.exe |
           Select-Object -First 1
    $patcher = Get-ChildItem (Join-Path $fj "jars") -Filter "ij1-patcher*.jar" |
               Select-Object -First 1
    if (-not $jre -or -not $patcher) { throw "Fiji Java runtime or ij1-patcher is missing." }

    $fixtureRoot = Join-Path ([IO.Path]::GetTempPath()) ("ifq_endpoint_fixture_" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $fixtureRoot | Out-Null
    $env:IFQ_ENDPOINT_FIXTURE_ROOT = $fixtureRoot
    $cp = "$fj\jars\*;$fj\plugins\*"

    function Invoke-FijiGroovy(
      [string]$ScriptPath,
      [string]$LogPath,
      [switch]$ExpectFailure
    ) {
      $ErrorActionPreference = "Continue"
      & $jre.FullName '--add-opens=java.base/java.lang=ALL-UNNAMED' `
        "-javaagent:$($patcher.FullName)=init" `
        '-Djava.awt.headless=true' "-Dplugins.dir=$fj" '-Xmx1g' `
        -cp $cp net.imagej.Main --headless --run $ScriptPath 2>&1 |
        Out-File -LiteralPath $LogPath -Encoding utf8
      $rc = $LASTEXITCODE
      if (-not $ExpectFailure -and $rc -ne 0) {
        Get-Content -LiteralPath $LogPath | Select-Object -Last 30
        throw "Fiji script failed with exit code ${rc}: $ScriptPath"
      }
      if ($ExpectFailure -and $rc -eq 0) {
        throw "Fiji script unexpectedly succeeded: $ScriptPath"
      }
    }

    try {
      Invoke-FijiGroovy "$valDir\generate_endpoint_fixture.groovy" "$fixtureRoot\generate.log"

      $env:IFQ_ENDPOINT_SPEC = "$fixtureRoot\spec.json"
      $env:IFQ_ANALYSIS_DIR = "$fixtureRoot\analysis"
      $env:IFQ_ENDPOINT_REGION_MODE = "tissue_mask"
      $env:IFQ_TISSUE_MASK_DIR = "$fixtureRoot\tissue_masks"
      $env:IFQ_ENDPOINT_OUT = "$fixtureRoot\endpoint_areas.csv"
      $env:IFQ_ENDPOINT_AREA_TOL = "0"
      $env:IFQ_ENDPOINT_AREA_CHECK = "fail"
      $env:IFQ_ENDPOINT_ALLOW_UNCALIBRATED = "false"
      Invoke-FijiGroovy "$repo\endpoints\evaluate_endpoints.groovy" "$fixtureRoot\evaluate.log"

      $rows = @(Import-Csv -LiteralPath "$fixtureRoot\endpoint_areas.csv")
      if ($rows.Count -ne 1) { throw "Expected one endpoint row; found $($rows.Count)." }
      $row = $rows[0]
      $num = [double]$row.synthetic_pod_area_um2
      $den = [double]$row.synthetic_denominator_area_um2
      $frac = [double]$row.synthetic_fraction
      $bare = [double]$row.qc_bare_KRT5_pod_mask_area_um2_in_region
      $region = [double]$row.qc_region_area_um2_from_mask
      if ($num -ne 2 -or $den -ne 6 -or [math]::Abs($frac - (1.0/3.0)) -gt 1e-12 -or
          $bare -ne 4 -or $region -ne 8) {
        throw "Unexpected algebra: numerator=$num denominator=$den fraction=$frac bare=$bare region=$region"
      }
      Write-Host "VERDICT: PASS -- numerator=2 denominator=6 fraction=1/3 bare_KRT5=4 region=8"

      $uncalSpec = Get-Content -LiteralPath "$fixtureRoot\spec.json" -Raw | ConvertFrom-Json
      $uncalSpec.parameters.t1a_threshold.value = $null
      $uncalSpec.parameters.t1a_threshold.status = "NOT_CALIBRATED"
      $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
      [IO.File]::WriteAllText(
        "$fixtureRoot\uncalibrated.json",
        ($uncalSpec | ConvertTo-Json -Depth 20),
        $utf8NoBom)
      $env:IFQ_ENDPOINT_SPEC = "$fixtureRoot\uncalibrated.json"
      $env:IFQ_ENDPOINT_OUT = "$fixtureRoot\must_not_exist.csv"
      Invoke-FijiGroovy "$repo\endpoints\evaluate_endpoints.groovy" `
        "$fixtureRoot\uncalibrated.log" -ExpectFailure
      if (Test-Path -LiteralPath $env:IFQ_ENDPOINT_OUT) {
        throw "Uncalibrated endpoint failure still created an output CSV."
      }
      if (-not (Select-String -LiteralPath "$fixtureRoot\uncalibrated.log" `
          -Pattern "uncalibrated parameter" -Quiet)) {
        throw "Uncalibrated endpoint failed without the expected diagnostic."
      }
      Write-Host "GUARD: PASS -- uncalibrated endpoint refused before output"

      $uncalSpec | Add-Member -NotePropertyName RETRACTED -NotePropertyValue "synthetic retraction test"
      [IO.File]::WriteAllText(
        "$fixtureRoot\retracted.json",
        ($uncalSpec | ConvertTo-Json -Depth 20),
        $utf8NoBom)
      $env:IFQ_ENDPOINT_SPEC = "$fixtureRoot\retracted.json"
      Invoke-FijiGroovy "$repo\endpoints\evaluate_endpoints.groovy" `
        "$fixtureRoot\retracted.log" -ExpectFailure
      if (-not (Select-String -LiteralPath "$fixtureRoot\retracted.log" `
          -Pattern "marked RETRACTED" -Quiet)) {
        throw "Retracted endpoint failed without the expected diagnostic."
      }
      Write-Host "GUARD: PASS -- retracted endpoint refused"
    }
    finally {
      if ($KeepFixture) {
        Write-Host "Fixture kept at $fixtureRoot"
      } elseif (Test-Path -LiteralPath $fixtureRoot) {
        $resolved = (Resolve-Path -LiteralPath $fixtureRoot).Path
        $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
        if (-not $resolved.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase)) {
          throw "Refusing to remove fixture outside the temp directory: $resolved"
        }
        Remove-Item -LiteralPath $resolved -Recurse -Force
      }
    }
}
finally {
    Restore-IfqEnvSnapshot -Snapshot $ifqEnvBefore
}
