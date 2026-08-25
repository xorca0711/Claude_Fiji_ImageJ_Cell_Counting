<#
.SYNOPSIS
    Run STAGE 2 (IF_Quant_Pipeline.groovy) over a Stage 1 tile folder in N
    parallel shards.

.DESCRIPTION
    Stage 2 costs roughly 2-3 minutes per tile. A whole slide is ~370 tiles, so
    a single-threaded run is 12-15 hours. The Fiji engine loops over files with
    a per-file try/catch and never carries state between images, so tiles can be
    split across independent processes safely.

    Sharding uses NTFS HARD LINKS, so no image data is copied and no extra disk
    is used. Each shard gets its own tiles folder containing links to its tiles
    AND to their companion "<stem>.ome_RoiSet.zip" files, plus its own
    samplesheet.csv carved from the Stage 1 one.

    After every shard completes, build_stage2_run_index.py writes an explicit,
    hashed stage2_run_index.json. Stage 3 consumes only those declared outputs;
    it never guesses which retry or sibling analysis folder is authoritative.

    THIS SCRIPT DOES NOT SET THRESHOLDS. Pass calibrated values, or the engine
    falls back to per-tile adaptive Otsu, which on a mostly-background tile
    reports KRT5_pod_area_frac ~0.89. See docs/WSI_TILING_WORKFLOW.md section 7.

.EXAMPLE
    .\scripts\Invoke-Stage2Sharded.ps1 `
        -TilesDir   "D:\IFQ_Runs\<run_name>\slideA\tiles" `
        -OutputRoot "D:\IFQ_Runs\<run_name>\slideA" `
        -Shards 5 `
        -Krt5Threshold 400 -AgerThreshold 600 -T1aThreshold 400
#>
param(
    [Parameter(Mandatory = $true)][string]$TilesDir,
    [Parameter(Mandatory = $true)][string]$OutputRoot,
    [int]$Shards = 4,
    [string]$FijiDir = "X:\Fiji",
    [string]$ScriptPath = (Join-Path (Split-Path $PSScriptRoot -Parent) 'IF_Quant_Pipeline.groovy'),
    [string]$Panel = "LEFT",
    [string]$Segmenter = "classic",
    [string]$Krt5Threshold = "",
    [string]$AgerThreshold = "",
    [string]$T1aThreshold = "",
    [string]$JavaXmx = "8g",
    [string]$PythonExe = "python",
    [switch]$ReplaceExistingShards
)

$ErrorActionPreference = 'Stop'

function Get-NormalizedFullPath {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)
    return [System.IO.Path]::GetFullPath($LiteralPath)
}

function Test-IsDirectChildPath {
    param(
        [Parameter(Mandatory = $true)][string]$Candidate,
        [Parameter(Mandatory = $true)][string]$Parent
    )
    $candidateFull = Get-NormalizedFullPath $Candidate
    $parentFull = Get-NormalizedFullPath $Parent
    $candidateParent = [System.IO.Path]::GetDirectoryName($candidateFull)
    return [string]::Equals(
        $candidateParent,
        $parentFull,
        [System.StringComparison]::OrdinalIgnoreCase
    )
}

