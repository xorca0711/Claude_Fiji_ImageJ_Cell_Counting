// =====================================================================
// HeReviewForm.cs                                                v1.9.7
// ---------------------------------------------------------------------
// Isolated H&E engineering/review tooling.  This screen deliberately is
// NOT a Route 3 analysis runner.  Its command surface is closed to the
// three review-only commands in the packaged scripts/he_pipeline.py:
// status, build-review and aggregate-review.
// =====================================================================

using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using IFQuantLauncher.Routing;

namespace IFQuantLauncher
{
    internal sealed class HeAuthorityRoots
    {
        public string SourceRoot;
        public string R1Root;
        public string H4Root;
    }

    internal sealed class HeStatusSnapshot
    {
        public string Summary;
    }

    internal sealed class HePythonIdentity
    {
        public string ExecutablePath;
        public string ExecutableSha256;
        public string Implementation;
        public int Major;
        public int Minor;
        public int Micro;

        public string Receipt
        {
            get
            {
                return "Python runtime: " + Implementation + " " +
                    Major.ToString(CultureInfo.InvariantCulture) + "." +
                    Minor.ToString(CultureInfo.InvariantCulture) + "." +
                    Micro.ToString(CultureInfo.InvariantCulture) + "\r\n" +
                    "Python executable: " + ExecutablePath + "\r\n" +
                    "Python executable SHA-256: " + ExecutableSha256;
            }
        }
    }

    internal static class HeReviewContract
    {
        internal const string StudyId = "g_surf_he_20260812";
        internal const string ReviewManifestStatus =
            "H5_H7_DEVELOPMENT_REVIEW_REQUIRED_NOT_AN_ANALYSIS_RESULT";
        internal const string AggregateAuditStatus =
            "ACCEPTED_REVIEW_AGGREGATED_DESCRIPTIVE_ONLY";

        private static readonly string[] LockedReviewFields = new string[]
        {
            "blind_section_id",
            "reviewable_yes_no_uncertain",
            "whole_section_inflammation_extent_0_4_uncertain",
            "alveolar_interstitial_inflammation_0_4_uncertain",
            "peribronchial_inflammation_0_4_uncertain",
            "perivascular_inflammation_0_4_uncertain",
            "consolidation_airspace_loss_0_4_uncertain",
            "airway_epithelial_injury_debris_0_4_uncertain",
            "edema_hemorrhage_necrosis_present_yes_no_uncertain",
            "dominant_pattern",
            "representative_region_ids",
            "technical_limitation_none_minor_major",
            "confidence_low_medium_high",
            "reviewer_id",
            "reviewed_utc",
            "notes"
        };

        private sealed class HeOrdinalEndpoint
        {
            internal string SourceColumn;
            internal string EndpointId;
            internal string ReferenceSpaceId;
            internal string CompartmentLabel;
            internal string ScaleId;
        }

        private sealed class HeSectionIdentity
        {
            internal string BlindSectionId;
            internal string SectionId;
            internal string MouseId;
            internal string Genotype;
            internal string Condition;
            internal int TechnicalSectionOrder;
            internal string SourcePackageSha256;
        }

        private sealed class HeCsvTable
        {
            internal string[] Header;
            internal List<Dictionary<string, string>> Rows;
        }

        private static readonly HeOrdinalEndpoint[] OrdinalEndpoints =
            new HeOrdinalEndpoint[]
        {
            Endpoint(
                "whole_section_inflammation_extent_0_4_uncertain",
                "he.whole_section_inflammation_extent",
                "he.usable_whole_section.v1",
                "whole_section_usable_tissue",
                "g_surf-he-inflammation-extent-0-4-v1"),
            Endpoint(
                "alveolar_interstitial_inflammation_0_4_uncertain",
                "he.alveolar_interstitial_inflammation",
                "he.alveolar_interstitial.v1",
                "alveolar_interstitial",
                "g_surf-he-morphology-severity-0-4-v1"),
            Endpoint(
                "peribronchial_inflammation_0_4_uncertain",
                "he.peribronchial_inflammation",
                "he.peribronchial.v1",
                "peribronchial",
                "g_surf-he-morphology-severity-0-4-v1"),
            Endpoint(
                "perivascular_inflammation_0_4_uncertain",
                "he.perivascular_inflammation",
                "he.perivascular.v1",
                "perivascular",
                "g_surf-he-morphology-severity-0-4-v1"),
            Endpoint(
                "consolidation_airspace_loss_0_4_uncertain",
                "he.consolidation_airspace_loss",
                "he.usable_whole_section.v1",
                "whole_section_usable_tissue",
                "g_surf-he-morphology-severity-0-4-v1"),
            Endpoint(
                "airway_epithelial_injury_debris_0_4_uncertain",
                "he.airway_epithelial_injury_debris",
                "he.airway_epithelium.v1",
                "airway_epithelium",
                "g_surf-he-morphology-severity-0-4-v1")
        };

        private static JavaScriptSerializer Json()
        {
            JavaScriptSerializer json = new JavaScriptSerializer();
            json.MaxJsonLength = 8 * 1024 * 1024;
            json.RecursionLimit = 128;
            return json;
        }

        private static HeOrdinalEndpoint Endpoint(
            string sourceColumn, string endpointId, string referenceSpaceId,
            string compartmentLabel, string scaleId)
        {
            HeOrdinalEndpoint value = new HeOrdinalEndpoint();
            value.SourceColumn = sourceColumn;
            value.EndpointId = endpointId;
            value.ReferenceSpaceId = referenceSpaceId;
            value.CompartmentLabel = compartmentLabel;
            value.ScaleId = scaleId;
            return value;
        }

        internal static HeAuthorityRoots ValidateRuntime(RuntimePaths paths)
        {
            if (paths == null) throw new ArgumentNullException("paths");
            string[] required = new string[]
            {
                paths.HePipelinePath,
                paths.HeStudyPath,
                paths.HeRubricPath,
                paths.HeStainProfilePath,
                paths.MeasurementSchemaPath,
                Path.Combine(paths.RuntimeDirectory, "ifquant", "__init__.py"),
                Path.Combine(paths.RuntimeDirectory, "ifquant", "adapters.py"),
                Path.Combine(paths.RuntimeDirectory, "ifquant", "contracts.py"),
                Path.Combine(paths.RuntimeDirectory, "ifquant", "route_records.py"),
                Path.Combine(paths.RuntimeDirectory, "ifquant", "stage2_index.py")
            };
            foreach (string path in required)
                RequireRegularFile(path, "packaged H&E authority");

            string script = File.ReadAllText(paths.HePipelinePath, Encoding.UTF8);
            Require(script.IndexOf("This module does not make lesion calls",
                                   StringComparison.Ordinal) >= 0,
                    "Packaged H&E code lost its no-lesion-call boundary.");
            foreach (string command in new string[]
                     { "status", "build-review", "aggregate-review" })
                Require(script.IndexOf("\"" + command + "\"",
                                       StringComparison.Ordinal) >= 0,
                        "Packaged H&E code is missing command " + command + ".");
            Require(script.IndexOf("MEASUREMENT_RECORD_SCHEMA_PATH",
                                   StringComparison.Ordinal) >= 0 &&
                    script.IndexOf("group_inference_supported",
                                   StringComparison.Ordinal) >= 0,
                    "Packaged H&E code lacks its schema or inference boundary.");
            foreach (string role in new string[]
                     {
                         "aggregation_code", "ifquant_package_init",
                         "measurement_record_builder", "measurement_record_contract",
                         "measurement_record_route_adapter", "stage2_index_contract",
                         "python_interpreter"
                     })
                Require(script.IndexOf("\"" + role + "\"",
                                       StringComparison.Ordinal) >= 0,
                        "Packaged H&E code lacks provenance role " + role + ".");

            Dictionary<string, object> study = ReadJsonObject(paths.HeStudyPath);
            Require(StringAt(study, "schema_version") == "1.0.0" &&
                    StringAt(study, "study_id") == StudyId &&
                    StringAt(study, "modality") == "brightfield_he",
                    "Packaged H&E study identity is not the locked contract.");
            Dictionary<string, object> approved = ObjectAt(study, "approved_packages");
            HeAuthorityRoots roots = new HeAuthorityRoots();
            roots.SourceRoot = LiteralAbsoluteString(study, "source_root");
            roots.R1Root = LiteralAbsoluteString(approved, "r1_root");
            roots.H4Root = LiteralAbsoluteString(approved, "h4_development_root");

            Dictionary<string, object> rubric = ReadJsonObject(paths.HeRubricPath);
            Require(StringAt(rubric, "schema_version") == "1.0.0" &&
                    StringAt(rubric, "study_id") == StudyId &&
                    StringAt(rubric, "rubric_id") ==
                        "g_surf_he_pathology_review_development_v1" &&
                    StringAt(rubric, "review_unit") == "blinded_whole_section" &&
                    StringAt(rubric, "status") ==
                        "DEVELOPMENT_REVIEW_REQUIRED_NOT_A_VALIDATED_PATHOLOGY_SCORE",
                    "Packaged H&E rubric identity or claim boundary is invalid.");

            Dictionary<string, object> profile = ReadJsonObject(paths.HeStainProfilePath);
            Require(StringAt(profile, "schema_version") == "1.0.0" &&
                    StringAt(profile, "study_id") == StudyId &&
                    StringAt(profile, "profile_id") ==
                        "g_surf_he_20260812_reviewed_locked_v1" &&
                    StringAt(profile, "status") == "REVIEWED_LOCKED",
                    "Packaged H&E stain-profile identity is invalid.");

            Dictionary<string, object> schema = ReadJsonObject(paths.MeasurementSchemaPath);
            Dictionary<string, object> properties = ObjectAt(schema, "properties");
            Dictionary<string, object> version = ObjectAt(properties, "schema_version");
            Require(StringAt(version, "const") == "2.0.0",
                    "Packaged measurement-record schema is not version 2.0.0.");
            return roots;
        }

        internal static string NormalizePython(string value)
        {
            string path = NormalizeAbsolute(value, "Python executable");
            Require(path.EndsWith(".exe", StringComparison.OrdinalIgnoreCase),
                    "Python executable must be an explicit .exe file.");
            Require(IsPythonExecutableName(Path.GetFileNameWithoutExtension(path)),
                    "Python executable filename must identify Python (for example " +
                    "python.exe or python3.12.exe).");
            RequireRegularFile(path, "Python executable");
            return path;
        }

        private static bool IsPythonExecutableName(string value)
        {
            if (string.IsNullOrEmpty(value) ||
                !value.StartsWith("python", StringComparison.OrdinalIgnoreCase))
                return false;
            string suffix = value.Substring("python".Length);
            if (suffix.Length == 0) return true;
            if (suffix[0] < '0' || suffix[0] > '9' ||
                suffix[suffix.Length - 1] == '.') return false;
            bool previousDot = false;
            foreach (char character in suffix)
            {
                if (character == '.')
                {
                    if (previousDot) return false;
                    previousDot = true;
                }
                else
                {
                    if (character < '0' || character > '9') return false;
                    previousDot = false;
                }
            }
            return true;
        }

        internal static string PythonProbeArguments()
        {
            const string code =
                "import json,platform,sys;" +
                "print(json.dumps({" +
                "'implementation':platform.python_implementation()," +
                "'major':sys.version_info[0]," +
                "'minor':sys.version_info[1]," +
                "'micro':sys.version_info[2]," +
                "'executable':sys.executable," +
                "'isolated':bool(sys.flags.isolated)," +
                "'no_site':bool(sys.flags.no_site)," +
                "'dont_write_bytecode':bool(sys.dont_write_bytecode)" +
                "},sort_keys=True,separators=(',',':')))";
            return Arguments(new string[] { "-I", "-B", "-S", "-c", code });
        }

        internal static HePythonIdentity ParsePythonProbe(
            string jsonText, string selectedPath, string executableSha256)
        {
            Dictionary<string, object> probe = ParseJsonObject(
                jsonText.Trim(), "Python interpreter probe");
            RequireExactKeys(
                probe,
                new string[]
                {
                    "implementation", "major", "minor", "micro", "executable",
                    "isolated", "no_site", "dont_write_bytecode"
                },
                "Python interpreter probe");
            string implementation = StringAt(probe, "implementation");
            long major = IntegerAt(probe, "major");
            long minor = IntegerAt(probe, "minor");
            long micro = IntegerAt(probe, "micro");
            string reportedPath = NormalizeAbsolute(
                StringAt(probe, "executable"), "Probed Python executable");
            Require(implementation == "CPython" &&
                    (major > 3 || (major == 3 && minor >= 10)),
                    "H&E review tooling requires CPython 3.10 or newer.");
            Require(string.Equals(reportedPath, selectedPath,
                                  StringComparison.OrdinalIgnoreCase),
                    "Python probe sys.executable does not match the selected executable.");
            Require(BoolAt(probe, "isolated") && BoolAt(probe, "no_site") &&
                    BoolAt(probe, "dont_write_bytecode"),
                    "Python probe did not retain -I -B -S isolation.");
            Require(IsLowerSha256(executableSha256),
                    "Python executable SHA-256 is invalid.");
            Require(major <= Int32.MaxValue && minor <= Int32.MaxValue &&
                    micro <= Int32.MaxValue,
                    "Python version components exceed supported integer bounds.");
            HePythonIdentity identity = new HePythonIdentity();
            identity.ExecutablePath = selectedPath;
            identity.ExecutableSha256 = executableSha256;
            identity.Implementation = implementation;
            identity.Major = (int)major;
            identity.Minor = (int)minor;
            identity.Micro = (int)micro;
            return identity;
        }

        internal static string NormalizeExistingDirectory(string value, string label)
        {
            string path = NormalizeAbsolute(value, label);
            Require(Directory.Exists(path), label + " does not exist: " + path);
            RejectReparseAncestors(path, label);
            FileAttributes attributes = File.GetAttributes(path);
            Require((attributes & (FileAttributes.ReparsePoint | FileAttributes.Device)) == 0,
                    label + " must be a regular non-reparse directory: " + path);
            return path;
        }

        internal static string NormalizeReviewCsv(string value)
        {
            string path = NormalizeAbsolute(value, "Completed blinded review CSV");
            Require(path.EndsWith(".csv", StringComparison.OrdinalIgnoreCase),
                    "Completed blinded review must be a .csv file.");
            RequireRegularFile(path, "Completed blinded review CSV");
            return path;
        }

        internal static string NormalizeFreshOutput(string value, string label)
        {
            string path = NormalizeAbsolute(value, label);
            Require(!File.Exists(path) && !Directory.Exists(path),
                    label + " already exists; overwrite and resume are forbidden: " + path);
            string parent = Path.GetDirectoryName(path);
            Require(!string.IsNullOrEmpty(parent) && Directory.Exists(parent),
                    label + " parent directory does not exist: " + parent);
            RejectReparseAncestors(parent, label + " parent");
            return path;
        }

        internal static void AssertOutputSeparated(
            string output, RuntimePaths runtime, HeAuthorityRoots roots,
            string r1Root, string h4Root)
        {
            foreach (string protectedRoot in new string[]
                     { runtime.RuntimeDirectory, roots.SourceRoot, r1Root, h4Root })
                if (PathsOverlap(output, protectedRoot))
                    throw new InvalidOperationException(
                        "H&E output must not overlap a protected input/runtime root: " +
                        protectedRoot);
        }

        internal static string NewStagingPath(string finalPath)
        {
            string parent = Path.GetDirectoryName(finalPath);
            string leaf = Path.GetFileName(finalPath);
            string staging = Path.Combine(
                parent,
                "." + leaf + ".ifquant-he-staging-" + Guid.NewGuid().ToString("N"));
            Require(!File.Exists(staging) && !Directory.Exists(staging),
                    "Unique H&E staging path already exists: " + staging);
            return staging;
        }

        internal static string StatusArguments(
            RuntimePaths runtime, string r1Root, string h4Root)
        {
            return Arguments(new string[]
            {
                "-I", "-B", "-S", runtime.HePipelinePath, "status",
                "--study", runtime.HeStudyPath,
                "--r1-root", r1Root,
                "--h4-root", h4Root
            });
        }

        internal static string BuildReviewArguments(
            RuntimePaths runtime, string r1Root, string h4Root, string staging)
        {
            return Arguments(new string[]
            {
                "-I", "-B", "-S", runtime.HePipelinePath, "build-review",
                "--study", runtime.HeStudyPath,
                "--rubric", runtime.HeRubricPath,
                "--r1-root", r1Root,
                "--h4-root", h4Root,
                "--output-root", staging
            });
        }

        internal static string AggregateReviewArguments(
            RuntimePaths runtime, string reviewCsv, string staging)
        {
            return Arguments(new string[]
            {
                "-I", "-B", "-S", runtime.HePipelinePath, "aggregate-review",
                "--review-csv", reviewCsv,
                "--study", runtime.HeStudyPath,
                "--rubric", runtime.HeRubricPath,
                "--profile", runtime.HeStainProfilePath,
                "--output-root", staging
            });
        }

        private static string Arguments(string[] tokens)
        {
            StringBuilder text = new StringBuilder();
            foreach (string token in tokens)
            {
                if (text.Length > 0) text.Append(' ');
                text.Append(WindowsCommandLine.Quote(token));
            }
            return text.ToString();
        }

