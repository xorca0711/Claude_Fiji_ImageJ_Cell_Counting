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

    StarDist additionally requires -StarDistModelPath and
    -StarDistRuntimeManifest. Both must be absolute regular non-reparse files;
    both are forbidden for classic segmentation. They are read-locked through
    index publication and passed to every shard.

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
    [string]$ScriptPath = "",
    [string]$MarkerRegistryPath = "",
    [string]$PanelConfigPath = "",
    [string]$Panel = "LEFT",
    [string]$Segmenter = "classic",
    [string]$StarDistModelPath = "",
    [string]$StarDistRuntimeManifest = "",
    [double]$StarDistProbability = 0.5,
    [double]$StarDistNms = 0.4,
    [int]$StarDistTiles = 1,
    [switch]$UseInheritedIfqConfig,
    [string]$Krt5Threshold = "",
    [string]$AgerThreshold = "",
    [string]$T1aThreshold = "",
    [string]$JavaXmx = "8g",
    [string]$PythonExe = "python",
    [switch]$ReplaceExistingShards
)

$ErrorActionPreference = 'Stop'

# Windows PowerShell 5.1 does not reliably initialize $PSScriptRoot while
# parameter default expressions are being bound. Resolve repository-relative
# defaults only after script entry so direct CLI use works without spelling
# out paths that the launcher always passes explicitly.
$repositoryRoot = [System.IO.Directory]::GetParent($PSScriptRoot).FullName
if ([string]::IsNullOrWhiteSpace($ScriptPath)) {
    $ScriptPath = Join-Path $repositoryRoot 'IF_Quant_Pipeline.groovy'
}
if ([string]::IsNullOrWhiteSpace($MarkerRegistryPath)) {
    $MarkerRegistryPath = Join-Path $repositoryRoot 'config\lung_marker_registry.json'
}

$Segmenter = ([string]$Segmenter).Trim().ToLowerInvariant()
if ($Segmenter -notin @('classic', 'stardist')) {
    throw "Segmenter must be classic or stardist; found '$Segmenter'."
}
$isStarDist = $Segmenter -eq 'stardist'

if ([double]::IsNaN($StarDistProbability) -or
    [double]::IsInfinity($StarDistProbability) -or
    $StarDistProbability -lt 0.0 -or $StarDistProbability -gt 1.0) {
    throw "StarDistProbability must be finite and between 0 and 1."
}
if ([double]::IsNaN($StarDistNms) -or
    [double]::IsInfinity($StarDistNms) -or
    $StarDistNms -lt 0.0 -or $StarDistNms -gt 1.0) {
    throw "StarDistNms must be finite and between 0 and 1."
}
if ($StarDistTiles -lt 1) {
    throw "StarDistTiles must be a positive integer."
}
$starDistProbabilityText = [Convert]::ToString(
    $StarDistProbability,
    [Globalization.CultureInfo]::InvariantCulture
)
$starDistNmsText = [Convert]::ToString(
    $StarDistNms,
    [Globalization.CultureInfo]::InvariantCulture
)
$starDistTilesText = [Convert]::ToString(
    $StarDistTiles,
    [Globalization.CultureInfo]::InvariantCulture
)

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

function Resolve-RegularAuthorityFile {
    param(
        [Parameter(Mandatory = $true)][string]$Candidate,
        [Parameter(Mandatory = $true)][string]$RequiredExtension,
        [Parameter(Mandatory = $true)][string]$Label,
        [int64]$MaximumBytes = 0
    )
    if ([string]::IsNullOrWhiteSpace($Candidate)) {
        throw "$Label is required."
    }
    if (-not [System.IO.Path]::IsPathRooted($Candidate)) {
        throw "$Label must use an absolute path: $Candidate"
    }
    $fullPath = Get-NormalizedFullPath $Candidate
    if (-not [string]::Equals(
            [System.IO.Path]::GetExtension($fullPath),
            $RequiredExtension,
            [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must identify a $RequiredExtension file: $fullPath"
    }
    Assert-NoReparsePointInExistingChain -Candidate $fullPath -Label $Label
    if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
        throw "$Label is not an existing regular file: $fullPath"
    }
    $item = Get-Item -LiteralPath $fullPath -Force
    if ($item.PSIsContainer -or
        (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) -or
        (($item.Attributes -band [System.IO.FileAttributes]::Device) -ne 0)) {
        throw "$Label must be a regular non-reparse file: $fullPath"
    }
    if ([int64]$item.Length -le 0) {
        throw "$Label must not be empty: $fullPath"
    }
    if ($MaximumBytes -gt 0 -and [int64]$item.Length -gt $MaximumBytes) {
        throw "$Label exceeds the $MaximumBytes-byte limit: $fullPath"
    }
    return (Resolve-Path -LiteralPath $fullPath).Path
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

function ConvertTo-WindowsCommandLineArgument {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Argument)

    # ProcessStartInfo.ArgumentList is unavailable on Windows PowerShell 5.1's
    # .NET Framework. Encode one argv token using the CommandLineToArgvW / C
    # runtime rules: backslashes are literal except immediately before a quote,
    # and trailing backslashes must be doubled inside enclosing quotes.
    if ($Argument.Length -gt 0 -and $Argument -notmatch '[\s"]') {
        return $Argument
    }

    $quoted = New-Object System.Text.StringBuilder
    [void]$quoted.Append('"')
    $backslashes = 0
    foreach ($character in $Argument.ToCharArray()) {
        if ($character -eq [char]0x5c) {
            $backslashes++
            continue
        }
        if ($character -eq [char]0x22) {
            [void]$quoted.Append([char]0x5c, (2 * $backslashes) + 1)
            [void]$quoted.Append('"')
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            [void]$quoted.Append([char]0x5c, $backslashes)
            $backslashes = 0
        }
        [void]$quoted.Append($character)
    }
    if ($backslashes -gt 0) {
        [void]$quoted.Append([char]0x5c, 2 * $backslashes)
    }
    [void]$quoted.Append('"')
    return $quoted.ToString()
}

function Join-WindowsCommandLineArguments {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [AllowEmptyString()]
        [string[]]$Arguments
    )
    return (($Arguments | ForEach-Object {
        ConvertTo-WindowsCommandLineArgument -Argument $_
    }) -join ' ')
}

