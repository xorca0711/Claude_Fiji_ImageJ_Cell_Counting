param(
  [string]$OutputRoot = "D:\IFQ_Runs\confocal_260808",
  [string]$DataRoot = "D:\Microscopy_Images\260808-CW_Confocal\260808-CW",
  [string]$PanelMapPath = "D:\IFQ_Runs\confocal_visual_panels_Retouch_80\panel_map.csv"
)

$ErrorActionPreference = "Stop"
$repo = "X:\GitHub\IFQuant-Lung"
$data = [System.IO.Path]::GetFullPath($DataRoot)
$out  = [System.IO.Path]::GetFullPath($OutputRoot)
$fj   = "X:\Fiji"
$jreItem = Get-ChildItem "$fj\java" -Recurse -Filter java.exe -ErrorAction SilentlyContinue |
           Select-Object -First 1
$patcher = Join-Path $fj "jars\ij1-patcher-2.0.0.jar"
if (-not $jreItem -or -not (Test-Path -LiteralPath $patcher -PathType Leaf)) {
  throw "Fiji Java runtime or ij1-patcher is missing under: $fj"
}
$jre = $jreItem.FullName
$panelMap = [System.IO.Path]::GetFullPath($PanelMapPath)

if (-not (Test-Path -LiteralPath "$data\samplesheet.csv" -PathType Leaf)) {
  throw "The engine requires samplesheet.csv in the input directory: $data"
}
if (-not (Test-Path -LiteralPath $panelMap -PathType Leaf)) {
  throw "Panel map not found: $panelMap"
}
if (Test-Path -LiteralPath $out) {
  $existing = @(Get-ChildItem -LiteralPath $out -Force -ErrorAction SilentlyContinue)
  if ($existing.Count -gt 0) {
    throw "Refusing to reuse a non-empty run root: $out"
  }
}
New-Item -ItemType Directory -Path $out -Force | Out-Null
Copy-Item -LiteralPath "$data\samplesheet.csv" -Destination "$out\samplesheet.csv" -Force
Copy-Item -LiteralPath $panelMap -Destination "$out\panel_map.csv"

$env:IFQ_INPUT_DIR      = $data
$env:IFQ_OUTPUT_DIR     = "$out\analysis"
$env:IFQ_ENGINE_SCRIPT_PATH = "$repo\IF_Quant_Pipeline.groovy"
$env:IFQ_RECURSIVE      = "true"
# IFQ_INCLUDE_REGEX is a FULL match against the ABSOLUTE PATH. Without it,
# recursive discovery also picks up the 288 4x navigation fields, which are not
# analysis images and have no panel-map row -- the engine then fail-closes.
$env:IFQ_INCLUDE_REGEX  = ".*20x 2k.*\.oir"
$env:IFQ_PANEL_MAP_PATH = "$out\panel_map.csv"   # authoritative; every image needs a row
$env:IFQ_MARKER_REGISTRY = "$repo\config\lung_marker_registry.json"
$env:IFQ_SEGMENTER      = "classic"
$env:IFQ_PROJECTION     = "max"                   # SizeZ=1, so no projection actually runs
# Area-preserving tradeoff: sparse tissue fields remain measurable, but a zero
# floor cannot catch a nucleus-segmentation collapse. Candidate-acceptance QC
# and plausibility review are mandatory (docs/AI_HANDOFF.md section 7).
$env:IFQ_MIN_INCLUDED_NUCLEI = "0"

# KRT5 frozen from the nominal UNINFECTED CONTROLS ONLY.
# Pooled in-tissue p99.99 was 283 (M4-2) and 255 (M6); 300 sits just above both,
# giving a false-positive area fraction <= 1e-4 in each. M6 LEFT is an
# established staining failure, however, so this calibration rests on the one
# sound control (M4-2) and must be re-derived. Infected M2 has 8.1% of tissue
# above 500, so the observed separation is 3+ orders.
$env:IFQ_KRT5_THRESHOLD = "300"

# AGER and T1A are deliberately LEFT ADAPTIVE. They are constitutively expressed,
# so "the control should be negative" gives no calibration handle for them. The
# engine will mark their calls exploratory_* which is the honest label.

$ErrorActionPreference = "Continue"
& $jre '--add-opens=java.base/java.lang=ALL-UNNAMED' `
  "-javaagent:$patcher=init" `
  '-Djava.awt.headless=true' "-Dplugins.dir=$fj" '-Xmx4g' `
  -cp "$fj\jars\*;$fj\plugins\*" net.imagej.Main --headless `
  --run "$repo\IF_Quant_Pipeline.groovy" 2>&1 |
  Out-File "$out\engine.log" -Encoding utf8

$javaExit = $LASTEXITCODE
$manifestPath = Join-Path $out "analysis\run_manifest.json"
$manifestStatus = "absent"
$publicationStatus = "absent"
$manifestAccepted = $false
if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
  try {
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding utf8 |
                ConvertFrom-Json -ErrorAction Stop
    $manifestStatus = [string]$manifest.status
    $publicationStatus = [string]$manifest.publication_status
    $manifestAccepted =
      $manifestStatus -eq "complete" -and
      $publicationStatus -eq "sealed_complete"
  } catch {
    $manifestStatus = "unparseable"
    $publicationStatus = "unparseable"
    [Console]::Error.WriteLine(
      "Run manifest is unreadable after Fiji returned: " + $_.Exception.Message)
  }
}

$finalExit = $javaExit
if ($finalExit -eq 0 -and -not $manifestAccepted) {
  # SciJava can log a script exception yet return zero from the Java process.
  # Only the engine's audit-last, sealed-complete manifest can authorize a
  # successful wrapper exit. Incomplete/partial manifests stay available for
  # QC, but the caller receives a failure status.
  $finalExit = 1
  [Console]::Error.WriteLine(
    "Fiji returned zero without a sealed-complete run manifest; " +
    "status=$manifestStatus publication_status=$publicationStatus")
}
"java_exit=$javaExit"
"manifest_status=$manifestStatus"
"publication_status=$publicationStatus"
"exit=$finalExit"
exit $finalExit