        internal static HeStatusSnapshot ParseStatus(string jsonText)
        {
            Dictionary<string, object> root = ParseJsonObject(jsonText, "H&E status");
            RequireExactKeys(
                root,
                new string[]
                {
                    "schema_version", "checked_utc", "study_id",
                    "highest_authorized_release", "highest_authorized_stage",
                    "launcher_route_enabled", "package_roots", "source", "r1", "h4",
                    "stages", "reportability"
                },
                "H&E status");
            Require(StringAt(root, "schema_version") == "1.0.0" &&
                    StringAt(root, "study_id") == StudyId &&
                    StringAt(root, "highest_authorized_release") == "R1" &&
                    StringAt(root, "highest_authorized_stage") == "H3" &&
                    !BoolAt(root, "launcher_route_enabled"),
                    "H&E status exceeds or disagrees with the authorized R1/H3 state.");
            RequireUtc(StringAt(root, "checked_utc"), "H&E status checked_utc");

            Dictionary<string, object> r1 = ObjectAt(root, "r1");
            Dictionary<string, object> h4 = ObjectAt(root, "h4");
            Require(StringAt(r1, "decision") == "APPROVED_IMAGE_QC",
                    "H&E R1 image-QC decision is not approved.");
            Require(StringAt(h4, "status") ==
                        "H4_REGION_REVIEW_REQUIRED_NOT_R2_RESULT" &&
                    !BoolAt(h4, "use_as_endpoint"),
                    "H&E H4 context is not development-only.");

            Dictionary<string, string> stages = new Dictionary<string, string>(
                StringComparer.Ordinal);
            foreach (object value in ArrayAt(root, "stages"))
            {
                Dictionary<string, object> stage = AsObject(value, "H&E stage row");
                RequireExactKeys(stage, new string[] { "stage", "status", "evidence" },
                                 "H&E stage row");
                string name = StringAt(stage, "stage");
                Require(!stages.ContainsKey(name), "Duplicate H&E stage " + name + ".");
                stages[name] = StringAt(stage, "status");
            }
            Require(new HashSet<string>(stages.Keys, StringComparer.Ordinal).SetEquals(
                        new string[]
                        {
                            "H0", "H1", "H2", "H3", "H4",
                            "H5", "H6", "H7", "H8", "H9"
                        }) &&
                    stages["H0"] == "PASS" && stages["H1"] == "PASS" &&
                    stages["H2"] == "APPROVED_R1" &&
                    stages["H3"] == "APPROVED_R1" &&
                    stages["H4"] == "DEVELOPMENT_CONTEXT_AVAILABLE" &&
                    stages["H5"] == "BLOCKED" && stages["H6"] == "BLOCKED" &&
                    stages["H7"] == "RUBRIC_DEFINED_REVIEW_REQUIRED" &&
                    stages["H8"] == "BLOCKED" && stages["H9"] == "BLOCKED",
                    "H&E stage status is not the authorized engineering/review state.");

            Dictionary<string, object> report = ObjectAt(root, "reportability");
            RequireExactKeys(
                report,
                new string[]
                {
                    "image_qc_and_denominator", "section_pathology_scores",
                    "automated_lesion_burden", "mouse_summary", "group_inference",
                    "he_identifies_krt5_pod", "immune_lineage_from_he"
                },
                "H&E reportability");
            Require(StringAt(report, "image_qc_and_denominator") ==
                        "APPROVED_FOR_THIS_COHORT" &&
                    StringAt(report, "section_pathology_scores") == "REVIEW_REQUIRED" &&
                    StringAt(report, "automated_lesion_burden") == "NOT_AVAILABLE" &&
                    StringAt(report, "mouse_summary") == "BLOCKED" &&
                    StringAt(report, "group_inference") ==
                        "NOT_SUPPORTED_N1_PER_DESIGN_CELL" &&
                    !BoolAt(report, "he_identifies_krt5_pod") &&
                    !BoolAt(report, "immune_lineage_from_he"),
                    "H&E reportability boundary has widened.");

            HeStatusSnapshot snapshot = new HeStatusSnapshot();
            snapshot.Summary =
                "Highest authorized release/stage: R1 / H3\r\n" +
                "H4: development context available; not an endpoint\r\n" +
                "H5/H6: blocked (no validated nuclei, lesion, compartment or topology runner)\r\n" +
                "H7: blinded whole-section rubric defined; review required\r\n" +
                "H8/H9: blocked; no biological endpoint or group inference\r\n" +
                "Unavailable: automated lesion burden, immune lineage, KRT5-pod identity, " +
                "mouse-level biological reporting and hypothesis testing.";
            return snapshot;
        }

        internal static HePackageLease ValidateReviewPackage(
            string root, RuntimePaths runtime)
        {
            HePackageLease package = HePackageLease.Open(root);
            try
            {
                Dictionary<string, object> manifest = package.Json("PACKAGE_MANIFEST.json");
                RequireExactKeys(
                    manifest,
                    new string[]
                    {
                        "schema_version", "created_utc", "status", "study_id",
                        "highest_input_release", "section_count", "primary_review_unit",
                        "supporting_region_role", "files"
                    },
                    "H&E review-package manifest");
                Require(StringAt(manifest, "schema_version") == "1.0.0" &&
                        StringAt(manifest, "status") == ReviewManifestStatus &&
                        StringAt(manifest, "study_id") == StudyId &&
                        StringAt(manifest, "highest_input_release") == "R1" &&
                        IntegerAt(manifest, "section_count") == 8 &&
                        StringAt(manifest, "primary_review_unit") ==
                            "blinded_whole_section" &&
                        StringAt(manifest, "supporting_region_role") ==
                            "evidence_locator_not_replicate_or_prevalence_sample",
                        "H&E review-package manifest identity or claim boundary is invalid.");
                RequireUtc(StringAt(manifest, "created_utc"),
                           "H&E review-package created_utc");
                ValidateLedger(package, ArrayAt(manifest, "files"),
                               "PACKAGE_MANIFEST.json", null);
                foreach (string required in new string[]
                         {
                             "00_START_HERE/README_REVIEW.md",
                             "04_REVIEW_FORMS/H7_SECTION_PATHOLOGY_REVIEW.csv",
                             "04_REVIEW_FORMS/PATHOLOGY_REVIEW_RUBRIC.json",
                             "INTERNAL_DO_NOT_SEND/PIPELINE_STATUS.json"
                         })
                    Require(package.Contains(required),
                            "H&E review package is missing " + required + ".");
                ParseStatus(package.ReadUtf8(
                    "INTERNAL_DO_NOT_SEND/PIPELINE_STATUS.json"));
                Require(package.Hash("04_REVIEW_FORMS/PATHOLOGY_REVIEW_RUBRIC.json") ==
                        Sha256File(runtime.HeRubricPath),
                        "Published review rubric differs from the packaged authority.");
                package.VerifyUnchanged();
                return package;
            }
            catch
            {
                package.Dispose();
                throw;
            }
        }

        internal static HePackageLease ValidateAggregatePackage(
            string root, RuntimePaths runtime, string reviewCsv,
            HePythonIdentity pythonIdentity)
        {
            Require(pythonIdentity != null,
                    "Python runtime identity is required for aggregation validation.");
            HePackageLease package = HePackageLease.Open(root);
            try
            {
                string auditName = "he_review_aggregation.audit.json";
                string[] expectedOutputs = new string[]
                {
                    "he_section_pathology_scores.csv",
                    "he_mouse_pathology_summary.csv",
                    "he_technical_section_agreement.csv",
                    "he_measurement_records.schema-v2.jsonl"
                };
                Require(package.FileCount == 5 && package.DirectoryCount == 1,
                        "H&E aggregation package contains unexpected files/directories.");
                Dictionary<string, object> audit = package.Json(auditName);
                RequireExactKeys(
                    audit,
                    new string[]
                    {
                        "schema_version", "audit_type", "status", "created_utc",
                        "study_id", "rubric_id", "review_contract",
                        "measurement_record_contract", "statistical_policy",
                        "input_artifacts", "output_artifacts", "audit_payload_sha256"
                    },
                    "H&E aggregation audit");
                Require(StringAt(audit, "schema_version") == "1.0.0" &&
                        StringAt(audit, "audit_type") ==
                            "he_blinded_review_aggregation" &&
                        StringAt(audit, "status") == AggregateAuditStatus &&
                        StringAt(audit, "study_id") == StudyId &&
                        StringAt(audit, "rubric_id") ==
                            "g_surf_he_pathology_review_development_v1" &&
                        IsLowerSha256(StringAt(audit, "audit_payload_sha256")),
                        "H&E aggregation audit identity or status is invalid.");
                Require(StringAt(audit, "audit_payload_sha256") ==
                        CanonicalJsonSha256(audit, "audit_payload_sha256"),
                        "H&E aggregation audit payload SHA-256 does not match its " +
                        "canonical content.");
                RequireUtc(StringAt(audit, "created_utc"),
                           "H&E aggregation audit created_utc");

                Dictionary<string, object> review = ObjectAt(audit, "review_contract");
                RequireExactKeys(
                    review,
                    new string[]
                    {
                        "review_unit", "locked_row_count", "validated_row_count",
                        "locked_header", "header_sha256",
                        "all_required_cells_complete",
                        "all_review_timestamps_explicit_utc",
                        "unblinding_performed_after_full_validation"
                    },
                    "H&E aggregation review contract");
                Require(IntegerAt(review, "locked_row_count") == 8 &&
                        IntegerAt(review, "validated_row_count") == 8 &&
                        StringAt(review, "review_unit") == "blinded_whole_section" &&
                        StringArrayEquals(ArrayAt(review, "locked_header"),
                                          LockedReviewFields) &&
                        StringAt(review, "header_sha256") ==
                            CanonicalJsonValueSha256(LockedReviewFields) &&
                        BoolAt(review, "all_required_cells_complete") &&
                        BoolAt(review, "all_review_timestamps_explicit_utc") &&
                        BoolAt(review, "unblinding_performed_after_full_validation"),
                        "H&E aggregation review contract is incomplete.");
                Dictionary<string, object> records =
                    ObjectAt(audit, "measurement_record_contract");
                RequireExactKeys(
                    records,
                    new string[]
                    {
                        "schema_version", "schema_sha256", "track", "record_level",
                        "measurement_profile_id", "measurement_profile_sha256",
                        "record_count", "measured_record_count",
                        "explicit_nonmeasured_record_count",
                        "measured_records_aggregation_eligibility_checked",
                        "estimand_scope", "review_status"
                    },
                    "H&E measurement-record contract");
                Require(StringAt(records, "schema_version") == "2.0.0" &&
                        IsLowerSha256(StringAt(records, "schema_sha256")) &&
                        StringAt(records, "schema_sha256") ==
                            Sha256File(runtime.MeasurementSchemaPath) &&
                        StringAt(records, "track") == "he_pathology" &&
                        StringAt(records, "record_level") == "section" &&
                        StringAt(records, "measurement_profile_id") ==
                            "g_surf_he_pathology_review_development_v1:" +
                            "section-ordinal-schema-v2" &&
                        IsLowerSha256(StringAt(
                            records, "measurement_profile_sha256")) &&
                        IntegerAt(records, "record_count") == 48 &&
                        IntegerAt(records, "measured_record_count") <= 48 &&
                        IntegerAt(records, "explicit_nonmeasured_record_count") <= 48 &&
                        IntegerAt(records, "measured_record_count") +
                            IntegerAt(records, "explicit_nonmeasured_record_count") == 48 &&
                        StringAt(records, "estimand_scope") == "observed_units" &&
                        StringAt(records, "review_status") == "accepted" &&
                        BoolAt(records, "measured_records_aggregation_eligibility_checked"),
                        "H&E measurement-record publication contract is invalid.");
                Dictionary<string, object> policy = ObjectAt(audit, "statistical_policy");
                RequireExactKeys(
                    policy,
                    new string[]
                    {
                        "biological_unit", "technical_sections_per_mouse",
                        "technical_sections_are_biological_replicates",
                        "ordinal_scalar_composite_emitted", "retained_descriptors",
                        "group_inference_supported"
                    },
                    "H&E aggregation statistical policy");
                Require(StringAt(policy, "biological_unit") == "mouse" &&
                        IntegerAt(policy, "technical_sections_per_mouse") == 2 &&
                        !BoolAt(policy, "technical_sections_are_biological_replicates") &&
                        !BoolAt(policy, "ordinal_scalar_composite_emitted") &&
                        StringArrayEquals(
                            ArrayAt(policy, "retained_descriptors"),
                            new string[]
                            {
                                "ordered_section_values", "minimum_observed_rank",
                                "maximum_observed_rank", "exact_agreement"
                            }) &&
                        !BoolAt(policy, "group_inference_supported"),
                        "H&E aggregation statistical boundary has widened.");

                HashSet<string> inputRoles = new HashSet<string>(StringComparer.Ordinal);
                Dictionary<string, string> inputHashes =
                    new Dictionary<string, string>(StringComparer.Ordinal);
                object[] inputArtifacts = ArrayAt(audit, "input_artifacts");
                Require(inputArtifacts.Length == 12,
                        "H&E aggregation input-artifact ledger must contain twelve rows.");
                foreach (object value in inputArtifacts)
                {
                    Dictionary<string, object> item = AsObject(value, "H&E input artifact");
                    RequireExactKeys(item, new string[] { "role", "sha256" },
                                     "H&E input artifact");
                    Require(IsLowerSha256(StringAt(item, "sha256")),
                            "H&E input artifact SHA-256 is invalid.");
                    string inputRole = StringAt(item, "role");
                    Require(inputRoles.Add(inputRole),
                            "H&E aggregation input-artifact role is duplicated.");
                    inputHashes[inputRole] = StringAt(item, "sha256");
                }
                Require(inputRoles.SetEquals(new string[]
                        {
                            "blinded_review_csv", "study_contract", "review_rubric",
                            "locked_stain_profile", "measurement_record_schema",
                            "aggregation_code", "ifquant_package_init",
                            "measurement_record_builder", "measurement_record_contract",
                            "measurement_record_route_adapter", "stage2_index_contract",
                            "python_interpreter"
                        }),
                        "H&E aggregation input-artifact ledger is incomplete.");
                Require(inputHashes["blinded_review_csv"] == Sha256File(reviewCsv) &&
                        inputHashes["study_contract"] == Sha256File(runtime.HeStudyPath) &&
                        inputHashes["review_rubric"] == Sha256File(runtime.HeRubricPath) &&
                        inputHashes["locked_stain_profile"] ==
                            Sha256File(runtime.HeStainProfilePath) &&
                        inputHashes["measurement_record_schema"] ==
                            Sha256File(runtime.MeasurementSchemaPath) &&
                        inputHashes["aggregation_code"] ==
                            Sha256File(runtime.HePipelinePath) &&
                        inputHashes["ifquant_package_init"] == Sha256File(
                            Path.Combine(runtime.RuntimeDirectory,
                                         "ifquant", "__init__.py")) &&
                        inputHashes["measurement_record_builder"] == Sha256File(
                            Path.Combine(runtime.RuntimeDirectory,
                                         "ifquant", "adapters.py")) &&
                        inputHashes["measurement_record_contract"] == Sha256File(
                            Path.Combine(runtime.RuntimeDirectory,
                                         "ifquant", "contracts.py")) &&
                        inputHashes["measurement_record_route_adapter"] == Sha256File(
                            Path.Combine(runtime.RuntimeDirectory,
                                         "ifquant", "route_records.py")) &&
                        inputHashes["stage2_index_contract"] == Sha256File(
                            Path.Combine(runtime.RuntimeDirectory,
                                         "ifquant", "stage2_index.py")) &&
                        inputHashes["python_interpreter"] ==
                            pythonIdentity.ExecutableSha256 &&
                        inputHashes["measurement_record_schema"] ==
                            StringAt(records, "schema_sha256"),
                        "H&E aggregation audit is not bound to the packaged code/config/schema " +
                        "authorities.");

                ValidateLedger(package, ArrayAt(audit, "output_artifacts"),
                               auditName, new HashSet<string>(expectedOutputs,
                                                             StringComparer.Ordinal));
                ValidateAggregatePayloads(
                    package, runtime, reviewCsv, review, records, policy,
                    inputHashes);
                package.VerifyUnchanged();
                return package;
            }
            catch
            {
                package.Dispose();
                throw;
            }
        }

        private static void ValidateLedger(
            HePackageLease package, object[] rows, string authorityFile,
            HashSet<string> fixedExpected)
        {
            HashSet<string> expected = fixedExpected == null
                ? package.RelativeFilesExcept(authorityFile)
                : new HashSet<string>(fixedExpected, StringComparer.Ordinal);
            HashSet<string> observed = new HashSet<string>(StringComparer.Ordinal);
            HashSet<string> observedRoles = new HashSet<string>(StringComparer.Ordinal);
            foreach (object value in rows)
            {
                Dictionary<string, object> item = AsObject(value, "artifact ledger row");
                if (fixedExpected == null)
                    RequireExactKeys(item,
                        new string[] { "relative_path", "bytes", "sha256" },
                        "review-package artifact row");
                else
                    RequireExactKeys(item,
                        new string[] { "role", "relative_path", "bytes", "sha256" },
                        "aggregation artifact row");
                string relative = StringAt(item, "relative_path");
                Require(IsSafeRelativePath(relative) && expected.Contains(relative) &&
                        observed.Add(relative),
                        "Artifact path is unsafe, unknown or duplicated: " + relative);
                Require(package.Length(relative) == IntegerAt(item, "bytes") &&
                        package.Hash(relative) == StringAt(item, "sha256") &&
                        IsLowerSha256(StringAt(item, "sha256")),
                        "Artifact size/hash mismatch: " + relative);
                if (fixedExpected != null)
                {
                    string role = StringAt(item, "role");
                    Require(observedRoles.Add(role) &&
                            role == ExpectedAggregateRole(relative),
                            "Aggregation artifact role is duplicated or disagrees with its " +
                            "path: " + relative);
                }
            }
            Require(observed.SetEquals(expected), "Artifact ledger is not exact.");
        }

