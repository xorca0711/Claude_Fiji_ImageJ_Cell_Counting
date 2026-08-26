import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class EngineProvenanceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = (ROOT / "IF_Quant_Pipeline.groovy").read_text(encoding="utf-8")
        cls.stage2 = (ROOT / "scripts" / "Invoke-Stage2Sharded.ps1").read_text(
            encoding="utf-8"
        )
        cls.confocal = (ROOT / "scripts" / "run_confocal_260808.ps1").read_text(
            encoding="utf-8"
        )

    def test_engine_hashes_its_script_and_every_analytical_input_twice(self):
        self.assertIn('envOr("IFQ_ENGINE_SCRIPT_PATH", "")', self.engine)
        self.assertIn(
            'contentSnapshot(engineScriptFile, "IFQ_ENGINE_SCRIPT_PATH")',
            self.engine,
        )
        self.assertIn('contentSnapshot(f, "analytical input")', self.engine)
        self.assertIn("source_content_verified_before_and_after: true", self.engine)
        self.assertIn("engine_script_verified_before_and_after = true", self.engine)
        self.assertIn(
            'input_content_authority = "sha256_streamed_bytes_verified_before_and_after_analysis"',
            self.engine,
        )

    def test_engine_atomically_seals_outputs_and_publishes_manifest_last(self):
        self.assertIn("def atomicWriteBytes(File target, byte[] payload)", self.engine)
        self.assertIn("output.getFD().sync()", self.engine)
        self.assertNotIn("output.fd.sync()", self.engine)
        self.assertIn("StandardCopyOption.ATOMIC_MOVE", self.engine)
        self.assertIn('run_manifest_schema_version: "2.0.0"', self.engine)
        self.assertIn(
            'publication_contract: "sealed_outputs_manifest_published_last"',
            self.engine,
        )
        self.assertIn('outputRoot, "run_summary", "run_summary.csv"', self.engine)
        self.assertIn('outputRoot, "image_params", imageRecord.params_relative_path', self.engine)
        self.assertIn('outputRoot, "summary_workbook", manifest.summary_workbook', self.engine)
        publication_block = self.engine.index(
            "// Seal the exact publication set only after every canonical output"
        )
        manifest_write = self.engine.index(
            'new File(outputRoot, "run_manifest.json")', publication_block
        )
        artifact_set = self.engine.index(
            "manifest.output_artifact_set_sha256 = outputArtifactSetSha256"
        )
        self.assertLess(artifact_set, manifest_write)

    def test_engine_rechecks_authorities_at_manifest_publication(self):
        publication_block = self.engine.index(
            "// Seal the exact publication set only after every canonical output"
        )
        self.assertGreater(
            self.engine.index("verifyStarDistAuthority(stardistAuthority)", publication_block),
            publication_block,
        )
        self.assertGreater(
            self.engine.index(
                'contentSnapshot(engineScriptFile, "IFQ_ENGINE_SCRIPT_PATH")',
                publication_block,
            ),
            publication_block,
        )
        self.assertIn(
            'contentSnapshot(currentSource, "analytical input at manifest publication")',
            self.engine,
        )

    def test_preview_only_run_does_not_quarantine_quantitative_manifest(self):
        self.assertIn(
            "if (!DISPLAY_PREVIEW_ONLY && priorRunManifest.isFile())",
            self.engine,
        )

    def test_stage2_children_override_engine_path_to_verified_snapshot(self):
        inherited = self.stage2.index(
            "Launcher-sealed Stage 2 environment is missing IFQ_ENGINE_SCRIPT_PATH"
        )
        dynamic_overlay = self.stage2.index(
            "$envPairs['IFQ_ENGINE_SCRIPT_PATH'] = $ExecutionScriptPath"
        )
        child_start = self.stage2.index("[System.Diagnostics.Process]::Start($psi)")
        self.assertLess(inherited, dynamic_overlay)
        self.assertLess(dynamic_overlay, child_start)

    def test_direct_confocal_runner_supplies_executed_engine_path(self):
        assignment = '$env:IFQ_ENGINE_SCRIPT_PATH = "$repo\\IF_Quant_Pipeline.groovy"'
        self.assertIn(assignment, self.confocal)
        self.assertLess(
            self.confocal.index(assignment),
            self.confocal.index('--run "$repo\\IF_Quant_Pipeline.groovy"'),
        )

    def test_direct_runner_requires_a_sealed_complete_manifest_for_zero_exit(self):
        self.assertNotIn("[string]$IncludeRegex", self.confocal)
        self.assertIn(
            '$env:IFQ_INCLUDE_REGEX  = ".*20x 2k.*\\.oir"', self.confocal
        )
        self.assertIn('$manifestStatus -eq "complete"', self.confocal)
        self.assertIn('$publicationStatus -eq "sealed_complete"', self.confocal)
        self.assertIn('$finalExit -eq 0 -and -not $manifestAccepted', self.confocal)
        self.assertIn('$finalExit = 1', self.confocal)


if __name__ == "__main__":
    unittest.main()
