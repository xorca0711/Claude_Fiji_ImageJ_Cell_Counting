from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_visual_merge_scale_bar_is_calibrated_and_internal():
    pipeline = (ROOT / "IF_Quant_Pipeline.groovy").read_text(encoding="utf-8")

    assert 'envDouble("IFQ_DISPLAY_SCALE_BAR_UM", 100.0d)' in pipeline
    assert 'envInt("IFQ_DISPLAY_SCALE_BAR_THICKNESS_PX", 6)' in pipeline
    assert "cfg.displayScaleBarUm as double" in pipeline
    assert "cfg.displayScaleBarThicknessPx as int" in pipeline
    assert "composite.setCalibration(first.getCalibration())" in pipeline
    assert "labeled.setCalibration(source.getCalibration())" in pipeline
    assert "Cannot draw visual-merge scale bar without positive micrometre calibration" in pipeline
    assert "cp.setRoi(barX, barY, barWidthPx, thickness)" in pipeline
    assert r'" \u00b5m"' in pipeline


def test_launcher_owns_modern_scale_bar_defaults_but_not_legacy_route():
    launcher = (ROOT / "launcher" / "IFQuantLauncher.cs").read_text(encoding="utf-8")
    routing = (ROOT / "launcher" / "IFQuantLauncher.Routing.cs").read_text(encoding="utf-8")

    assert 'AssemblyFileVersion("1.9.7.0")' in launcher
    assert '"IFQ_DISPLAY_SCALE_BAR_UM", "IFQ_DISPLAY_SCALE_BAR_THICKNESS_PX"' in launcher
    assert 'public const string Version = "1.9.7";' in routing
    assert 'env["IFQ_DISPLAY_SCALE_BAR_UM"] = "100";' in routing
    assert 'env["IFQ_DISPLAY_SCALE_BAR_THICKNESS_PX"] = "6";' in routing

    legacy = routing[routing.index("internal static class LegacyProfile") :]
    assert "IFQ_DISPLAY_SCALE_BAR_UM" not in legacy
    assert "IFQ_DISPLAY_SCALE_BAR_THICKNESS_PX" not in legacy


def test_launcher_route2_is_manifest_discovered_indexed_and_self_contained():
    launcher = (ROOT / "launcher" / "IFQuantLauncher.cs").read_text(encoding="utf-8")
    routes = (ROOT / "launcher" / "MainForm.Routes.partial.cs").read_text(encoding="utf-8")
    routing = (ROOT / "launcher" / "IFQuantLauncher.Routing.cs").read_text(encoding="utf-8")
    build = (ROOT / "launcher" / "build.ps1").read_text(encoding="utf-8")

    assert "Directory.CreateDirectory(input);" not in launcher
    assert "Stage1LayoutDiscovery.Discover(config.OutputDirectory)" in routes
    assert "IsSafeDirectChildName" in launcher
    assert "stage2_run_index.json" in routes
    assert "BuildStage2OrchestratorArguments" in routes
    assert 'Append(" -UseInheritedIfqConfig")' in routes
    assert '" --stage1-manifest "' in routes
    assert '" --stage2-script "' in routes
    assert "FinishSlideScannerRun" in routes
    assert "WSI_REQUIRES_BUNDLED_JVM" in routing
    assert "POWERSHELL_MISSING" in routing
    assert 'env["IFQ_STARDIST_PROB"] = "0.5";' in routing
    assert 'env["IFQ_STARDIST_NMS"] = "0.4";' in routing
    assert 'env["IFQ_STARDIST_TILES"] = "1";' in routing

    for required in (
        "Invoke-Stage2Sharded.ps1",
        "build_stage2_run_index.py",
        "aggregate_to_mouse.py",
        "ifquant\\adapters.py",
        "ifquant\\contracts.py",
        "ifquant\\route_records.py",
        "ifquant\\stage2_index.py",
        "stage2-run-index.schema.json",
        "measurement-record.schema.json",
        "wsi-reference-mask-profile.schema.json",
        "stardist-runtime-manifest.schema.json",
        "scripts\\he_pipeline.py",
        "g_surf_he_20260812.json",
        "he_pathology_review_rubric.json",
        "g_surf_he_20260812_reviewed_locked_v1.json",
    ):
        assert required in build