        private static string ExpectedAggregateRole(string relative)
        {
            if (relative == "he_section_pathology_scores.csv")
                return "unblinded_section_scores";
            if (relative == "he_mouse_pathology_summary.csv")
                return "paired_technical_section_mouse_summary";
            if (relative == "he_technical_section_agreement.csv")
                return "technical_section_exact_agreement";
            if (relative == "he_measurement_records.schema-v2.jsonl")
                return "schema_v2_measurement_records";
            throw new InvalidOperationException(
                "Unknown H&E aggregation artifact path: " + relative);
        }

        private static void ValidateAggregatePayloads(
            HePackageLease package, RuntimePaths runtime, string reviewCsv,
            Dictionary<string, object> reviewContract,
            Dictionary<string, object> recordContract,
            Dictionary<string, object> statisticalPolicy,
            Dictionary<string, string> inputHashes)
        {
            Dictionary<string, HeSectionIdentity> expectedSections =
                ExpectedSections(runtime.HeStudyPath);
            Dictionary<string, Dictionary<string, string>> authoritativeReview =
                ValidateAuthoritativeReview(reviewCsv, expectedSections);
            Dictionary<string, Dictionary<string, string>> sectionRows =
                ValidateSectionScores(
                    package, expectedSections, authoritativeReview);
            Dictionary<string, Dictionary<string, string>> mouseRows =
                ValidateMouseSummaries(package, expectedSections, sectionRows);
            ValidateTechnicalAgreement(package, expectedSections, mouseRows);

            string expectedProfileSha256 = ExpectedMeasurementProfileSha256(
                inputHashes["review_rubric"], inputHashes["locked_stain_profile"]);
            Require(StringAt(recordContract, "measurement_profile_sha256") ==
                        expectedProfileSha256,
                    "H&E measurement-profile SHA-256 does not match the locked " +
                    "rubric/profile/endpoint contract.");
            int measured = ValidateMeasurementRecords(
                package, expectedSections, sectionRows, inputHashes,
                StringAt(recordContract, "measurement_profile_id"),
                expectedProfileSha256);
            Require(IntegerAt(recordContract, "record_count") == 48 &&
                    IntegerAt(recordContract, "measured_record_count") == measured &&
                    IntegerAt(recordContract,
                              "explicit_nonmeasured_record_count") == 48 - measured,
                    "H&E measurement-record audit counts disagree with the JSONL bytes.");
            Require(IntegerAt(reviewContract, "validated_row_count") ==
                        sectionRows.Count &&
                    IntegerAt(reviewContract, "locked_row_count") ==
                        expectedSections.Count,
                    "H&E review row counts disagree with the published CSV bytes.");
            Require(StringArrayEquals(
                        ArrayAt(statisticalPolicy, "retained_descriptors"),
                        new string[]
                        {
                            "ordered_section_values", "minimum_observed_rank",
                            "maximum_observed_rank", "exact_agreement"
                        }),
                    "H&E retained descriptors differ from the non-composite contract.");
        }

        private static Dictionary<string, HeSectionIdentity> ExpectedSections(
            string studyPath)
        {
            Dictionary<string, object> study = ReadJsonObject(studyPath);
            Require(StringAt(study, "study_id") == StudyId &&
                    StringAt(study, "biological_unit") == "mouse" &&
                    IntegerAt(study, "expected_mouse_count") == 4 &&
                    IntegerAt(study, "expected_analytical_sections") == 8,
                    "Packaged H&E study is not the declared four-mouse/eight-section " +
                    "contract.");
            object[] samples = ArrayAt(study, "samples");
            Require(samples.Length == 4,
                    "Packaged H&E study must declare exactly four mice.");
            Dictionary<string, HeSectionIdentity> result =
                new Dictionary<string, HeSectionIdentity>(StringComparer.Ordinal);
            HashSet<string> mice = new HashSet<string>(StringComparer.Ordinal);
            foreach (object value in samples)
            {
                Dictionary<string, object> sample = AsObject(value, "H&E study sample");
                string mouse = StringAt(sample, "mouse_id");
                Require(mice.Add(mouse), "Duplicate H&E mouse declaration: " + mouse);
                object[] sections = ArrayAt(sample, "section_ids");
                Require(sections.Length == 2,
                        "Each H&E mouse must declare exactly two technical sections.");
                Dictionary<string, object> sourcePackage =
                    ObjectAt(sample, "source_package");
                string packageSha = StringAt(sourcePackage, "package_sha256");
                Require(IsLowerSha256(packageSha),
                        "H&E source-package SHA-256 is invalid for mouse " + mouse + ".");
                for (int index = 0; index < sections.Length; index++)
                {
                    string section = sections[index] as string;
                    Require(!string.IsNullOrEmpty(section),
                            "H&E section IDs must be non-empty strings.");
                    HeSectionIdentity identity = new HeSectionIdentity();
                    identity.SectionId = section;
                    identity.MouseId = mouse;
                    identity.Genotype = StringAt(sample, "genotype");
                    identity.Condition = StringAt(sample, "condition");
                    identity.TechnicalSectionOrder = index + 1;
                    identity.SourcePackageSha256 = packageSha;
                    Require(!result.ContainsKey(section),
                            "Duplicate H&E section declaration: " + section);
                    result.Add(section, identity);
                }
            }
            object[] blindMap = ArrayAt(study, "blind_section_map");
            Require(blindMap.Length == 8 && result.Count == 8,
                    "Packaged H&E study must declare exactly eight blinded sections.");
            HashSet<string> blindIds = new HashSet<string>(StringComparer.Ordinal);
            foreach (object value in blindMap)
            {
                Dictionary<string, object> row = AsObject(value, "H&E blind-map row");
                RequireExactKeys(row, new string[] { "blind_section_id", "section_id" },
                                 "H&E blind-map row");
                string blind = StringAt(row, "blind_section_id");
                string section = StringAt(row, "section_id");
                HeSectionIdentity identity;
                Require(result.TryGetValue(section, out identity) && blindIds.Add(blind),
                        "H&E blind map contains an unknown section or duplicate blind ID.");
                identity.BlindSectionId = blind;
            }
            for (int index = 1; index <= 8; index++)
                Require(blindIds.Contains(
                            "HE-" + index.ToString("000", CultureInfo.InvariantCulture)),
                        "H&E blind map differs from HE-001 through HE-008.");
            foreach (HeSectionIdentity identity in result.Values)
                Require(!string.IsNullOrEmpty(identity.BlindSectionId),
                        "H&E section lacks a blind-map identity: " + identity.SectionId);
            return result;
        }

        private static string ExpectedMeasurementProfileSha256(
            string rubricSha256, string profileSha256)
        {
            Dictionary<string, object> sampling = NewObject();
            sampling["design"] = "purposive";
            sampling["estimand_scope"] = "observed_units";
            sampling["selection_source"] =
                "locked_two_technical_whole_sections_per_mouse";

            object[] endpoints = new object[OrdinalEndpoints.Length];
            for (int index = 0; index < OrdinalEndpoints.Length; index++)
            {
                HeOrdinalEndpoint endpoint = OrdinalEndpoints[index];
                Dictionary<string, object> item = NewObject();
                item["source_column"] = endpoint.SourceColumn;
                item["endpoint_id"] = endpoint.EndpointId;
                item["reference_space_id"] = endpoint.ReferenceSpaceId;
                item["compartment_label"] = endpoint.CompartmentLabel;
                item["scale_id"] = endpoint.ScaleId;
                endpoints[index] = item;
            }

            Dictionary<string, object> aggregation = NewObject();
            aggregation["retain_technical_section_order"] = true;
            aggregation["allow_scalar_ordinal_composite"] = false;
            aggregation["agreement_statistic"] = "exact_agreement";

            Dictionary<string, object> profile = NewObject();
            profile["schema_version"] = "1.0.0";
            profile["measurement_profile_id"] =
                "g_surf_he_pathology_review_development_v1:" +
                "section-ordinal-schema-v2";
            profile["rubric_sha256"] = rubricSha256;
            profile["locked_stain_profile_sha256"] = profileSha256;
            profile["review_unit"] = "whole_section";
            profile["record_level"] = "section";
            profile["sampling"] = sampling;
            profile["ordinal_endpoints"] = endpoints;
            profile["aggregation_policy"] = aggregation;
            return CanonicalJsonValueSha256(profile);
        }

        private static Dictionary<string, object> NewObject()
        {
            return new Dictionary<string, object>(StringComparer.Ordinal);
        }

        private static Dictionary<string, Dictionary<string, string>>
            ValidateAuthoritativeReview(
                string reviewCsv,
                Dictionary<string, HeSectionIdentity> expectedSections)
        {
            HeCsvTable table = ParseCsv(
                ReadUtf8File(reviewCsv, 2 * 1024 * 1024,
                             "Completed blinded H&E review CSV"),
                "Completed blinded H&E review CSV");
            Require(StringArrayEquals(table.Header, LockedReviewFields) &&
                    table.Rows.Count == 8,
                    "Completed blinded H&E review must have the exact locked header " +
                    "and eight rows.");
            HashSet<string> expectedBlindIds = new HashSet<string>(
                StringComparer.Ordinal);
            foreach (HeSectionIdentity section in expectedSections.Values)
                expectedBlindIds.Add(section.BlindSectionId);
            Dictionary<string, Dictionary<string, string>> result =
                new Dictionary<string, Dictionary<string, string>>(
                    StringComparer.Ordinal);
            foreach (Dictionary<string, string> row in table.Rows)
            {
                ValidateLockedReviewRow(row, "Completed blinded H&E review row");
                string blind = CsvAt(row, "blind_section_id");
                Require(expectedBlindIds.Contains(blind) &&
                        !result.ContainsKey(blind),
                        "Completed blinded H&E review contains an unknown or duplicate " +
                        "blind section.");
                result.Add(blind, row);
            }
            Require(result.Count == expectedBlindIds.Count,
                    "Completed blinded H&E review does not cover every locked blind " +
                    "section exactly once.");
            return result;
        }

        private static void ValidateLockedReviewRow(
            Dictionary<string, string> row, string label)
        {
            RequireCsvCellsTrimmed(row, label);
            foreach (string field in LockedReviewFields)
                if (field != "notes")
                    Require(CsvAt(row, field).Length > 0,
                            label + " contains an incomplete field: " + field);
            string reviewability = CsvAt(row, "reviewable_yes_no_uncertain");
            Require(In(reviewability, "yes", "no", "uncertain"),
                    label + " has invalid reviewability.");
            foreach (HeOrdinalEndpoint endpoint in OrdinalEndpoints)
                Require(IsOrdinalToken(CsvAt(row, endpoint.SourceColumn)),
                        label + " has an invalid ordinal token.");
            Require(In(CsvAt(row,
                             "edema_hemorrhage_necrosis_present_yes_no_uncertain"),
                       "yes", "no", "uncertain") &&
                    In(CsvAt(row, "dominant_pattern"),
                       "none", "alveolar_interstitial", "peribronchial",
                       "perivascular", "consolidative", "airway_epithelial",
                       "mixed", "unresolved") &&
                    In(CsvAt(row, "technical_limitation_none_minor_major"),
                       "none", "minor", "major") &&
                    In(CsvAt(row, "confidence_low_medium_high"),
                       "low", "medium", "high"),
                    label + " contains a value outside the locked vocabulary.");
            RequireUtc(CsvAt(row, "reviewed_utc"), label + " reviewed_utc");
            if (reviewability != "yes")
            {
                foreach (HeOrdinalEndpoint endpoint in OrdinalEndpoints)
                    Require(CsvAt(row, endpoint.SourceColumn) == "uncertain",
                            label + " is nonreviewable but carries an ordinal call.");
                Require(CsvAt(row,
                              "edema_hemorrhage_necrosis_present_yes_no_uncertain") ==
                            "uncertain" &&
                        CsvAt(row, "dominant_pattern") == "unresolved",
                        label + " is nonreviewable but carries a lesion call.");
            }
        }

        private static void RequireLockedReviewFieldsMatch(
            Dictionary<string, string> published,
            Dictionary<string, string> authoritative,
            string sectionId)
        {
            foreach (string field in LockedReviewFields)
                Require(CsvAt(published, field) == CsvAt(authoritative, field),
                        "Published H&E section field " + field +
                        " differs from the selected blinded review CSV for " +
                        sectionId + ".");
        }

        private static Dictionary<string, Dictionary<string, string>>
            ValidateSectionScores(
                HePackageLease package,
                Dictionary<string, HeSectionIdentity> expectedSections,
                Dictionary<string, Dictionary<string, string>> authoritativeReview)
        {
            string[] header = JoinArrays(
                new string[]
                {
                    "study_id", "mouse_id", "genotype", "condition", "section_id",
                    "technical_section_order", "source_package_sha256",
                    "section_evaluability"
                },
                LockedReviewFields);
            HeCsvTable table = ParseCsv(
                package.ReadUtf8("he_section_pathology_scores.csv"),
                "H&E section pathology scores");
            Require(StringArrayEquals(table.Header, header) && table.Rows.Count == 8,
                    "H&E section pathology CSV must have its exact 24-column header and " +
                    "eight rows.");
            Dictionary<string, Dictionary<string, string>> result =
                new Dictionary<string, Dictionary<string, string>>(StringComparer.Ordinal);
            foreach (Dictionary<string, string> row in table.Rows)
            {
                RequireCsvCellsTrimmed(row, "H&E section pathology row");
                string sectionId = CsvAt(row, "section_id");
                HeSectionIdentity expected;
                Require(expectedSections.TryGetValue(sectionId, out expected) &&
                        !result.ContainsKey(sectionId),
                        "H&E section pathology CSV contains an unknown or duplicate section.");
                Require(CsvAt(row, "study_id") == StudyId &&
                        CsvAt(row, "mouse_id") == expected.MouseId &&
                        CsvAt(row, "genotype") == expected.Genotype &&
                        CsvAt(row, "condition") == expected.Condition &&
                        CsvAt(row, "technical_section_order") ==
                            expected.TechnicalSectionOrder.ToString(
                                CultureInfo.InvariantCulture) &&
                        CsvAt(row, "source_package_sha256") ==
                            expected.SourcePackageSha256 &&
                        CsvAt(row, "blind_section_id") == expected.BlindSectionId,
                        "H&E section pathology identity disagrees with the study contract: " +
                        sectionId);
                Dictionary<string, string> authoritative = null;
                Require(authoritativeReview.TryGetValue(
                            expected.BlindSectionId, out authoritative),
                        "H&E authoritative review lacks blind section " +
                        expected.BlindSectionId + ".");
                RequireLockedReviewFieldsMatch(row, authoritative, sectionId);

                string reviewability = CsvAt(row, "reviewable_yes_no_uncertain");
                Require(In(reviewability, "yes", "no", "uncertain"),
                        "H&E section reviewability is outside the locked vocabulary.");
                string expectedEvaluability = reviewability == "yes"
                    ? "evaluable"
                    : reviewability == "no"
                    ? "not_reviewable"
                    : "reviewability_uncertain";
                Require(CsvAt(row, "section_evaluability") == expectedEvaluability,
                        "H&E section evaluability disagrees with reviewability.");

                foreach (HeOrdinalEndpoint endpoint in OrdinalEndpoints)
                    Require(IsOrdinalToken(CsvAt(row, endpoint.SourceColumn)),
                            "H&E ordinal review token is outside 0-4/uncertain.");
                Require(In(CsvAt(row,
                                 "edema_hemorrhage_necrosis_present_yes_no_uncertain"),
                           "yes", "no", "uncertain") &&
                        In(CsvAt(row, "dominant_pattern"),
                           "none", "alveolar_interstitial", "peribronchial",
                           "perivascular", "consolidative", "airway_epithelial",
                           "mixed", "unresolved") &&
                        In(CsvAt(row, "technical_limitation_none_minor_major"),
                           "none", "minor", "major") &&
                        In(CsvAt(row, "confidence_low_medium_high"),
                           "low", "medium", "high"),
                        "H&E section review row contains a value outside the locked " +
                        "vocabulary.");
                foreach (string field in LockedReviewFields)
                    if (field != "notes")
                        Require(CsvAt(row, field).Length > 0,
                                "H&E section review row contains an incomplete field: " +
                                field);
                RequireUtc(CsvAt(row, "reviewed_utc"),
                           "H&E section reviewed_utc");
                if (reviewability != "yes")
                {
                    foreach (HeOrdinalEndpoint endpoint in OrdinalEndpoints)
                        Require(CsvAt(row, endpoint.SourceColumn) == "uncertain",
                                "Nonreviewable H&E section carries an ordinal lesion call.");
                    Require(CsvAt(row,
                                  "edema_hemorrhage_necrosis_present_yes_no_uncertain") ==
                                "uncertain" &&
                            CsvAt(row, "dominant_pattern") == "unresolved",
                            "Nonreviewable H&E section carries a lesion call.");
                }
                result.Add(sectionId, row);
            }
            Require(result.Count == expectedSections.Count,
                    "H&E section pathology CSV does not cover all declared sections.");
            return result;
        }

