// =====================================================================
// LegacyEquivalence.cs -- the route 4 backward-compatibility proof
// ---------------------------------------------------------------------
// THE CLAIM
//   For identical inputs, v1.8.0 route 4 hands the Fiji process the same
//   environment and the same command line as IFQuantLauncher-v1.7.2.exe.
//
// It is compiled TOGETHER WITH the shipping launcher/IFQuantLauncher.Routing.cs,
// so every assertion below is made against the code that will ship, not
// against a copy of it.
//
// FIVE LAYERS, all executed:
//
//   (a) ORACLE. OracleEnvironment() is a verbatim transcription of the real
//       v1.7.2 assignments (launcher/IFQuantLauncher.cs:1394-1424 @ dfa3cfa):
//       same statements, same order, UI reads replaced by parameters and
//       nothing else changed. LegacyProfile.BuildEnvironment must produce a
//       byte-identical canonical serialisation across a parameter matrix that
//       exercises both conditional keys.
//
//   (b) SOURCE DRIFT GUARD. The real v1.7.2 file is re-read from the
//       repository and every `env["IFQ_..."] =` assignment is extracted by
//       regex. The key list must equal LegacyProfile.KeyOrder, in order. The
//       same pass asserts the file really is 1.7.2.0, that both hardcoded
//       invariants are present, and that it never mentions
//       IFQ_MIN_INCLUDED_NUCLEI or writes an IFQ_*_THRESHOLD.
//
//   (c) NEW-SOURCE GUARD. The v1.8.0 IFQuantLauncher.cs is read too, and its
//       route-4 branch is checked for the two things that would silently stop
//       it being legacy: writing the nuclei floor, or building route 4 through
//       the route 1/2 environment builder.
//
//   (d) PROCESS-LEVEL DIFF. This is the layer that makes the claim about
//       reality rather than about dictionaries. For each case the harness
//       poisons its OWN environment with stale IFQ_* values, then starts a
//       real child process twice -- once with the environment v1.7.2 would
//       have applied and once with route 4's -- through the SAME
//       EnvironmentApply.Apply the launcher uses, and diffs what the two
//       children actually report receiving.
//
//   (e) ARTEFACT IDENTITY. The environment is half of a result. The other half
//       is the embedded pipeline and registry, so their SHA-256 are compared
//       against the values v1.7.2 shipped.
//
// WHAT IS STILL NOT PROVEN, stated plainly: that the two produce identical
// run_summary.csv on real images. That needs one paired run of the archived
// v1.7.2 binary and this one over the same folder, and it is not something a
// static harness can do.
// =====================================================================

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using IFQuantLauncher.Routing;

namespace IFQuantLauncher.LegacyCheck
{
    internal static class LegacyEquivalence
    {
        private static int checks;
        private static int failures;
        private static readonly List<string> Differences = new List<string>();

        private static void Check(bool condition, string label)
        {
            checks++;
            Console.WriteLine((condition ? "  PASS  " : "  FAIL  ") + label);
            if (!condition) { failures++; Differences.Add(label); }
        }

        // -------------------------------------------------------------
        // (a) The oracle: v1.7.2's own assignments, transcribed.
        //     DO NOT TIDY THIS METHOD. Its value is that it can be read
        //     side by side with the original.
        // -------------------------------------------------------------
        public static Dictionary<string, string> OracleEnvironment(
            string input, string outputDirectory, string panelKey, string registryPath,
            string autoPanelMapPath, string panelConfig, bool recursive, string includeRegex,
            int maxImages, string segmenter, string projection, int singlePlane,
            bool previewOnly, string tissueMode, string compartmentMode, string wholeCompartment,
            Dictionary<string, string> advanced)
        {
            Dictionary<string, string> env =
                new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            env["IFQ_INPUT_DIR"] = Path.GetFullPath(input);
            env["IFQ_OUTPUT_DIR"] = outputDirectory;
            env["IFQ_PANEL"] = panelKey;
            env["IFQ_MARKER_REGISTRY"] = registryPath;
            if (!string.IsNullOrWhiteSpace(autoPanelMapPath))
                env["IFQ_PANEL_MAP_PATH"] = autoPanelMapPath;
            if (panelConfig != null && panelConfig.Length > 0)
                env["IFQ_PANEL_CONFIG"] = Path.GetFullPath(panelConfig);
            env["IFQ_RECURSIVE"] = recursive ? "true" : "false";
            env["IFQ_INCLUDE_REGEX"] = includeRegex;
            env["IFQ_MAX_IMAGES"] = maxImages.ToString(CultureInfo.InvariantCulture);
            env["IFQ_SEGMENTER"] = segmenter;
            env["IFQ_PROJECTION"] = projection;
            env["IFQ_SINGLE_PLANE"] = singlePlane.ToString(CultureInfo.InvariantCulture);
            env["IFQ_EXPORT_DISPLAY_CHANNELS"] = "true";   // DisplayChannelExportSetting(previewOnly)
            env["IFQ_DISPLAY_PREVIEW_ONLY"] = previewOnly ? "true" : "false";
            env["IFQ_TISSUE_MODE"] = tissueMode;
            env["IFQ_COMPARTMENT_MODE"] = compartmentMode;
            env["IFQ_WHOLE_FIELD_COMPARTMENT"] = wholeCompartment;
            env["IFQ_ALLOW_NONEMPTY_OUTPUT"] = "false";
            env["IFQ_MORPHOLOGY_PRIMARY"] = "true";
            if (advanced != null)
                foreach (KeyValuePair<string, string> item in advanced)
                    env[item.Key] = item.Value;
            return env;
        }

