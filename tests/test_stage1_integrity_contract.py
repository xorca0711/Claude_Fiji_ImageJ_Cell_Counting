import ast
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class Stage1IntegritySourceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stage1 = (ROOT / "qupath_wsi_tile_export.groovy").read_text(
            encoding="utf-8"
        )
        cls.launcher_routing = (
            ROOT / "launcher" / "IFQuantLauncher.Routing.cs"
        ).read_text(encoding="utf-8")
        cls.launcher_ui = (
            ROOT / "launcher" / "MainForm.Routes.partial.cs"
        ).read_text(encoding="utf-8")
        cls.launcher = (ROOT / "launcher" / "IFQuantLauncher.cs").read_text(
            encoding="utf-8"
        )
        cls.stage2_launcher = (
            ROOT / "scripts" / "Invoke-Stage2Sharded.ps1"
        ).read_text(encoding="utf-8")
        cls.engine = (ROOT / "IF_Quant_Pipeline.groovy").read_text(
            encoding="utf-8"
        )

    def test_ordered_channel_patterns_are_positional_full_matches(self):
        self.assertIn('envOr(\n    "IFQ_WSI_ORDERED_CHANNEL_PATTERNS"', self.stage1)
        self.assertIn('.split(";", -1)', self.stage1)
        self.assertIn("ORDERED_CHANNEL_PATTERN_TEXTS.size() != EXPECT_NCH", self.stage1)
        self.assertIn("Pattern.compile(", self.stage1)
        self.assertIn(".matcher(name.toString()).matches()", self.stage1)
        self.assertNotIn("IFQ_WSI_CHANNEL_PATTERNS", self.stage1)
        self.assertIn(
            'channel_order_authority: "acquisition_order_pattern_only_not_biological_identity"',
            self.stage1,
        )
        self.assertIn("ordered_channel_names: chosen.channelNames", self.stage1)

        block = re.search(
            r"def DEFAULT_ORDERED_CHANNEL_PATTERNS = \[(.*?)\n\]",
            self.stage1,
            re.DOTALL,
        )
        self.assertIsNotNone(block)
        encoded_patterns = re.findall(
            r'''^\s*((?:'(?:\\.|[^'])*')|(?:"(?:\\.|[^"])*"))''',
            block.group(1),
            re.MULTILINE,
        )
        patterns = [
            re.compile(ast.literal_eval(value), re.IGNORECASE)
            for value in encoded_patterns
        ]
        self.assertEqual(len(patterns), 4)

        canonical = ["DAPI", "FITC", "Cy3", "Cy5(Gray)_01"]
        self.assertTrue(all(pattern.fullmatch(name) for pattern, name in zip(patterns, canonical)))
        permuted = ["DAPI", "Cy3", "FITC", "Cy5(Gray)_01"]
        self.assertFalse(all(pattern.fullmatch(name) for pattern, name in zip(patterns, permuted)))
        duplicated = ["DAPI", "DAPI", "DAPI", "DAPI"]
        self.assertFalse(all(pattern.fullmatch(name) for pattern, name in zip(patterns, duplicated)))

    def test_resume_is_disabled_at_script_ui_gate_and_launch_seal(self):
        self.assertIn('envBool("IFQ_WSI_RESUME", false)', self.stage1)
        guard = self.stage1.index("if (RESUME) {")
        tile_loop = self.stage1.index("slides.each { slideFile ->")
        self.assertLess(guard, tile_loop)
        self.assertIn("content-addressed", self.stage1[guard:tile_loop])

        self.assertIn("public bool WsiResume;", self.launcher_routing)
        self.assertNotIn("public bool WsiResume = true;", self.launcher_routing)
        self.assertIn("wsiResumeBox.Checked = false;", self.launcher_ui)
        self.assertIn("wsiResumeBox.Enabled = false;", self.launcher_ui)
        self.assertIn("WSI_RESUME_NOT_CONTENT_ADDRESSED", self.launcher_routing)
        self.assertIn(
            '"IFQ_WSI_PANEL", "IFQ_WSI_INPUT", "IFQ_WSI_OUTPUT", "IFQ_WSI_RESUME"',
            self.launcher_routing,
        )
        self.assertIn("IFQ_WSI_RESUME must be false", self.launcher_routing)

    def test_stage1_requires_an_empty_output_root_before_any_artifact_write(self):
        guard = self.stage1.index("def existingOutputEntries = outRoot.listFiles()")
        create = self.stage1.index("} else if (!outRoot.mkdirs())")
        run_record = self.stage1.index("def runRecord =")
        slide_loop = self.stage1.index("slides.each { slideFile ->")
        self.assertLess(guard, create)
        self.assertLess(create, run_record)
        self.assertLess(run_record, slide_loop)
        self.assertIn("existingOutputEntries.length > 0", self.stage1[guard:create])
        self.assertIn("Stage 1 resume is unavailable", self.stage1[guard:run_record])

    def test_stage1_binds_exact_bioformats_package_and_executed_script(self):
        self.assertIn('envOr("IFQ_WSI_STAGE1_SCRIPT_PATH", "")', self.stage1)
        self.assertIn("ContentHash.sha256File(stage1ScriptFile)", self.stage1)
        self.assertIn("reader.getUsedFiles()", self.stage1)
        self.assertIn(
            'discovery_authority: "bioformats_ImageReader_getUsedFiles"',
            self.stage1,
        )
        self.assertIn("package_hash_algorithm", self.stage1)
        self.assertIn("source_package: sourcePackageBefore", self.stage1)
        self.assertIn("sourcePackageAfter != sourcePackageBefore", self.stage1)
        manifest_write = self.stage1.index("stage1ManifestPath")
        post_source_check = self.stage1.index(
            "sourcePackageAfter != sourcePackageBefore"
        )
        post_script_check = self.stage1.index(
            "stage1ScriptRecordAfter != stage1ScriptRecord"
        )
        self.assertLess(post_source_check, manifest_write)
        self.assertLess(post_script_check, manifest_write)

    def test_candidate_manifest_keeps_non_workload_grid_cores_explicit(self):
        self.assertIn('envDouble("IFQ_WSI_MIN_TILE_TISSUE_UM2", 0.0d)', self.stage1)
        self.assertIn('new File(slideOut, "tile_candidate_manifest.csv")', self.stage1)
        for status in (
            "outside_tissue",
            "below_minimum_tissue",
            "empty_raster",
            "exported",
            "resumed",
            "dry_run",
            "excluded_by_max_tiles_cap",
        ):
            self.assertIn(f'"{status}"', self.stage1)
        self.assertIn("nSkippedLowTissue", self.stage1)
        self.assertIn("nSkippedEmptyRaster", self.stage1)
        self.assertIn("n_skipped_low_tissue: nSkippedLowTissue", self.stage1)
        self.assertIn("n_skipped_empty_raster: nSkippedEmptyRaster", self.stage1)
        self.assertIn("n_candidate_excluded_by_cap: nExcludedByCap", self.stage1)
        self.assertNotIn("cy < H && !capped", self.stage1)
        self.assertNotIn("capped = true; break", self.stage1)
        coverage = re.search(
            r"boolean coverageComplete = (.*?)\n\s*runRecord\.slides",
            self.stage1,
            re.DOTALL,
        )
        self.assertIsNotNone(coverage)
        for requirement in ("!capped", "!DRY_RUN", "nSkippedLowTissue == 0", "nSkippedEmptyRaster == 0"):
            self.assertIn(requirement, coverage.group(1))
        self.assertIn("relDiff <= 1e-6", coverage.group(1))
        self.assertIn(
            "Tile/reference area reconciliation failed", self.stage1
        )

    def test_stage1_profile_binds_partition_denominator_settings(self):
        downstream = re.search(
            r"downstream:\s*\[(.*?)\n\s*\],\n\s*slides:",
            self.stage1,
            re.DOTALL,
        )
        self.assertIsNotNone(downstream)
        for field in (
            "partition_damage: PARTITION",
            "ager_channel: AGER_CH",
            "ager_threshold: (PARTITION ? AGER_THRESHOLD : null)",
            "damage_sigma_um: DAMAGE_SIGMA",
            "damage_cutoff: DAMAGE_CUTOFF",
            "roi_compartment: ROI_COMPARTMENT",
            "roi_name: ROI_NAME",
            "roi_name_damaged: ROI_DAMAGED",
            "roi_name_intact: ROI_INTACT",
        ):
            self.assertIn(field, downstream.group(1))
        self.assertIn("Double.isFinite(AGER_THRESHOLD)", self.stage1)

    def test_sharded_preflight_rejects_incomplete_stage1_before_mutation(self):
        preflight_start = self.stage2_launcher.index("$matchingStage1Slides")
        shard_mutation = self.stage2_launcher.index("# ---- build shards")
        process_start = self.stage2_launcher.index("$psi = New-Object System.Diagnostics.ProcessStartInfo")
        self.assertLess(preflight_start, shard_mutation)
        self.assertLess(preflight_start, process_start)
        for field in (
            "coverage_complete",
            "dry_run",
            "n_skipped_low_tissue",
            "n_skipped_empty_raster",
        ):
            self.assertIn(field, self.stage2_launcher[preflight_start:shard_mutation])
        self.assertIn("Test-IsJsonInteger", self.stage2_launcher)
        self.assertIn("must contain exactly one slide_stem", self.stage2_launcher)

    def test_launcher_and_engine_environment_surfaces_are_not_noops(self):
        self.assertIn('"IFQ_WSI_ORDERED_CHANNEL_PATTERNS"', self.launcher_routing)
        self.assertNotIn('"IFQ_WSI_CHANNEL_PATTERNS"', self.launcher_routing)
        for name, reader in (
            ("IFQ_STARDIST_PROB", "envDouble"),
            ("IFQ_STARDIST_NMS", "envDouble"),
            ("IFQ_STARDIST_TILES", "envInt"),
        ):
            self.assertIn(f'"{name}"', self.launcher_routing)
            self.assertIn(f'{reader}("{name}"', self.engine)

    def test_stardist_rejection_qc_is_scoped_to_the_current_region(self):
        start = self.engine.index('if (cfg.segmenter == "stardist")')
        end = self.engine.index('  } else {', start)
        block = self.engine[start:end]
        self.assertIn("if (inRegion) {", block)
        self.assertNotIn('reason = !inRegion', block)
        self.assertNotIn('"outside_analysis_region"', self.engine)

    def test_engine_parses_hashes_and_copies_one_config_byte_snapshot(self):
        self.assertIn("def sha256Bytes(byte[] payload)", self.engine)
        self.assertIn("def utf8Text(byte[] payload)", self.engine)
        self.assertIn("MARKER_REGISTRY_BYTES = markerRegistryFile.isFile()", self.engine)
        self.assertEqual(self.engine.count("markerRegistryFile.isFile()"), 1)
        self.assertIn("utf8Text(MARKER_REGISTRY_BYTES)", self.engine)
        self.assertIn("PANEL_CONFIG_BYTES = panelConfigFile.bytes", self.engine)
        self.assertIn("utf8Text(PANEL_CONFIG_BYTES)", self.engine)
        for loader in ("loadSamplesheet", "loadPanelMap", "loadCanonicalManifest"):
            start = self.engine.index(f"def {loader}")
            next_method = self.engine.find("\ndef ", start + 5)
            block = self.engine[start:next_method if next_method >= 0 else None]
            self.assertIn("byte[] sourceBytes = f.bytes", block)
            self.assertIn("sourceSha256:sha256Bytes(sourceBytes)", block)
        self.assertIn("panelMap.sourceBytes", self.engine)
        self.assertIn("canonicalManifest.sourceBytes", self.engine)
        self.assertNotIn("panelMap.source.bytes", self.engine)
        self.assertNotIn("canonicalManifest.source.bytes", self.engine)

    def test_config_snapshots_are_hashed_locked_and_passed_to_every_shard(self):
        source = self.stage2_launcher
        self.assertIn("$MarkerRegistryPath", source)
        self.assertIn("$PanelConfigPath", source)
        self.assertIn("function New-ContentAddressedSnapshot", source)
        self.assertIn("stage2_config_provenance", source)
        self.assertIn("$markerRegistrySnapshot.Path", source)
        self.assertIn("IFQ_MARKER_REGISTRY", source)
        self.assertIn("IFQ_PANEL_CONFIG", source)
        lock_section = source[source.index("$sourceLockPaths ="):source.index("$cp =")]
        self.assertIn("$markerRegistrySnapshot.Path", lock_section)
        self.assertIn("$panelConfigSnapshotPath", lock_section)
        self.assertIn("$candidateManifest", lock_section)

    def test_shards_have_explicit_stardist_values_and_no_inherited_ifq_surface(self):
        source = self.stage2_launcher
        for declaration in (
            "[double]$StarDistProbability = 0.5",
            "[double]$StarDistNms = 0.4",
            "[int]$StarDistTiles = 1",
        ):
            self.assertIn(declaration, source)
        for key in (
            "IFQ_STARDIST_PROB",
            "IFQ_STARDIST_NMS",
            "IFQ_STARDIST_TILES",
        ):
            self.assertIn(key, source[source.index("$envPairs = @{"):])
        process_block = source[source.index("$psi = New-Object"):source.index("$psi.RedirectStandardOutput")]
        self.assertIn("$psi.EnvironmentVariables.Keys", process_block)
        self.assertIn(".StartsWith('IFQ_'", process_block)
        self.assertIn("$psi.EnvironmentVariables.Remove($inheritedIfqKey)", process_block)
        self.assertLess(
            process_block.index("EnvironmentVariables.Remove"),
            process_block.index("foreach ($k in $envPairs.Keys)"),
        )

    def test_shard_process_launch_is_powershell_51_compatible_and_cleaned_up(self):
        source = self.stage2_launcher
        self.assertIn("function ConvertTo-WindowsCommandLineArgument", source)
        self.assertIn("function Join-WindowsCommandLineArguments", source)
        self.assertIn(
            "$psi.Arguments = Join-WindowsCommandLineArguments -Arguments $javaArguments",
            source,
        )
        runtime = source[source.index("$psi = New-Object System.Diagnostics.ProcessStartInfo"):]
        self.assertNotIn("$psi.ArgumentList", runtime)
        self.assertIn("function Stop-StartedShardProcesses", source)
        self.assertIn("Stop-StartedShardProcesses -ProcessRecords $procs", source)
        self.assertIn("$procs += $processRecord", source)

    def test_shard_hard_links_are_identity_locked_before_any_jvm_launch(self):
        source = self.stage2_launcher
        source_lock = source.index("$sourceLockPaths =")
        hard_link = source.index("New-Item -ItemType HardLink")
        destination_lock = source.index(
            "OpenReadLockNoReparse(\n                $destinationPath"
        )
        identity_check = source.index(
            "[IFQuant.Native.Stage2ProcessJobV1]::RequireSameFile("
        )
        process_start = source.index("[System.Diagnostics.Process]::Start($psi)")
        self.assertLess(source_lock, hard_link)
        self.assertLess(hard_link, destination_lock)
        self.assertLess(destination_lock, identity_check)
        self.assertLess(identity_check, process_start)
        self.assertIn("FileFlagOpenReparsePoint", source)
        self.assertIn("VolumeSerialNumber", source)
        self.assertIn("FileIndexHigh", source)
        self.assertIn("FileIndexLow", source)

    def test_launcher_sealed_stage2_environment_is_preserved_fail_closed(self):
        source = self.stage2_launcher
        self.assertIn("[switch]$UseInheritedIfqConfig", source)
        self.assertIn("$inheritedIfqConfig = @{}", source)
        for required in (
            "IFQ_RECURSIVE = 'false'",
            "IFQ_INCLUDE_REGEX = '.*'",
            "IFQ_MAX_IMAGES = '0'",
            "IFQ_MIN_INCLUDED_NUCLEI = '0'",
            "IFQ_PROJECTION = 'max'",
            "IFQ_ALLOW_NONEMPTY_OUTPUT = 'false'",
        ):
            self.assertIn(required, source)
        copy = source.index("foreach ($inheritedName in $inheritedIfqConfig.Keys)")
        dynamic_overlay = source.index("$envPairs['IFQ_INPUT_DIR'] = $s.In", copy)
        child_start = source.index("[System.Diagnostics.Process]::Start($psi)")
        self.assertLess(copy, dynamic_overlay)
        self.assertLess(dynamic_overlay, child_start)

    def test_shard_processes_are_contained_by_a_ctrl_c_safe_job_object(self):
        source = self.stage2_launcher
        for contract in (
            "JobObjectLimitKillOnJobClose = 0x00002000",
            "SetConsoleCtrlHandler",
            "BeginProcessLaunch",
            "AssignProcessOrTerminate",
            "EndProcessLaunch",
            "TerminateAndCloseActiveJob",
        ):
            self.assertIn(contract, source)
        self.assertIn(
            "$stage2ProcessJob = [IFQuant.Native.Stage2ProcessJobV1]::CreateAndArm()",
            source,
        )
        self.assertEqual(
            source.count(
                "[IFQuant.Native.Stage2ProcessJobV1]::AssignProcessOrTerminate("
            ),
            2,
        )
        self.assertIn(
            "[IFQuant.Native.Stage2ProcessJobV1]::Close($stage2ProcessJob)",
            source,
        )
        handler = source[source.index("private static bool HandleConsoleControl"):]
        publish_cancel = handler.index(
            "Interlocked.Exchange(ref cancellationRequested, 1)"
        )
        observe_launch = handler.index("Volatile.Read(ref launchInProgress)")
        self.assertLess(publish_cancel, observe_launch)

    def test_launcher_max_images_self_test_requires_explicit_exclusions(self):
        self.assertIn("maxImagesExcludedFiles", self.launcher)
        self.assertIn("incomplete_max_images_limit", self.launcher)
        self.assertIn('"if (DISPLAY_PREVIEW_ONLY) files = files.take(5)"', self.launcher)


if __name__ == "__main__":
    unittest.main()