        private static Dictionary<string, Dictionary<string, string>>
            ValidateMouseSummaries(
                HePackageLease package,
                Dictionary<string, HeSectionIdentity> expectedSections,
                Dictionary<string, Dictionary<string, string>> sectionRows)
        {
            string[] header = new string[]
            {
                "study_id", "mouse_id", "genotype", "condition", "endpoint_id",
                "scale_id", "section_1_id", "section_1_reviewability",
                "section_1_value", "section_2_id", "section_2_reviewability",
                "section_2_value", "ordered_section_values_json",
                "n_evaluable_sections", "paired_evaluability",
                "minimum_observed_rank", "maximum_observed_rank", "exact_agreement"
            };
            HeCsvTable table = ParseCsv(
                package.ReadUtf8("he_mouse_pathology_summary.csv"),
                "H&E mouse pathology summary");
            Require(StringArrayEquals(table.Header, header) && table.Rows.Count == 24,
                    "H&E mouse pathology CSV must have its exact non-composite header " +
                    "and 24 rows.");
            Dictionary<string, List<HeSectionIdentity>> byMouse =
                SectionsByMouse(expectedSections);
            Dictionary<string, Dictionary<string, string>> result =
                new Dictionary<string, Dictionary<string, string>>(StringComparer.Ordinal);
            foreach (Dictionary<string, string> row in table.Rows)
            {
                RequireCsvCellsTrimmed(row, "H&E mouse pathology row");
                string mouse = CsvAt(row, "mouse_id");
                HeOrdinalEndpoint endpoint = EndpointById(CsvAt(row, "endpoint_id"));
                List<HeSectionIdentity> sections = null;
                Require(endpoint != null && byMouse.TryGetValue(mouse, out sections),
                        "H&E mouse summary contains an unknown mouse or endpoint.");
                string key = MouseEndpointKey(mouse, endpoint.EndpointId);
                Require(!result.ContainsKey(key),
                        "H&E mouse summary duplicates a mouse/endpoint row.");
                Dictionary<string, string> first = sectionRows[sections[0].SectionId];
                Dictionary<string, string> second = sectionRows[sections[1].SectionId];
                string firstValue = CsvAt(first, endpoint.SourceColumn);
                string secondValue = CsvAt(second, endpoint.SourceColumn);
                int firstRank;
                int secondRank;
                bool firstMeasured = Int32.TryParse(
                    firstValue, NumberStyles.None, CultureInfo.InvariantCulture,
                    out firstRank);
                bool secondMeasured = Int32.TryParse(
                    secondValue, NumberStyles.None, CultureInfo.InvariantCulture,
                    out secondRank);
                int evaluable = (firstMeasured ? 1 : 0) + (secondMeasured ? 1 : 0);
                string paired = evaluable == 2 ? "both_evaluable" :
                    evaluable == 1 ? "partial" : "none";
                string agreement = evaluable == 2
                    ? (firstRank == secondRank ? "true" : "false")
                    : "not_evaluable";
                string minimum = evaluable == 0 ? "" :
                    (firstMeasured && secondMeasured
                        ? Math.Min(firstRank, secondRank)
                        : firstMeasured ? firstRank : secondRank).ToString(
                            CultureInfo.InvariantCulture);
                string maximum = evaluable == 0 ? "" :
                    (firstMeasured && secondMeasured
                        ? Math.Max(firstRank, secondRank)
                        : firstMeasured ? firstRank : secondRank).ToString(
                            CultureInfo.InvariantCulture);
                Require(CsvAt(row, "study_id") == StudyId &&
                        CsvAt(row, "genotype") == sections[0].Genotype &&
                        CsvAt(row, "condition") == sections[0].Condition &&
                        CsvAt(row, "scale_id") == endpoint.ScaleId &&
                        CsvAt(row, "section_1_id") == sections[0].SectionId &&
                        CsvAt(row, "section_2_id") == sections[1].SectionId &&
                        CsvAt(row, "section_1_reviewability") ==
                            CsvAt(first, "reviewable_yes_no_uncertain") &&
                        CsvAt(row, "section_2_reviewability") ==
                            CsvAt(second, "reviewable_yes_no_uncertain") &&
                        CsvAt(row, "section_1_value") == firstValue &&
                        CsvAt(row, "section_2_value") == secondValue &&
                        CsvAt(row, "n_evaluable_sections") ==
                            evaluable.ToString(CultureInfo.InvariantCulture) &&
                        CsvAt(row, "paired_evaluability") == paired &&
                        CsvAt(row, "minimum_observed_rank") == minimum &&
                        CsvAt(row, "maximum_observed_rank") == maximum &&
                        CsvAt(row, "exact_agreement") == agreement &&
                        StringArrayEquals(
                            ParseJsonArrayText(
                                CsvAt(row, "ordered_section_values_json"),
                                "ordered_section_values_json"),
                            new string[] { firstValue, secondValue }),
                        "H&E mouse summary does not reconcile to its two declared " +
                        "technical sections: " + key);
                result.Add(key, row);
            }
            Require(result.Count == byMouse.Count * OrdinalEndpoints.Length,
                    "H&E mouse summary does not cover all four mice and six endpoints.");
            foreach (string mouse in byMouse.Keys)
                foreach (HeOrdinalEndpoint endpoint in OrdinalEndpoints)
                    Require(result.ContainsKey(MouseEndpointKey(mouse,
                                                               endpoint.EndpointId)),
                            "H&E mouse summary lacks a declared mouse/endpoint row.");
            return result;
        }

        private static void ValidateTechnicalAgreement(
            HePackageLease package,
            Dictionary<string, HeSectionIdentity> expectedSections,
            Dictionary<string, Dictionary<string, string>> mouseRows)
        {
            string[] header = new string[]
            {
                "study_id", "endpoint_id", "scale_id", "n_mouse_pairs_declared",
                "n_pairs_both_evaluable", "n_pairs_not_both_evaluable",
                "n_exact_agreement", "exact_agreement_fraction",
                "ordered_mouse_pairs_json"
            };
            HeCsvTable table = ParseCsv(
                package.ReadUtf8("he_technical_section_agreement.csv"),
                "H&E technical-section agreement");
            Require(StringArrayEquals(table.Header, header) && table.Rows.Count == 6,
                    "H&E technical-agreement CSV must have its exact header and six " +
                    "endpoint rows.");
            Dictionary<string, List<HeSectionIdentity>> mice =
                SectionsByMouse(expectedSections);
            HashSet<string> endpoints = new HashSet<string>(StringComparer.Ordinal);
            foreach (Dictionary<string, string> row in table.Rows)
            {
                RequireCsvCellsTrimmed(row, "H&E technical-agreement row");
                HeOrdinalEndpoint endpoint = EndpointById(CsvAt(row, "endpoint_id"));
                Require(endpoint != null && endpoints.Add(endpoint.EndpointId),
                        "H&E technical-agreement CSV has an unknown or duplicate endpoint.");
                int both = 0;
                int exact = 0;
                foreach (string mouse in mice.Keys)
                {
                    Dictionary<string, string> summary =
                        mouseRows[MouseEndpointKey(mouse, endpoint.EndpointId)];
                    if (CsvAt(summary, "paired_evaluability") == "both_evaluable")
                    {
                        both++;
                        if (CsvAt(summary, "exact_agreement") == "true") exact++;
                    }
                }
                Require(CsvAt(row, "study_id") == StudyId &&
                        CsvAt(row, "scale_id") == endpoint.ScaleId &&
                        CsvAt(row, "n_mouse_pairs_declared") == "4" &&
                        CsvAt(row, "n_pairs_both_evaluable") ==
                            both.ToString(CultureInfo.InvariantCulture) &&
                        CsvAt(row, "n_pairs_not_both_evaluable") ==
                            (4 - both).ToString(CultureInfo.InvariantCulture) &&
                        CsvAt(row, "n_exact_agreement") ==
                            exact.ToString(CultureInfo.InvariantCulture),
                        "H&E technical-agreement counts do not reconcile to the four " +
                        "mouse pairs.");
                string fractionText = CsvAt(row, "exact_agreement_fraction");
                if (both == 0)
                    Require(fractionText.Length == 0,
                            "H&E agreement fraction must be blank when no pair is evaluable.");
                else
                {
                    double fraction;
                    Require(Double.TryParse(
                                fractionText, NumberStyles.Float,
                                CultureInfo.InvariantCulture, out fraction) &&
                            !Double.IsNaN(fraction) && !Double.IsInfinity(fraction) &&
                            Math.Abs(fraction - ((double)exact / (double)both)) < 1e-12,
                            "H&E agreement fraction disagrees with exact pair counts.");
                }
                object[] pairValues = ParseJsonArrayText(
                    CsvAt(row, "ordered_mouse_pairs_json"),
                    "ordered_mouse_pairs_json");
                Require(pairValues.Length == 4,
                        "H&E ordered mouse-pair ledger must contain four rows.");
                HashSet<string> observedMice = new HashSet<string>(StringComparer.Ordinal);
                foreach (object value in pairValues)
                {
                    Dictionary<string, object> pair =
                        AsObject(value, "H&E ordered mouse-pair row");
                    RequireExactKeys(
                        pair,
                        new string[]
                        {
                            "mouse_id", "ordered_section_values",
                            "paired_evaluability", "exact_agreement"
                        },
                        "H&E ordered mouse-pair row");
                    string mouse = StringAt(pair, "mouse_id");
                    Dictionary<string, string> summary = null;
                    Require(mice.ContainsKey(mouse) && observedMice.Add(mouse) &&
                            mouseRows.TryGetValue(
                                MouseEndpointKey(mouse, endpoint.EndpointId), out summary),
                            "H&E ordered mouse-pair row has an unknown or duplicate mouse.");
                    Require(StringArrayEquals(
                                ArrayAt(pair, "ordered_section_values"),
                                new string[]
                                {
                                    CsvAt(summary, "section_1_value"),
                                    CsvAt(summary, "section_2_value")
                                }) &&
                            StringAt(pair, "paired_evaluability") ==
                                CsvAt(summary, "paired_evaluability") &&
                            StringAt(pair, "exact_agreement") ==
                                CsvAt(summary, "exact_agreement"),
                            "H&E ordered mouse-pair details disagree with mouse summary.");
                }
                Require(observedMice.SetEquals(mice.Keys),
                        "H&E ordered mouse-pair ledger is not exact.");
            }
            Require(endpoints.Count == OrdinalEndpoints.Length,
                    "H&E technical-agreement CSV omits a declared endpoint.");
        }

        private static int ValidateMeasurementRecords(
            HePackageLease package,
            Dictionary<string, HeSectionIdentity> expectedSections,
            Dictionary<string, Dictionary<string, string>> sectionRows,
            Dictionary<string, string> inputHashes,
            string measurementProfileId,
            string measurementProfileSha256)
        {
            string text = package.ReadUtf8(
                "he_measurement_records.schema-v2.jsonl");
            List<Dictionary<string, object>> records =
                ParseJsonLines(text, "H&E schema-v2 measurement records");
            Require(records.Count == 48,
                    "H&E measurement JSONL must contain exactly 48 nonblank records.");
            HashSet<string> recordIds = new HashSet<string>(StringComparer.Ordinal);
            HashSet<string> identities = new HashSet<string>(StringComparer.Ordinal);
            int measuredCount = 0;
            string expectedRunId = "he-review-aggregate:" +
                inputHashes["blinded_review_csv"].Substring(0, 20);
            foreach (Dictionary<string, object> record in records)
            {
                RequireExactKeys(
                    record,
                    new string[]
                    {
                        "schema_version", "record_id", "track", "record_level",
                        "identifiers", "measurement_profile_id", "channel_signature",
                        "segmentation_model", "endpoint", "sampling", "compartment",
                        "provenance", "qc"
                    },
                    "H&E measurement record");
                string recordId = StringAt(record, "record_id");
                Require(recordId.StartsWith("ifqmr-", StringComparison.Ordinal) &&
                        recordId.Length == 70 && recordIds.Add(recordId) &&
                        StringAt(record, "schema_version") == "2.0.0" &&
                        StringAt(record, "track") == "he_pathology" &&
                        StringAt(record, "record_level") == "section" &&
                        StringAt(record, "measurement_profile_id") ==
                            measurementProfileId &&
                        IsNullAt(record, "segmentation_model"),
                        "H&E measurement-record identity/schema/profile is invalid.");

                Dictionary<string, object> identifiers = ObjectAt(record, "identifiers");
                RequireExactKeys(
                    identifiers,
                    new string[]
                    {
                        "mouse_id", "slide_id", "section_id", "field_id", "tile_id",
                        "region_id", "cell_id"
                    },
                    "H&E measurement identifiers");
                string sectionId = StringAt(identifiers, "section_id");
                HeSectionIdentity expectedSection;
                Require(expectedSections.TryGetValue(sectionId, out expectedSection) &&
                        StringAt(identifiers, "mouse_id") == expectedSection.MouseId &&
                        IsNullAt(identifiers, "slide_id") &&
                        IsNullAt(identifiers, "field_id") &&
                        IsNullAt(identifiers, "tile_id") &&
                        IsNullAt(identifiers, "region_id") &&
                        IsNullAt(identifiers, "cell_id"),
                        "H&E measurement identifiers disagree with the study section.");

                object[] channels = ArrayAt(record, "channel_signature");
                Require(channels.Length == 1,
                        "H&E measurement channel signature must contain one RGB channel.");
                Dictionary<string, object> channel =
                    AsObject(channels[0], "H&E measurement channel");
                RequireExactKeys(channel, new string[] { "index", "label", "role" },
                                 "H&E measurement channel");
                Require(IntegerAt(channel, "index") == 1 &&
                        StringAt(channel, "label") == "RGB_brightfield_HE" &&
                        StringAt(channel, "role") == "histology_source",
                        "H&E measurement channel signature is invalid.");

                Dictionary<string, object> endpoint = ObjectAt(record, "endpoint");
                RequireExactKeys(
                    endpoint,
                    new string[]
                    {
                        "endpoint_id", "calculation", "reference_space_id",
                        "evaluability", "rank", "minimum_rank", "maximum_rank",
                        "scale_id", "reason_code"
                    },
                    "H&E ordinal endpoint");
                HeOrdinalEndpoint expectedEndpoint =
                    EndpointById(StringAt(endpoint, "endpoint_id"));
                Require(expectedEndpoint != null,
                        "H&E measurement record contains an unknown endpoint.");
                Require(recordId == ExpectedMeasurementRecordId(
                            StringAt(identifiers, "mouse_id"), sectionId,
                            expectedEndpoint, measurementProfileId),
                        "H&E measurement record_id does not match its deterministic " +
                        "measurement identity.");
                string identity = sectionId + "\n" + expectedEndpoint.EndpointId;
                Require(identities.Add(identity),
                        "H&E measurement JSONL duplicates a section/endpoint identity.");
                Dictionary<string, string> sectionRow = sectionRows[sectionId];
                string token = CsvAt(sectionRow, expectedEndpoint.SourceColumn);
                string reviewability =
                    CsvAt(sectionRow, "reviewable_yes_no_uncertain");
                int rank;
                bool tokenMeasured = Int32.TryParse(
                    token, NumberStyles.None, CultureInfo.InvariantCulture, out rank);
                string expectedEvaluability;
                string expectedReason;
                string expectedQc;
                if (reviewability == "yes" && tokenMeasured)
                {
                    expectedEvaluability = "measured";
                    expectedReason = null;
                    expectedQc = "pass";
                    measuredCount++;
                }
                else if (reviewability == "no")
                {
                    expectedEvaluability = "not_evaluable";
                    expectedReason = "section_not_reviewable";
                    expectedQc = "warning";
                }
                else if (reviewability == "uncertain")
                {
                    expectedEvaluability = "not_evaluable";
                    expectedReason = "section_reviewability_uncertain";
                    expectedQc = "warning";
                }
                else
                {
                    expectedEvaluability = "not_evaluable";
                    expectedReason = "endpoint_score_uncertain";
                    expectedQc = "warning";
                }
                long? publishedRank = NullableIntegerAt(endpoint, "rank");
                string publishedReason = NullableStringAt(endpoint, "reason_code");
                Require(StringAt(endpoint, "calculation") == "ordinal" &&
                        StringAt(endpoint, "reference_space_id") ==
                            expectedEndpoint.ReferenceSpaceId &&
                        StringAt(endpoint, "evaluability") == expectedEvaluability &&
                        IntegerAt(endpoint, "minimum_rank") == 0 &&
                        IntegerAt(endpoint, "maximum_rank") == 4 &&
                        StringAt(endpoint, "scale_id") == expectedEndpoint.ScaleId &&
                        ((tokenMeasured && publishedRank.HasValue &&
                          publishedRank.Value == rank) ||
                         (!tokenMeasured && !publishedRank.HasValue)) &&
                        string.Equals(publishedReason, expectedReason,
                                      StringComparison.Ordinal),
                        "H&E ordinal endpoint disagrees with its reviewed section value.");

                Dictionary<string, object> sampling = ObjectAt(record, "sampling");
                RequireExactKeys(
                    sampling,
                    new string[]
                    {
                        "design", "inclusion_probability", "selection_source",
                        "estimand_scope", "estimator_profile_id"
                    },
                    "H&E measurement sampling");
                Require(StringAt(sampling, "design") == "purposive" &&
                        IsNullAt(sampling, "inclusion_probability") &&
                        StringAt(sampling, "selection_source") ==
                            "locked_two_technical_whole_sections_per_mouse" &&
                        StringAt(sampling, "estimand_scope") == "observed_units" &&
                        IsNullAt(sampling, "estimator_profile_id"),
                        "H&E measurement sampling exceeds the observed-units contract.");

                Dictionary<string, object> compartment =
                    ObjectAt(record, "compartment");
                RequireExactKeys(
                    compartment,
                    new string[] { "status", "labels", "assignment_profile_id" },
                    "H&E measurement compartment");
                Require(StringAt(compartment, "status") == "assigned" &&
                        StringArrayEquals(
                            ArrayAt(compartment, "labels"),
                            new string[] { expectedEndpoint.CompartmentLabel }) &&
                        StringAt(compartment, "assignment_profile_id") ==
                            "g_surf_he_pathology_review_development_v1:" +
                            "anatomy-reference-v1",
                        "H&E measurement compartment identity is invalid.");

                Dictionary<string, object> provenance = ObjectAt(record, "provenance");
                RequireExactKeys(
                    provenance,
                    new string[]
                    {
                        "code_revision", "config_sha256",
                        "measurement_profile_sha256", "inputs", "run_id"
                    },
                    "H&E measurement provenance");
                Require(StringAt(provenance, "code_revision") ==
                            inputHashes["aggregation_code"] &&
                        StringAt(provenance, "config_sha256") ==
                            inputHashes["review_rubric"] &&
                        StringAt(provenance, "measurement_profile_sha256") ==
                            measurementProfileSha256 &&
                        StringAt(provenance, "run_id") == expectedRunId,
                        "H&E measurement provenance authority fields are invalid.");
                ValidateRecordInputLedger(
                    ArrayAt(provenance, "inputs"), inputHashes,
                    expectedSection.SourcePackageSha256);

                Dictionary<string, object> qc = ObjectAt(record, "qc");
                RequireExactKeys(qc,
                                 new string[]
                                 {
                                     "status", "reason_codes", "review_status"
                                 },
                                 "H&E measurement QC");
                string[] expectedReasons = expectedReason == null
                    ? new string[0] : new string[] { expectedReason };
                Require(StringAt(qc, "status") == expectedQc &&
                        StringArrayEquals(ArrayAt(qc, "reason_codes"),
                                          expectedReasons) &&
                        StringAt(qc, "review_status") == "accepted",
                        "H&E measurement QC/review state disagrees with evaluability.");
            }
            Require(identities.Count == expectedSections.Count *
                        OrdinalEndpoints.Length,
                    "H&E measurement JSONL does not cover all section/endpoint pairs.");
            foreach (string section in expectedSections.Keys)
                foreach (HeOrdinalEndpoint endpoint in OrdinalEndpoints)
                    Require(identities.Contains(section + "\n" + endpoint.EndpointId),
                            "H&E measurement JSONL omits a section/endpoint identity.");
            return measuredCount;
        }