        /// Route 4 as the launcher builds it: LegacyProfile plus the v1.7.2
        /// Advanced overlay, in the same order IFQuantLauncher.cs applies them.
        public static Dictionary<string, string> RouteFourEnvironment(
            string input, string outputDirectory, string panelKey, string registryPath,
            string autoPanelMapPath, string panelConfig, bool recursive, string includeRegex,
            int maxImages, string segmenter, string projection, int singlePlane,
            bool previewOnly, string tissueMode, string compartmentMode, string wholeCompartment,
            Dictionary<string, string> advanced)
        {
            Dictionary<string, string> env = LegacyProfile.BuildEnvironment(
                input, outputDirectory, panelKey, registryPath, autoPanelMapPath,
                panelConfig, recursive, includeRegex, maxImages, segmenter, projection,
                singlePlane, previewOnly, tissueMode, compartmentMode, wholeCompartment);
            if (advanced != null)
                foreach (KeyValuePair<string, string> item in advanced)
                    env[item.Key] = item.Value;
            return env;
        }

        // -------------------------------------------------------------
        // (f) THE ADVANCED-BOX ORACLE.
        //
        // v1.7.2's ParseAdvancedEnvironment (launcher/IFQuantLauncher.cs:
        // 1449-1475 @ dfa3cfa), transcribed as a predicate. Four rules and no
        // fifth: KEY=VALUE, ^IFQ_[A-Z0-9_]+$, not one of ITS nineteen
        // ProtectedEnvironmentKeys, non-empty value. It knew nothing about
        // which markers a panel has, which stage a variable belongs to, or
        // whether the engine reads the name at all.
        //
        // DO NOT TIDY. Its value is that it reads like the original.
        // -------------------------------------------------------------
        public static bool V172AcceptsAdvancedLine(string line)
        {
            string trimmed = (line ?? "").Trim();
            if (trimmed.Length == 0 || trimmed.StartsWith("#", StringComparison.Ordinal))
                return true;                       // skipped, not refused
            int equals = trimmed.IndexOf('=');
            if (equals <= 0) return false;         // "must use KEY=VALUE"
            string key = trimmed.Substring(0, equals).Trim().ToUpperInvariant();
            string value = trimmed.Substring(equals + 1).Trim();
            if (!Regex.IsMatch(key, @"^IFQ_[A-Z0-9_]+$")) return false;
            if (V172ProtectedEnvironmentKeys.Contains(key)) return false;
            if (value.Length == 0) return false;
            return true;
        }

        /// v1.7.2's ProtectedEnvironmentKeys, retyped from the original file so
        /// that this oracle is independent of LegacyProfile. The [b] pass below
        /// proves the same nineteen names against the real source.
        private static readonly HashSet<string> V172ProtectedEnvironmentKeys =
            new HashSet<string>(StringComparer.OrdinalIgnoreCase)
            {
                "IFQ_INPUT_DIR", "IFQ_OUTPUT_DIR", "IFQ_PANEL",
                "IFQ_MARKER_REGISTRY", "IFQ_PANEL_CONFIG", "IFQ_PANEL_MAP_PATH",
                "IFQ_RECURSIVE", "IFQ_INCLUDE_REGEX", "IFQ_MAX_IMAGES",
                "IFQ_SEGMENTER", "IFQ_PROJECTION", "IFQ_SINGLE_PLANE",
                "IFQ_EXPORT_DISPLAY_CHANNELS", "IFQ_DISPLAY_PREVIEW_ONLY",
                "IFQ_TISSUE_MODE", "IFQ_COMPARTMENT_MODE",
                "IFQ_WHOLE_FIELD_COMPARTMENT",
                "IFQ_ALLOW_NONEMPTY_OUTPUT", "IFQ_MORPHOLOGY_PRIMARY"
            };

        /// Every Advanced line worth arguing about, including all four names
        /// v1.8.0 newly took ownership of and every category v1.8.0 added a
        /// judgement for.
        private static string[] AdvancedLines()
        {
            return new string[]
            {
                "IFQ_MIN_INCLUDED_NUCLEI=3",       // v1.8.0-protected; v1.7.2's ONLY floor control
                "IFQ_WSI_PANEL=LEFT",              // v1.8.0-protected, v1.7.2 unaware
                "IFQ_WSI_INPUT=C:\\slides",        // v1.8.0-protected, v1.7.2 unaware
                "IFQ_WSI_OUTPUT=C:\\tiles",        // v1.8.0-protected, v1.7.2 unaware
                "IFQ_TOTALLY_UNKNOWN_KEY=1",       // v1.8.0 ADV_UNKNOWN_KEY
                "IFQ_KRT_5_THRESHOLD=400",         // the typo v1.8.0 added a rule for
                "IFQ_PDPN_THRESHOLD=400",          // v1.8.0 ADV_UNKNOWN_MARKER on LEFT
                "IFQ_WSI_HALO_PX=64",              // v1.8.0 ADV_STAGE1_ON_FIJI_ROUTE
                "IFQ_RING_EXPAND_UM=3.5",          // ordinary stage 2 setting
                "IFQ_KRT5_THRESHOLD=480",          // freezes a LEFT channel
                "IFQ_KRT5_THRESHOLD=high",         // v1.8.0 H2_THRESHOLD_INVALID; the engine
                                                   // still refuses it, so no number escapes
                "IFQ_AGER_THRESHOLD=0",            // v1.8.0 H2_THRESHOLD_INVALID (zero cutoff)
                "IFQ_DAPI_METHOD=global_otsu",     // ordinary stage 2 setting
                "IFQ_PANEL=T",                     // v1.7.2 protected
                "IFQ_OUTPUT_DIR=C:\\elsewhere",    // v1.7.2 protected
                "IFQ_ALLOW_NONEMPTY_OUTPUT=true",  // v1.7.2 protected
                "IFQ_MORPHOLOGY_PRIMARY=false",    // v1.7.2 protected
                "IFQ_RING_EXPAND_UM=",             // v1.7.2: empty value
                "NOT_AN_IFQ_KEY=1",                // v1.7.2: invalid IFQ key
                "no equals sign at all",           // v1.7.2: must use KEY=VALUE
                "ifq_ring_expand_um=4.0",          // v1.7.2 upper-cases the key
                "# a comment",                     // skipped by both
                ""                                 // skipped by both
            };
        }