function Assert-DirectChildPath {
    param(
        [Parameter(Mandatory = $true)][string]$Candidate,
        [Parameter(Mandatory = $true)][string]$Parent,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if (-not (Test-IsDirectChildPath -Candidate $Candidate -Parent $Parent)) {
        throw "$Label must resolve directly inside $Parent; got $Candidate"
    }
}

function Test-IsWithinOrEqualPath {
    param(
        [Parameter(Mandatory = $true)][string]$Candidate,
        [Parameter(Mandatory = $true)][string]$Root
    )
    $candidateFull = Get-NormalizedFullPath $Candidate
    $rootFull = Get-NormalizedFullPath $Root
    if ([string]::Equals($candidateFull, $rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    $trimChars = [char[]]@(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $rootPrefix = $rootFull.TrimEnd($trimChars) + [System.IO.Path]::DirectorySeparatorChar
    return $candidateFull.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)
}

function Assert-NoReparsePointInExistingChain {
    param(
        [Parameter(Mandatory = $true)][string]$Candidate,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $current = Get-NormalizedFullPath $Candidate
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "$Label crosses a reparse-point path at $current. Use the physical path."
            }
        }
        $parent = [System.IO.Directory]::GetParent($current)
        if ($null -eq $parent) { break }
        $parentPath = $parent.FullName
        if ([string]::Equals(
                $parentPath,
                $current,
                [System.StringComparison]::OrdinalIgnoreCase)) { break }
        $current = $parentPath
    }
}

function Assert-NoReparsePointTree {
    param(
        [Parameter(Mandatory = $true)][string]$Candidate,
        [Parameter(Mandatory = $true)][string]$Label
    )
    Assert-NoReparsePointInExistingChain -Candidate $Candidate -Label $Label
    if (-not (Test-Path -LiteralPath $Candidate -PathType Container)) { return }

    $pending = [System.Collections.Generic.Stack[string]]::new()
    $pending.Push((Get-NormalizedFullPath $Candidate))
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        foreach ($child in @(Get-ChildItem -LiteralPath $directory -Force)) {
            if (($child.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "$Label contains a reparse point at $($child.FullName); recursive replacement is unsafe."
            }
            if ($child.PSIsContainer) { $pending.Push($child.FullName) }
        }
    }
}

function Test-SafeBasename {
    param([AllowEmptyString()][string]$Name)
    if ([string]::IsNullOrWhiteSpace($Name)) { return $false }
    if ($Name -ne $Name.Trim()) { return $false }
    if ($Name.EndsWith('.')) { return $false }
    if ($Name -eq '.' -or $Name -eq '..') { return $false }
    if ([System.IO.Path]::IsPathRooted($Name)) { return $false }
    if ($Name.Contains('\') -or $Name.Contains('/')) { return $false }
    if ($Name.IndexOfAny([System.IO.Path]::GetInvalidFileNameChars()) -ge 0) { return $false }
    return [System.IO.Path]::GetFileName($Name) -ceq $Name
}

$tilesCandidate = Get-NormalizedFullPath $TilesDir
$scriptCandidate = Get-NormalizedFullPath $ScriptPath
$outputCandidate = Get-NormalizedFullPath $OutputRoot
Assert-NoReparsePointInExistingChain -Candidate $tilesCandidate -Label 'TilesDir'
Assert-NoReparsePointInExistingChain -Candidate $scriptCandidate -Label 'Stage 2 script'
Assert-NoReparsePointInExistingChain -Candidate $outputCandidate -Label 'OutputRoot'
$TilesDir   = (Resolve-Path -LiteralPath $tilesCandidate).Path
$ScriptPath = (Resolve-Path -LiteralPath $scriptCandidate).Path
if (-not (Test-Path -LiteralPath $outputCandidate)) {
    New-Item -ItemType Directory -Path $outputCandidate -Force | Out-Null
}
$OutputRoot = (Resolve-Path -LiteralPath $OutputRoot).Path
Assert-NoReparsePointInExistingChain -Candidate $TilesDir -Label 'TilesDir'
Assert-NoReparsePointInExistingChain -Candidate $ScriptPath -Label 'Stage 2 script'
Assert-NoReparsePointInExistingChain -Candidate $OutputRoot -Label 'OutputRoot'

if (Test-IsWithinOrEqualPath -Candidate $OutputRoot -Root $TilesDir) {
    throw "OutputRoot must not be the Stage 1 TilesDir or a descendant of it."
}

$samplesheet = Join-Path $TilesDir 'samplesheet.csv'
if (-not (Test-Path -LiteralPath $samplesheet -PathType Leaf)) {
    throw "No samplesheet.csv in $TilesDir. Stage 1 writes it; do not run Stage 2 without it (mouse_id would become 'NA')."
}
$samplesheet = (Resolve-Path -LiteralPath $samplesheet).Path
Assert-DirectChildPath -Candidate $samplesheet -Parent $TilesDir -Label 'Stage 1 samplesheet'

# Fail before preserving/removing any prior output or launching Fiji if the
# provenance builder cannot run or the Stage 1 authority is unavailable.
$indexBuilder = Join-Path $PSScriptRoot 'build_stage2_run_index.py'
$stage1Manifest = Join-Path (Split-Path $OutputRoot -Parent) 'stage1_manifest.json'
if (-not (Test-Path -LiteralPath $stage1Manifest -PathType Leaf)) {
    throw "Stage 1 manifest not found beside the slide folder: $stage1Manifest"
}
$stage1Manifest = (Resolve-Path -LiteralPath $stage1Manifest).Path
if (-not (Test-Path -LiteralPath $indexBuilder -PathType Leaf)) {
    throw "Stage 2 index builder not found: $indexBuilder"
}
$indexBuilder = (Resolve-Path -LiteralPath $indexBuilder).Path
Assert-NoReparsePointInExistingChain -Candidate $stage1Manifest -Label 'Stage 1 manifest'
Assert-NoReparsePointInExistingChain -Candidate $indexBuilder -Label 'Stage 2 index builder'

$tileManifest = Join-Path $OutputRoot 'tile_manifest.csv'
if (-not (Test-Path -LiteralPath $tileManifest -PathType Leaf)) {
    throw "Stage 1 tile manifest not found in the slide folder: $tileManifest"
}
$tileManifest = (Resolve-Path -LiteralPath $tileManifest).Path
Assert-DirectChildPath -Candidate $tileManifest -Parent $OutputRoot -Label 'Stage 1 tile manifest'
Assert-NoReparsePointInExistingChain -Candidate $tileManifest -Label 'Stage 1 tile manifest'

# Execute a content-addressed byte snapshot, not the mutable repository script.
# Every shard and the index builder use this exact filename/byte payload.
$scriptBytes = [System.IO.File]::ReadAllBytes($ScriptPath)
$sha256 = [System.Security.Cryptography.SHA256]::Create()
try {
    $scriptHash = (($sha256.ComputeHash($scriptBytes) | ForEach-Object {
        $_.ToString('x2')
    }) -join '')
} finally {
    $sha256.Dispose()
}
$snapshotDir = Get-NormalizedFullPath (
    Join-Path $OutputRoot ("stage2_engine_snapshot_" + $scriptHash)
)
Assert-DirectChildPath -Candidate $snapshotDir -Parent $OutputRoot -Label 'Engine snapshot directory'
Assert-NoReparsePointInExistingChain -Candidate $snapshotDir -Label 'Engine snapshot directory'
if (-not (Test-Path -LiteralPath $snapshotDir)) {
    New-Item -ItemType Directory -Path $snapshotDir | Out-Null
}
Assert-NoReparsePointTree -Candidate $snapshotDir -Label 'Engine snapshot directory'
$ExecutionScriptPath = Get-NormalizedFullPath (
    Join-Path $snapshotDir ([System.IO.Path]::GetFileName($ScriptPath))
)
Assert-DirectChildPath -Candidate $ExecutionScriptPath -Parent $snapshotDir -Label 'Engine snapshot'
if (Test-Path -LiteralPath $ExecutionScriptPath) {
    if (-not (Test-Path -LiteralPath $ExecutionScriptPath -PathType Leaf)) {
        throw "Engine snapshot path is not a regular file: $ExecutionScriptPath"
    }
    $existingSnapshotHash = (Get-FileHash -LiteralPath $ExecutionScriptPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($existingSnapshotHash -ne $scriptHash) {
        throw "Existing content-addressed engine snapshot has the wrong hash: $ExecutionScriptPath"
    }
} else {
    $snapshotTemp = $ExecutionScriptPath + '.tmp.' + [Guid]::NewGuid().ToString('N')
    Assert-DirectChildPath -Candidate $snapshotTemp -Parent $snapshotDir -Label 'Temporary engine snapshot'
    try {
        [System.IO.File]::WriteAllBytes($snapshotTemp, $scriptBytes)
        Move-Item -LiteralPath $snapshotTemp -Destination $ExecutionScriptPath
    } finally {
        if (Test-Path -LiteralPath $snapshotTemp) {
            Remove-Item -LiteralPath $snapshotTemp -Force
        }
    }
}
if ((Get-FileHash -LiteralPath $ExecutionScriptPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $scriptHash) {
    throw "Engine snapshot verification failed: $ExecutionScriptPath"
}
$ExecutionScriptPath = (Resolve-Path -LiteralPath $ExecutionScriptPath).Path
Assert-NoReparsePointInExistingChain -Candidate $ExecutionScriptPath -Label 'Engine snapshot'

try {
    $pythonCommand = Get-Command -Name $PythonExe -CommandType Application -ErrorAction Stop |
                     Select-Object -First 1
} catch {
    throw "Python executable not found: $PythonExe"
}
if (-not $pythonCommand) { throw "Python executable not found: $PythonExe" }
$PythonExe = $pythonCommand.Source
Assert-NoReparsePointInExistingChain -Candidate $PythonExe -Label 'Python executable'
try {
    $pythonPreflightOutput = @(& $PythonExe $indexBuilder '--help' 2>&1)
    $pythonPreflightExit = $LASTEXITCODE
} catch {
    throw "Stage 2 index builder could not start with $PythonExe : $($_.Exception.Message)"
}
if ($pythonPreflightExit -ne 0) {
    throw ("Stage 2 index builder preflight failed (exit $pythonPreflightExit): " +
           ($pythonPreflightOutput -join [Environment]::NewLine))
}

# Hard links only work within one volume.
if ((Split-Path $TilesDir -Qualifier) -ne (Split-Path $OutputRoot -Qualifier)) {
    throw "TilesDir ($TilesDir) and OutputRoot ($OutputRoot) must be on the same volume for hard links."
}

if (-not $Krt5Threshold -or -not $AgerThreshold -or -not $T1aThreshold) {
    Write-Warning ("No fixed thresholds supplied for one or more markers. The engine will use " +
                   "per-tile adaptive Otsu and every call will be exploratory. On background-dominated " +
                   "tiles this manufactures large false pod areas. Supply calibrated thresholds " +
                   "before any confirmatory run.")
}

$rows = @(Import-Csv -LiteralPath $samplesheet)
if ($rows.Count -eq 0) { throw "samplesheet.csv has no rows." }
Write-Host "Tiles in samplesheet : $($rows.Count)"

$FijiDir = (Resolve-Path -LiteralPath $FijiDir).Path
Assert-NoReparsePointInExistingChain -Candidate $FijiDir -Label 'FijiDir'
$java = Get-ChildItem (Join-Path $FijiDir 'java') -Recurse -Filter java.exe -ErrorAction SilentlyContinue |
        Select-Object -First 1
if (-not $java) { throw "No java.exe found under $FijiDir\java" }
$patcher = Get-ChildItem (Join-Path $FijiDir 'jars') -Filter 'ij1-patcher-*.jar' -ErrorAction SilentlyContinue |
           Select-Object -First 1
if (-not $patcher) { throw "No ij1-patcher-*.jar found in $FijiDir\jars (required for headless ImageJ1)" }

if ($Shards -lt 1) { $Shards = 1 }
if ($Shards -gt $rows.Count) { $Shards = $rows.Count }

# Resolve every output path and every samplesheet-declared input before any
# prior shard is removed. Samplesheet filenames are data, not trusted paths:
# each one must be a basename and each resulting hard-link endpoint must remain
# a direct child of its declared source/destination directory.
$indexPath = Get-NormalizedFullPath (Join-Path $OutputRoot 'stage2_run_index.json')
Assert-DirectChildPath -Candidate $indexPath -Parent $OutputRoot -Label 'Stage 2 index'

$shardLayouts = @()
$outputPaths = [System.Collections.Generic.HashSet[string]]::new(
    [System.StringComparer]::OrdinalIgnoreCase
)
for ($i = 0; $i -lt $Shards; $i++) {
    $tag = 'shard_{0:d2}' -f ($i + 1)
    $shardRoot = Get-NormalizedFullPath (Join-Path $OutputRoot $tag)
    $shardIn = Get-NormalizedFullPath (Join-Path $shardRoot 'tiles')
    $shardOut = Get-NormalizedFullPath (Join-Path $OutputRoot "analysis_$tag")
    $logPath = Get-NormalizedFullPath (Join-Path $OutputRoot ("stage2_" + $tag + ".log"))

    Assert-DirectChildPath -Candidate $shardRoot -Parent $OutputRoot -Label "$tag root"
    Assert-DirectChildPath -Candidate $shardIn -Parent $shardRoot -Label "$tag input"
    Assert-DirectChildPath -Candidate $shardOut -Parent $OutputRoot -Label "$tag analysis output"
    Assert-DirectChildPath -Candidate $logPath -Parent $OutputRoot -Label "$tag log"

    foreach ($candidate in @($shardRoot, $shardIn, $shardOut, $logPath)) {
        if (-not $outputPaths.Add($candidate)) {
            throw "Output layout collision at $candidate"
        }
    }
    foreach ($mutableRoot in @($shardRoot, $shardOut)) {
        foreach ($protectedPath in @(
            $TilesDir,
            $samplesheet,
            $ScriptPath,
            $ExecutionScriptPath,
            $indexBuilder,
            $stage1Manifest,
            $PythonExe,
            $java.FullName,
            $patcher.FullName
        )) {
            if (Test-IsWithinOrEqualPath -Candidate $protectedPath -Root $mutableRoot) {
                throw "Unsafe output layout: replacing $mutableRoot would remove protected input $protectedPath"
            }
        }
    }
    $shardLayouts += [pscustomobject]@{
        Tag = $tag
        Root = $shardRoot
        In = $shardIn
        Out = $shardOut
        Log = $logPath
    }
}

$plannedRows = @()
$seenFilenames = [System.Collections.Generic.HashSet[string]]::new(
    [System.StringComparer]::OrdinalIgnoreCase
)
$seenDestinations = [System.Collections.Generic.HashSet[string]]::new(
    [System.StringComparer]::OrdinalIgnoreCase
)
for ($k = 0; $k -lt $rows.Count; $k++) {
    $row = $rows[$k]
    $filename = [string]$row.filename
    $csvRow = $k + 2
    if (-not (Test-SafeBasename $filename)) {
        throw "Unsafe filename in samplesheet.csv row ${csvRow}: '$filename'. Expected a plain basename with no root, traversal, or separators."
    }
    if (-not $seenFilenames.Add($filename)) {
        throw "Duplicate filename in samplesheet.csv row ${csvRow}: '$filename'"
    }

    $tifCandidate = Join-Path $TilesDir $filename
    if (-not (Test-Path -LiteralPath $tifCandidate -PathType Leaf)) {
        throw "Tile listed in samplesheet.csv is missing: $tifCandidate"
    }
    $tifItem = Get-Item -LiteralPath $tifCandidate
    if (($tifItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Tile in samplesheet.csv row $csvRow is a reparse point; hard-link sources must be regular files inside TilesDir."
    }
    $tif = (Resolve-Path -LiteralPath $tifCandidate).Path
    Assert-DirectChildPath -Candidate $tif -Parent $TilesDir -Label "Tile in samplesheet.csv row $csvRow"

    # The engine strips only the FINAL extension: foo.ome.tif -> foo.ome_RoiSet.zip
    $stem = [System.IO.Path]::GetFileNameWithoutExtension($filename)
    $roiFilename = $stem + '_RoiSet.zip'
    if (-not (Test-SafeBasename $roiFilename)) {
        throw "Unsafe companion ROI basename derived from samplesheet.csv row ${csvRow}: '$roiFilename'"
    }
    $roiCandidate = Join-Path $TilesDir $roiFilename
    if (-not (Test-Path -LiteralPath $roiCandidate -PathType Leaf)) {
        throw ("Missing companion ROI for ${filename}: expected $roiCandidate . Without it the engine " +
               "falls back to auto tissue detection or to the whole halo-inclusive frame, " +
               "double-counting every seam.")
    }
    $roiItem = Get-Item -LiteralPath $roiCandidate
    if (($roiItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Companion ROI for samplesheet.csv row $csvRow is a reparse point; hard-link sources must be regular files inside TilesDir."
    }
    $roi = (Resolve-Path -LiteralPath $roiCandidate).Path
    Assert-DirectChildPath -Candidate $roi -Parent $TilesDir -Label "Companion ROI for samplesheet.csv row $csvRow"

    $layout = $shardLayouts[$k % $Shards]
    $tifDestination = Get-NormalizedFullPath (Join-Path $layout.In $filename)
    $roiDestination = Get-NormalizedFullPath (Join-Path $layout.In $roiFilename)
    Assert-DirectChildPath -Candidate $tifDestination -Parent $layout.In -Label "Tile hard-link destination for row $csvRow"
    Assert-DirectChildPath -Candidate $roiDestination -Parent $layout.In -Label "ROI hard-link destination for row $csvRow"
    foreach ($destination in @($tifDestination, $roiDestination)) {
        if (-not $seenDestinations.Add($destination)) {
            throw "Hard-link destination collision from samplesheet.csv row ${csvRow}: $destination"
        }
    }
    $plannedRows += [pscustomobject]@{
        ShardIndex = ($k % $Shards)
        Row = $row
        Filename = $filename
        TifSource = $tif
        RoiSource = $roi
        TifDestination = $tifDestination
        RoiDestination = $roiDestination
    }
}

# Refuse an accidental overwrite before creating any shard. A retry must be an
# explicit operator decision because an abandoned sibling output is precisely
# what the Stage 2 index is designed to keep out of analytical aggregation.
$existingShardPaths = @()
foreach ($layout in $shardLayouts) {
    foreach ($candidate in @($layout.Root, $layout.Out, $layout.Log)) {
        if (Test-Path -LiteralPath $candidate) { $existingShardPaths += $candidate }
    }
    foreach ($recursiveTarget in @($layout.Root, $layout.Out)) {
        if (Test-Path -LiteralPath $recursiveTarget) {
            $targetItem = Get-Item -LiteralPath $recursiveTarget
            if (($targetItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Refusing to recursively replace reparse-point output path: $recursiveTarget"
            }
        }
    }
    if ((Test-Path -LiteralPath $layout.Log) -and
        (Get-Item -LiteralPath $layout.Log).PSIsContainer) {
        throw "Unsafe output layout: expected a log file but found a directory at $($layout.Log)"
    }
}
if ($existingShardPaths.Count -gt 0 -and -not $ReplaceExistingShards) {
    throw ("Existing shard paths would be replaced: " + ($existingShardPaths -join ', ') +
           ". Choose a new OutputRoot or pass -ReplaceExistingShards explicitly.")
}

if (Test-Path -LiteralPath $indexPath) {
    if (-not (Test-Path -LiteralPath $indexPath -PathType Leaf)) {
        throw "Unsafe output layout: expected the Stage 2 index to be a file at $indexPath"
    }
    if (-not $ReplaceExistingShards) {
        throw "A Stage 2 index already exists: $indexPath. Choose a new OutputRoot or pass -ReplaceExistingShards."
    }
    $stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
    $staleIndex = Join-Path $OutputRoot ("stage2_run_index.STALE_" + $stamp + ".json")
    Assert-DirectChildPath -Candidate $staleIndex -Parent $OutputRoot -Label 'Preserved Stage 2 index'
    if (Test-Path -LiteralPath $staleIndex) {
        throw "Cannot preserve the prior Stage 2 index because the stale destination already exists: $staleIndex"
    }
    Move-Item -LiteralPath $indexPath -Destination $staleIndex
    Write-Host "  preserved prior index -> $staleIndex"
}

# ---- build shards -------------------------------------------------------
$shardDirs = @()
for ($i = 0; $i -lt $Shards; $i++) {
    $layout = $shardLayouts[$i]
    $tag = $layout.Tag
    $shardRoot = $layout.Root
    $shardIn = $layout.In
    $shardOut = $layout.Out
    $logPath = $layout.Log
    if (Test-Path -LiteralPath $shardRoot) {
        Assert-NoReparsePointTree -Candidate $shardRoot -Label "$tag shard input tree"
        Remove-Item -LiteralPath $shardRoot -Recurse -Force
    }
    if (Test-Path -LiteralPath $shardOut) {
        Assert-NoReparsePointTree -Candidate $shardOut -Label "$tag analysis output tree"
        Remove-Item -LiteralPath $shardOut -Recurse -Force
    }
    if (Test-Path -LiteralPath $logPath) { Remove-Item -LiteralPath $logPath -Force }
    New-Item -ItemType Directory -Path $shardIn -Force | Out-Null
    $resolvedShardIn = (Resolve-Path -LiteralPath $shardIn).Path
    if (-not [string]::Equals(
            $resolvedShardIn,
            $shardIn,
            [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Shard input directory resolved somewhere unexpected: $shardIn -> $resolvedShardIn"
    }

    $minePlans = @($plannedRows | Where-Object { $_.ShardIndex -eq $i })
    foreach ($plan in $minePlans) {
        # Re-resolve immediately before link creation to preserve the containment
        # guarantee even if the filesystem changed after the initial preflight.
        if (-not (Test-Path -LiteralPath $plan.TifSource -PathType Leaf)) {
            throw "Preflighted tile disappeared before hard-link creation: $($plan.TifSource)"
        }
        if (-not (Test-Path -LiteralPath $plan.RoiSource -PathType Leaf)) {
            throw "Preflighted ROI disappeared before hard-link creation: $($plan.RoiSource)"
        }
        foreach ($sourcePath in @($plan.TifSource, $plan.RoiSource)) {
            $sourceItem = Get-Item -LiteralPath $sourcePath
            if (($sourceItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Preflighted hard-link source became a reparse point: $sourcePath"
            }
        }
        $tifSource = (Resolve-Path -LiteralPath $plan.TifSource).Path
        $roiSource = (Resolve-Path -LiteralPath $plan.RoiSource).Path
        Assert-DirectChildPath -Candidate $tifSource -Parent $TilesDir -Label 'Tile hard-link source'
        Assert-DirectChildPath -Candidate $roiSource -Parent $TilesDir -Label 'ROI hard-link source'
        Assert-DirectChildPath -Candidate $plan.TifDestination -Parent $resolvedShardIn -Label 'Tile hard-link destination'
        Assert-DirectChildPath -Candidate $plan.RoiDestination -Parent $resolvedShardIn -Label 'ROI hard-link destination'

        New-Item -ItemType HardLink -Path $plan.TifDestination -Target $tifSource | Out-Null
        New-Item -ItemType HardLink -Path $plan.RoiDestination -Target $roiSource | Out-Null
    }
    $mine = @($minePlans | ForEach-Object { $_.Row })
    $shardSamplesheet = Get-NormalizedFullPath (Join-Path $resolvedShardIn 'samplesheet.csv')
    Assert-DirectChildPath -Candidate $shardSamplesheet -Parent $resolvedShardIn -Label 'Shard samplesheet'
    $mine | Export-Csv -LiteralPath $shardSamplesheet -NoTypeInformation -Encoding UTF8
    Write-Host ("  {0}: {1} tiles -> {2}" -f $tag, $mine.Count, $shardOut)
    $shardDirs += [pscustomobject]@{
        Tag = $tag
        In = $resolvedShardIn
        Out = $shardOut
        Log = $logPath
        N = $mine.Count
    }
}

# ---- launch -------------------------------------------------------------
$lockedInputs = [System.Collections.Generic.List[System.IO.FileStream]]::new()
try {
    # Deny writes/deletes while Fiji and the index builder read the declared
    # inputs. Hard links refer to the same file records, so locking the source
    # paths also freezes every shard's tile and ROI bytes.
    $lockPaths = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    foreach ($pathToLock in @(
        $ExecutionScriptPath,
        $stage1Manifest,
        $tileManifest
    )) { [void]$lockPaths.Add($pathToLock) }
    foreach ($plan in $plannedRows) {
        [void]$lockPaths.Add($plan.TifSource)
        [void]$lockPaths.Add($plan.RoiSource)
    }
    foreach ($shard in $shardDirs) {
        [void]$lockPaths.Add((Join-Path $shard.In 'samplesheet.csv'))
    }
    foreach ($pathToLock in $lockPaths) {
        $lockedInputs.Add([System.IO.File]::Open(
            $pathToLock,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::Read
        ))
    }

$cp = (Join-Path $FijiDir 'jars\*') + ';' + (Join-Path $FijiDir 'plugins\*')
$procs = @()
foreach ($s in $shardDirs) {
    $envPairs = @{
        IFQ_INPUT_DIR          = $s.In
        IFQ_OUTPUT_DIR         = $s.Out
        IFQ_PANEL              = $Panel
        IFQ_SEGMENTER          = $Segmenter
        IFQ_MIN_INCLUDED_NUCLEI = '0'
    }
    if ($Krt5Threshold) { $envPairs['IFQ_KRT5_THRESHOLD'] = $Krt5Threshold }
    if ($AgerThreshold) { $envPairs['IFQ_AGER_THRESHOLD'] = $AgerThreshold }
    if ($T1aThreshold)  { $envPairs['IFQ_T1A_THRESHOLD']  = $T1aThreshold }

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $java.FullName
    foreach ($a in @(
        '--add-opens=java.base/java.lang=ALL-UNNAMED',
        ("-javaagent:" + $patcher.FullName + "=init"),
        '-Djava.awt.headless=true',
        ("-Dplugins.dir=" + $FijiDir),
        ("-Xmx" + $JavaXmx),
        '-cp', $cp,
        'net.imagej.Main', '--headless', '--run', $ExecutionScriptPath)) {
        $psi.ArgumentList.Add($a)
    }
    foreach ($k in $envPairs.Keys) { $psi.EnvironmentVariables[$k] = $envPairs[$k] }
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError  = $true

    $logPath = $s.Log
    $p = [System.Diagnostics.Process]::Start($psi)
    # Drain both pipes asynchronously or a full buffer deadlocks the child.
    # Freeze the per-shard log path into each callback; a loop-variable closure
    # would otherwise let concurrent shards write into whichever path was last.
    $callbackLogPath = $logPath
    $outputCallback = {
        param($sender, $e)
        if ($e.Data) { Add-Content -LiteralPath $callbackLogPath -Value $e.Data }
    }.GetNewClosure()
    $errorCallback = {
        param($sender, $e)
        if ($e.Data) { Add-Content -LiteralPath $callbackLogPath -Value $e.Data }
    }.GetNewClosure()
    $p.add_OutputDataReceived($outputCallback)
    $p.add_ErrorDataReceived($errorCallback)
    $p.BeginOutputReadLine(); $p.BeginErrorReadLine()
    Write-Host ("  launched {0} (pid {1}) -> {2}" -f $s.Tag, $p.Id, $logPath)
    $procs += [pscustomobject]@{
        Shard = $s
        Proc = $p
        Log = $logPath
        OutputCallback = $outputCallback
        ErrorCallback = $errorCallback
    }
}

Write-Host ""
Write-Host "Running $($procs.Count) shard(s). This is the long step."
foreach ($x in $procs) { $x.Proc.WaitForExit() }

Write-Host ""
$bad = 0
foreach ($x in $procs) {
    $summary = Join-Path $x.Shard.Out 'run_summary.csv'
    $nRows = 0
    $nSections = 0
    if (Test-Path -LiteralPath $summary) {
        $summaryRows = @(Import-Csv -LiteralPath $summary)
        $nRows = $summaryRows.Count
        $nSections = @($summaryRows | ForEach-Object { $_.section_id } | Sort-Object -Unique).Count
    }
    # exit code 1 means "at least one image failed", NOT "no results" -- outputs
    # are written before the terminal failRun. Parse run_manifest.json instead.
    $status = 'unknown'
    $manifest = Join-Path $x.Shard.Out 'run_manifest.json'
    if (Test-Path -LiteralPath $manifest) {
        try { $status = (Get-Content -LiteralPath $manifest -Raw | ConvertFrom-Json).status } catch { $status = 'unparseable' }
    }
    Write-Host ("  {0}: exit={1} status={2} sections={3}/{4} region_rows={5}  log={6}" -f `
        $x.Shard.Tag, $x.Proc.ExitCode, $status, $nSections, $x.Shard.N, $nRows, $x.Log)
    if ($x.Proc.ExitCode -ne 0 -or $status -ne 'complete' -or $nSections -ne $x.Shard.N) { $bad++ }
}

Write-Host ""
if ($bad -gt 0) {
    throw ("$bad shard(s) failed exit/status/unique-section reconciliation. No Stage 2 index " +
           "was published; a partial run must not enter Stage 3.")
} else {
    Write-Host "All shards covered every assigned tile (partitioned tiles may emit multiple region rows)."
}

if ((Get-FileHash -LiteralPath $ExecutionScriptPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $scriptHash) {
    throw "The content-addressed Stage 2 engine snapshot changed during execution. No index was published."
}

$indexArgs = @(
    $indexBuilder,
    '--slide-dir', $OutputRoot,
    '--stage1-manifest', $stage1Manifest,
    '--stage2-script', $ExecutionScriptPath,
    '--output', $indexPath
)
foreach ($x in $procs) {
    $indexArgs += @(
        '--run',
        $x.Shard.Out,
        (Join-Path $x.Shard.In 'samplesheet.csv'),
        ([string]$x.Proc.ExitCode)
    )
}
& $PythonExe @indexArgs
if ($LASTEXITCODE -ne 0) {
    throw "Stage 2 outputs failed index validation; no authoritative index was published."
}
$indexedScriptHash = (
    (Get-Content -LiteralPath $indexPath -Raw | ConvertFrom-Json).stage2_script.sha256
)
if ($indexedScriptHash -ne $scriptHash) {
    $invalidIndex = Join-Path $OutputRoot (
        "stage2_run_index.INVALID_SCRIPT_" + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ') + ".json"
    )
    Move-Item -LiteralPath $indexPath -Destination $invalidIndex
    throw "Published index did not bind the executed engine snapshot; quarantined at $invalidIndex"
}
} finally {
    foreach ($lockedInput in $lockedInputs) { $lockedInput.Dispose() }
}
Write-Host "Published explicit Stage 2 index -> $indexPath"
Write-Host "Next: python aggregate_tiles_to_slide.py --slide-root $(Split-Path $OutputRoot -Parent)"