        private static string ExpectedMeasurementRecordId(
            string mouseId, string sectionId, HeOrdinalEndpoint endpoint,
            string measurementProfileId)
        {
            object[] measurementIdentity = new object[]
            {
                "he_pathology", "section",
                mouseId, "", sectionId, "", "", "", "",
                endpoint.EndpointId, endpoint.ReferenceSpaceId,
                measurementProfileId
            };
            return "ifqmr-" + CanonicalJsonValueSha256(measurementIdentity);
        }

        private static void ValidateRecordInputLedger(
            object[] values, Dictionary<string, string> auditHashes,
            string sourcePackageSha256)
        {
            Require(values.Length == 13,
                    "H&E measurement provenance must contain exactly thirteen inputs.");
            Dictionary<string, string> observed =
                new Dictionary<string, string>(StringComparer.Ordinal);
            foreach (object value in values)
            {
                Dictionary<string, object> row =
                    AsObject(value, "H&E measurement provenance input");
                RequireExactKeys(row, new string[] { "role", "sha256" },
                                 "H&E measurement provenance input");
                string role = StringAt(row, "role");
                string digest = StringAt(row, "sha256");
                Require(IsLowerSha256(digest) && !observed.ContainsKey(role),
                        "H&E measurement provenance input is invalid or duplicated.");
                observed.Add(role, digest);
            }
            Require(observed.Count == auditHashes.Count + 1 &&
                    observed.ContainsKey("declared_source_package_ledger") &&
                    observed["declared_source_package_ledger"] ==
                        sourcePackageSha256,
                    "H&E measurement provenance lacks its declared source-package " +
                    "ledger binding.");
            foreach (KeyValuePair<string, string> expected in auditHashes)
                Require(observed.ContainsKey(expected.Key) &&
                        observed[expected.Key] == expected.Value,
                        "H&E measurement provenance input disagrees with audit role " +
                        expected.Key + ".");
        }

        private static Dictionary<string, List<HeSectionIdentity>> SectionsByMouse(
            Dictionary<string, HeSectionIdentity> sections)
        {
            Dictionary<string, List<HeSectionIdentity>> result =
                new Dictionary<string, List<HeSectionIdentity>>(StringComparer.Ordinal);
            foreach (HeSectionIdentity section in sections.Values)
            {
                List<HeSectionIdentity> rows;
                if (!result.TryGetValue(section.MouseId, out rows))
                {
                    rows = new List<HeSectionIdentity>();
                    result.Add(section.MouseId, rows);
                }
                rows.Add(section);
            }
            foreach (List<HeSectionIdentity> rows in result.Values)
            {
                rows.Sort(delegate(HeSectionIdentity left, HeSectionIdentity right)
                {
                    return left.TechnicalSectionOrder.CompareTo(
                        right.TechnicalSectionOrder);
                });
                Require(rows.Count == 2 && rows[0].TechnicalSectionOrder == 1 &&
                        rows[1].TechnicalSectionOrder == 2,
                        "H&E mouse does not have the declared ordered section pair.");
            }
            Require(result.Count == 4,
                    "H&E section declarations do not resolve to exactly four mice.");
            return result;
        }

        private static HeOrdinalEndpoint EndpointById(string endpointId)
        {
            foreach (HeOrdinalEndpoint endpoint in OrdinalEndpoints)
                if (endpoint.EndpointId == endpointId) return endpoint;
            return null;
        }

        private static string MouseEndpointKey(string mouse, string endpoint)
        {
            return mouse + "\n" + endpoint;
        }

        private static bool IsOrdinalToken(string value)
        {
            return In(value, "0", "1", "2", "3", "4", "uncertain");
        }

        private static bool In(string value, params string[] allowed)
        {
            foreach (string candidate in allowed)
                if (string.Equals(value, candidate, StringComparison.Ordinal)) return true;
            return false;
        }

        private static string[] JoinArrays(string[] first, string[] second)
        {
            string[] result = new string[first.Length + second.Length];
            Array.Copy(first, 0, result, 0, first.Length);
            Array.Copy(second, 0, result, first.Length, second.Length);
            return result;
        }

        private static string CsvAt(
            Dictionary<string, string> row, string key)
        {
            string value;
            Require(row.TryGetValue(key, out value),
                    "Missing H&E CSV field " + key + ".");
            return value;
        }

        private static void RequireCsvCellsTrimmed(
            Dictionary<string, string> row, string label)
        {
            foreach (KeyValuePair<string, string> cell in row)
                Require(cell.Value.IndexOf('\0') < 0 &&
                        cell.Value == cell.Value.Trim(),
                        label + " field " + cell.Key +
                        " contains NUL or surrounding whitespace.");
        }

        private static HeCsvTable ParseCsv(string text, string label)
        {
            Require(text != null && text.Length > 0,
                    label + " is empty.");
            List<string[]> rawRows = new List<string[]>();
            List<string> row = new List<string>();
            StringBuilder field = new StringBuilder();
            bool inQuotes = false;
            bool afterQuote = false;
            bool touched = false;
            for (int index = 0; index < text.Length; index++)
            {
                char character = text[index];
                Require(character != '\0', label + " contains NUL.");
                if (inQuotes)
                {
                    if (character == '"')
                    {
                        if (index + 1 < text.Length && text[index + 1] == '"')
                        {
                            field.Append('"');
                            index++;
                        }
                        else
                        {
                            inQuotes = false;
                            afterQuote = true;
                        }
                    }
                    else
                    {
                        field.Append(character);
                    }
                    touched = true;
                    continue;
                }
                if (afterQuote)
                {
                    if (character == ',')
                    {
                        row.Add(field.ToString());
                        field.Length = 0;
                        afterQuote = false;
                        touched = true;
                        continue;
                    }
                    if (character == '\r' || character == '\n')
                    {
                        if (character == '\r' && index + 1 < text.Length &&
                            text[index + 1] == '\n') index++;
                        row.Add(field.ToString());
                        rawRows.Add(row.ToArray());
                        row.Clear();
                        field.Length = 0;
                        afterQuote = false;
                        touched = false;
                        continue;
                    }
                    throw new InvalidOperationException(
                        label + " has characters after a quoted CSV field.");
                }
                if (character == '"')
                {
                    Require(field.Length == 0,
                            label + " contains a quote inside an unquoted field.");
                    inQuotes = true;
                    touched = true;
                }
                else if (character == ',')
                {
                    row.Add(field.ToString());
                    field.Length = 0;
                    touched = true;
                }
                else if (character == '\r' || character == '\n')
                {
                    if (character == '\r' && index + 1 < text.Length &&
                        text[index + 1] == '\n') index++;
                    row.Add(field.ToString());
                    rawRows.Add(row.ToArray());
                    row.Clear();
                    field.Length = 0;
                    touched = false;
                }
                else
                {
                    field.Append(character);
                    touched = true;
                }
            }
            Require(!inQuotes, label + " ends inside a quoted CSV field.");
            if (afterQuote || touched || row.Count > 0 || field.Length > 0)
            {
                row.Add(field.ToString());
                rawRows.Add(row.ToArray());
            }
            Require(rawRows.Count >= 1, label + " has no header.");
            string[] header = rawRows[0];
            if (header.Length > 0 && header[0].Length > 0 && header[0][0] == '\ufeff')
                header[0] = header[0].Substring(1);
            Require(header.Length > 0,
                    label + " has an empty header.");
            HashSet<string> keys = new HashSet<string>(StringComparer.Ordinal);
            foreach (string key in header)
                Require(!string.IsNullOrEmpty(key) && keys.Add(key),
                        label + " has an empty or duplicate header field.");
            HeCsvTable table = new HeCsvTable();
            table.Header = header;
            table.Rows = new List<Dictionary<string, string>>();
            for (int rowIndex = 1; rowIndex < rawRows.Count; rowIndex++)
            {
                string[] values = rawRows[rowIndex];
                Require(values.Length == header.Length,
                        label + " row " + (rowIndex + 1).ToString(
                            CultureInfo.InvariantCulture) +
                        " does not match the exact header width.");
                Dictionary<string, string> mapped =
                    new Dictionary<string, string>(StringComparer.Ordinal);
                for (int column = 0; column < header.Length; column++)
                    mapped.Add(header[column], values[column]);
                table.Rows.Add(mapped);
            }
            return table;
        }

        private static object[] ParseJsonArrayText(string text, string label)
        {
            try
            {
                object value = Json().DeserializeObject(text);
                object[] result = AsArrayValue(value);
                Require(result != null, label + " must be a JSON array.");
                return result;
            }
            catch (InvalidOperationException) { throw; }
            catch (Exception ex)
            {
                throw new InvalidOperationException(label + " is not valid JSON.", ex);
            }
        }

        private static List<Dictionary<string, object>> ParseJsonLines(
            string text, string label)
        {
            Require(text != null && text.Length > 0, label + " is empty.");
            List<Dictionary<string, object>> result =
                new List<Dictionary<string, object>>();
            using (StringReader reader = new StringReader(text))
            {
                string line;
                int number = 0;
                while ((line = reader.ReadLine()) != null)
                {
                    number++;
                    Require(line.Length > 0,
                            label + " contains a blank line at " +
                            number.ToString(CultureInfo.InvariantCulture) + ".");
                    result.Add(ParseJsonObject(
                        line, label + " line " +
                        number.ToString(CultureInfo.InvariantCulture)));
                }
            }
            return result;
        }

        private static object[] AsArrayValue(object value)
        {
            object[] result = value as object[];
            if (result == null)
            {
                ArrayList list = value as ArrayList;
                if (list != null) result = list.ToArray();
            }
            return result;
        }

        private static bool StringArrayEquals(object[] values, string[] expected)
        {
            if (values == null || values.Length != expected.Length) return false;
            for (int index = 0; index < values.Length; index++)
                if (!(values[index] is string) ||
                    !string.Equals((string)values[index], expected[index],
                                   StringComparison.Ordinal)) return false;
            return true;
        }

        private static bool IsNullAt(Dictionary<string, object> root, string key)
        {
            object value;
            Require(root.TryGetValue(key, out value), "Missing JSON field " + key + ".");
            return value == null;
        }

        private static string NullableStringAt(
            Dictionary<string, object> root, string key)
        {
            object value;
            Require(root.TryGetValue(key, out value), "Missing JSON field " + key + ".");
            if (value == null) return null;
            Require(value is string && ((string)value).Length > 0,
                    key + " must be null or a non-empty string.");
            return (string)value;
        }

        private static long? NullableIntegerAt(
            Dictionary<string, object> root, string key)
        {
            object value;
            Require(root.TryGetValue(key, out value), "Missing JSON field " + key + ".");
            if (value == null) return null;
            return IntegerAt(root, key);
        }