        public static List<string> ExtractEnvKeys(string sourceText)
        {
            List<string> keys = new List<string>();
            foreach (Match m in Regex.Matches(sourceText, "env\\[\"(?<k>IFQ_[A-Z0-9_]+)\"\\]\\s*="))
            {
                string key = m.Groups["k"].Value;
                if (!keys.Contains(key)) keys.Add(key);
            }
            return keys;
        }

        private static string Sha256(string path)
        {
            using (SHA256 algorithm = SHA256.Create())
            using (FileStream stream = File.OpenRead(path))
            {
                byte[] hash = algorithm.ComputeHash(stream);
                StringBuilder text = new StringBuilder(hash.Length * 2);
                foreach (byte b in hash) text.Append(b.ToString("x2", CultureInfo.InvariantCulture));
                return text.ToString();
            }
        }

        // -------------------------------------------------------------
        // (d) Process-level: what the child actually receives.
        // -------------------------------------------------------------
        /// <summary>
        /// Starts the probe with an environment, through the launcher's own
        /// application path.
        ///
        /// v1.8.0 added a launch choke point: EnvironmentApply.Apply takes a
        /// RunSeal, and a RunSeal is only issued after the merged environment
        /// has been re-derived and compared with the run record. This harness
        /// deliberately goes through it rather than around it -- that is the
        /// whole reason it links the shipping source instead of a copy -- so
        /// route 4 is proved to still reach a real child process with the same
        /// twenty variables v1.7.2 sent, choke point and all.
        ///
        /// The seal's inputs are built from what the CASE says (the panel key
        /// and the Advanced lines), never from the environment being applied,
        /// so the comparison is not circular.
        /// </summary>
        private static string RunProbe(
            string probeExe, string arguments, Dictionary<string, string> env,
            string panelKey, string panelConfig, Dictionary<string, string> advanced,
            Dictionary<string, PanelDef> enginePanels, HashSet<string> engineThresholds)
        {
            ProcessStartInfo psi = new ProcessStartInfo();
            psi.FileName = probeExe;
            psi.Arguments = arguments;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;
            // The launcher's own code, not a copy of it.
            EnvironmentApply.Apply(
                psi,
                LegacySeal(env, panelKey, panelConfig, advanced, enginePanels, engineThresholds));

            using (Process process = Process.Start(psi))
            {
                string output = process.StandardOutput.ReadToEnd();
                process.StandardError.ReadToEnd();
                process.WaitForExit();
                return output;
            }
        }

        /// <summary>
        /// A route-4 seal for a probe environment, assembled the way the
        /// launcher assembles one: a route-4 request carrying the case's
        /// Advanced lines, the gate's verdict on that request, and the panel the
        /// case names. Nothing here is read back out of the environment being
        /// sealed.
        /// </summary>
        private static RunSeal LegacySeal(
            Dictionary<string, string> env, string panelKey, string panelConfig,
            Dictionary<string, string> advanced,
            Dictionary<string, PanelDef> enginePanels, HashSet<string> engineThresholds)
        {
            StringBuilder advancedText = new StringBuilder();
            List<string> advancedKeys = new List<string>();
            if (advanced != null)
                foreach (KeyValuePair<string, string> item in advanced)
                {
                    advancedText.AppendLine(item.Key + "=" + item.Value);
                    advancedKeys.Add(item.Key);
                }

            RunRequest request = new RunRequest();
            request.Route = ImageRoute.LegacyFiji172;
            request.Tier = RunTier.Exploratory;
            request.PanelKey = panelKey;
            request.PanelConfigJson = panelConfig;
            request.OutputBase = OutputDir;
            request.AdvancedText = advancedText.ToString();

            PanelDef panel = null;
            if (enginePanels != null && !string.IsNullOrEmpty(panelKey))
                enginePanels.TryGetValue(panelKey, out panel);

            GateResult gate =
                FailClosedGate.Evaluate(request, panel, engineThresholds, null);

            SealInput input = new SealInput();
            input.Stage = LaunchStage.Stage2Fiji;
            input.Request = request;
            input.Panel = panel;
            input.EngineThresholdMarkers = engineThresholds;
            input.Gate = gate;
            input.Environment = env;
            input.OutputDirectory = null;    // the fixture path is not on disk
            input.AdvancedKeys = advancedKeys;
            return RunSeal.Issue(input);
        }

        private sealed class Case
        {
            public string Name;
            public string PanelMap;
            public string PanelConfig;
            public bool Recursive;
            public string Regex;
            public int MaxImages;
            public string Segmenter;
            public string Projection;
            public int Plane;
            public bool Preview;
            public string Tissue;
            public string Compartment;
            public string Whole;
            public string Panel;
            public Dictionary<string, string> Advanced;
        }