def test_launcher_runtime_is_content_addressed_atomic_and_held_immutable():
    launcher = (ROOT / "launcher" / "IFQuantLauncher.cs").read_text(encoding="utf-8")

    assert 'RuntimeFormat = "ifquant-runtime-v2"' in launcher
    assert 'version + "-" + bundleSha256' in launcher
    assert "FileMode.CreateNew" in launcher
    assert "FileOptions.WriteThrough" in launcher
    assert "output.Flush(true)" in launcher
    assert "Directory.Move(staging, finalPath)" in launcher
    assert "FileFlagOpenReparsePoint" in launcher
    assert "information.NumberOfLinks != 1" in launcher
    assert "runtimeDirectoryLeases" in launcher
    assert "runtimeFileLeases" in launcher
    assert "private static void ExtractResource" not in launcher
    assert 'Path.Combine(Path.GetTempPath(), "IFQuantLauncher")' in launcher


def test_launcher_cancellation_is_latched_job_contained_and_single_prompt():
    launcher = (ROOT / "launcher" / "IFQuantLauncher.cs").read_text(encoding="utf-8")
    routes = (ROOT / "launcher" / "MainForm.Routes.partial.cs").read_text(
        encoding="utf-8"
    )

    assert "Interlocked.Exchange(ref cancellationRequested, 1)" in launcher
    assert "ThrowIfCancellationRequested();" in routes
    assert "JobObjectLimitKillOnJobClose = 0x00002000" in launcher
    assert "AssignProcessToJobObject" in launcher
    assert "accounting.ActiveProcesses" in launcher
    assert "TerminateAndWait(process, 15000" in launcher
    assert launcher.count("CancelRunningProcess();") == 1
    assert "RequestCancellationAndTerminate(out cancellationFailure)" in launcher
    assert "cancellationTerminationConfirmed" in launcher
    assert "cancelButton.Enabled = false;" in launcher
    assert 'cancellation ? "cancelled" : manifestStatus' in launcher
    assert 'string status = cancellation' in routes
    assert "closeAfterCancellation" in launcher
    assert 'string.Equals(args[0], "--contained-stage"' in launcher
    assert "containedLaunch.Release();" in launcher
    assert "Thread.Sleep(750)" not in launcher
    assert 'taskKill.FileName = "taskkill.exe"' not in launcher


def test_launcher_quotes_windows_argv_and_executes_stage4():
    launcher = (ROOT / "launcher" / "IFQuantLauncher.cs").read_text(encoding="utf-8")
    routes = (ROOT / "launcher" / "MainForm.Routes.partial.cs").read_text(
        encoding="utf-8"
    )
    routing = (ROOT / "launcher" / "IFQuantLauncher.Routing.cs").read_text(
        encoding="utf-8"
    )

    assert "quoted.Append('\\\\', backslashes * 2);" in routing
    assert "quoted.Append('\\\\', (backslashes * 2) + 1);" in routing
    assert 'string.Equals(args[0], "--argv-echo"' in launcher
    assert '@"C:\\Program Files\\Fiji\\"' in launcher
    assert "ArgumentRoundTripSelfTest" in launcher
    assert "Stage4Python" in routing
    assert "config.Stage4ScriptPath" in routes
    assert "config.Stage4Seal" in routes
    assert "mouse_level_summary.csv" in routes
    assert "group_level_summary.csv" in routes
    assert "AssertStage1DryRunManifest" in routes
    assert "!config.Gate.Exploratory" in routes
    assert '"dry_run_complete"' in routes
    assert "IFQ_WSI_STAGE1_SCRIPT_PATH" in launcher
    assert "IFQ_WSI_STAGE1_SCRIPT_PATH" in routing
    assert "ExpectedStage1ScriptPath" in routing
    assert 'env["IFQ_ENGINE_SCRIPT_PATH"] = Path.GetFullPath(engineScriptPath);' in routing
    assert "ExpectedEngineScriptPath" in routing