        internal static bool SelfTest(RuntimePaths paths)
        {
            string sandbox = null;
            try
            {
                HeAuthorityRoots roots = ValidateRuntime(paths);
                string status = StatusArguments(paths, roots.R1Root, roots.H4Root);
                string build = BuildReviewArguments(
                    paths, roots.R1Root, roots.H4Root, @"C:\fixture\review");
                string aggregate = AggregateReviewArguments(
                    paths, @"C:\fixture\review.csv", @"C:\fixture\aggregate");
                foreach (string command in new string[] { status, build, aggregate })
                {
                    Require(command.IndexOf(WindowsCommandLine.Quote("-I"),
                                        StringComparison.Ordinal) >= 0 &&
                        command.IndexOf(WindowsCommandLine.Quote("-S"),
                                        StringComparison.Ordinal) >= 0 &&
                        command.IndexOf("Fiji", StringComparison.OrdinalIgnoreCase) < 0 &&
                        command.IndexOf("QuPath", StringComparison.OrdinalIgnoreCase) < 0,
                        "H&E self-test command surface is not isolated.");
                }
                Require(status.IndexOf(WindowsCommandLine.Quote("status"),
                                   StringComparison.Ordinal) >= 0 &&
                    build.IndexOf(WindowsCommandLine.Quote("build-review"),
                                  StringComparison.Ordinal) >= 0 &&
                    aggregate.IndexOf(WindowsCommandLine.Quote("aggregate-review"),
                                      StringComparison.Ordinal) >= 0,
                    "H&E self-test command names are incomplete.");
                ParseStatus(StatusFixture());
                Dictionary<string, object> boolNumber =
                    new Dictionary<string, object>(StringComparer.Ordinal);
                boolNumber["value"] = true;
                bool boolRefused = false;
                try { IntegerAt(boolNumber, "value"); }
                catch (InvalidOperationException) { boolRefused = true; }
                Require(boolRefused,
                        "H&E integer parser accepted a JSON boolean as a number.");

                Dictionary<string, object> canonicalFixture =
                    new Dictionary<string, object>(StringComparer.Ordinal);
                canonicalFixture["z"] = 2;
                canonicalFixture["a"] = true;
                canonicalFixture["audit_payload_sha256"] = "excluded";
                Require(CanonicalJsonSha256(
                            canonicalFixture, "audit_payload_sha256") ==
                        "da052c406337c760913b699171f5fbe76c74cc8f0cc22403e1a44a6229f8fe3f",
                        "H&E canonical audit JSON/hash self-test failed.");
                Require(CanonicalJsonValueSha256(LockedReviewFields) ==
                            "4c62e0d0767e046548167d0e109dfd4f9bc96daeda9cf02625b898fa3ee09d8f" &&
                        ExpectedMeasurementProfileSha256(
                            Sha256File(paths.HeRubricPath),
                            Sha256File(paths.HeStainProfilePath)) ==
                            "711bf401eca70a63e0821b50c504337a0d75d756a08347357fcfa44563343daa",
                        "H&E locked header/profile canonical hash self-test failed.");

                HeCsvTable csvFixture = ParseCsv(
                    "\ufeffa,b\r\n1,\"two,quoted\"\r\n", "H&E CSV self-test");
                Require(StringArrayEquals(csvFixture.Header,
                                          new string[] { "a", "b" }) &&
                        csvFixture.Rows.Count == 1 &&
                        CsvAt(csvFixture.Rows[0], "a") == "1" &&
                        CsvAt(csvFixture.Rows[0], "b") == "two,quoted",
                        "H&E strict CSV parser self-test failed.");
                Require(ExpectedMeasurementRecordId(
                            "M2", "M2_BF_01", OrdinalEndpoints[0],
                            "g_surf_he_pathology_review_development_v1:" +
                            "section-ordinal-schema-v2") ==
                            "ifqmr-b83aad8741ddffcc2f4b91fc6074612ee4cce8dc65ffae3" +
                            "ace362e19bd529454",
                        "H&E deterministic measurement record_id self-test failed.");
                Dictionary<string, string> authoritativeFixture =
                    new Dictionary<string, string>(StringComparer.Ordinal);
                Dictionary<string, string> publishedFixture =
                    new Dictionary<string, string>(StringComparer.Ordinal);
                foreach (string field in LockedReviewFields)
                {
                    authoritativeFixture[field] = "locked";
                    publishedFixture[field] = "locked";
                }
                publishedFixture[
                    "whole_section_inflammation_extent_0_4_uncertain"] = "changed";
                bool reviewDriftRefused = false;
                try
                {
                    RequireLockedReviewFieldsMatch(
                        publishedFixture, authoritativeFixture, "fixture-section");
                }
                catch (InvalidOperationException) { reviewDriftRefused = true; }
                Require(reviewDriftRefused,
                        "H&E published section accepted drift from the selected review CSV.");

                Require(IsPythonExecutableName("python") &&
                        IsPythonExecutableName("python3.12") &&
                        !IsPythonExecutableName("Fiji") &&
                        !IsPythonExecutableName("pythonw"),
                        "H&E Python executable-name gate self-test failed.");
                string fixturePython = @"C:\CPython310\python.exe";
                Dictionary<string, object> probeFixture = NewObject();
                probeFixture["implementation"] = "CPython";
                probeFixture["major"] = 3;
                probeFixture["minor"] = 10;
                probeFixture["micro"] = 0;
                probeFixture["executable"] = fixturePython;
                probeFixture["isolated"] = true;
                probeFixture["no_site"] = true;
                probeFixture["dont_write_bytecode"] = true;
                HePythonIdentity probe = ParsePythonProbe(
                    Json().Serialize(probeFixture), fixturePython,
                    new string('a', 64));
                Require(probe.Major == 3 && probe.Minor == 10 &&
                        PythonProbeArguments().IndexOf(
                            WindowsCommandLine.Quote("-c"),
                            StringComparison.Ordinal) >= 0,
                        "H&E isolated Python probe self-test failed.");
                probeFixture["minor"] = 9;
                bool oldPythonRefused = false;
                try
                {
                    ParsePythonProbe(
                        Json().Serialize(probeFixture), fixturePython,
                        new string('a', 64));
                }
                catch (InvalidOperationException) { oldPythonRefused = true; }
                Require(oldPythonRefused,
                        "H&E Python probe accepted an unsupported CPython version.");
                probeFixture["minor"] = 10;
                probeFixture["executable"] = @"C:\Other\python.exe";
                bool substitutedPythonRefused = false;
                try
                {
                    ParsePythonProbe(
                        Json().Serialize(probeFixture), fixturePython,
                        new string('a', 64));
                }
                catch (InvalidOperationException)
                {
                    substitutedPythonRefused = true;
                }
                Require(substitutedPythonRefused,
                        "H&E Python probe accepted a substituted sys.executable path.");

                sandbox = Path.Combine(
                    Path.GetTempPath(), "IFQuantLauncher-he-selftest-" +
                    Guid.NewGuid().ToString("N"));
                Directory.CreateDirectory(sandbox);
                string moveSource = Path.Combine(sandbox, "package-staging");
                string moveFinal = Path.Combine(sandbox, "package-final");
                Directory.CreateDirectory(moveSource);
                File.WriteAllText(
                    Path.Combine(moveSource, "artifact.txt"),
                    "validated-package-fixture", new UTF8Encoding(false));
                using (HePackageLease package = HePackageLease.Open(moveSource))
                {
                    string leasedArtifact = Path.Combine(moveSource, "artifact.txt");
                    string renamedArtifact = Path.Combine(moveSource, "renamed.txt");
                    Require(package.FileCount == 1 &&
                            package.Hash("artifact.txt") ==
                                Sha256File(leasedArtifact),
                            "H&E publication fixture failed staging validation.");
                    bool renameRefused = false;
                    try { File.Move(leasedArtifact, renamedArtifact); }
                    catch (IOException) { renameRefused = true; }
                    catch (UnauthorizedAccessException) { renameRefused = true; }
                    bool deleteRefused = false;
                    try { File.Delete(leasedArtifact); }
                    catch (IOException) { deleteRefused = true; }
                    catch (UnauthorizedAccessException) { deleteRefused = true; }
                    Require(renameRefused && deleteRefused &&
                            File.Exists(leasedArtifact) &&
                            !File.Exists(renamedArtifact),
                            "H&E leased package file allowed rename or delete.");
                    package.VerifyUnchanged();
                }
                Directory.Move(moveSource, moveFinal);
                using (HePackageLease published = HePackageLease.Open(moveFinal))
                {
                    Require(published.FileCount == 1 &&
                            published.Contains("artifact.txt"),
                            "H&E publication fixture failed post-move reopening.");
                    published.VerifyUnchanged();
                }
                string fresh = Path.Combine(sandbox, "fresh-review");
                Require(string.Equals(NormalizeFreshOutput(fresh, "self-test"), fresh,
                                      StringComparison.OrdinalIgnoreCase),
                        "H&E self-test fresh path changed during normalization.");
                Directory.CreateDirectory(fresh);
                bool refused = false;
                try { NormalizeFreshOutput(fresh, "self-test"); }
                catch (InvalidOperationException) { refused = true; }
                Require(refused, "H&E self-test accepted an existing output path.");
                return true;
            }
            catch (Exception ex)
            {
                try
                {
                    File.WriteAllText(
                        Path.Combine(Path.GetTempPath(),
                                     "IFQuantLauncher-he-self-test-error.txt"),
                        ex.ToString(), Encoding.UTF8);
                }
                catch { }
                return false;
            }
            finally
            {
                try
                {
                    if (sandbox != null && Directory.Exists(sandbox) &&
                        Path.GetFileName(sandbox).StartsWith(
                            "IFQuantLauncher-he-selftest-", StringComparison.Ordinal))
                        Directory.Delete(sandbox, true);
                }
                catch { }
            }
        }

        private static string StatusFixture()
        {
            return "{" +
                "\"schema_version\":\"1.0.0\"," +
                "\"checked_utc\":\"2026-08-26T00:00:00+00:00\"," +
                "\"study_id\":\"g_surf_he_20260812\"," +
                "\"highest_authorized_release\":\"R1\"," +
                "\"highest_authorized_stage\":\"H3\"," +
                "\"launcher_route_enabled\":false," +
                "\"package_roots\":{},\"source\":{}," +
                "\"r1\":{\"decision\":\"APPROVED_IMAGE_QC\"}," +
                "\"h4\":{\"status\":\"H4_REGION_REVIEW_REQUIRED_NOT_R2_RESULT\"," +
                "\"use_as_endpoint\":false}," +
                "\"stages\":[" +
                StageFixture("H0", "PASS") + "," +
                StageFixture("H1", "PASS") + "," +
                StageFixture("H2", "APPROVED_R1") + "," +
                StageFixture("H3", "APPROVED_R1") + "," +
                StageFixture("H4", "DEVELOPMENT_CONTEXT_AVAILABLE") + "," +
                StageFixture("H5", "BLOCKED") + "," +
                StageFixture("H6", "BLOCKED") + "," +
                StageFixture("H7", "RUBRIC_DEFINED_REVIEW_REQUIRED") + "," +
                StageFixture("H8", "BLOCKED") + "," +
                StageFixture("H9", "BLOCKED") + "]," +
                "\"reportability\":{" +
                "\"image_qc_and_denominator\":\"APPROVED_FOR_THIS_COHORT\"," +
                "\"section_pathology_scores\":\"REVIEW_REQUIRED\"," +
                "\"automated_lesion_burden\":\"NOT_AVAILABLE\"," +
                "\"mouse_summary\":\"BLOCKED\"," +
                "\"group_inference\":\"NOT_SUPPORTED_N1_PER_DESIGN_CELL\"," +
                "\"he_identifies_krt5_pod\":false," +
                "\"immune_lineage_from_he\":false}}";
        }

        private static string StageFixture(string stage, string status)
        {
            return "{\"stage\":\"" + stage + "\",\"status\":\"" + status +
                   "\",\"evidence\":\"fixture\"}";
        }

        internal static Dictionary<string, object> ReadJsonObject(string path)
        {
            return ParseJsonObject(File.ReadAllText(path, Encoding.UTF8), path);
        }

        internal static Dictionary<string, object> ParseJsonObject(
            string text, string label)
        {
            try
            {
                Dictionary<string, object> value =
                    Json().Deserialize<Dictionary<string, object>>(text);
                Require(value != null, label + " must be a JSON object.");
                return value;
            }
            catch (InvalidOperationException) { throw; }
            catch (Exception ex)
            {
                throw new InvalidOperationException(label + " is not valid JSON.", ex);
            }
        }

        internal static Dictionary<string, object> AsObject(object value, string label)
        {
            Dictionary<string, object> result = value as Dictionary<string, object>;
            Require(result != null, label + " must be an object.");
            return result;
        }

        internal static Dictionary<string, object> ObjectAt(
            Dictionary<string, object> root, string key)
        {
            object value;
            Require(root.TryGetValue(key, out value), "Missing JSON field " + key + ".");
            return AsObject(value, key);
        }

        internal static object[] ArrayAt(Dictionary<string, object> root, string key)
        {
            object value;
            Require(root.TryGetValue(key, out value), "Missing JSON field " + key + ".");
            object[] result = value as object[];
            if (result == null)
            {
                ArrayList list = value as ArrayList;
                if (list != null) result = list.ToArray();
            }
            Require(result != null, key + " must be an array.");
            return result;
        }

        internal static string StringAt(Dictionary<string, object> root, string key)
        {
            object value;
            Require(root.TryGetValue(key, out value) && value is string &&
                    ((string)value).Length > 0,
                    key + " must be a non-empty string.");
            return (string)value;
        }

        internal static bool BoolAt(Dictionary<string, object> root, string key)
        {
            object value;
            Require(root.TryGetValue(key, out value) && value is bool,
                    key + " must be boolean.");
            return (bool)value;
        }

        internal static long IntegerAt(Dictionary<string, object> root, string key)
        {
            object value;
            Require(root.TryGetValue(key, out value) && value != null,
                    key + " must be an integer.");
            Require(!(value is bool), key + " must be an integer, not boolean.");
            try
            {
                decimal numeric = Convert.ToDecimal(value, CultureInfo.InvariantCulture);
                Require(numeric == Decimal.Truncate(numeric) && numeric >= 0,
                        key + " must be a nonnegative integer.");
                return Decimal.ToInt64(numeric);
            }
            catch (InvalidOperationException) { throw; }
            catch (Exception ex)
            {
                throw new InvalidOperationException(key + " must be an integer.", ex);
            }
        }

        internal static void RequireExactKeys(
            Dictionary<string, object> value, string[] expected, string label)
        {
            HashSet<string> observed = new HashSet<string>(
                value.Keys, StringComparer.Ordinal);
            Require(observed.SetEquals(expected), label + " fields are not exact.");
        }

        internal static void Require(bool condition, string message)
        {
            if (!condition) throw new InvalidOperationException(message);
        }

        internal static string Sha256File(string path)
        {
            using (FileStream stream = new FileStream(
                       path, FileMode.Open, FileAccess.Read, FileShare.Read))
                return Sha256(stream);
        }

        private static string ReadUtf8File(
            string path, int maximumBytes, string label)
        {
            using (FileStream stream = new FileStream(
                       path, FileMode.Open, FileAccess.Read, FileShare.Read))
            {
                Require(stream.Length <= maximumBytes,
                        label + " exceeds the safe byte limit.");
                byte[] bytes = new byte[stream.Length];
                int offset = 0;
                while (offset < bytes.Length)
                {
                    int count = stream.Read(bytes, offset, bytes.Length - offset);
                    if (count == 0) throw new EndOfStreamException(label);
                    offset += count;
                }
                return new UTF8Encoding(false, true).GetString(bytes);
            }
        }

        internal static string Sha256(Stream stream)
        {
            stream.Position = 0;
            using (SHA256 algorithm = SHA256.Create())
            {
                byte[] hash = algorithm.ComputeHash(stream);
                stream.Position = 0;
                StringBuilder text = new StringBuilder(64);
                foreach (byte value in hash)
                    text.Append(value.ToString("x2", CultureInfo.InvariantCulture));
                return text.ToString();
            }
        }

        internal static string CanonicalJsonSha256(
            Dictionary<string, object> root, string excludedRootKey)
        {
            StringBuilder canonical = new StringBuilder();
            WriteCanonicalObject(canonical, root, excludedRootKey);
            return Sha256Utf8(canonical.ToString());
        }

        private static string CanonicalJsonValueSha256(object value)
        {
            StringBuilder canonical = new StringBuilder();
            WriteCanonicalValue(canonical, value);
            return Sha256Utf8(canonical.ToString());
        }

        private static string Sha256Utf8(string textValue)
        {
            byte[] bytes = new UTF8Encoding(false, true).GetBytes(textValue);
            using (SHA256 algorithm = SHA256.Create())
            {
                byte[] hash = algorithm.ComputeHash(bytes);
                StringBuilder text = new StringBuilder(64);
                foreach (byte value in hash)
                    text.Append(value.ToString("x2", CultureInfo.InvariantCulture));
                return text.ToString();
            }
        }

        private static void WriteCanonicalObject(
            StringBuilder output, Dictionary<string, object> value,
            string excludedRootKey)
        {
            List<string> keys = new List<string>(value.Keys);
            keys.Sort(StringComparer.Ordinal);
            output.Append('{');
            bool first = true;
            foreach (string key in keys)
            {
                if (string.Equals(key, excludedRootKey, StringComparison.Ordinal)) continue;
                if (!first) output.Append(',');
                first = false;
                WriteCanonicalString(output, key);
                output.Append(':');
                WriteCanonicalValue(output, value[key]);
            }
            output.Append('}');
        }

        private static void WriteCanonicalValue(StringBuilder output, object value)
        {
            if (value == null)
            {
                output.Append("null");
                return;
            }
            string text = value as string;
            if (text != null)
            {
                WriteCanonicalString(output, text);
                return;
            }
            if (value is bool)
            {
                output.Append((bool)value ? "true" : "false");
                return;
            }
            Dictionary<string, object> map = value as Dictionary<string, object>;
            if (map != null)
            {
                WriteCanonicalObject(output, map, null);
                return;
            }
            object[] array = value as object[];
            if (array == null)
            {
                ArrayList list = value as ArrayList;
                if (list != null) array = list.ToArray();
            }
            if (array != null)
            {
                output.Append('[');
                for (int index = 0; index < array.Length; index++)
                {
                    if (index > 0) output.Append(',');
                    WriteCanonicalValue(output, array[index]);
                }
                output.Append(']');
                return;
            }
            try
            {
                decimal number = Convert.ToDecimal(value, CultureInfo.InvariantCulture);
                Require(number == Decimal.Truncate(number),
                        "H&E aggregation audit contains a non-integer JSON number; " +
                        "canonical verification refuses an ambiguous representation.");
                output.Append(number.ToString("0", CultureInfo.InvariantCulture));
            }
            catch (InvalidOperationException) { throw; }
            catch (Exception ex)
            {
                throw new InvalidOperationException(
                    "H&E aggregation audit contains an unsupported JSON value type.", ex);
            }
        }

        private static void WriteCanonicalString(StringBuilder output, string value)
        {
            output.Append('"');
            for (int index = 0; index < value.Length; index++)
            {
                char character = value[index];
                switch (character)
                {
                    case '"': output.Append("\\\""); break;
                    case '\\': output.Append("\\\\"); break;
                    case '\b': output.Append("\\b"); break;
                    case '\f': output.Append("\\f"); break;
                    case '\n': output.Append("\\n"); break;
                    case '\r': output.Append("\\r"); break;
                    case '\t': output.Append("\\t"); break;
                    default:
                        if (character < 0x20)
                            output.Append("\\u").Append(
                                ((int)character).ToString("x4", CultureInfo.InvariantCulture));
                        else
                            output.Append(character);
                        break;
                }
            }
            output.Append('"');
        }

        internal static void RequireRegularFile(string path, string label)
        {
            Require(!string.IsNullOrWhiteSpace(path) && File.Exists(path),
                    label + " is missing: " + path);
            RejectReparseAncestors(path, label);
            FileAttributes attributes = File.GetAttributes(path);
            Require((attributes & (FileAttributes.Directory | FileAttributes.ReparsePoint |
                                   FileAttributes.Device)) == 0,
                    label + " must be a regular non-reparse file: " + path);
        }

        internal static void RejectReparseAncestors(string path, string label)
        {
            string full = Path.GetFullPath(path);
            string root = Path.GetPathRoot(full);
            string current = root;
            string remainder = full.Substring(root.Length);
            foreach (string part in remainder.Split(
                         new char[] { '\\', '/' }, StringSplitOptions.RemoveEmptyEntries))
            {
                current = Path.Combine(current, part);
                if (!File.Exists(current) && !Directory.Exists(current)) break;
                FileAttributes attributes = File.GetAttributes(current);
                Require((attributes & FileAttributes.ReparsePoint) == 0,
                        label + " contains a reparse-point path component: " + current);
            }
        }

        private static string NormalizeAbsolute(string value, string label)
        {
            Require(!string.IsNullOrWhiteSpace(value) && value == value.Trim(),
                    label + " must be an explicit path without surrounding whitespace.");
            Require(Path.IsPathRooted(value), label + " must be an absolute path.");
            foreach (string part in value.Split(
                         new char[] { '\\', '/' }, StringSplitOptions.RemoveEmptyEntries))
                Require(part != "." && part != "..",
                        label + " must not contain traversal components.");
            return Path.GetFullPath(value);
        }

        private static string LiteralAbsoluteString(
            Dictionary<string, object> root, string key)
        {
            string value = StringAt(root, key);
            Require(value == value.Trim() && Path.IsPathRooted(value),
                    key + " must be a literal absolute path.");
            return Path.GetFullPath(value);
        }

        private static bool PathsOverlap(string left, string right)
        {
            string a = Path.GetFullPath(left).TrimEnd('\\', '/');
            string b = Path.GetFullPath(right).TrimEnd('\\', '/');
            return string.Equals(a, b, StringComparison.OrdinalIgnoreCase) ||
                   a.StartsWith(b + Path.DirectorySeparatorChar,
                                StringComparison.OrdinalIgnoreCase) ||
                   b.StartsWith(a + Path.DirectorySeparatorChar,
                                StringComparison.OrdinalIgnoreCase);
        }