        private static Case[] Cases()
        {
            List<Case> cases = new List<Case>();
            Case a = new Case();
            a.Name = "1 recommended defaults, panel LEFT";
            a.Recursive = true; a.Regex = ".*"; a.MaxImages = 0; a.Segmenter = "classic";
            a.Projection = "layer_aware"; a.Plane = -1; a.Preview = false;
            a.Tissue = "auto"; a.Compartment = "required"; a.Whole = "unassigned";
            a.Panel = "LEFT";
            cases.Add(a);

            Case b = new Case();
            b.Name = "2 every non-default, preview mode, panel E";
            b.Recursive = false; b.Regex = "^.*A.*$"; b.MaxImages = 1; b.Segmenter = "stardist";
            b.Projection = "single"; b.Plane = 7; b.Preview = true;
            b.Tissue = "whole_field"; b.Compartment = "optional"; b.Whole = "airway";
            b.Panel = "E";
            cases.Add(b);

            Case c = new Case();
            c.Name = "3 AUTO panel map present (conditional key 1)";
            c.PanelMap = @"C:\fixture\runtime\auto_panel_maps\panel_map_deadbeef.csv";
            c.Recursive = true; c.Regex = ".*"; c.MaxImages = 0; c.Segmenter = "classic";
            c.Projection = "max"; c.Plane = -1; c.Preview = false;
            c.Tissue = "auto"; c.Compartment = "required"; c.Whole = "alveolar";
            c.Panel = "RIGHT";
            cases.Add(c);

            Case d = new Case();
            d.Name = "4 custom panel JSON present (conditional key 2)";
            d.PanelConfig = @"C:\fixture\custom_panel.json";
            d.Recursive = true; d.Regex = ".*"; d.MaxImages = 0; d.Segmenter = "classic";
            d.Projection = "avg"; d.Plane = -1; d.Preview = false;
            d.Tissue = "auto"; d.Compartment = "required"; d.Whole = "unassigned";
            d.Panel = "MYCUSTOM";
            cases.Add(d);

            Case e = new Case();
            e.Name = "5 BOTH conditional keys present at once";
            e.PanelMap = @"C:\fixture\runtime\auto_panel_maps\panel_map_cafe.csv";
            e.PanelConfig = @"C:\fixture\custom_panel.json";
            e.Recursive = false; e.Regex = "(?i)^.*\\.oir$"; e.MaxImages = 25;
            e.Segmenter = "classic"; e.Projection = "layer_aware"; e.Plane = -1;
            e.Preview = false; e.Tissue = "auto"; e.Compartment = "required";
            e.Whole = "fibrotic"; e.Panel = "ALI2";
            cases.Add(e);

            Case f = new Case();
            f.Name = "6 Advanced overlay, including one key that shadows a base key";
            f.Recursive = true; f.Regex = ".*"; f.MaxImages = 0; f.Segmenter = "classic";
            f.Projection = "layer_aware"; f.Plane = -1; f.Preview = false;
            f.Tissue = "auto"; f.Compartment = "required"; f.Whole = "unassigned";
            f.Panel = "LEFT";
            f.Advanced = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            f.Advanced["IFQ_RING_EXPAND_UM"] = "3.5";
            f.Advanced["IFQ_DAPI_METHOD"] = "global_otsu";
            f.Advanced["IFQ_KRT5_THRESHOLD"] = "480";
            cases.Add(f);

            Case g = new Case();
            g.Name = "7 values containing spaces, quotes and non-ASCII";
            g.Recursive = true; g.Regex = "^(?:.* .*)$"; g.MaxImages = 0;
            g.Segmenter = "classic"; g.Projection = "layer_aware"; g.Plane = -1;
            g.Preview = false; g.Tissue = "auto"; g.Compartment = "required";
            g.Whole = "unassigned"; g.Panel = "P";
            cases.Add(g);

            return cases.ToArray();
        }

        private const string InputDir = @"C:\fixture\Confocal Images\mouse 12";
        private const string OutputDir = @"C:\fixture\output\IFQ_run_20260807_000000";
        private const string RegistryPath = @"C:\fixture\runtime\config\lung_marker_registry.json";
        private const string ScriptPath = @"C:\fixture\runtime\IF_Quant_Pipeline.groovy";

        public static int Run(string v172Source, string v180Source, string probeExe,
                              string embeddedPipeline, string embeddedRegistry)
        {
            return Run(v172Source, v180Source, probeExe, embeddedPipeline, embeddedRegistry, null);
        }