def test_launcher_seals_reference_masks_and_stardist_content_authorities():
    launcher = (ROOT / "launcher" / "IFQuantLauncher.cs").read_text(encoding="utf-8")
    routes = (ROOT / "launcher" / "MainForm.Routes.partial.cs").read_text(
        encoding="utf-8"
    )
    routing = (ROOT / "launcher" / "IFQuantLauncher.Routing.cs").read_text(
        encoding="utf-8"
    )
    sharder = (ROOT / "scripts" / "Invoke-Stage2Sharded.ps1").read_text(
        encoding="utf-8"
    )
    docs = (ROOT / "launcher" / "README.md").read_text(encoding="utf-8")

    assert "referenceMaskProfileBox" in routes
    assert "NormalizeOptionalReferenceMaskProfile" in routes
    assert "automatic DAPI/Otsu engineering mask" in routes
    assert 'env["IFQ_WSI_REFERENCE_MASK_PROFILE"]' in routing
    assert "ExpectedReferenceMaskProfilePath" in routing
    assert "WSI_EXTERNAL_REFERENCE_MASK_PROFILE" in routing

    for key in ("IFQ_STARDIST_MODEL_PATH", "IFQ_STARDIST_RUNTIME_MANIFEST"):
        assert key in launcher
        assert key in routing
    assert "NormalizeRequiredStarDistModelPath" in routes
    assert "NormalizeRequiredStarDistRuntimeManifestPath" in routes
    assert 'value, ".zip", "StarDist model"' in routes
    assert 'value, ".json", "StarDist runtime manifest"' in routes
    assert "1024L * 1024L" in routes
    assert "ExpectedStarDistModelPath" in routing
    assert "ExpectedStarDistRuntimeManifestPath" in routing
    assert "classic segmentation must not receive StarDist model" in routing
    assert "LEGACY_STARDIST_AUTHORITY_UNREPRESENTABLE" in routing
    assert "StarDistAuthorityPathSelfTest" in launcher
    assert 'Append(" -StarDistModelPath ")' in routes
    assert 'Append(" -StarDistRuntimeManifest ")' in routes

    assert "foreach ($inheritedName in $inheritedIfqConfig.Keys)" in sharder
    assert (
        "$envPairs[$inheritedName] = [string]$inheritedIfqConfig[$inheritedName]"
        in sharder
    )
    assert "Remove('IFQ_STARDIST_MODEL_PATH')" not in sharder
    assert "Remove('IFQ_STARDIST_RUNTIME_MANIFEST')" not in sharder
    assert "[string]$StarDistModelPath" in sharder
    assert "[string]$StarDistRuntimeManifest" in sharder
    assert "[string]$ScriptPath = \"\"" in sharder
    assert "[System.IO.Directory]::GetParent($PSScriptRoot).FullName" in sharder
    assert "Resolve-RegularAuthorityFile" in sharder
    assert (
        "Launcher-sealed IFQ_STARDIST_MODEL_PATH disagrees with "
        "-StarDistModelPath" in sharder
    )
    assert (
        "Launcher-sealed IFQ_STARDIST_RUNTIME_MANIFEST disagrees with "
        in sharder
    )
    assert "$envPairs['IFQ_STARDIST_MODEL_PATH'] = $StarDistModelPath" in sharder
    assert (
        "$envPairs['IFQ_STARDIST_RUNTIME_MANIFEST'] = $StarDistRuntimeManifest"
        in sharder
    )
    assert sharder.count("$StarDistModelPath") >= 8
    assert sharder.count("$StarDistRuntimeManifest") >= 8
    assert "Content provenance does not establish segmentation performance" in routes
    assert "automatic DAPI/Otsu engineering mask" in docs
    assert "model accuracy, segmentation performance" in docs