        private static bool IsSafeRelativePath(string value)
        {
            if (string.IsNullOrEmpty(value) || value != value.Trim() ||
                value.IndexOf('\\') >= 0 || Path.IsPathRooted(value)) return false;
            foreach (string part in value.Split('/'))
                if (part.Length == 0 || part == "." || part == "..") return false;
            return true;
        }

        private static void RequireUtc(string value, string label)
        {
            DateTimeOffset parsed;
            Require(value.IndexOf('T') > 0 &&
                    (value.EndsWith("Z", StringComparison.Ordinal) ||
                     value.EndsWith("+00:00", StringComparison.Ordinal)) &&
                    DateTimeOffset.TryParse(
                        value, CultureInfo.InvariantCulture,
                        DateTimeStyles.None, out parsed) &&
                    parsed.Offset == TimeSpan.Zero,
                    label + " must be an explicit valid UTC timestamp.");
        }

        private static bool IsLowerSha256(string value)
        {
            if (value == null || value.Length != 64) return false;
            foreach (char character in value)
                if (!((character >= '0' && character <= '9') ||
                      (character >= 'a' && character <= 'f'))) return false;
            return true;
        }
    }

    internal sealed class HeTreeSnapshot
    {
        public readonly HashSet<string> Directories = new HashSet<string>(
            StringComparer.OrdinalIgnoreCase);
        public readonly HashSet<string> Files = new HashSet<string>(
            StringComparer.OrdinalIgnoreCase);
    }

    internal static class HeTreeSecurity
    {
        internal static HeTreeSnapshot Snapshot(IEnumerable<string> roots, int maximumFiles)
        {
            HeTreeSnapshot snapshot = new HeTreeSnapshot();
            Queue<string> pending = new Queue<string>();
            foreach (string value in roots)
                pending.Enqueue(Path.GetFullPath(value));
            while (pending.Count > 0)
            {
                string directory = pending.Dequeue();
                FileAttributes directoryAttributes = File.GetAttributes(directory);
                HeReviewContract.Require(
                    (directoryAttributes & (FileAttributes.Directory |
                                             FileAttributes.ReparsePoint)) ==
                        FileAttributes.Directory,
                    "H&E authority tree contains a non-directory or reparse point: " +
                    directory);
                if (!snapshot.Directories.Add(directory)) continue;
                string[] entries = Directory.GetFileSystemEntries(directory);
                Array.Sort(entries, StringComparer.OrdinalIgnoreCase);
                foreach (string entry in entries)
                {
                    string full = Path.GetFullPath(entry);
                    FileAttributes attributes = File.GetAttributes(full);
                    HeReviewContract.Require((attributes & FileAttributes.ReparsePoint) == 0,
                        "H&E authority tree contains a reparse point: " + full);
                    if ((attributes & FileAttributes.Directory) != 0)
                        pending.Enqueue(full);
                    else
                    {
                        HeReviewContract.Require((attributes & FileAttributes.Device) == 0,
                            "H&E authority tree contains a device: " + full);
                        snapshot.Files.Add(full);
                        HeReviewContract.Require(snapshot.Files.Count <= maximumFiles,
                            "H&E authority tree exceeds the safe file-count limit (" +
                            maximumFiles.ToString(CultureInfo.InvariantCulture) + ").");
                    }
                }
            }
            return snapshot;
        }

        internal static bool Same(HeTreeSnapshot left, HeTreeSnapshot right)
        {
            return left.Directories.SetEquals(right.Directories) &&
                   left.Files.SetEquals(right.Files);
        }
    }

    internal sealed class HeInputLease : IDisposable
    {
        private readonly string[] roots;
        private readonly HeTreeSnapshot snapshot;
        private List<FileStream> streams;

        private HeInputLease(
            string[] lockedRoots, HeTreeSnapshot lockedSnapshot,
            List<FileStream> lockedStreams)
        {
            roots = lockedRoots;
            snapshot = lockedSnapshot;
            streams = lockedStreams;
        }

        internal static HeInputLease Acquire(string[] roots, string[] extraFiles)
        {
            HeTreeSnapshot snapshot = HeTreeSecurity.Snapshot(roots, 20000);
            List<FileStream> streams = new List<FileStream>();
            try
            {
                foreach (string file in snapshot.Files)
                    streams.Add(new FileStream(
                        file, FileMode.Open, FileAccess.Read, FileShare.Read));
                foreach (string file in extraFiles)
                {
                    HeReviewContract.RequireRegularFile(file, "H&E direct input");
                    if (!snapshot.Files.Contains(Path.GetFullPath(file)))
                        streams.Add(new FileStream(
                            file, FileMode.Open, FileAccess.Read, FileShare.Read));
                }
                HeTreeSnapshot closed = HeTreeSecurity.Snapshot(roots, 20000);
                HeReviewContract.Require(HeTreeSecurity.Same(snapshot, closed),
                    "H&E authority tree changed while read locks were acquired.");
                return new HeInputLease(roots, snapshot, streams);
            }
            catch
            {
                foreach (FileStream stream in streams) stream.Dispose();
                throw;
            }
        }

        internal void VerifyUnchanged()
        {
            HeTreeSnapshot current = HeTreeSecurity.Snapshot(roots, 20000);
            HeReviewContract.Require(HeTreeSecurity.Same(snapshot, current),
                "H&E authority tree changed before publication.");
        }

        public void Dispose()
        {
            if (streams == null) return;
            foreach (FileStream stream in streams) stream.Dispose();
            streams = null;
        }
    }

    internal sealed class HePackageLease : IDisposable
    {
        private readonly string root;
        private readonly HeTreeSnapshot snapshot;
        private Dictionary<string, FileStream> files;

        private HePackageLease(
            string value, HeTreeSnapshot tree, Dictionary<string, FileStream> opened)
        {
            root = value;
            snapshot = tree;
            files = opened;
        }

        internal int FileCount { get { return files.Count; } }
        internal int DirectoryCount { get { return snapshot.Directories.Count; } }

        internal static HePackageLease Open(string value)
        {
            string root = Path.GetFullPath(value);
            HeTreeSnapshot snapshot = HeTreeSecurity.Snapshot(
                new string[] { root }, 4096);
            Dictionary<string, FileStream> files = new Dictionary<string, FileStream>(
                StringComparer.Ordinal);
            try
            {
                foreach (string path in snapshot.Files)
                {
                    string relative = path.Substring(
                        root.TrimEnd('\\', '/').Length + 1).Replace('\\', '/');
                    files.Add(relative, new FileStream(
                        path, FileMode.Open, FileAccess.Read,
                        FileShare.Read));
                }
                HeTreeSnapshot closed = HeTreeSecurity.Snapshot(
                    new string[] { root }, 4096);
                HeReviewContract.Require(HeTreeSecurity.Same(snapshot, closed),
                    "H&E output package changed while validation locks were acquired.");
                return new HePackageLease(root, snapshot, files);
            }
            catch
            {
                foreach (FileStream stream in files.Values) stream.Dispose();
                throw;
            }
        }

        internal bool Contains(string relative) { return files.ContainsKey(relative); }
        internal long Length(string relative) { return Stream(relative).Length; }
        internal string Hash(string relative)
        {
            return HeReviewContract.Sha256(Stream(relative));
        }
        internal string ReadUtf8(string relative)
        {
            FileStream stream = Stream(relative);
            HeReviewContract.Require(stream.Length <= 8 * 1024 * 1024,
                "H&E JSON authority exceeds 8 MiB: " + relative);
            stream.Position = 0;
            byte[] bytes = new byte[stream.Length];
            int offset = 0;
            while (offset < bytes.Length)
            {
                int count = stream.Read(bytes, offset, bytes.Length - offset);
                if (count == 0) throw new EndOfStreamException(relative);
                offset += count;
            }
            stream.Position = 0;
            return new UTF8Encoding(false, true).GetString(bytes);
        }
        internal Dictionary<string, object> Json(string relative)
        {
            return HeReviewContract.ParseJsonObject(ReadUtf8(relative), relative);
        }
        internal HashSet<string> RelativeFilesExcept(string excluded)
        {
            HashSet<string> result = new HashSet<string>(
                files.Keys, StringComparer.Ordinal);
            result.Remove(excluded);
            return result;
        }
        internal void VerifyUnchanged()
        {
            HeTreeSnapshot current = HeTreeSecurity.Snapshot(
                new string[] { root }, 4096);
            HeReviewContract.Require(HeTreeSecurity.Same(snapshot, current),
                "H&E output package changed during validation.");
        }
        private FileStream Stream(string relative)
        {
            FileStream value;
            HeReviewContract.Require(files.TryGetValue(relative, out value),
                "H&E package file is missing: " + relative);
            return value;
        }
        public void Dispose()
        {
            if (files == null) return;
            foreach (FileStream stream in files.Values) stream.Dispose();
            files = null;
        }
    }

    internal sealed class HeReviewForm : Form
    {
        private readonly RuntimePaths runtime;
        private readonly HeAuthorityRoots contractRoots;
        private TextBox pythonBox;
        private TextBox r1Box;
        private TextBox h4Box;
        private TextBox reviewOutputBox;
        private TextBox reviewCsvBox;
        private TextBox aggregateOutputBox;
        private TextBox statusBox;
        private Button statusButton;
        private Button buildButton;
        private Button aggregateButton;
        private Button cancelButton;
        private Button closeButton;
        private readonly object processSync = new object();
        private Process activeProcess;
        private ProcessJob activeJob;
        private int busy;
        private int cancellationRequested;
        private bool closeAfterCancellation;
        private readonly object publicationSync = new object();
        private bool finalOutputMoved;
        private bool finalOutputCommitted;
        private string committedOutputPath;
        private string operationPythonReceipt;

        internal HeReviewForm(RuntimePaths paths, string initialPython)
        {
            runtime = paths;
            contractRoots = HeReviewContract.ValidateRuntime(paths);
            Text = "IFQuant H&E engineering/review tools — biological analysis disabled";
            StartPosition = FormStartPosition.CenterParent;
            MinimumSize = new Size(900, 680);
            Size = new Size(1040, 790);
            AutoScaleMode = AutoScaleMode.Dpi;
            BuildInterface(initialPython ?? "");
            FormClosing += OnReviewFormClosing;
        }

        private void BuildInterface(string initialPython)
        {
            TableLayoutPanel root = new TableLayoutPanel();
            root.Dock = DockStyle.Fill;
            root.Padding = new Padding(12);
            root.ColumnCount = 1;
            root.RowCount = 5;
            root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            root.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
            root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            Controls.Add(root);

            Label boundary = new Label();
            boundary.Dock = DockStyle.Top;
            boundary.AutoSize = true;
            boundary.Padding = new Padding(10);
            boundary.BackColor = Color.FromArgb(255, 235, 195);
            boundary.ForeColor = Color.FromArgb(105, 45, 0);
            boundary.Text =
                "ENGINEERING / REVIEW ONLY. Route 3 biological execution remains disabled. " +
                "This screen cannot launch Fiji or QuPath and cannot run H5/H6 analysis. " +
                "It exposes only packaged status, blinded review-package construction, and " +
                "descriptive aggregation of a complete accepted review. Automated lesion " +
                "burden, immune lineage, KRT5-pod identity and group inference remain unavailable.";
            root.Controls.Add(boundary, 0, 0);

            GroupBox authorities = new GroupBox();
            authorities.Text = "Locked authorities and external package roots";
            authorities.Dock = DockStyle.Top;
            authorities.AutoSize = true;
            TableLayoutPanel authorityTable = NewTable();
            authorities.Controls.Add(authorityTable);
            pythonBox = AddPathRow(authorityTable, 0, "CPython 3.10+ executable", initialPython,
                                   "file", "Python executable (*.exe)|*.exe");
            AddReadOnlyRow(authorityTable, 1, "Packaged study", runtime.HeStudyPath);
            AddReadOnlyRow(authorityTable, 2, "Packaged rubric", runtime.HeRubricPath);
            AddReadOnlyRow(authorityTable, 3, "Packaged stain profile",
                           runtime.HeStainProfilePath);
            AddReadOnlyRow(authorityTable, 4, "Measurement schema",
                           runtime.MeasurementSchemaPath);
            AddReadOnlyRow(authorityTable, 5, "Declared source root",
                           contractRoots.SourceRoot);
            r1Box = AddPathRow(authorityTable, 6, "Approved R1 package root",
                               contractRoots.R1Root, "folder", null);
            h4Box = AddPathRow(authorityTable, 7, "H4 development context root",
                               contractRoots.H4Root, "folder", null);
            root.Controls.Add(authorities, 0, 1);

            GroupBox operations = new GroupBox();
            operations.Text = "Review-only operations";
            operations.Dock = DockStyle.Top;
            operations.AutoSize = true;
            TableLayoutPanel operationTable = NewTable();
            operations.Controls.Add(operationTable);
            statusButton = AddActionRow(
                operationTable, 0, "Validate status",
                "Validate exact source/R1/H4 identities and show the highest authorized state.",
                delegate { StartOperation("status validation", RunStatus); });
            reviewOutputBox = AddPathRow(
                operationTable, 1, "Fresh blinded-review output", "", "fresh",
                "H5_H7_PATHOLOGY_REVIEW_DEVELOPMENT");
            buildButton = AddActionRow(
                operationTable, 2, "Build blinded review package",
                "Publishes only after status, manifest, hash and fresh-path checks pass.",
                delegate { StartOperation("review-package build", BuildReviewPackage); });
            reviewCsvBox = AddPathRow(
                operationTable, 3, "Completed blinded H7 review CSV", "", "file",
                "CSV files (*.csv)|*.csv");
            aggregateOutputBox = AddPathRow(
                operationTable, 4, "Fresh descriptive output", "", "fresh",
                "H7_REVIEW_AGGREGATED_DESCRIPTIVE_ONLY");
            aggregateButton = AddActionRow(
                operationTable, 5, "Aggregate accepted review (descriptive only)",
                "No scalar ordinal composite, biological inference, or group hypothesis test.",
                delegate { StartOperation("descriptive review aggregation",
                                          AggregateReview); });
            root.Controls.Add(operations, 0, 2);

            statusBox = new TextBox();
            statusBox.Dock = DockStyle.Fill;
            statusBox.Multiline = true;
            statusBox.ReadOnly = true;
            statusBox.ScrollBars = ScrollBars.Both;
            statusBox.WordWrap = false;
            statusBox.Font = new Font(FontFamily.GenericMonospace, 9F);
            statusBox.Text =
                "Not yet validated. The packaged contract declares R1/H3 as the highest " +
                "authorized release/stage; press Validate status to verify external bytes.";
            root.Controls.Add(statusBox, 0, 3);

            FlowLayoutPanel buttons = new FlowLayoutPanel();
            buttons.Dock = DockStyle.Fill;
            buttons.FlowDirection = FlowDirection.RightToLeft;
            buttons.AutoSize = true;
            closeButton = new Button();
            closeButton.Text = "Close";
            closeButton.AutoSize = true;
            closeButton.Click += delegate { Close(); };
            cancelButton = new Button();
            cancelButton.Text = "Cancel active review operation";
            cancelButton.AutoSize = true;
            cancelButton.Enabled = false;
            cancelButton.Click += delegate { RequestCancellation(true); };
            buttons.Controls.Add(closeButton);
            buttons.Controls.Add(cancelButton);
            root.Controls.Add(buttons, 0, 4);
            AcceptButton = statusButton;
            CancelButton = closeButton;
        }