        public static int Run(string v172Source, string v180Source, string probeExe,
                              string embeddedPipeline, string embeddedRegistry,
                              string routingSource)
        {
            Console.WriteLine("Route 4 legacy (v1.7.2) equivalence -- executed, not asserted");
            Console.WriteLine("  v1.7.2 source : launcher/reference/IFQuantLauncher-v1.7.2.cs");
            Console.WriteLine("  current source: launcher/IFQuantLauncher.cs");
            Console.WriteLine("  env probe     : <temporary-build>/EnvProbe.exe");
            Console.WriteLine();

            // The panel table and the engine's threshold whitelist, needed by
            // the launch seal that every probe now goes through. Parsed once,
            // from the embedded pipeline, exactly as the launcher does it.
            Dictionary<string, PanelDef> enginePanels =
                new Dictionary<string, PanelDef>(StringComparer.OrdinalIgnoreCase);
            HashSet<string> engineThresholds = new HashSet<string>(StringComparer.Ordinal);
            if (File.Exists(embeddedPipeline))
            {
                string pipelineText = File.ReadAllText(embeddedPipeline, Encoding.UTF8);
                enginePanels = PanelRegistry.ParseFromPipeline(pipelineText);
                engineThresholds = PanelRegistry.ParseThresholdMarkerTokens(pipelineText);
            }

            Case[] cases = Cases();

            // ---- (a) oracle vs route 4, canonical diff -------------------
            Console.WriteLine("[a] Oracle transcription of v1.7.2 vs route 4, canonical diff");
            foreach (Case c in cases)
            {
                Dictionary<string, string> oracle = OracleEnvironment(
                    InputDir, OutputDir, c.Panel, RegistryPath, c.PanelMap, c.PanelConfig,
                    c.Recursive, c.Regex, c.MaxImages, c.Segmenter, c.Projection, c.Plane,
                    c.Preview, c.Tissue, c.Compartment, c.Whole, c.Advanced);
                Dictionary<string, string> subject = RouteFourEnvironment(
                    InputDir, OutputDir, c.Panel, RegistryPath, c.PanelMap, c.PanelConfig,
                    c.Recursive, c.Regex, c.MaxImages, c.Segmenter, c.Projection, c.Plane,
                    c.Preview, c.Tissue, c.Compartment, c.Whole, c.Advanced);

                string left = LegacyProfile.Canonicalize(oracle);
                string right = LegacyProfile.Canonicalize(subject);
                bool same = string.Equals(left, right, StringComparison.Ordinal);
                Check(same, "case " + c.Name + ": " + oracle.Count + " keys, byte-identical");
                if (!same) PrintDiff(left, right);
            }
            Console.WriteLine("      fixture fingerprint: " +
                              LegacyProfile.Fingerprint(LegacyProfile.Fixture()));

            // ---- (b) the real v1.7.2 source ------------------------------
            Console.WriteLine();
            Console.WriteLine("[b] Source drift guard against the real v1.7.2 file");
            if (!File.Exists(v172Source))
            {
                Check(false, "v1.7.2 source is readable at " + v172Source);
            }
            else
            {
                string source = File.ReadAllText(v172Source, Encoding.UTF8);
                Match version = Regex.Match(source, "AssemblyFileVersion\\(\"(?<v>[0-9.]+)\"\\)");
                Check(version.Success && version.Groups["v"].Value == LegacyProfile.FrozenVersion,
                      "the reference source really is " + LegacyProfile.FrozenVersion +
                      " (found " + (version.Success ? version.Groups["v"].Value : "nothing") + ")");

                List<string> found = ExtractEnvKeys(source);
                Console.WriteLine("      env[] assignments found: " + found.Count);
                List<string> expected = new List<string>(LegacyProfile.KeyOrder);
                List<string> missing = new List<string>();
                List<string> extra = new List<string>();
                foreach (string key in expected) if (!found.Contains(key)) missing.Add(key);
                foreach (string key in found) if (!expected.Contains(key)) extra.Add(key);
                Check(missing.Count == 0,
                      "every key LegacyProfile claims is really assigned by v1.7.2" +
                      (missing.Count > 0 ? " (missing " + string.Join(", ", missing.ToArray()) + ")" : ""));
                Check(extra.Count == 0,
                      "v1.7.2 assigns no key LegacyProfile is unaware of" +
                      (extra.Count > 0 ? " (extra " + string.Join(", ", extra.ToArray()) + ")" : ""));

                bool order = found.Count == expected.Count;
                for (int i = 0; order && i < found.Count; i++)
                    order = string.Equals(found[i], expected[i], StringComparison.Ordinal);
                Check(order, "assignment ORDER matches LegacyProfile.KeyOrder");

                Check(source.IndexOf("env[\"IFQ_ALLOW_NONEMPTY_OUTPUT\"] = \"false\"",
                                     StringComparison.Ordinal) >= 0,
                      "v1.7.2 hardcodes IFQ_ALLOW_NONEMPTY_OUTPUT=false");
                Check(source.IndexOf("env[\"IFQ_MORPHOLOGY_PRIMARY\"] = \"true\"",
                                     StringComparison.Ordinal) >= 0,
                      "v1.7.2 hardcodes IFQ_MORPHOLOGY_PRIMARY=true");
                Check(source.IndexOf("IFQ_MIN_INCLUDED_NUCLEI", StringComparison.Ordinal) < 0,
                      "v1.7.2 never mentions IFQ_MIN_INCLUDED_NUCLEI");
                Check(!Regex.IsMatch(source, "env\\[\"IFQ_[A-Z0-9_]+_THRESHOLD\"\\]"),
                      "v1.7.2 never writes an IFQ_*_THRESHOLD");
                Check(source.IndexOf(
                          "\"--headless --console --run \" + QuoteArgument(config.ScriptPath)",
                          StringComparison.Ordinal) >= 0,
                      "v1.7.2's Fiji command line is the one LegacyProfile.CommandLine reproduces");
                Check(!Regex.IsMatch(source, "qupath|\\.vsi|IFQ_WSI_|brightfield",
                                     RegexOptions.IgnoreCase),
                      "v1.7.2 contains no QuPath / .vsi / IFQ_WSI_ / brightfield reference");
            }

            // ---- (c) the new source's route 4 branch ---------------------
            Console.WriteLine();
            Console.WriteLine("[c] The current post-v1.7.2 source cannot quietly stop being legacy");
            if (!File.Exists(v180Source))
            {
                Check(false, "current source is readable at " + v180Source);
            }
            else
            {
                string source = File.ReadAllText(v180Source, Encoding.UTF8);
                Match version = Regex.Match(source, "AssemblyFileVersion\\(\"(?<v>[0-9.]+)\"\\)");
                Check(version.Success && version.Groups["v"].Value == LauncherBuild.AssemblyVersion,
                      "the current source declares AssemblyFileVersion " + LauncherBuild.AssemblyVersion);
                Check(source.IndexOf("LegacyProfile.BuildEnvironment", StringComparison.Ordinal) >= 0,
                      "route 4 is built by LegacyProfile.BuildEnvironment");
                Check(source.IndexOf("LegacyProfile.CommandLine", StringComparison.Ordinal) >= 0,
                      "route 4 uses LegacyProfile.CommandLine");
                // ASSIGNMENT, not mention: the self-test in this same file
                // legitimately READS env["IFQ_MIN_INCLUDED_NUCLEI"] to prove
                // routes 1/2 do write it. `[^=]` keeps `!=` and `==` out.
                Check(!Regex.IsMatch(source, "env\\[\"IFQ_MIN_INCLUDED_NUCLEI\"\\]\\s*=[^=]"),
                      "the current launcher assigns IFQ_MIN_INCLUDED_NUCLEI nowhere " +
                      "(only RunEnvironment.BuildStage2 does, and route 4 does not go through it)");
                Check(source.IndexOf("EnvironmentApply.Apply", StringComparison.Ordinal) >= 0,
                      "the launcher applies the environment through the code this harness runs");
                // v1.8.0: ...and it applies a SEAL, not a dictionary. Route 4
                // goes through the same choke point as every other route, so a
                // legacy run whose record and environment disagree cannot start
                // either.
                Check(Regex.IsMatch(
                          source, @"EnvironmentApply\.Apply\s*\(\s*psi\s*,\s*config\.Stage2Seal\s*\)"),
                      "the launcher applies a RunSeal, not a raw environment dictionary");
                Check(!Regex.IsMatch(
                          source, @"EnvironmentApply\.Apply\s*\(\s*psi\s*,\s*config\.Environment\s*\)"),
                      "...and the pre-choke-point call that took the dictionary is gone");
                Check(Regex.IsMatch(source, @"config\.Stage2Seal\s*=\s*RunSeal\.Issue\("),
                      "every route, route 4 included, is sealed before anything can start");

                // Route 4 must be rejected by the routes 1/2 builder.
                RunRequest legacyRequest = new RunRequest();
                legacyRequest.Route = ImageRoute.LegacyFiji172;
                legacyRequest.PanelKey = "LEFT";
                bool refused = false;
                try
                {
                    RunEnvironment.BuildStage2(
                        legacyRequest, null, null, "r", "engine.groovy",
                        "o", Path.GetTempPath(), null, false);
                }
                catch (InvalidOperationException) { refused = true; }
                Check(refused, "RunEnvironment.BuildStage2 refuses route 4 outright");

                // And the pre-start assertion refuses a legacy environment that
                // has picked up the nuclei floor from anywhere.
                Dictionary<string, string> poisoned = LegacyProfile.Fixture();
                poisoned["IFQ_MIN_INCLUDED_NUCLEI"] = "0";
                bool assertionFired = false;
                try
                {
                    PreStartAssertions.AssertStage2Environment(
                        poisoned, null, ImageRoute.LegacyFiji172);
                }
                catch (InvalidOperationException) { assertionFired = true; }
                Check(assertionFired,
                      "PreStartAssertions refuses a legacy environment carrying the nuclei floor");

                // ...but it must NOT refuse one the operator typed into the
                // Advanced box, because v1.7.2's Advanced box was the only way
                // to set the nuclei floor at all.
                bool typedFloorAllowed = true;
                try
                {
                    PreStartAssertions.AssertStage2Environment(
                        poisoned, null, ImageRoute.LegacyFiji172,
                        new string[] { "IFQ_MIN_INCLUDED_NUCLEI" });
                }
                catch (InvalidOperationException) { typedFloorAllowed = false; }
                Check(typedFloorAllowed,
                      "PreStartAssertions allows a nuclei floor that came from the Advanced box");

                // D4. Keep the route gate runtime-addressable. The launcher
                // must not fail its own self-test merely because the flag is
                // toggled; the environment builders remain the fail-closed
                // boundary until a brightfield engine is wired.
                Check(!Regex.IsMatch(
                          source,
                          @"if\s*\(\s*LauncherBuild\.BrightfieldRouteEnabled\s*\)\s*return"),
                      "no check in the launcher fails merely because the brightfield flag is on");
            }

            if (!string.IsNullOrEmpty(routingSource) && File.Exists(routingSource))
            {
                string routing = File.ReadAllText(routingSource, Encoding.UTF8);
                // A `const bool` is folded at compile time, so every branch
                // guarded on it becomes unreachable code. Keeping this as a
                // field allows self-test coverage without claiming the route
                // is complete or safe to enable by itself.
                Check(Regex.IsMatch(
                          routing,
                          @"static\s+readonly\s+bool\s+BrightfieldRouteEnabled\s*="),
                      "BrightfieldRouteEnabled is a static readonly field, not a const");
                Check(!Regex.IsMatch(
                          routing, @"const\s+bool\s+BrightfieldRouteEnabled"),
                      "...and is not declared const anywhere");
                Check(routing.IndexOf(
                          "Changing BrightfieldRouteEnabled alone remains insufficient.",
                          StringComparison.Ordinal) >= 0,
                      "the user-visible reason states that the brightfield flag alone is insufficient");
                Check(routing.IndexOf(
                          "Re-enabling it is one line",
                          StringComparison.Ordinal) < 0,
                      "the user-visible reason no longer claims brightfield is a one-line enable");
            }

            // ---- (d) process-level diff ----------------------------------
            Console.WriteLine();
            Console.WriteLine("[d] Process-level diff: what the child process actually receives");
            // Poison the parent environment. Both sides must strip these; if
            // only one did, the diff would catch it, and if neither did, the
            // stale-inheritance check below would.
            Environment.SetEnvironmentVariable("IFQ_PANEL", "T");
            Environment.SetEnvironmentVariable("IFQ_MIN_INCLUDED_NUCLEI", "9");
            Environment.SetEnvironmentVariable("IFQ_KRT5_THRESHOLD", "1");
            Environment.SetEnvironmentVariable("IFQ_STALE_LEFTOVER", "yes");

            if (!File.Exists(probeExe))
            {
                Check(false, "environment probe exists at " + probeExe);
            }
            else
            {
                string commandLine = LegacyProfile.CommandLine(ScriptPath);
                foreach (Case c in cases)
                {
                    Dictionary<string, string> oracle = OracleEnvironment(
                        InputDir, OutputDir, c.Panel, RegistryPath, c.PanelMap, c.PanelConfig,
                        c.Recursive, c.Regex, c.MaxImages, c.Segmenter, c.Projection, c.Plane,
                        c.Preview, c.Tissue, c.Compartment, c.Whole, c.Advanced);
                    Dictionary<string, string> subject = RouteFourEnvironment(
                        InputDir, OutputDir, c.Panel, RegistryPath, c.PanelMap, c.PanelConfig,
                        c.Recursive, c.Regex, c.MaxImages, c.Segmenter, c.Projection, c.Plane,
                        c.Preview, c.Tissue, c.Compartment, c.Whole, c.Advanced);

                    string left = RunProbe(
                        probeExe, commandLine, oracle, c.Panel, c.PanelConfig, c.Advanced,
                        enginePanels, engineThresholds);
                    string right = RunProbe(
                        probeExe, commandLine, subject, c.Panel, c.PanelConfig, c.Advanced,
                        enginePanels, engineThresholds);
                    bool same = string.Equals(left, right, StringComparison.Ordinal);
                    Check(same, "case " + c.Name + ": child process environment identical");
                    if (!same) PrintDiff(left, right);

                    Check(left.IndexOf("IFQ_STALE_LEFTOVER", StringComparison.Ordinal) < 0 &&
                          right.IndexOf("IFQ_STALE_LEFTOVER", StringComparison.Ordinal) < 0,
                          "case " + c.Name + ": inherited IFQ_* were stripped from the child");
                    Check(left.IndexOf("\nIFQ_MIN_INCLUDED_NUCLEI=", StringComparison.Ordinal) < 0 &&
                          right.IndexOf("\nIFQ_MIN_INCLUDED_NUCLEI=", StringComparison.Ordinal) < 0,
                          "case " + c.Name + ": the child received no IFQ_MIN_INCLUDED_NUCLEI");
                }
            }

            // ---- command line --------------------------------------------
            Console.WriteLine();
            Console.WriteLine("[d2] Command line");
            string expectedCommandLine =
                "--headless --console --run \"" + ScriptPath + "\"";
            Check(LegacyProfile.CommandLine(ScriptPath) == expectedCommandLine,
                  "route 4 command line == v1.7.2's: " + expectedCommandLine);
            Check(FijiCommand.LauncherExeArguments(ScriptPath) == expectedCommandLine,
                  "the shared builder produces the same string");

            // ---- (f) the Advanced box on route 4 -------------------------
            //
            // v1.8.0 ran the whole fail-closed gate on every route including 4,
            // so route 4 blocked IFQ_MIN_INCLUDED_NUCLEI as ADV_PROTECTED --
            // and under v1.7.2 the Advanced box was the ONLY way to set the
            // nuclei floor -- and blocked any name outside the EnvSurface
            // tables as ADV_UNKNOWN_KEY. Verified before the fix: route 4 plus
            // Advanced IFQ_MIN_INCLUDED_NUCLEI=3 gave runButton.Enabled=False.
            // v1.7.2 emitted 20 variables for that input; v1.8.0 emitted none.
            //
            // The oracle above decides, line by line, what v1.7.2 accepted.
            // Route 4's gate must agree with it exactly -- and where the line
            // is accepted, the variable must actually reach a real child
            // process, which is checked below rather than assumed.
            Console.WriteLine();
            Console.WriteLine("[f] Route 4 Advanced box == v1.7.2's Advanced box");
            {
                // Parsed once at the top of Run, because the launch seal every
                // probe goes through needs the same tables.
                PanelDef left = enginePanels["LEFT"];

                int agreed = 0;
                foreach (string line in AdvancedLines())
                {
                    RunRequest request = new RunRequest();
                    request.Route = ImageRoute.LegacyFiji172;
                    request.PanelKey = "LEFT";
                    request.OutputBase = OutputDir;
                    request.Tier = RunTier.Exploratory;
                    request.AdvancedText = line;

                    bool v172 = V172AcceptsAdvancedLine(line);
                    bool v180 = !FailClosedGate.Evaluate(
                        request, left, engineThresholds, null).Blocked;
                    Check(v172 == v180,
                          "advanced line " + Display(line) + ": v1.7.2 " +
                          (v172 ? "accepted" : "refused") + ", route 4 " +
                          (v180 ? "accepts" : "refuses"));
                    if (v172 == v180) agreed++;
                }
                Console.WriteLine("      " + agreed + "/" + AdvancedLines().Length +
                                  " lines decided identically");

                // The floor really reaches the process, exactly as it did under
                // v1.7.2: same 19 base keys, plus the one the operator typed.
                if (File.Exists(probeExe))
                {
                    Dictionary<string, string> advanced =
                        new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
                    advanced["IFQ_MIN_INCLUDED_NUCLEI"] = "3";
                    // Both conditional keys present, so this is v1.7.2's widest
                    // environment: its nineteen, plus the one typed line.
                    const string PanelMap = @"C:\fixture\runtime\auto_panel_maps\map.csv";
                    const string PanelConfig = @"C:\fixture\custom_panel.json";
                    Dictionary<string, string> oracle = OracleEnvironment(
                        InputDir, OutputDir, "LEFT", RegistryPath, PanelMap, PanelConfig,
                        true, ".*", 0, "classic", "layer_aware", -1, false,
                        "auto", "required", "unassigned", advanced);
                    Dictionary<string, string> subject = RouteFourEnvironment(
                        InputDir, OutputDir, "LEFT", RegistryPath, PanelMap, PanelConfig,
                        true, ".*", 0, "classic", "layer_aware", -1, false,
                        "auto", "required", "unassigned", advanced);
                    Check(oracle.Count == 20,
                          "v1.7.2 emits 20 variables for Advanced IFQ_MIN_INCLUDED_NUCLEI=3 " +
                          "(got " + oracle.Count + ")");
                    string commandLine2 = LegacyProfile.CommandLine(ScriptPath);
                    string leftText = RunProbe(
                        probeExe, commandLine2, oracle, "LEFT", PanelConfig, advanced,
                        enginePanels, engineThresholds);
                    string rightText = RunProbe(
                        probeExe, commandLine2, subject, "LEFT", PanelConfig, advanced,
                        enginePanels, engineThresholds);
                    bool identical = string.Equals(leftText, rightText, StringComparison.Ordinal);
                    Check(identical,
                          "the child process receives the same 20 variables from route 4");
                    if (!identical) PrintDiff(leftText, rightText);
                    Check(rightText.IndexOf("IFQ_MIN_INCLUDED_NUCLEI=3",
                                            StringComparison.Ordinal) >= 0,
                          "and the nuclei floor the operator typed is one of them");
                }
            }

            // ---- (e) embedded artefacts ----------------------------------
            Console.WriteLine();
            Console.WriteLine("[e] Embedded analysis artefacts");
            if (File.Exists(embeddedPipeline) && File.Exists(embeddedRegistry))
            {
                string pipelineHash = Sha256(embeddedPipeline);
                string registryHash = Sha256(embeddedRegistry);
                Console.WriteLine("      pipeline " + pipelineHash);
                Console.WriteLine("      registry " + registryHash);
                // TEST THE MECHANISM, NOT A TRANSIENT STATE.
                //
                // This used to assert note == null: that the embedded engine is
                // byte-identical to the one v1.7.2 shipped. That assertion fails the
                // moment the engine is legitimately corrected -- and it did. Fixing a
                // missing `black` token in an ImageJ Binary Options macro string
                // (IF_Quant_Pipeline.groovy:1766, which was globally flipping
                // Prefs.blackBackground and erasing ~89x of the nuclei) moved the
                // pipeline hash from defffe67... to b45e4289.... The old check forced a
                // choice between shipping a known-buggy engine and a red suite, and that
                // is how suites come to be ignored.
                //
                // LegacyProfile.CheckEmbeddedArtefacts deliberately does NOT refuse on
                // drift -- it returns a note so the launcher downgrades legacy mode
                // honestly. So the thing to verify is that drift is DETECTED and NAMED.
                // That holds across every legitimate engine change, and still fails
                // loudly if the drift detector itself breaks.
                string note = LegacyProfile.CheckEmbeddedArtefacts(pipelineHash, registryHash);
                bool pipelineMatches = string.Equals(pipelineHash,
                    LegacyProfile.V172PipelineSha256, StringComparison.OrdinalIgnoreCase);
                bool registryMatches = string.Equals(registryHash,
                    LegacyProfile.V172RegistrySha256, StringComparison.OrdinalIgnoreCase);

                if (pipelineMatches && registryMatches)
                {
                    Check(note == null,
                          "artefacts equal v1.7.2's, and no drift is reported" +
                          (note == null ? "" : "\n" + note));
                }
                else
                {
                    Check(note != null,
                          "embedded artefacts DIFFER from v1.7.2's and the drift is DETECTED");
                    Check(note == null || pipelineMatches || note.Contains(pipelineHash),
                          "the drift note names the actual embedded pipeline hash");
                    Check(note != null && note.Contains(LegacyProfile.V172ExeArchivePath),
                          "the drift note points at the archived v1.7.2 binary for exact numbers");
                    Console.WriteLine("      NOTE: embedded artefacts intentionally differ from v1.7.2's.");
                    Console.WriteLine("      Route 4 reproduces the v1.7.2 ENVIRONMENT and COMMAND LINE");
                    Console.WriteLine("      exactly; the engine carries subsequent fixes.");
                }
            }
            else
            {
                Check(false, "embedded artefacts are readable");
            }

            Console.WriteLine();
            Console.WriteLine("checks: " + checks + "   failures: " + failures);
            if (failures > 0)
            {
                Console.WriteLine();
                Console.WriteLine("DIFFERENCES FOUND:");
                foreach (string d in Differences) Console.WriteLine("  * " + d);
            }
            return failures == 0 ? 0 : 41;
        }