def test_launcher_exposes_only_isolated_he_engineering_review_tools():
    launcher = (ROOT / "launcher" / "IFQuantLauncher.cs").read_text(encoding="utf-8")
    routing = (ROOT / "launcher" / "IFQuantLauncher.Routing.cs").read_text(
        encoding="utf-8"
    )
    routes = (ROOT / "launcher" / "MainForm.Routes.partial.cs").read_text(
        encoding="utf-8"
    )
    review = (ROOT / "launcher" / "HeReviewForm.cs").read_text(encoding="utf-8")
    build = (ROOT / "launcher" / "build.ps1").read_text(encoding="utf-8")
    legacy_runner = (ROOT / "launcher" / "run_legacy_equivalence.ps1").read_text(
        encoding="utf-8"
    )
    launcher_docs = (ROOT / "launcher" / "README.md").read_text(encoding="utf-8")
    root_docs = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "public static readonly bool BrightfieldRouteEnabled = false;" in routing
    assert "HeReviewForm.cs" in build
    assert "HeReviewForm.cs" in legacy_runner
    assert "heReviewToolsButton" in routes
    assert "Open H&E engineering/review tools (no biological analysis)" in routes
    assert "using (HeReviewForm review" in routes
    assert "BuildStage2" not in review
    assert "RunEnvironment" not in review

    for command in ("status", "build-review", "aggregate-review"):
        assert f'"{command}"' in review
    assert '"-I", "-B", "-S"' in review
    assert "EnvironmentApply.PrepareNonAnalysis(info);" in review
    assert "ClearIfq(psi.EnvironmentVariables);" in routing
    assert "ContainedStageLaunch.Prepare(info)" in review
    assert "ProcessJob.CreateArmed()" in review

    for resource in (
        "IFQuant.he_pipeline.py",
        "IFQuant.g_surf_he_20260812.json",
        "IFQuant.he_pathology_review_rubric.json",
        "IFQuant.g_surf_he_20260812_reviewed_locked_v1.json",
    ):
        assert resource in launcher
        assert resource in build
    assert "paths.MeasurementSchemaPath" in launcher
    assert "ifquant\", \"adapters.py" in review
    assert "ifquant\", \"contracts.py" in review
    assert "ifquant\", \"route_records.py" in review
    assert "ifquant\", \"stage2_index.py" in review

    assert ".ifquant-he-staging-" in review
    assert "NormalizeFreshOutput" in review
    assert "Directory.Move(staging, final);" in review
    assert "ValidateReviewPackage(staging, runtime)" in review
    assert "ValidateAggregatePackage(" in review
    assert "staging, runtime, reviewCsv" in review
    assert "PACKAGE_MANIFEST.json" in review
    assert "he_review_aggregation.audit.json" in review
    assert "FileShare.Read | FileShare.Delete" not in review
    assert "H&E leased package file allowed rename or delete" in review
    assert "File.Move(leasedArtifact, renamedArtifact)" in review
    assert "File.Delete(leasedArtifact)" in review
    assert "inputs.VerifyUnchanged();" in review
    assert "package.VerifyUnchanged();" in review
    assert "technical_sections_are_biological_replicates" in review
    assert "group_inference_supported" in review
    assert "Require(!(value is bool)" in review
    assert "CanonicalJsonSha256(audit, \"audit_payload_sha256\")" in review
    assert "H&E integer parser accepted a JSON boolean as a number" in review
    assert "inputArtifacts.Length == 12" in review
    assert 'string inputRole = StringAt(item, "role")' in review
    assert "Require(inputRoles.Add(inputRole)" in review
    assert "ExpectedAggregateRole(relative)" in review
    assert 'StringAt(records, "schema_sha256")' in review
    assert 'inputHashes["aggregation_code"]' in review
    assert 'inputHashes["blinded_review_csv"] == Sha256File(reviewCsv)' in review
    assert 'inputHashes["python_interpreter"]' in review
    assert "values.Length == 13" in review
    assert "ValidateSectionScores(" in review
    assert "ValidateAuthoritativeReview(reviewCsv" in review
    assert "RequireLockedReviewFieldsMatch(" in review
    assert "ValidateMouseSummaries(" in review
    assert "ValidateTechnicalAgreement(" in review
    assert "ValidateMeasurementRecords(" in review
    assert "ExpectedMeasurementRecordId(" in review
    assert 'return "ifqmr-" + CanonicalJsonValueSha256(measurementIdentity)' in review
    assert "expectedSections.Count *" in review
    assert "OrdinalEndpoints.Length" in review
    assert "StringArrayEquals(" in review
    assert "CanonicalJsonValueSha256(LockedReviewFields)" in review
    assert "PythonProbeArguments()" in review
    assert 'implementation == "CPython"' in review
    assert "major == 3 && minor >= 10" in review
    assert "reportedPath, selectedPath" in review
    assert "pythonIdentity.Receipt" in review
    assert "operationPythonReceipt = identity.Receipt" in review
    assert "ReceiptSuffix(pythonReceipt)" in review
    assert "lock (publicationSync)" in review
    assert "finalOutputMoved = true" in review
    assert "finalOutputCommitted = true" in review
    assert "FINAL PATH EXISTS BUT IS NOT AUTHORIZED" in review
    assert "Action validatePublished" in review
    assert "ValidateReviewPackage(\n                                           final, runtime)" in review
    assert "ValidateAggregatePackage(\n                                           final, runtime" in review
    assert review.count("Directory.Move(staging, final);") == 1
    assert review.count("PublishStaging(") == 3
    assert "Directory.Move(moveSource, moveFinal);" in review
    assert "HePackageLease.Open(moveFinal)" in review
    assert "biological execution disabled" in launcher_docs
    assert "separate review-only launcher screen" in root_docs
