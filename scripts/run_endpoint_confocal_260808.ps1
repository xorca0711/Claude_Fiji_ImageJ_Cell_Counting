param(
  [string]$RunRoot = "D:\IFQ_Runs\confocal_260808",
  [string]$DataRoot = "D:\Microscopy_Images\260808-CW_Confocal\260808-CW",
  [switch]$AllowUncalibratedExploratory
)

# Relational endpoint for the 260808 confocal batch (whole fields, no RoiSet).
#
# Two stages, both OUTSIDE the frozen engine:
#   1. export_tissue_region_masks.groovy re-derives the auto_dapi tissue region
#      per field and PROVES it against run_summary.region_area_um2.
#   2. evaluate_endpoints.groovy runs with IFQ_ENDPOINT_REGION_MODE=tissue_mask,
#      which clips the mask algebra to those regions.
#
# Nothing is written to D:\Microscopy_Images (read only) or to <run>\analysis
# (the measured result). Masks land in <run>\tissue_masks.

$ErrorActionPreference = "Stop"
$repo = "X:\GitHub\IFQuant-Lung"
$data = [System.IO.Path]::GetFullPath($DataRoot)
$out  = [System.IO.Path]::GetFullPath($RunRoot)
$fj   = "X:\Fiji"
$jreItem = Get-ChildItem "$fj\java" -Recurse -Filter java.exe -ErrorAction SilentlyContinue |
           Select-Object -First 1
$patcher = Join-Path $fj "jars\ij1-patcher-2.0.0.jar"
if (-not $jreItem -or -not (Test-Path -LiteralPath $patcher -PathType Leaf)) {
  throw "Fiji Java runtime or ij1-patcher is missing under: $fj"
}
$jre = $jreItem.FullName
$specPath = "$repo\config\endpoints\dysplastic_over_damaged.json"
$spec = Get-Content -LiteralPath $specPath -Raw | ConvertFrom-Json
$t1a = $spec.parameters.t1a_threshold
$t1aStatus = if ($t1a.status) { $t1a.status.ToString().ToUpperInvariant() } else { "" }
$t1aCalibrated = ($null -ne $t1a.value) -and
                 ($t1aStatus.StartsWith("LOCKED") -or
                  $t1aStatus.StartsWith("CALIBRATED") -or
                  $t1aStatus.StartsWith("FIXED"))
if (-not $t1aCalibrated -and -not $AllowUncalibratedExploratory) {
  throw "T1A/PDPN is not calibrated. Refusing to create endpoint output. Use -AllowUncalibratedExploratory only for a labelled engineering run."
}
if (-not (Test-Path -LiteralPath "$out\analysis\run_summary.csv" -PathType Leaf)) {
  throw "Completed engine output is missing: $out\analysis\run_summary.csv"
}
if ((Test-Path -LiteralPath "$out\endpoint_areas.csv") -or
    (Test-Path -LiteralPath "$out\tissue_masks")) {
  throw "Refusing to overwrite existing endpoint output under: $out"
}

function Invoke-FijiScript($scriptPath, $logPath) {
  $ErrorActionPreference = "Continue"
  & $jre '--add-opens=java.base/java.lang=ALL-UNNAMED' `
    "-javaagent:$patcher=init" `
    '-Djava.awt.headless=true' "-Dplugins.dir=$fj" '-Xmx4g' `
    -cp "$fj\jars\*;$fj\plugins\*" net.imagej.Main --headless `
    --run $scriptPath 2>&1 | Out-File $logPath -Encoding utf8
  return $LASTEXITCODE
}

# ---- stage 1: tissue region masks ------------------------------------------
$env:IFQ_ANALYSIS_DIR     = "$out\analysis"
$env:IFQ_SOURCE_DIR       = $data
$env:IFQ_TISSUE_MASK_DIR  = "$out\tissue_masks"
$env:IFQ_PANEL_FILTER     = "LEFT"          # the endpoint is a LEFT-panel relation
$env:IFQ_TISSUE_RECON_TOL = "0.001"
$env:IFQ_MAX_FIELDS       = ""

$rc1 = Invoke-FijiScript "$repo\endpoints\export_tissue_region_masks.groovy" "$out\tissue_export.log"
"stage1 exit=$rc1"
if ($rc1 -ne 0) { "stage 1 FAILED - see $out\tissue_export.log"; exit 1 }

# ---- stage 2: relational endpoint ------------------------------------------
$env:IFQ_ENDPOINT_SPEC        = $specPath
$env:IFQ_ANALYSIS_DIR         = "$out\analysis"
$env:IFQ_ENDPOINT_REGION_MODE = "tissue_mask"
$env:IFQ_TISSUE_MASK_DIR      = "$out\tissue_masks"
$env:IFQ_ENDPOINT_OUT         = "$out\endpoint_areas.csv"
$env:IFQ_TILES_DIR            = ""          # not used in tissue_mask mode
$env:IFQ_ENDPOINT_ALLOW_UNCALIBRATED = if ($AllowUncalibratedExploratory) { "true" } else { "false" }

$rc2 = Invoke-FijiScript "$repo\endpoints\evaluate_endpoints.groovy" "$out\endpoint.log"
"stage2 exit=$rc2"
if ($rc2 -ne 0) { "stage 2 FAILED - see $out\endpoint.log"; exit $rc2 }

if ($AllowUncalibratedExploratory) {
  "WARNING: endpoint output is exploratory because T1A/PDPN is not calibrated."
}
exit 0
