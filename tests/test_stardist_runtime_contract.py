import json
import unittest
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]


def artifact(role, expected_classes):
    return {
        "role": role,
        "path": f"runtime/{role}.bin",
        "size_bytes": 1,
        "sha256": "0" * 64,
        "expected_classes": expected_classes,
    }


def valid_manifest():
    return {
        "schema_version": "1.0.0",
        "profile_id": "fiji-stardist-lung-v1",
        "artifacts": [
            artifact(
                "stardist_plugin",
                [
                    "de.csbdresden.stardist.StarDist2D",
                    "de.csbdresden.stardist.StarDist2DNMS",
                ],
            ),
            artifact(
                "csbdeep_plugin",
                ["de.csbdresden.csbdeep.commands.GenericNetwork"],
            ),
            artifact("tensorflow_java", ["org.tensorflow.Graph"]),
            artifact("tensorflow_native", []),
        ],
    }


class StarDistRuntimeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = (ROOT / "IF_Quant_Pipeline.groovy").read_text(
            encoding="utf-8"
        )
        cls.schema = json.loads(
            (ROOT / "schemas" / "stardist-runtime-manifest.schema.json").read_text(
                encoding="utf-8"
            )
        )
        cls.validator = Draft202012Validator(cls.schema)

    def test_schema_accepts_required_closed_runtime_manifest(self):
        self.validator.validate(valid_manifest())

    def test_schema_rejects_missing_role_class_and_unknown_fields(self):
        missing_role = valid_manifest()
        missing_role["artifacts"][-1]["role"] = "support_file"
        self.assertTrue(list(self.validator.iter_errors(missing_role)))

        missing_class = valid_manifest()
        missing_class["artifacts"][0]["expected_classes"].pop()
        self.assertTrue(list(self.validator.iter_errors(missing_class)))

        unknown = valid_manifest()
        unknown["unsealed_note"] = "not allowed"
        self.assertTrue(list(self.validator.iter_errors(unknown)))

    def test_schema_rejects_invalid_hash_size_and_profile(self):
        for mutate in (
            lambda doc: doc["artifacts"][0].update(sha256="A" * 64),
            lambda doc: doc["artifacts"][0].update(size_bytes=0),
            lambda doc: doc["artifacts"][0].update(size_bytes=1.5),
            lambda doc: doc.update(profile_id="unsafe profile"),
        ):
            doc = deepcopy(valid_manifest())
            mutate(doc)
            self.assertTrue(list(self.validator.iter_errors(doc)))

    def test_engine_requires_explicit_model_and_runtime_manifest(self):
        self.assertIn('envOr("IFQ_STARDIST_MODEL_PATH", "")', self.engine)
        self.assertIn('envOr("IFQ_STARDIST_RUNTIME_MANIFEST", "")', self.engine)
        self.assertIn(
            "IFQ_STARDIST_MODEL_PATH is required when IFQ_SEGMENTER=stardist",
            self.engine,
        )
        self.assertIn(
            "IFQ_STARDIST_RUNTIME_MANIFEST is required when IFQ_SEGMENTER=stardist",
            self.engine,
        )
        self.assertIn("validateStarDistModelArchive(model)", self.engine)
        self.assertIn("is not a readable, intact ZIP archive", self.engine)
        self.assertIn(
            "IFQ_STARDIST_RUNTIME_MANIFEST must identify a .json file",
            self.engine,
        )
        self.assertIn('model_choice: "Model (.zip) from File"', self.engine)
        self.assertNotIn('"Versatile (fluorescent nuclei)"', self.engine)
        self.assertNotIn("plugin_managed_builtin_not_content_bound", self.engine)

    def test_engine_binds_required_runtime_roles_to_loaded_class_origins(self):
        for role in (
            "stardist_plugin",
            "csbdeep_plugin",
            "tensorflow_java",
            "tensorflow_native",
        ):
            self.assertIn(f"{role}:", self.engine)
        for class_name in (
            "de.csbdresden.stardist.StarDist2D",
            "de.csbdresden.stardist.StarDist2DNMS",
            "de.csbdresden.csbdeep.commands.GenericNetwork",
            "org.tensorflow.Graph",
        ):
            self.assertIn(class_name, self.engine)
        self.assertIn("getProtectionDomain()?.getCodeSource()?.getLocation()", self.engine)
        self.assertIn("not its sealed manifest artifact", self.engine)

    def test_engine_uses_headless_scijava_label_output_without_roi_manager(self):
        start = self.engine.index("def runStarDistLabelCommand")
        end = self.engine.index("def segmentNuclei", start)
        command = self.engine[start:end]
        self.assertIn('IJ.runPlugIn("org.scijava.Context", "")', command)
        self.assertIn('Class.forName("org.scijava.command.CommandService")', command)
        self.assertIn('Class.forName("de.csbdresden.stardist.StarDist2D")', command)
        self.assertIn('params.put("modelFile",', command)
        self.assertIn('params.put("outputType", "Label Image")', command)
        self.assertIn('module.getOutput("label")', command)

        star_branch = self.engine[
            self.engine.index('if (cfg.segmenter == "stardist")') :
            self.engine.index("  } else {", self.engine.index('if (cfg.segmenter == "stardist")'))
        ]
        self.assertNotIn("RoiManager", star_branch)
        self.assertNotIn("Command From Macro", star_branch)
        self.assertNotIn("crop.show()", star_branch)

    def test_engine_verifies_runtime_before_after_and_records_label_evidence(self):
        self.assertGreaterEqual(
            self.engine.count("verifyStarDistAuthority(cfg.stardistAuthority)"), 2
        )
        self.assertIn(
            'contentSnapshot(new File(authority.model_path), "IFQ_STARDIST_MODEL_PATH")',
            self.engine,
        )
        self.assertIn("runtime_manifest_content", self.engine)
        self.assertIn("runtime_artifacts", self.engine)
        self.assertIn("class_bindings", self.engine)
        self.assertIn("__StarDist_instance_labels.tif", self.engine)
        self.assertIn("raw > 65535.0d", self.engine)
        self.assertIn("unsigned 16-bit integer labels", self.engine)
        self.assertIn("canonical_pixel_sha256", self.engine)
        self.assertIn("output_content_verified_before_params = true", self.engine)


if __name__ == "__main__":
    unittest.main()