        private static TableLayoutPanel NewTable()
        {
            TableLayoutPanel table = new TableLayoutPanel();
            table.Dock = DockStyle.Top;
            table.AutoSize = true;
            table.ColumnCount = 3;
            table.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 210F));
            table.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100F));
            table.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 120F));
            return table;
        }

        private TextBox AddPathRow(
            TableLayoutPanel table, int row, string label, string value,
            string mode, string filterOrSuggestedName)
        {
            Label caption = new Label();
            caption.Text = label;
            caption.AutoSize = true;
            caption.Anchor = AnchorStyles.Left;
            TextBox box = new TextBox();
            box.Dock = DockStyle.Fill;
            box.Text = value;
            Button browse = new Button();
            browse.Text = mode == "fresh" ? "Choose parent..." : "Browse...";
            browse.Dock = DockStyle.Fill;
            if (mode == "folder")
                browse.Click += delegate { BrowseFolder(box); };
            else if (mode == "fresh")
                browse.Click += delegate { BrowseFreshTarget(box, filterOrSuggestedName); };
            else
                browse.Click += delegate { BrowseFile(box, filterOrSuggestedName); };
            table.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            table.Controls.Add(caption, 0, row);
            table.Controls.Add(box, 1, row);
            table.Controls.Add(browse, 2, row);
            return box;
        }

        private static void AddReadOnlyRow(
            TableLayoutPanel table, int row, string label, string value)
        {
            Label caption = new Label();
            caption.Text = label;
            caption.AutoSize = true;
            caption.Anchor = AnchorStyles.Left;
            TextBox box = new TextBox();
            box.Dock = DockStyle.Fill;
            box.ReadOnly = true;
            box.Text = value;
            table.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            table.Controls.Add(caption, 0, row);
            table.Controls.Add(box, 1, row);
            table.SetColumnSpan(box, 2);
        }

        private static Button AddActionRow(
            TableLayoutPanel table, int row, string text, string explanation,
            EventHandler click)
        {
            Label label = new Label();
            label.Text = explanation;
            label.AutoSize = true;
            label.MaximumSize = new Size(620, 0);
            label.Anchor = AnchorStyles.Left;
            Button button = new Button();
            button.Text = text;
            button.AutoSize = true;
            button.Dock = DockStyle.Fill;
            button.Click += click;
            table.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            table.Controls.Add(label, 0, row);
            table.SetColumnSpan(label, 2);
            table.Controls.Add(button, 2, row);
            return button;
        }

        private void BrowseFolder(TextBox target)
        {
            using (FolderBrowserDialog dialog = new FolderBrowserDialog())
            {
                if (Directory.Exists(target.Text)) dialog.SelectedPath = target.Text;
                if (dialog.ShowDialog(this) == DialogResult.OK)
                    target.Text = dialog.SelectedPath;
            }
        }

        private void BrowseFreshTarget(TextBox target, string suggestedName)
        {
            using (FolderBrowserDialog dialog = new FolderBrowserDialog())
            {
                string currentParent = null;
                try { currentParent = Path.GetDirectoryName(target.Text); }
                catch { }
                if (currentParent != null && Directory.Exists(currentParent))
                    dialog.SelectedPath = currentParent;
                dialog.Description =
                    "Choose an existing parent. The launcher will create a new child folder " +
                    "and refuses overwrite.";
                if (dialog.ShowDialog(this) == DialogResult.OK)
                {
                    string leaf = suggestedName;
                    try
                    {
                        string typed = Path.GetFileName(target.Text.TrimEnd('\\', '/'));
                        if (!string.IsNullOrWhiteSpace(typed)) leaf = typed;
                    }
                    catch { }
                    target.Text = Path.Combine(dialog.SelectedPath, leaf);
                }
            }
        }

        private void BrowseFile(TextBox target, string filter)
        {
            using (OpenFileDialog dialog = new OpenFileDialog())
            {
                dialog.Filter = string.IsNullOrEmpty(filter)
                    ? "All files (*.*)|*.*" : filter;
                dialog.CheckFileExists = true;
                if (File.Exists(target.Text)) dialog.FileName = target.Text;
                if (dialog.ShowDialog(this) == DialogResult.OK)
                    target.Text = dialog.FileName;
            }
        }

        private void StartOperation(string name, Func<string> work)
        {
            if (Interlocked.CompareExchange(ref busy, 1, 0) != 0) return;
            Interlocked.Exchange(ref cancellationRequested, 0);
            lock (publicationSync)
            {
                finalOutputCommitted = false;
                finalOutputMoved = false;
                committedOutputPath = null;
                operationPythonReceipt = null;
            }
            closeAfterCancellation = false;
            SetBusy(true);
            statusBox.Text = "Running " + name + "...";
            ThreadPool.QueueUserWorkItem(delegate
            {
                string result = null;
                Exception failure = null;
                try { result = work(); }
                catch (Exception ex) { failure = ex; }
                try
                {
                    BeginInvoke((MethodInvoker)delegate
                    {
                        bool cancelled = Interlocked.CompareExchange(
                            ref cancellationRequested, 0, 0) != 0;
                        bool committed;
                        bool moved;
                        string publishedPath;
                        string pythonReceipt;
                        lock (publicationSync)
                        {
                            committed = finalOutputCommitted;
                            moved = finalOutputMoved;
                            publishedPath = committedOutputPath;
                            pythonReceipt = operationPythonReceipt;
                        }
                        Interlocked.Exchange(ref busy, 0);
                        SetBusy(false);
                        if (moved && !committed)
                            statusBox.Text =
                                "FINAL PATH EXISTS BUT IS NOT AUTHORIZED.\r\n" +
                                publishedPath + "\r\n\r\nPost-move validation failed: " +
                                (failure == null ? "unknown failure" : failure.Message) +
                                ReceiptSuffix(pythonReceipt);
                        else if (committed && failure != null)
                            statusBox.Text =
                                "FINAL OUTPUT WAS PUBLISHED before a completion error.\r\n" +
                                publishedPath + "\r\n\r\n" + failure.Message +
                                ReceiptSuffix(pythonReceipt);
                        else if (failure == null && (!cancelled || committed))
                            statusBox.Text = result;
                        else if (cancelled)
                            statusBox.Text = "Cancelled. No final output was published.\r\n" +
                                (failure == null ? "" : failure.Message) +
                                ReceiptSuffix(pythonReceipt);
                        else
                            statusBox.Text = "FAILED CLOSED. No result is authorized.\r\n\r\n" +
                                failure.Message + ReceiptSuffix(pythonReceipt);
                        if (closeAfterCancellation) Close();
                    });
                }
                catch (InvalidOperationException) { }
            });
        }

        private void SetBusy(bool value)
        {
            statusButton.Enabled = !value;
            buildButton.Enabled = !value;
            aggregateButton.Enabled = !value;
            cancelButton.Enabled = value;
            closeButton.Enabled = !value;
        }

        private string RunStatus()
        {
            string python;
            string r1;
            string h4;
            ReadCommonPaths(out python, out r1, out h4);
            using (HeInputLease inputs = AcquireInputs(python, r1, h4, null))
            {
                HePythonIdentity pythonIdentity = ProbePython(python);
                string stdout = RunPython(
                    python, HeReviewContract.StatusArguments(runtime, r1, h4));
                HeStatusSnapshot status = HeReviewContract.ParseStatus(stdout);
                inputs.VerifyUnchanged();
                return "STATUS VALIDATED — REVIEW/ENGINEERING ONLY\r\n\r\n" +
                    status.Summary + "\r\n\r\nRuntime bundle SHA-256: " +
                    runtime.BundleSha256 + "\r\n\r\n" + pythonIdentity.Receipt;
            }
        }

        private string BuildReviewPackage()
        {
            string python;
            string r1;
            string h4;
            ReadCommonPaths(out python, out r1, out h4);
            string final = HeReviewContract.NormalizeFreshOutput(
                reviewOutputBox.Text, "Blinded-review output");
            HeReviewContract.AssertOutputSeparated(final, runtime, contractRoots, r1, h4);
            string staging = HeReviewContract.NewStagingPath(final);
            try
            {
                using (HeInputLease inputs = AcquireInputs(python, r1, h4, null))
                {
                    HePythonIdentity pythonIdentity = ProbePython(python);
                    HeStatusSnapshot status = HeReviewContract.ParseStatus(RunPython(
                        python, HeReviewContract.StatusArguments(runtime, r1, h4)));
                    ThrowIfCancelled();
                    RunPython(python, HeReviewContract.BuildReviewArguments(
                        runtime, r1, h4, staging));
                    ThrowIfCancelled();
                    using (HePackageLease package =
                               HeReviewContract.ValidateReviewPackage(staging, runtime))
                    {
                        inputs.VerifyUnchanged();
                        package.VerifyUnchanged();
                    }
                    PublishStaging(
                        staging, final, "Blinded-review output", delegate
                        {
                            using (HePackageLease published =
                                       HeReviewContract.ValidateReviewPackage(
                                           final, runtime))
                            {
                                inputs.VerifyUnchanged();
                                published.VerifyUnchanged();
                            }
                        });
                    return "BLINDED DEVELOPMENT REVIEW PACKAGE PUBLISHED\r\n" + final +
                        "\r\n\r\n" + status.Summary +
                        "\r\n\r\nPackage status: " +
                        HeReviewContract.ReviewManifestStatus +
                        "\r\nThis is not an analysis result or biological report.\r\n\r\n" +
                        pythonIdentity.Receipt;
                }
            }
            catch (Exception ex)
            {
                if (Directory.Exists(staging))
                    throw new InvalidOperationException(
                        ex.Message + "\r\nIncomplete, unpublished staging was retained at: " +
                        staging, ex);
                throw;
            }
        }

        private string AggregateReview()
        {
            string python;
            string r1;
            string h4;
            ReadCommonPaths(out python, out r1, out h4);
            string reviewCsv = HeReviewContract.NormalizeReviewCsv(reviewCsvBox.Text);
            string final = HeReviewContract.NormalizeFreshOutput(
                aggregateOutputBox.Text, "Descriptive aggregation output");
            HeReviewContract.AssertOutputSeparated(final, runtime, contractRoots, r1, h4);
            string staging = HeReviewContract.NewStagingPath(final);
            try
            {
                using (HeInputLease inputs = AcquireInputs(
                           python, r1, h4, reviewCsv))
                {
                    HePythonIdentity pythonIdentity = ProbePython(python);
                    HeStatusSnapshot status = HeReviewContract.ParseStatus(RunPython(
                        python, HeReviewContract.StatusArguments(runtime, r1, h4)));
                    ThrowIfCancelled();
                    RunPython(python, HeReviewContract.AggregateReviewArguments(
                        runtime, reviewCsv, staging));
                    ThrowIfCancelled();
                    using (HePackageLease package =
                               HeReviewContract.ValidateAggregatePackage(
                                   staging, runtime, reviewCsv, pythonIdentity))
                    {
                        inputs.VerifyUnchanged();
                        package.VerifyUnchanged();
                    }
                    PublishStaging(
                        staging, final, "Descriptive aggregation output", delegate
                        {
                            using (HePackageLease published =
                                       HeReviewContract.ValidateAggregatePackage(
                                           final, runtime, reviewCsv, pythonIdentity))
                            {
                                inputs.VerifyUnchanged();
                                published.VerifyUnchanged();
                            }
                        });
                    return "ACCEPTED REVIEW AGGREGATED — DESCRIPTIVE ONLY\r\n" + final +
                        "\r\n\r\n" + status.Summary +
                        "\r\n\r\nAudit status: " +
                        HeReviewContract.AggregateAuditStatus +
                        "\r\nTechnical sections are not biological replicates; no scalar " +
                        "ordinal composite or group inference was emitted.\r\n\r\n" +
                        pythonIdentity.Receipt;
                }
            }
            catch (Exception ex)
            {
                if (Directory.Exists(staging))
                    throw new InvalidOperationException(
                        ex.Message + "\r\nIncomplete, unpublished staging was retained at: " +
                        staging, ex);
                throw;
            }
        }

        private void ReadCommonPaths(out string python, out string r1, out string h4)
        {
            python = HeReviewContract.NormalizePython(pythonBox.Text);
            r1 = HeReviewContract.NormalizeExistingDirectory(
                r1Box.Text, "Approved R1 package root");
            h4 = HeReviewContract.NormalizeExistingDirectory(
                h4Box.Text, "H4 development context root");
        }

        private HeInputLease AcquireInputs(
            string python, string r1, string h4, string reviewCsv)
        {
            string sourceRoot = HeReviewContract.NormalizeExistingDirectory(
                contractRoots.SourceRoot, "Declared H&E source root");
            List<string> direct = new List<string>();
            direct.Add(python);
            if (!string.IsNullOrEmpty(reviewCsv)) direct.Add(reviewCsv);
            return HeInputLease.Acquire(
                new string[] { sourceRoot, r1, h4 }, direct.ToArray());
        }

        private HePythonIdentity ProbePython(string python)
        {
            string before = HeReviewContract.Sha256File(python);
            string stdout = RunPython(
                python, HeReviewContract.PythonProbeArguments(), 15000, 65536, true);
            string after = HeReviewContract.Sha256File(python);
            HeReviewContract.Require(before == after,
                    "Python executable changed during its isolated identity probe.");
            HePythonIdentity identity =
                HeReviewContract.ParsePythonProbe(stdout, python, before);
            lock (publicationSync) operationPythonReceipt = identity.Receipt;
            return identity;
        }

        private void PublishStaging(
            string staging, string final, string label,
            Action validatePublished)
        {
            if (validatePublished == null)
                throw new ArgumentNullException("validatePublished");
            lock (publicationSync)
            {
                ThrowIfCancelled();
                HeReviewContract.NormalizeFreshOutput(final, label);
                Directory.Move(staging, final);
                finalOutputMoved = true;
                committedOutputPath = final;
                validatePublished();
                finalOutputCommitted = true;
            }
        }

        private string RunPython(string python, string arguments)
        {
            return RunPython(python, arguments, 0, 8 * 1024 * 1024, false);
        }

        private string RunPython(
            string python, string arguments, int timeoutMilliseconds,
            int maximumOutputCharacters, bool requireEmptyStandardError)
        {
            ThrowIfCancelled();
            ProcessStartInfo info = new ProcessStartInfo();
            info.FileName = python;
            info.Arguments = arguments;
            info.WorkingDirectory = runtime.RuntimeDirectory;
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            info.RedirectStandardOutput = true;
            info.RedirectStandardError = true;
            EnvironmentApply.PrepareNonAnalysis(info);

            Process process = new Process();
            ProcessJob job = null;
            ContainedStageLaunch contained = null;
            try
            {
                contained = ContainedStageLaunch.Prepare(info);
                process.StartInfo = info;
                job = ProcessJob.CreateArmed();
                lock (processSync)
                {
                    activeProcess = process;
                    activeJob = job;
                }
                ThrowIfCancelled();
                process.Start();
                job.AssignOrTerminate(process);
                ThrowIfCancelled();
                contained.Release();
                Task<string> output = Task.Factory.StartNew(
                    delegate { return process.StandardOutput.ReadToEnd(); });
                Task<string> error = Task.Factory.StartNew(
                    delegate { return process.StandardError.ReadToEnd(); });
                bool exited;
                if (timeoutMilliseconds > 0)
                    exited = process.WaitForExit(timeoutMilliseconds);
                else
                {
                    process.WaitForExit();
                    exited = true;
                }
                if (!exited)
                {
                    string timeoutFailure;
                    job.TerminateAndWait(process, 15000, out timeoutFailure);
                    throw new TimeoutException(
                        "H&E contained process exceeded its allowed runtime.");
                }
                Task.WaitAll(new Task[] { output, error }, 15000);
                string containmentFailure;
                if (!job.TerminateAndWait(process, 15000, out containmentFailure))
                    throw new InvalidOperationException(
                        "H&E child-process containment did not reach zero active processes: " +
                        containmentFailure);
                ThrowIfCancelled();
                if (!output.IsCompleted || !error.IsCompleted)
                    throw new IOException("H&E child output did not drain completely.");
                if (output.Result.Length > maximumOutputCharacters ||
                    error.Result.Length > maximumOutputCharacters)
                    throw new IOException(
                        "H&E child output exceeded the bounded capture limit.");
                if (process.ExitCode != 0)
                    throw new InvalidOperationException(
                        "Packaged H&E command exited " +
                        process.ExitCode.ToString(CultureInfo.InvariantCulture) + ".\r\n" +
                        Limit(error.Result, 6000));
                if (requireEmptyStandardError && error.Result.Length != 0)
                    throw new InvalidOperationException(
                        "Python interpreter probe wrote unexpected standard error.");
                return output.Result;
            }
            finally
            {
                lock (processSync)
                {
                    if (object.ReferenceEquals(activeProcess, process)) activeProcess = null;
                    if (object.ReferenceEquals(activeJob, job)) activeJob = null;
                }
                if (contained != null) contained.Dispose();
                if (job != null) job.Dispose();
                process.Dispose();
            }
        }

        private void RequestCancellation(bool prompt)
        {
            if (Interlocked.CompareExchange(ref busy, 0, 0) == 0) return;
            if (prompt && MessageBox.Show(
                    this,
                    "Cancel the active H&E review-only operation? No final output will be " +
                    "published; an incomplete uniquely named staging directory may remain " +
                    "for diagnosis.",
                    "Cancel H&E review operation",
                    MessageBoxButtons.OKCancel,
                    MessageBoxIcon.Question) != DialogResult.OK) return;
            lock (publicationSync)
            {
                if (finalOutputMoved)
                {
                    statusBox.Text =
                        (finalOutputCommitted
                            ? "The final H&E output is already published at:\r\n"
                            : "The final H&E path already exists and is still being " +
                              "validated at:\r\n") +
                        committedOutputPath +
                        "\r\n\r\nCancellation was not applied after the directory move.";
                    return;
                }
                if (Interlocked.Exchange(ref cancellationRequested, 1) != 0) return;
            }
            cancelButton.Enabled = false;
            statusBox.Text = "Cancelling — terminating the contained Python process tree...";
            Process process;
            ProcessJob job;
            lock (processSync)
            {
                process = activeProcess;
                job = activeJob;
            }
            if (job != null)
                ThreadPool.QueueUserWorkItem(delegate
                {
                    string ignored;
                    job.TerminateAndWait(process, 15000, out ignored);
                });
        }

        private void ThrowIfCancelled()
        {
            if (Interlocked.CompareExchange(ref cancellationRequested, 0, 0) != 0)
                throw new OperationCanceledException(
                    "Cancellation was requested; no later H&E command was started.");
        }

        private void OnReviewFormClosing(object sender, FormClosingEventArgs e)
        {
            if (Interlocked.CompareExchange(ref busy, 0, 0) == 0) return;
            e.Cancel = true;
            if (closeAfterCancellation) return;
            if (MessageBox.Show(
                    this,
                    "An H&E review-only operation is active. Cancel its complete process " +
                    "tree and close after termination is confirmed?",
                    "Close H&E review tools",
                    MessageBoxButtons.OKCancel,
                    MessageBoxIcon.Question) != DialogResult.OK) return;
            closeAfterCancellation = true;
            RequestCancellation(false);
        }

        private static string Limit(string value, int maximum)
        {
            string text = value ?? "";
            return text.Length <= maximum ? text : text.Substring(0, maximum) + "...";
        }

        private static string ReceiptSuffix(string receipt)
        {
            return string.IsNullOrEmpty(receipt) ? "" : "\r\n\r\n" + receipt;
        }
    }
}