function Stop-StartedShardProcesses {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [object[]]$ProcessRecords
    )
    foreach ($record in $ProcessRecords) {
        if ($null -eq $record.Proc) { continue }
        try {
            if (-not $record.Proc.HasExited) { $record.Proc.Kill() }
        } catch {
            Write-Warning "Could not terminate failed-run shard process $($record.Proc.Id): $($_.Exception.Message)"
        }
    }
    foreach ($record in $ProcessRecords) {
        if ($null -eq $record.Proc) { continue }
        try {
            if ($record.Proc.WaitForExit(30000)) {
                # The parameterless call lets asynchronous stdout/stderr
                # callbacks finish after the native process has exited.
                $record.Proc.WaitForExit()
            } else {
                Write-Warning "Timed out waiting for failed-run shard process $($record.Proc.Id) to exit."
            }
        } catch {
            Write-Warning "Could not wait for failed-run shard process cleanup: $($_.Exception.Message)"
        }
    }
}

function Initialize-Stage2ProcessJobType {
    if ($null -ne ('IFQuant.Native.Stage2ProcessJobV1' -as [type])) { return }
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using Microsoft.Win32.SafeHandles;
using System.Runtime.InteropServices;
using System.Threading;

namespace IFQuant.Native
{
    public static class Stage2ProcessJobV1
    {
        private const uint JobObjectLimitKillOnJobClose = 0x00002000;
        private const int JobObjectExtendedLimitInformation = 9;
        private const uint CancelExitCode = 0xC000013A;
        private const uint GenericRead = 0x80000000;
        private const uint FileShareRead = 0x00000001;
        private const uint OpenExisting = 3;
        private const uint FileAttributeDirectory = 0x00000010;
        private const uint FileAttributeReparsePoint = 0x00000400;
        private const uint FileFlagOpenReparsePoint = 0x00200000;

        [UnmanagedFunctionPointer(CallingConvention.Winapi)]
        private delegate bool ConsoleControlHandler(uint controlType);

        private static readonly ConsoleControlHandler Handler = HandleConsoleControl;
        private static IntPtr activeJob = IntPtr.Zero;
        private static int handlerInstalled;
        private static int launchInProgress;
        private static int cancellationRequested;

        [StructLayout(LayoutKind.Sequential)]
        private struct IoCounters
        {
            public ulong ReadOperationCount;
            public ulong WriteOperationCount;
            public ulong OtherOperationCount;
            public ulong ReadTransferCount;
            public ulong WriteTransferCount;
            public ulong OtherTransferCount;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct BasicLimitInformation
        {
            public long PerProcessUserTimeLimit;
            public long PerJobUserTimeLimit;
            public uint LimitFlags;
            public UIntPtr MinimumWorkingSetSize;
            public UIntPtr MaximumWorkingSetSize;
            public uint ActiveProcessLimit;
            public UIntPtr Affinity;
            public uint PriorityClass;
            public uint SchedulingClass;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct ExtendedLimitInformation
        {
            public BasicLimitInformation BasicLimitInformation;
            public IoCounters IoInfo;
            public UIntPtr ProcessMemoryLimit;
            public UIntPtr JobMemoryLimit;
            public UIntPtr PeakProcessMemoryUsed;
            public UIntPtr PeakJobMemoryUsed;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateJobObject(IntPtr attributes, string name);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool SetInformationJobObject(
            IntPtr job,
            int informationClass,
            ref ExtendedLimitInformation information,
            uint informationLength
        );

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool TerminateProcess(IntPtr process, uint exitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool TerminateJobObject(IntPtr job, uint exitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CloseHandle(IntPtr handle);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool SetConsoleCtrlHandler(
            ConsoleControlHandler handler,
            [MarshalAs(UnmanagedType.Bool)] bool add
        );

        [StructLayout(LayoutKind.Sequential)]
        private struct NativeFileTime
        {
            public uint LowDateTime;
            public uint HighDateTime;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct ByHandleFileInformation
        {
            public uint FileAttributes;
            public NativeFileTime CreationTime;
            public NativeFileTime LastAccessTime;
            public NativeFileTime LastWriteTime;
            public uint VolumeSerialNumber;
            public uint FileSizeHigh;
            public uint FileSizeLow;
            public uint NumberOfLinks;
            public uint FileIndexHigh;
            public uint FileIndexLow;
        }

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool GetFileInformationByHandle(
            IntPtr file,
            out ByHandleFileInformation information
        );

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern SafeFileHandle CreateFile(
            string fileName,
            uint desiredAccess,
            uint shareMode,
            IntPtr securityAttributes,
            uint creationDisposition,
            uint flagsAndAttributes,
            IntPtr templateFile
        );

        public static IntPtr CreateAndArm()
        {
            IntPtr job = CreateJobObject(IntPtr.Zero, null);
            if (job == IntPtr.Zero)
                throw new Win32Exception(Marshal.GetLastWin32Error(), "CreateJobObject failed");

            var limits = new ExtendedLimitInformation();
            limits.BasicLimitInformation.LimitFlags = JobObjectLimitKillOnJobClose;
            if (!SetInformationJobObject(
                    job,
                    JobObjectExtendedLimitInformation,
                    ref limits,
                    (uint)Marshal.SizeOf(typeof(ExtendedLimitInformation))))
            {
                int error = Marshal.GetLastWin32Error();
                CloseHandle(job);
                throw new Win32Exception(error, "Could not configure KILL_ON_JOB_CLOSE");
            }

            if (Interlocked.CompareExchange(ref activeJob, job, IntPtr.Zero) != IntPtr.Zero)
            {
                CloseHandle(job);
                throw new InvalidOperationException("A Stage 2 process job is already active");
            }
            Interlocked.Exchange(ref launchInProgress, 0);
            Interlocked.Exchange(ref cancellationRequested, 0);

            if (Interlocked.CompareExchange(ref handlerInstalled, 1, 0) == 0 &&
                (!SetConsoleCtrlHandler(null, false) ||
                 !SetConsoleCtrlHandler(Handler, true)))
            {
                int error = Marshal.GetLastWin32Error();
                Interlocked.Exchange(ref handlerInstalled, 0);
                IntPtr removed = Interlocked.Exchange(ref activeJob, IntPtr.Zero);
                if (removed != IntPtr.Zero) CloseHandle(removed);
                throw new Win32Exception(error, "SetConsoleCtrlHandler failed");
            }

            if (Interlocked.CompareExchange(ref activeJob, job, job) != job)
                throw new OperationCanceledException("Stage 2 process job was cancelled during setup");
            return job;
        }

        public static void BeginProcessLaunch(IntPtr job)
        {
            if (Interlocked.CompareExchange(ref activeJob, job, job) != job)
                throw new OperationCanceledException("Stage 2 process job is no longer active");
            if (Interlocked.CompareExchange(ref launchInProgress, 1, 0) != 0)
                throw new InvalidOperationException("Nested Stage 2 process launch is not permitted");
        }

        public static void AssignProcessOrTerminate(IntPtr job, IntPtr process)
        {
            if (!AssignProcessToJobObject(job, process))
            {
                int error = Marshal.GetLastWin32Error();
                TerminateProcess(process, CancelExitCode);
                throw new Win32Exception(error, "AssignProcessToJobObject failed; child terminated");
            }
        }

        public static void EndProcessLaunch()
        {
            if (Interlocked.Exchange(ref launchInProgress, 0) != 1)
                throw new InvalidOperationException("No Stage 2 process launch is active");
            if (Interlocked.Exchange(ref cancellationRequested, 0) != 0)
            {
                TerminateAndCloseActiveJob();
                throw new OperationCanceledException(
                    "Console cancellation occurred while a Stage 2 process was being launched"
                );
            }
        }

        public static void Close(IntPtr job)
        {
            if (job == IntPtr.Zero) return;
            IntPtr removed = Interlocked.CompareExchange(ref activeJob, IntPtr.Zero, job);
            if (removed == job)
            {
                if (!CloseHandle(job))
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "CloseHandle(job) failed");
            }
            else if (removed != IntPtr.Zero)
            {
                throw new InvalidOperationException("Attempted to close a non-active Stage 2 process job");
            }
        }

        public static SafeFileHandle OpenReadLockNoReparse(string path)
        {
            SafeFileHandle handle = CreateFile(
                path,
                GenericRead,
                FileShareRead,
                IntPtr.Zero,
                OpenExisting,
                FileFlagOpenReparsePoint,
                IntPtr.Zero
            );
            if (handle == null || handle.IsInvalid)
            {
                int error = Marshal.GetLastWin32Error();
                if (handle != null) handle.Dispose();
                throw new Win32Exception(error, "Could not lock analytical input: " + path);
            }
            ByHandleFileInformation information;
            if (!GetFileInformationByHandle(handle.DangerousGetHandle(), out information))
            {
                int error = Marshal.GetLastWin32Error();
                handle.Dispose();
                throw new Win32Exception(error, "Could not inspect locked analytical input: " + path);
            }
            if ((information.FileAttributes & FileAttributeReparsePoint) != 0 ||
                (information.FileAttributes & FileAttributeDirectory) != 0)
            {
                handle.Dispose();
                throw new InvalidOperationException(
                    "Analytical input must be a regular non-reparse file: " + path
                );
            }
            return handle;
        }

        public static void RequireSameFile(
            SafeFileHandle source,
            SafeFileHandle hardLink,
            string label
        )
        {
            ByHandleFileInformation sourceInfo;
            if (!GetFileInformationByHandle(source.DangerousGetHandle(), out sourceInfo))
                throw new Win32Exception(
                    Marshal.GetLastWin32Error(),
                    "Could not read source file identity for " + label
                );
            ByHandleFileInformation hardLinkInfo;
            if (!GetFileInformationByHandle(hardLink.DangerousGetHandle(), out hardLinkInfo))
                throw new Win32Exception(
                    Marshal.GetLastWin32Error(),
                    "Could not read shard-link file identity for " + label
                );
            if (sourceInfo.VolumeSerialNumber != hardLinkInfo.VolumeSerialNumber ||
                sourceInfo.FileIndexHigh != hardLinkInfo.FileIndexHigh ||
                sourceInfo.FileIndexLow != hardLinkInfo.FileIndexLow)
            {
                throw new InvalidOperationException(
                    "Stage 1 source and shard hard link no longer name the same file: " + label
                );
            }
        }

        private static bool HandleConsoleControl(uint controlType)
        {
            if (controlType != 0 && controlType != 1 && controlType != 2 &&
                controlType != 5 && controlType != 6)
                return false;

            if (controlType == 0 || controlType == 1)
            {
                // Publish cancellation before observing the launch state. This
                // ordering closes the boundary where EndProcessLaunch could
                // clear launchInProgress immediately before the handler set the
                // flag, consuming Ctrl+C without either side terminating.
                Interlocked.Exchange(ref cancellationRequested, 1);
                if (Volatile.Read(ref launchInProgress) != 0)
                {
                    // Consume the signal until the just-created process has
                    // been assigned. EndProcessLaunch then kills the job.
                    return true;
                }
            }

            TerminateAndCloseActiveJob();
            // Let PowerShell perform its normal Ctrl+C / Ctrl+Break handling
            // after every assigned shard has been terminated.
            return false;
        }

        private static void TerminateAndCloseActiveJob()
        {
            IntPtr job = Interlocked.Exchange(ref activeJob, IntPtr.Zero);
            if (job == IntPtr.Zero) return;
            TerminateJobObject(job, CancelExitCode);
            CloseHandle(job);
        }
    }
}
'@
}

function Test-IsJsonInteger {
    param([AllowNull()][object]$Value)
    if ($null -eq $Value -or $Value -is [bool]) { return $false }
    return $Value -is [sbyte] -or $Value -is [byte] -or
        $Value -is [int16] -or $Value -is [uint16] -or
        $Value -is [int32] -or $Value -is [uint32] -or
        $Value -is [int64] -or $Value -is [uint64]
}

function New-ContentAddressedSnapshot {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$DestinationDirectory,
        [Parameter(Mandatory = $true)][string]$Kind
    )
    $hash = (Get-FileHash -LiteralPath $Source -Algorithm SHA256).Hash.ToLowerInvariant()
    $destination = Get-NormalizedFullPath (
        Join-Path $DestinationDirectory ($Kind + '.' + $hash + '.json')
    )
    Assert-DirectChildPath -Candidate $destination -Parent $DestinationDirectory -Label "$Kind snapshot"
    if (Test-Path -LiteralPath $destination) {
        if (-not (Test-Path -LiteralPath $destination -PathType Leaf)) {
            throw "$Kind snapshot path is not a regular file: $destination"
        }
        if ((Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant() -ne $hash) {
            throw "Existing content-addressed $Kind snapshot has the wrong hash: $destination"
        }
    } else {
        $temporary = $destination + '.tmp.' + [Guid]::NewGuid().ToString('N')
        Assert-DirectChildPath -Candidate $temporary -Parent $DestinationDirectory -Label "Temporary $Kind snapshot"
        try {
            [System.IO.File]::WriteAllBytes(
                $temporary,
                [System.IO.File]::ReadAllBytes($Source)
            )
            if ((Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash.ToLowerInvariant() -ne $hash) {
                throw "Source changed while creating the content-addressed $Kind snapshot: $Source"
            }
            Move-Item -LiteralPath $temporary -Destination $destination
        } finally {
            if (Test-Path -LiteralPath $temporary) {
                Remove-Item -LiteralPath $temporary -Force
            }
        }
    }
    if ((Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant() -ne $hash) {
        throw "$Kind snapshot verification failed: $destination"
    }
    return [pscustomobject]@{
        Path = (Resolve-Path -LiteralPath $destination).Path
        Sha256 = $hash
    }
}

if ($isStarDist) {
    if ([string]::IsNullOrWhiteSpace($StarDistModelPath) -or
        [string]::IsNullOrWhiteSpace($StarDistRuntimeManifest)) {
        throw ("Segmenter=stardist requires both -StarDistModelPath and " +
               "-StarDistRuntimeManifest.")
    }
    $StarDistModelPath = Resolve-RegularAuthorityFile `
        -Candidate $StarDistModelPath `
        -RequiredExtension '.zip' `
        -Label 'StarDist model'
    $StarDistRuntimeManifest = Resolve-RegularAuthorityFile `
        -Candidate $StarDistRuntimeManifest `
        -RequiredExtension '.json' `
        -Label 'StarDist runtime manifest' `
        -MaximumBytes (1024L * 1024L)
} else {
    if (-not [string]::IsNullOrWhiteSpace($StarDistModelPath) -or
        -not [string]::IsNullOrWhiteSpace($StarDistRuntimeManifest)) {
        throw ("StarDistModelPath and StarDistRuntimeManifest are forbidden when " +
               "Segmenter=classic; classic shards emit neither authority key.")
    }
    $StarDistModelPath = $null
    $StarDistRuntimeManifest = $null
}

$tilesCandidate = Get-NormalizedFullPath $TilesDir
$scriptCandidate = Get-NormalizedFullPath $ScriptPath
$outputCandidate = Get-NormalizedFullPath $OutputRoot
$markerRegistryCandidate = Get-NormalizedFullPath $MarkerRegistryPath
$panelConfigCandidate = if ([string]::IsNullOrWhiteSpace($PanelConfigPath)) {
    $null
} else {
    Get-NormalizedFullPath $PanelConfigPath
}
Assert-NoReparsePointInExistingChain -Candidate $tilesCandidate -Label 'TilesDir'
Assert-NoReparsePointInExistingChain -Candidate $scriptCandidate -Label 'Stage 2 script'
Assert-NoReparsePointInExistingChain -Candidate $outputCandidate -Label 'OutputRoot'
Assert-NoReparsePointInExistingChain -Candidate $markerRegistryCandidate -Label 'Marker registry'
if ($panelConfigCandidate) {
    Assert-NoReparsePointInExistingChain -Candidate $panelConfigCandidate -Label 'Panel config'
}
$TilesDir   = (Resolve-Path -LiteralPath $tilesCandidate).Path
$ScriptPath = (Resolve-Path -LiteralPath $scriptCandidate).Path
$MarkerRegistryPath = (Resolve-Path -LiteralPath $markerRegistryCandidate).Path
if (-not (Test-Path -LiteralPath $MarkerRegistryPath -PathType Leaf)) {
    throw "Marker registry is not a regular file: $MarkerRegistryPath"
}
if ($panelConfigCandidate) {
    $PanelConfigPath = (Resolve-Path -LiteralPath $panelConfigCandidate).Path
    if (-not (Test-Path -LiteralPath $PanelConfigPath -PathType Leaf)) {
        throw "Panel config is not a regular file: $PanelConfigPath"
    }
} else {
    $PanelConfigPath = $null
}
if (-not (Test-Path -LiteralPath $outputCandidate)) {
    New-Item -ItemType Directory -Path $outputCandidate -Force | Out-Null
}
$OutputRoot = (Resolve-Path -LiteralPath $OutputRoot).Path
Assert-NoReparsePointInExistingChain -Candidate $TilesDir -Label 'TilesDir'
Assert-NoReparsePointInExistingChain -Candidate $ScriptPath -Label 'Stage 2 script'
Assert-NoReparsePointInExistingChain -Candidate $OutputRoot -Label 'OutputRoot'
Assert-NoReparsePointInExistingChain -Candidate $MarkerRegistryPath -Label 'Marker registry'
if ($PanelConfigPath) {
    Assert-NoReparsePointInExistingChain -Candidate $PanelConfigPath -Label 'Panel config'
}
foreach ($jsonConfig in @($MarkerRegistryPath, $PanelConfigPath)) {
    if (-not $jsonConfig) { continue }
    try {
        $parsedConfig = Get-Content -LiteralPath $jsonConfig -Raw | ConvertFrom-Json
    } catch {
        throw "Stage 2 JSON configuration is invalid: $jsonConfig ($($_.Exception.Message))"
    }
    if ($null -eq $parsedConfig) {
        throw "Stage 2 JSON configuration has a null root: $jsonConfig"
    }
}

# The desktop launcher seals the complete Stage 2 IFQ_* surface before it
# starts this orchestrator. In that mode, preserve those validated settings
# instead of silently reducing them to this script's small CLI parameter set.
# The per-shard input/output paths and content-addressed config snapshots remain
# owned here and are overlaid below. Direct CLI use keeps the historical,
# explicit-parameter behaviour unless -UseInheritedIfqConfig is requested.
$inheritedIfqConfig = @{}
if ($UseInheritedIfqConfig) {
    $processEnvironment = [Environment]::GetEnvironmentVariables()
    foreach ($rawName in $processEnvironment.Keys) {
        $name = [string]$rawName
        if (-not $name.StartsWith('IFQ_', [StringComparison]::OrdinalIgnoreCase)) {
            continue
        }
        if ($name -notmatch '^IFQ_[A-Z0-9_]+$') {
            throw "Inherited IFQ environment name is invalid: '$name'"
        }
        $value = [string]$processEnvironment[$rawName]
        if ([string]::IsNullOrEmpty($value)) {
            throw "Inherited IFQ environment value is empty: $name"
        }
        $inheritedIfqConfig[$name] = $value
    }

    $requiredInheritedValues = @{
        IFQ_PANEL = $Panel
        IFQ_SEGMENTER = $Segmenter
        IFQ_RECURSIVE = 'false'
        IFQ_INCLUDE_REGEX = '.*'
        IFQ_MAX_IMAGES = '0'
        IFQ_MIN_INCLUDED_NUCLEI = '0'
        IFQ_PROJECTION = 'max'
        IFQ_SINGLE_PLANE = '-1'
        IFQ_ALLOW_NONEMPTY_OUTPUT = 'false'
        IFQ_DISPLAY_PREVIEW_ONLY = 'false'
    }
    foreach ($requiredName in $requiredInheritedValues.Keys) {
        if (-not $inheritedIfqConfig.ContainsKey($requiredName)) {
            throw "Launcher-sealed Stage 2 environment is missing $requiredName"
        }
        $expectedValue = [string]$requiredInheritedValues[$requiredName]
        if (-not [string]::Equals(
                [string]$inheritedIfqConfig[$requiredName],
                $expectedValue,
                [StringComparison]::OrdinalIgnoreCase)) {
            throw ("Launcher-sealed $requiredName disagrees with the orchestrator: " +
                   "'$($inheritedIfqConfig[$requiredName])' vs '$expectedValue'")
        }
    }

    if (-not $inheritedIfqConfig.ContainsKey('IFQ_MARKER_REGISTRY')) {
        throw "Launcher-sealed Stage 2 environment is missing IFQ_MARKER_REGISTRY"
    }
    $inheritedRegistry = Get-NormalizedFullPath (
        [string]$inheritedIfqConfig['IFQ_MARKER_REGISTRY']
    )
    if (-not [string]::Equals(
            $inheritedRegistry,
            $MarkerRegistryPath,
            [StringComparison]::OrdinalIgnoreCase)) {
        throw "Launcher-sealed IFQ_MARKER_REGISTRY disagrees with -MarkerRegistryPath"
    }

    if (-not $inheritedIfqConfig.ContainsKey('IFQ_ENGINE_SCRIPT_PATH')) {
        throw "Launcher-sealed Stage 2 environment is missing IFQ_ENGINE_SCRIPT_PATH"
    }
    $inheritedEngineScript = Get-NormalizedFullPath (
        [string]$inheritedIfqConfig['IFQ_ENGINE_SCRIPT_PATH']
    )
    if (-not [string]::Equals(
            $inheritedEngineScript,
            $ScriptPath,
            [StringComparison]::OrdinalIgnoreCase)) {
        throw "Launcher-sealed IFQ_ENGINE_SCRIPT_PATH disagrees with -ScriptPath"
    }

    if ($isStarDist) {
        if (-not $inheritedIfqConfig.ContainsKey('IFQ_STARDIST_MODEL_PATH')) {
            throw "Launcher-sealed StarDist environment is missing IFQ_STARDIST_MODEL_PATH"
        }
        if (-not $inheritedIfqConfig.ContainsKey('IFQ_STARDIST_RUNTIME_MANIFEST')) {
            throw "Launcher-sealed StarDist environment is missing IFQ_STARDIST_RUNTIME_MANIFEST"
        }
        $inheritedStarDistModel = Resolve-RegularAuthorityFile `
            -Candidate ([string]$inheritedIfqConfig['IFQ_STARDIST_MODEL_PATH']) `
            -RequiredExtension '.zip' `
            -Label 'Launcher-sealed StarDist model'
        $inheritedStarDistManifest = Resolve-RegularAuthorityFile `
            -Candidate ([string]$inheritedIfqConfig['IFQ_STARDIST_RUNTIME_MANIFEST']) `
            -RequiredExtension '.json' `
            -Label 'Launcher-sealed StarDist runtime manifest' `
            -MaximumBytes (1024L * 1024L)
        if (-not [string]::Equals(
                $inheritedStarDistModel,
                $StarDistModelPath,
                [StringComparison]::OrdinalIgnoreCase)) {
            throw "Launcher-sealed IFQ_STARDIST_MODEL_PATH disagrees with -StarDistModelPath"
        }
        if (-not [string]::Equals(
                $inheritedStarDistManifest,
                $StarDistRuntimeManifest,
                [StringComparison]::OrdinalIgnoreCase)) {
            throw ("Launcher-sealed IFQ_STARDIST_RUNTIME_MANIFEST disagrees with " +
                   "-StarDistRuntimeManifest")
        }
    } elseif ($inheritedIfqConfig.ContainsKey('IFQ_STARDIST_MODEL_PATH') -or
              $inheritedIfqConfig.ContainsKey('IFQ_STARDIST_RUNTIME_MANIFEST')) {
        throw ("Launcher-sealed classic environment must omit IFQ_STARDIST_MODEL_PATH " +
               "and IFQ_STARDIST_RUNTIME_MANIFEST")
    }

    if ($inheritedIfqConfig.ContainsKey('IFQ_PANEL_CONFIG')) {
        if (-not $PanelConfigPath) {
            throw "Launcher-sealed IFQ_PANEL_CONFIG requires -PanelConfigPath"
        }
        $inheritedPanelConfig = Get-NormalizedFullPath (
            [string]$inheritedIfqConfig['IFQ_PANEL_CONFIG']
        )
        if (-not [string]::Equals(
                $inheritedPanelConfig,
                $PanelConfigPath,
                [StringComparison]::OrdinalIgnoreCase)) {
            throw "Launcher-sealed IFQ_PANEL_CONFIG disagrees with -PanelConfigPath"
        }
    } elseif ($PanelConfigPath) {
        throw "-PanelConfigPath was supplied but the launcher-sealed environment has no IFQ_PANEL_CONFIG"
    }

    foreach ($thresholdBinding in @(
        @('IFQ_KRT5_THRESHOLD', 'Krt5Threshold'),
        @('IFQ_AGER_THRESHOLD', 'AgerThreshold'),
        @('IFQ_T1A_THRESHOLD', 'T1aThreshold')
    )) {
        $environmentName = $thresholdBinding[0]
        $parameterName = $thresholdBinding[1]
        if (-not $inheritedIfqConfig.ContainsKey($environmentName)) { continue }
        $inheritedThreshold = [string]$inheritedIfqConfig[$environmentName]
        $parameterThreshold = [string](Get-Variable -Name $parameterName -ValueOnly)
        if ($parameterThreshold -and
            -not [string]::Equals(
                $inheritedThreshold,
                $parameterThreshold,
                [StringComparison]::Ordinal)) {
            throw "Launcher-sealed $environmentName disagrees with -$parameterName"
        }
        Set-Variable -Name $parameterName -Value $inheritedThreshold
    }

    foreach ($starDistBinding in @(
        @('IFQ_STARDIST_PROB', $starDistProbabilityText),
        @('IFQ_STARDIST_NMS', $starDistNmsText),
        @('IFQ_STARDIST_TILES', $starDistTilesText)
    )) {
        $environmentName = $starDistBinding[0]
        if (-not $inheritedIfqConfig.ContainsKey($environmentName)) { continue }
        if (-not [string]::Equals(
                [string]$inheritedIfqConfig[$environmentName],
                [string]$starDistBinding[1],
                [StringComparison]::Ordinal)) {
            throw "Launcher-sealed $environmentName disagrees with the orchestrator parameter"
        }
    }
}

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

# A capped, dry-run, or candidate-dropping Stage 1 layout is an engineering
# fixture, not an exhaustive slide. Refuse it before creating/replacing shard
# directories or launching Fiji.
try {
    $stage1Document = Get-Content -LiteralPath $stage1Manifest -Raw | ConvertFrom-Json
} catch {
    throw "Stage 1 manifest is not valid JSON: $stage1Manifest ($($_.Exception.Message))"
}
$slideStem = [System.IO.Path]::GetFileName($OutputRoot)
$matchingStage1Slides = @(
    @($stage1Document.slides) | Where-Object {
        [string]::Equals(
            [string]$_.slide_stem,
            $slideStem,
            [System.StringComparison]::Ordinal
        )
    }
)
if ($matchingStage1Slides.Count -ne 1) {
    throw "Stage 1 manifest must contain exactly one slide_stem '$slideStem'; found $($matchingStage1Slides.Count)."
}
$stage1Slide = $matchingStage1Slides[0]
foreach ($booleanField in @('coverage_complete', 'dry_run')) {
    if ($stage1Slide.PSObject.Properties.Name -notcontains $booleanField -or
        $stage1Slide.$booleanField -isnot [bool]) {
        throw "Stage 1 slide '$slideStem' must declare Boolean $booleanField."
    }
}
if (-not $stage1Slide.coverage_complete) {
    throw "Stage 1 slide '$slideStem' has coverage_complete=false; Stage 2 requires exhaustive candidate coverage."
}
if ($stage1Slide.dry_run) {
    throw "Stage 1 slide '$slideStem' has dry_run=true; no analytical Stage 2 run is permitted."
}
foreach ($counterField in @('n_skipped_low_tissue', 'n_skipped_empty_raster')) {
    if ($stage1Slide.PSObject.Properties.Name -notcontains $counterField -or
        -not (Test-IsJsonInteger -Value $stage1Slide.$counterField)) {
        throw "Stage 1 slide '$slideStem' must declare integer $counterField."
    }
    if ([int64]$stage1Slide.$counterField -ne 0) {
        throw "Stage 1 slide '$slideStem' has $counterField=$($stage1Slide.$counterField); Stage 2 requires zero omitted candidate tiles."
    }
}

$tileManifest = Join-Path $OutputRoot 'tile_manifest.csv'
if (-not (Test-Path -LiteralPath $tileManifest -PathType Leaf)) {
    throw "Stage 1 tile manifest not found in the slide folder: $tileManifest"
}
$tileManifest = (Resolve-Path -LiteralPath $tileManifest).Path
Assert-DirectChildPath -Candidate $tileManifest -Parent $OutputRoot -Label 'Stage 1 tile manifest'
Assert-NoReparsePointInExistingChain -Candidate $tileManifest -Label 'Stage 1 tile manifest'
$candidateManifestName = [string]$stage1Slide.tile_candidate_manifest
if (-not (Test-SafeBasename $candidateManifestName)) {
    throw "Stage 1 tile_candidate_manifest must be a plain filename: '$candidateManifestName'"
}
$candidateManifest = Join-Path $OutputRoot $candidateManifestName
if (-not (Test-Path -LiteralPath $candidateManifest -PathType Leaf)) {
    throw "Stage 1 candidate manifest not found in the slide folder: $candidateManifest"
}
$candidateManifest = (Resolve-Path -LiteralPath $candidateManifest).Path
Assert-DirectChildPath -Candidate $candidateManifest -Parent $OutputRoot -Label 'Stage 1 candidate manifest'
Assert-NoReparsePointInExistingChain -Candidate $candidateManifest -Label 'Stage 1 candidate manifest'

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

$configSnapshotDir = Get-NormalizedFullPath (Join-Path $OutputRoot 'stage2_config_provenance')
Assert-DirectChildPath -Candidate $configSnapshotDir -Parent $OutputRoot -Label 'Stage 2 config provenance directory'
Assert-NoReparsePointInExistingChain -Candidate $configSnapshotDir -Label 'Stage 2 config provenance directory'
if (-not (Test-Path -LiteralPath $configSnapshotDir)) {
    New-Item -ItemType Directory -Path $configSnapshotDir | Out-Null
}
Assert-NoReparsePointTree -Candidate $configSnapshotDir -Label 'Stage 2 config provenance directory'
$markerRegistrySnapshot = New-ContentAddressedSnapshot `
    -Source $MarkerRegistryPath `
    -DestinationDirectory $configSnapshotDir `
    -Kind 'marker_registry'
$panelConfigSnapshot = $null
if ($PanelConfigPath) {
    $panelConfigSnapshot = New-ContentAddressedSnapshot `
        -Source $PanelConfigPath `
        -DestinationDirectory $configSnapshotDir `
        -Kind 'panel_config'
}
$panelConfigSnapshotPath = if ($panelConfigSnapshot) { $panelConfigSnapshot.Path } else { $null }
Assert-NoReparsePointInExistingChain -Candidate $markerRegistrySnapshot.Path -Label 'Marker registry snapshot'
if ($panelConfigSnapshotPath) {
    Assert-NoReparsePointInExistingChain -Candidate $panelConfigSnapshotPath -Label 'Panel config snapshot'
}

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
            $candidateManifest,
            $MarkerRegistryPath,
            $PanelConfigPath,
            $StarDistModelPath,
            $StarDistRuntimeManifest,
            $configSnapshotDir,
            $markerRegistrySnapshot.Path,
            $panelConfigSnapshotPath,
            $PythonExe,
            $java.FullName,
            $patcher.FullName
        )) {
            if (-not $protectedPath) { continue }
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

$lockedInputs = [System.Collections.Generic.List[Microsoft.Win32.SafeHandles.SafeFileHandle]]::new()
$lockedInputByPath = @{}
$procs = @()
$stage2ProcessJob = [IntPtr]::Zero
Initialize-Stage2ProcessJobType
try {
    # Freeze every authoritative source pathname before creating any hard link.
    # FILE_FLAG_OPEN_REPARSE_POINT makes the regular-file check and the held
    # deny-write/delete handle atomic with respect to the directory entry.
    $sourceLockPaths = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    foreach ($sourcePathToLock in @(
        $ExecutionScriptPath,
        $stage1Manifest,
        $tileManifest,
        $candidateManifest,
        $markerRegistrySnapshot.Path,
        $panelConfigSnapshotPath,
        $StarDistModelPath,
        $StarDistRuntimeManifest
    )) {
        if ($sourcePathToLock) { [void]$sourceLockPaths.Add($sourcePathToLock) }
    }
    foreach ($plan in $plannedRows) {
        [void]$sourceLockPaths.Add($plan.TifSource)
        [void]$sourceLockPaths.Add($plan.RoiSource)
    }
    foreach ($sourcePathToLock in $sourceLockPaths) {
        $sourceHandle = [IFQuant.Native.Stage2ProcessJobV1]::OpenReadLockNoReparse(
            $sourcePathToLock
        )
        $lockedInputs.Add($sourceHandle)
        $lockedInputByPath[$sourcePathToLock] = $sourceHandle
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

        foreach ($identityCheck in @(
            @($plan.TifSource, $plan.TifDestination, "tile $($plan.Filename)"),
            @($plan.RoiSource, $plan.RoiDestination, "ROI $($plan.Filename)")
        )) {
            $sourcePath = [string]$identityCheck[0]
            $destinationPath = [string]$identityCheck[1]
            $destinationHandle = [IFQuant.Native.Stage2ProcessJobV1]::OpenReadLockNoReparse(
                $destinationPath
            )
            $lockedInputs.Add($destinationHandle)
            $lockedInputByPath[$destinationPath] = $destinationHandle
            [IFQuant.Native.Stage2ProcessJobV1]::RequireSameFile(
                $lockedInputByPath[$sourcePath],
                $destinationHandle,
                [string]$identityCheck[2]
            )
        }
    }
    $mine = @($minePlans | ForEach-Object { $_.Row })
    $shardSamplesheet = Get-NormalizedFullPath (Join-Path $resolvedShardIn 'samplesheet.csv')
    Assert-DirectChildPath -Candidate $shardSamplesheet -Parent $resolvedShardIn -Label 'Shard samplesheet'
    $mine | Export-Csv -LiteralPath $shardSamplesheet -NoTypeInformation -Encoding UTF8
    $samplesheetHandle = [IFQuant.Native.Stage2ProcessJobV1]::OpenReadLockNoReparse(
        $shardSamplesheet
    )
    $lockedInputs.Add($samplesheetHandle)
    $lockedInputByPath[$shardSamplesheet] = $samplesheetHandle
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
$stage2ProcessJob = [IFQuant.Native.Stage2ProcessJobV1]::CreateAndArm()

$cp = (Join-Path $FijiDir 'jars\*') + ';' + (Join-Path $FijiDir 'plugins\*')
foreach ($s in $shardDirs) {
    $envPairs = @{
        IFQ_INPUT_DIR          = $s.In
        IFQ_OUTPUT_DIR         = $s.Out
        IFQ_ENGINE_SCRIPT_PATH = $ExecutionScriptPath
        IFQ_PANEL              = $Panel
        IFQ_SEGMENTER          = $Segmenter
        IFQ_STARDIST_PROB      = $starDistProbabilityText
        IFQ_STARDIST_NMS       = $starDistNmsText
        IFQ_STARDIST_TILES     = $starDistTilesText
        IFQ_MIN_INCLUDED_NUCLEI = '0'
        IFQ_MARKER_REGISTRY    = $markerRegistrySnapshot.Path
    }
    if ($UseInheritedIfqConfig) {
        foreach ($inheritedName in $inheritedIfqConfig.Keys) {
            $envPairs[$inheritedName] = [string]$inheritedIfqConfig[$inheritedName]
        }
        # These values are deliberately owned by the orchestrator. Inputs and
        # outputs differ per shard, while config paths must point at the exact
        # byte snapshots created above rather than the mutable launcher paths.
        $envPairs['IFQ_INPUT_DIR'] = $s.In
        $envPairs['IFQ_OUTPUT_DIR'] = $s.Out
        # The child executes the immutable content-addressed copy created by
        # this orchestrator, not the launcher's mutable extraction path.
        $envPairs['IFQ_ENGINE_SCRIPT_PATH'] = $ExecutionScriptPath
        $envPairs['IFQ_PANEL'] = $Panel
        $envPairs['IFQ_SEGMENTER'] = $Segmenter
        $envPairs['IFQ_STARDIST_PROB'] = $starDistProbabilityText
        $envPairs['IFQ_STARDIST_NMS'] = $starDistNmsText
        $envPairs['IFQ_STARDIST_TILES'] = $starDistTilesText
        $envPairs['IFQ_MIN_INCLUDED_NUCLEI'] = '0'
        $envPairs['IFQ_MARKER_REGISTRY'] = $markerRegistrySnapshot.Path
    }
    if ($isStarDist) {
        $envPairs['IFQ_STARDIST_MODEL_PATH'] = $StarDistModelPath
        $envPairs['IFQ_STARDIST_RUNTIME_MANIFEST'] = $StarDistRuntimeManifest
    }
    if ($panelConfigSnapshotPath) { $envPairs['IFQ_PANEL_CONFIG'] = $panelConfigSnapshotPath }
    elseif ($envPairs.ContainsKey('IFQ_PANEL_CONFIG')) { $envPairs.Remove('IFQ_PANEL_CONFIG') }
    if ($Krt5Threshold) { $envPairs['IFQ_KRT5_THRESHOLD'] = $Krt5Threshold }
    if ($AgerThreshold) { $envPairs['IFQ_AGER_THRESHOLD'] = $AgerThreshold }
    if ($T1aThreshold)  { $envPairs['IFQ_T1A_THRESHOLD']  = $T1aThreshold }

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.UseShellExecute = $false
    $psi.FileName = $java.FullName
    $javaArguments = @(
        '--add-opens=java.base/java.lang=ALL-UNNAMED',
        ("-javaagent:" + $patcher.FullName + "=init"),
        '-Djava.awt.headless=true',
        ("-Dplugins.dir=" + $FijiDir),
        ("-Xmx" + $JavaXmx),
        '-cp', $cp,
        'net.imagej.Main', '--headless', '--run', $ExecutionScriptPath
    )
    $psi.Arguments = Join-WindowsCommandLineArguments -Arguments $javaArguments
    # ProcessStartInfo starts with a copy of the parent environment. Remove the
    # entire inherited IFQ surface before applying this script's explicit,
    # provenance-recorded configuration; otherwise a stale panel map, threshold,
    # or canonicality override could silently change a shard.
    $inheritedIfqKeys = @(
        $psi.EnvironmentVariables.Keys | ForEach-Object { [string]$_ } |
            Where-Object { $_.StartsWith('IFQ_', [System.StringComparison]::OrdinalIgnoreCase) }
    )
    foreach ($inheritedIfqKey in $inheritedIfqKeys) {
        $psi.EnvironmentVariables.Remove($inheritedIfqKey)
    }
    foreach ($k in $envPairs.Keys) { $psi.EnvironmentVariables[$k] = $envPairs[$k] }
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError  = $true

    $logPath = $s.Log
    [IFQuant.Native.Stage2ProcessJobV1]::BeginProcessLaunch($stage2ProcessJob)
    try {
        $p = [System.Diagnostics.Process]::Start($psi)
        # Register the process immediately. If callback setup or a later shard
        # launch fails, the outer catch can still terminate every started JVM.
        $processRecord = [pscustomobject]@{
            Shard = $s
            Proc = $p
            Log = $logPath
            OutputCallback = $null
            ErrorCallback = $null
        }
        $procs += $processRecord
        # The native launch guard consumes console cancellation only across the
        # tiny Start-to-Assign window. If a signal lands there, EndProcessLaunch
        # terminates the now-complete job before surfacing cancellation.
        [IFQuant.Native.Stage2ProcessJobV1]::AssignProcessOrTerminate(
            $stage2ProcessJob,
            $p.Handle
        )
    } finally {
        [IFQuant.Native.Stage2ProcessJobV1]::EndProcessLaunch()
    }
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
    $processRecord.OutputCallback = $outputCallback
    $processRecord.ErrorCallback = $errorCallback
    $p.BeginOutputReadLine(); $p.BeginErrorReadLine()
    Write-Host ("  launched {0} (pid {1}) -> {2}" -f $s.Tag, $p.Id, $logPath)
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
if ((Get-FileHash -LiteralPath $markerRegistrySnapshot.Path -Algorithm SHA256).Hash.ToLowerInvariant() -ne
    $markerRegistrySnapshot.Sha256) {
    throw "The content-addressed marker registry snapshot changed during execution. No index was published."
}
if ($panelConfigSnapshotPath -and
    (Get-FileHash -LiteralPath $panelConfigSnapshotPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne
    $panelConfigSnapshot.Sha256) {
    throw "The content-addressed panel config snapshot changed during execution. No index was published."
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
$indexPsi = New-Object System.Diagnostics.ProcessStartInfo
$indexPsi.FileName = $PythonExe
$indexPsi.Arguments = Join-WindowsCommandLineArguments -Arguments $indexArgs
$indexPsi.UseShellExecute = $false
[IFQuant.Native.Stage2ProcessJobV1]::BeginProcessLaunch($stage2ProcessJob)
try {
    $indexProcess = [System.Diagnostics.Process]::Start($indexPsi)
    $indexProcessRecord = [pscustomobject]@{
        Shard = $null
        Proc = $indexProcess
        Log = $null
        OutputCallback = $null
        ErrorCallback = $null
    }
    $procs += $indexProcessRecord
    [IFQuant.Native.Stage2ProcessJobV1]::AssignProcessOrTerminate(
        $stage2ProcessJob,
        $indexProcess.Handle
    )
} finally {
    [IFQuant.Native.Stage2ProcessJobV1]::EndProcessLaunch()
}
$indexProcess.WaitForExit()
if ($indexProcess.ExitCode -ne 0) {
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
} catch {
    $launchFailure = $_
    # A later launch/setup/reconciliation failure must not leave earlier Fiji
    # shards running after the input locks are released.
    Stop-StartedShardProcesses -ProcessRecords $procs
    throw $launchFailure
} finally {
    if ($stage2ProcessJob -ne [IntPtr]::Zero) {
        try {
            [IFQuant.Native.Stage2ProcessJobV1]::Close($stage2ProcessJob)
        } catch {
            Write-Warning "Could not close the Stage 2 process job: $($_.Exception.Message)"
        }
    }
    foreach ($x in $procs) {
        if ($null -eq $x.Proc) { continue }
        if ($x.OutputCallback) {
            try { $x.Proc.remove_OutputDataReceived($x.OutputCallback) } catch { }
        }
        if ($x.ErrorCallback) {
            try { $x.Proc.remove_ErrorDataReceived($x.ErrorCallback) } catch { }
        }
        try { $x.Proc.Dispose() } catch { }
    }
    foreach ($lockedInput in $lockedInputs) {
        try { $lockedInput.Dispose() } catch { }
    }
}
Write-Host "Published explicit Stage 2 index -> $indexPath"
Write-Host "Next: python aggregate_tiles_to_slide.py --slide-root $(Split-Path $OutputRoot -Parent)"