        private static string Display(string line)
        {
            if (line == null) return "<null>";
            if (line.Length == 0) return "<empty>";
            return "'" + line + "'";
        }

        private static void PrintDiff(string left, string right)
        {
            string[] a = left.Replace("\r\n", "\n").Split('\n');
            string[] b = right.Replace("\r\n", "\n").Split('\n');
            int n = Math.Max(a.Length, b.Length);
            for (int i = 0; i < n; i++)
            {
                string x = i < a.Length ? a[i] : "<absent>";
                string y = i < b.Length ? b[i] : "<absent>";
                if (!string.Equals(x, y, StringComparison.Ordinal))
                {
                    Console.WriteLine("        v1.7.2 : " + x);
                    Console.WriteLine("        route4 : " + y);
                }
            }
        }
    }

    internal static class LegacyEquivalenceProgram
    {
        /// Walk up from the executable and from the working directory looking for the
        /// engine file that marks the repository root. Returns "." if not found, so the
        /// caller still fails with a readable "source is readable at ..." message rather
        /// than a null-reference.
        private static string FindRepoRoot()
        {
            string[] starts =
            {
                AppDomain.CurrentDomain.BaseDirectory,
                Environment.CurrentDirectory
            };
            foreach (string start in starts)
            {
                if (string.IsNullOrEmpty(start)) continue;
                DirectoryInfo d = new DirectoryInfo(start);
                for (int hop = 0; d != null && hop < 8; hop++, d = d.Parent)
                {
                    if (File.Exists(Path.Combine(d.FullName, "IF_Quant_Pipeline.groovy")))
                        return d.FullName;
                }
            }
            return ".";
        }

        private static int Main(string[] args)
        {
            try
            {
                // ZERO-ARGUMENT DEFAULTS, resolved from the repository.
                //
                // This harness previously required six positional paths, one of which
                // (the v1.7.2 reference) had to be extracted from git history by hand
                // because commit f7dbb02 overwrote launcher/IFQuantLauncher.cs with
                // v1.8.0. A reader cloning this repository therefore could not reproduce
                // the equivalence claim at all -- and an unreproducible claim in a
                // repository is worth less than no claim.
                //
                // The v1.7.2 source is now committed at launcher/reference/, so running
                // this with no arguments works from anywhere inside the checkout.
                string root = FindRepoRoot();
                string v172 = args.Length > 0 ? args[0]
                    : Path.Combine(root, "launcher", "reference", "IFQuantLauncher-v1.7.2.cs");
                string v180 = args.Length > 1 ? args[1]
                    : Path.Combine(root, "launcher", "IFQuantLauncher.cs");
                string probe = args.Length > 2 ? args[2] : "";
                string pipeline = args.Length > 3 ? args[3]
                    : Path.Combine(root, "IF_Quant_Pipeline.groovy");
                string registry = args.Length > 4 ? args[4]
                    : Path.Combine(root, "config", "lung_marker_registry.json");
                string routing = args.Length > 5 ? args[5]
                    : Path.Combine(root, "launcher", "IFQuantLauncher.Routing.cs");
                return LegacyEquivalence.Run(v172, v180, probe, pipeline, registry, routing);
            }
            catch (Exception ex)
            {
                Console.WriteLine("HARNESS CRASHED: " + ex);
                return 51;
            }
        }
    }
}
