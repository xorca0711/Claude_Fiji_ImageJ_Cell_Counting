// =====================================================================
// IFQuantLauncher.Routing.cs                                     v1.8.0
// ---------------------------------------------------------------------
// The route model, the environment surface, and the fail-closed gate.
//
// This file contains NO WinForms code and NO measurement code. Everything
// here is a decision or a description; IF_Quant_Pipeline.groovy remains the
// only thing that measures, and it is frozen.
//
// It is compiled together with IFQuantLauncher.cs and
// MainForm.Routes.partial.cs into a single assembly (launcher/build.ps1).
// It is ALSO compiled standalone, without WinForms, into the legacy
// equivalence harness, which is how route 4 is proven by execution rather
// than by assertion. Do not add a WinForms dependency to this file.
//
// THE FOUR ROUTES
//   R1  IF - confocal / field images     Fiji only. v1.7.2 behaviour + gates.
//   R2  IF - slide scanner (.vsi)        QuPath stage 1 -> Fiji -> stage 3.
//   R3  H&E / brightfield                DISABLED. See LauncherBuild below.
//   R4  Fiji-only legacy                 Byte-identical v1.7.2 environment.
//
// THE HAZARDS THIS FILE EXISTS TO MAKE IMPOSSIBLE BY ACCIDENT
//   H1  IFQ_PANEL defaults to "T"              IF_Quant_Pipeline.groovy:154
//   H2  no fixed threshold -> per-region Otsu  IF_Quant_Pipeline.groovy:2132-2140
//   H3  IFQ_MIN_INCLUDED_NUCLEI defaults to 1  IF_Quant_Pipeline.groovy:329
//   H4  a non-empty IFQ_OUTPUT_DIR aborts      IF_Quant_Pipeline.groovy:3479-3484
//   H5  an unfrozen run must say so in writing (this launcher's record)
// =====================================================================

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;

namespace IFQuantLauncher.Routing
{
    // =================================================================
    // 0. BUILD-TIME SWITCHES
    // =================================================================

    /// <summary>
    /// Compile-time policy for this launcher build.
    /// </summary>
    internal static class LauncherBuild
    {
        // 1.9.0, not 1.8.1, ended the historical version collision: three
        // different binaries had been called v1.8.0. Version 1.9.3 adds a calibrated,
        // thin internal scale bar to visual merge panels while preserving the
        // frozen v1.7.2-compatible legacy route.
        // Version 1.9.4 fixes native ARM64 detection and prevents modern routes
        // from using Fiji's ARM64 launcher executable, which can exit 0 without
        // running the script or writing a manifest.
        public const string Version = "1.9.5";
        public const string AssemblyVersion = "1.9.5.0";

        // =============================================================
        // >>> THE ONE LINE THAT RE-ENABLES ROUTE 3 (H&E / brightfield) <<<
        //
        //   BrightfieldRouteEnabled = false;   // this build
        //   BrightfieldRouteEnabled = true;    // when the module lands
        //
        // It is `static readonly` and NOT `const` on purpose. A `const bool`
        // is folded at compile time, so every `if (BrightfieldRouteEnabled)`
        // branch in the build becomes unreachable code (CS0162) and every
        // `else` of `if (!BrightfieldRouteEnabled)` does too. That is what
        // forced RouteSelfTest to carry a `if (enabled) return 30;` guard,
        // which made flipping this line fail --self-test and get the binary
        // discarded by build.ps1 -- i.e. it was NOT one line. A static
        // readonly field costs one field read, keeps both branches real code,
        // and makes the claim in BrightfieldDisabledReason below true.
        //
        // Flip this to true ONLY when a validated brightfield measurement
        // module exists and is wired to a route-3 stage list. Today there
        // is none:
        //
        //   * IF_Quant_Pipeline.groovy is fluorescence-only. It sets
        //     Prefs.blackBackground = true (line ~3472) and every marker
        //     rule assumes signal is BRIGHT on a DARK background. An H&E
        //     slide is the inverse, so every threshold, every area mask and
        //     every morphology rule inverts its meaning. It would not fail;
        //     it would produce numbers.
        //   * There is no colour deconvolution and no stain-vector model
        //     anywhere in the engine.
        //   * config/lung_marker_registry.json has no haematoxylin/eosin
        //     entry, so there is no validated numerator to report.
        //   * The morphometry module that would supply one is an unreviewed
        //     draft and is not part of this release.
        //
        // Turning this to true is NOT sufficient on its own. It makes the
        // route selectable and stops the gate blocking it; the stage list in
        // RouteCatalog.Describe and the environment builders in
        // RunEnvironment.BuildStage2 and RunEnvironment.BuildStage1 must then
        // be given a real brightfield engine to call. All three places assert
        // on this flag and both builders throw NotImplementedException while
        // the flag is on and nothing is wired, so a half-finished re-enable
        // fails loudly at the Run button instead of running empty.
        // --self-test stays green when the flag is flipped: every check it
        // makes about route 3 is written against the flag's value, not
        // against the assumption that the value is false.
        // =============================================================
        public static readonly bool BrightfieldRouteEnabled = false;

        /// Shown next to the greyed route-3 entry and in the block message.
        public const string BrightfieldDisabledReason =
            "R1 image QC is approved for the current four-mouse/eight-section cohort, " +
            "but pathology analysis is not available in v" + Version + ".\r\n\r\n" +
            "The approved R1/H3 scope covers stain separation, tissue/artifact masks, " +
            "and the usable-tissue denominator. It does not authorize an inflammatory " +
            "or structural-injury numerator, lesion burden, ordinal score, or mouse " +
            "summary. The H4 context and H7 rubric remain development inputs.\r\n\r\n" +
            "The fluorescence engine is incompatible with brightfield absorbance and " +
            "must never be used for H&E. The dedicated H&E status/review tool is " +
            "scripts/he_pipeline.py; it fails closed on identity, profile, package, or " +
            "review-gate drift.\r\n\r\n" +
            "Route 3 stays disabled until a validated H5-H6 QuPath measurement runner, " +
            "completed H7 review, and H8 aggregation contract are wired into the launcher. " +
            "Changing BrightfieldRouteEnabled alone remains insufficient.";
    }

    // =================================================================
    // 1. PANEL REGISTRY -- derived from the frozen engine, never retyped
    // =================================================================

    internal sealed class ChannelDef
    {
        public int Idx;
        public string Marker;
        public string Role;
        public bool AreaMarker;

        /// The engine's own normalisation, IF_Quant_Pipeline.groovy:194:
        ///   marker.toUpperCase().replaceAll(/[^A-Z0-9]+/, "")
        /// so panel LEFT's declared marker "T1A" gives IFQ_T1A_THRESHOLD.
        /// There is no IFQ_PDPN_THRESHOLD for that panel.
        public string Token
        {
            get { return PanelRegistry.NormalizeMarkerToken(Marker); }
        }

        public string ThresholdEnvName
        {
            get { return "IFQ_" + Token + "_THRESHOLD"; }
        }

        public bool IsNuclear
        {
            get { return string.Equals(Role, "nuclear", StringComparison.Ordinal); }
        }
    }

    internal sealed class PanelDef
    {
        public string Key;
        public string Label;

        /// true when this panel came from an IFQ_PANEL_CONFIG JSON rather than
        /// from the engine's own table. A custom panel is a fully supported
        /// engine path (IF_Quant_Pipeline.groovy:688-743), so the launcher
        /// treats it exactly like a built-in one -- same threshold grid, same
        /// H2 rules -- rather than pretending it has no channels.
        public bool IsCustom;

        /// Where the channel list was read from, for the run record.
        public string SourceDescription;

        /// <summary>
        /// True when the engine reads IFQ_&lt;TOKEN&gt;_THRESHOLD for EVERY
        /// non-nuclear channel of EVERY declared panel, not only for the
        /// markers on its `def thresholdMarkers = [...]` list.
        ///
        /// IF_Quant_Pipeline.groovy:873-882 does exactly that:
        ///     allAnalysisChannels = PANELS.values().collectMany { it.channels }
        ///                                  .findAll { it.role != "nuclear" }
        ///     allAnalysisChannels.each { c -&gt;
        ///         rawThreshold = System.getenv("IFQ_" + token + "_THRESHOLD") ... }
        /// and PANELS by then already contains the custom panels. So a channel
        /// of the panel being run is ALWAYS freezable, and thresholdMarkers
        /// only decides whether a marker that is not a channel anywhere has a
        /// variable at all.
        ///
        /// Derived from the embedded pipeline, never assumed: if that block
        /// ever leaves the engine the flag goes false and the launcher falls
        /// back to the narrower thresholdMarkers list.
        public bool ChannelsAreThresholdable;

        public readonly List<ChannelDef> Channels = new List<ChannelDef>();

        public List<ChannelDef> AnalysisChannels
        {
            get
            {
                List<ChannelDef> list = new List<ChannelDef>();
                foreach (ChannelDef c in Channels)
                    if (!c.IsNuclear) list.Add(c);
                return list;
            }
        }

        public List<ChannelDef> AreaMarkers
        {
            get
            {
                List<ChannelDef> list = new List<ChannelDef>();
                foreach (ChannelDef c in Channels)
                    if (c.AreaMarker) list.Add(c);
                return list;
            }
        }

        public HashSet<string> Tokens
        {
            get
            {
                HashSet<string> set = new HashSet<string>(StringComparer.Ordinal);
                foreach (ChannelDef c in Channels) set.Add(c.Token);
                return set;
            }
        }
    }

    /// <summary>
    /// Parses the panel table AND the engine's threshold-marker whitelist out
    /// of the embedded IF_Quant_Pipeline.groovy.
    ///
    /// Parsing rather than retyping is deliberate. The launcher embeds that
    /// exact file at build time, so its idea of "which markers does panel LEFT
    /// have" cannot drift from the engine's. A hand-kept C# table is how you
    /// end up offering an IFQ_PDPN_THRESHOLD box that the engine ignores.
    /// </summary>
    internal static class PanelRegistry
    {
        private static readonly Regex PanelHead = new Regex(
            "\"(?<key>[A-Za-z0-9_]+)\"\\s*:\\s*\\[\\s*label:\"(?<label>[^\"]*)\"",
            RegexOptions.CultureInvariant);

        private static readonly Regex ChannelRow = new Regex(
            "idx:\\s*(?<idx>\\d+)\\s*,\\s*marker:\"(?<marker>[^\"]+)\"\\s*,\\s*role:\"(?<role>[^\"]+)\"",
            RegexOptions.CultureInvariant);

        private static readonly Regex ThresholdMarkerList = new Regex(
            "def\\s+thresholdMarkers\\s*=\\s*\\[(?<body>[^\\]]*)\\]",
            RegexOptions.CultureInvariant | RegexOptions.Singleline);

        public static string NormalizeMarkerToken(string value)
        {
            if (value == null) return "";
            return Regex.Replace(
                value.ToUpperInvariant(), "[^A-Z0-9]+", "", RegexOptions.CultureInvariant);
        }

        public static Dictionary<string, PanelDef> ParseFromPipeline(string groovyText)
        {
            if (string.IsNullOrEmpty(groovyText))
                throw new InvalidOperationException(
                    "The embedded pipeline is empty; panel definitions cannot be derived.");

            MatchCollection heads = PanelHead.Matches(groovyText);
            if (heads.Count == 0)
                throw new InvalidOperationException(
                    "No panel definitions were found in the embedded pipeline. The launcher " +
                    "refuses to guess a channel map.");

            bool everyChannelThresholdable = ThresholdsEveryPanelChannel(groovyText);

            Dictionary<string, PanelDef> panels =
                new Dictionary<string, PanelDef>(StringComparer.OrdinalIgnoreCase);
            for (int i = 0; i < heads.Count; i++)
            {
                Match head = heads[i];
                int start = head.Index;
                int end = (i + 1 < heads.Count) ? heads[i + 1].Index : groovyText.Length;
                string segment = groovyText.Substring(start, end - start);

                PanelDef panel = new PanelDef();
                panel.Key = head.Groups["key"].Value;
                panel.Label = head.Groups["label"].Value;
                panel.IsCustom = false;
                panel.SourceDescription = "built into IF_Quant_Pipeline.groovy";
                panel.ChannelsAreThresholdable = everyChannelThresholdable;

                foreach (Match ch in ChannelRow.Matches(segment))
                {
                    ChannelDef channel = new ChannelDef();
                    channel.Idx = Int32.Parse(ch.Groups["idx"].Value, CultureInfo.InvariantCulture);
                    channel.Marker = ch.Groups["marker"].Value;
                    channel.Role = ch.Groups["role"].Value;

                    // areaMarker lives inside this channel's own [...] literal.
                    // Stop at the closing bracket so a later channel's flag is
                    // never attributed to this one.
                    int from = ch.Index;
                    int close = segment.IndexOf(']', from);
                    if (close < 0) close = segment.Length;
                    channel.AreaMarker = segment.IndexOf(
                        "areaMarker:true", from, close - from, StringComparison.Ordinal) >= 0;

                    panel.Channels.Add(channel);
                }

                if (panel.Channels.Count == 0) continue;
                panels[panel.Key] = panel;
            }
            return panels;
        }

        /// <summary>
        /// The engine reads IFQ_&lt;TOKEN&gt;_THRESHOLD for a CLOSED list of markers
        /// (IF_Quant_Pipeline.groovy:189-199). A panel channel whose marker is not
        /// on that list has no threshold variable at all: setting one is a silent
        /// no-op and the channel stays on adaptive Otsu forever. The threshold grid
        /// must therefore say so instead of offering a box that does nothing.
        /// </summary>
        public static HashSet<string> ParseThresholdMarkerTokens(string groovyText)
        {
            HashSet<string> tokens = new HashSet<string>(StringComparer.Ordinal);
            Match list = ThresholdMarkerList.Match(groovyText ?? "");
            if (!list.Success)
                throw new InvalidOperationException(
                    "The embedded pipeline does not declare 'def thresholdMarkers = [...]'. " +
                    "The launcher will not guess which channels accept a fixed threshold.");
            foreach (Match m in Regex.Matches(list.Groups["body"].Value, "\"(?<m>[^\"]+)\""))
                tokens.Add(NormalizeMarkerToken(m.Groups["m"].Value));
            if (tokens.Count == 0)
                throw new InvalidOperationException(
                    "The embedded pipeline's thresholdMarkers list parsed empty.");
            return tokens;
        }

        /// <summary>
        /// True when IF_Quant_Pipeline.groovy extends the fixed-threshold
        /// lookup to every non-nuclear channel of every DECLARED panel, which
        /// includes the panels an IFQ_PANEL_CONFIG adds. See
        /// PanelDef.ChannelsAreThresholdable for why this matters.
        ///
        /// Three fragments must all be present; any one of them alone could be
        /// coincidence, and getting this wrong in the permissive direction is
        /// H2 (the launcher would claim a fixed cutoff the engine never read).
        /// </summary>
        public static bool ThresholdsEveryPanelChannel(string groovyText)
        {
            if (string.IsNullOrEmpty(groovyText)) return false;
            return
                groovyText.IndexOf(
                    "PANELS.values().collectMany { it.channels }",
                    StringComparison.Ordinal) >= 0 &&
                groovyText.IndexOf(
                    ".findAll { it.role != \"nuclear\" }",
                    StringComparison.Ordinal) >= 0 &&
                groovyText.IndexOf(
                    "System.getenv(\"IFQ_\" + token + \"_THRESHOLD\")",
                    StringComparison.Ordinal) >= 0;
        }
    }

    /// <summary>
    /// The one place that answers "does the engine read a fixed threshold for
    /// this channel". Both the gate and the environment builder must agree, or
    /// the run record claims a cutoff that was never handed to the engine.
    /// </summary>
    internal static class ThresholdSurface
    {
        public static bool EngineReads(
            PanelDef panel, ChannelDef channel, HashSet<string> engineThresholdMarkers)
        {
            if (channel == null) return false;
            // IF_Quant_Pipeline.groovy:873-882. A non-nuclear channel of the
            // panel being run always has an IFQ_<TOKEN>_THRESHOLD, whether the
            // panel is built in or came from IFQ_PANEL_CONFIG.
            if (panel != null && panel.ChannelsAreThresholdable && !channel.IsNuclear)
                return true;
            // Fallback: the narrower `def thresholdMarkers = [...]` list.
            return engineThresholdMarkers == null ||
                   engineThresholdMarkers.Contains(channel.Token);
        }

        /// <summary>
        /// The same question asked from the OTHER end: given a name that is
        /// about to be put into the child's environment, which analysis channel
        /// of this panel does it freeze?
        ///
        /// The gate asks "which channels are frozen" of the UI. The launch seal
        /// asks it of the merged environment, and needs to go name -> channel to
        /// do that. Both directions must use the same normalisation or the seal
        /// would clear an environment the gate never validated.
        /// </summary>
        public static ChannelDef ChannelForThresholdName(PanelDef panel, string name)
        {
            if (panel == null || string.IsNullOrEmpty(name)) return null;
            foreach (ChannelDef channel in panel.AnalysisChannels)
                if (string.Equals(channel.ThresholdEnvName, name,
                                  StringComparison.OrdinalIgnoreCase))
                    return channel;
            return null;
        }

        /// <summary>
        /// True for IFQ_&lt;TOKEN&gt;_THRESHOLD, the stage 2 marker cutoff family.
        /// IFQ_WSI_AGER_THRESHOLD also ends in _THRESHOLD but is a stage 1
        /// tissue-finding setting, not a marker cutoff, so it is excluded by
        /// name against the stage 1 table rather than by a prefix guess.
        /// </summary>
        public static bool IsMarkerThresholdName(string name)
        {
            if (string.IsNullOrEmpty(name)) return false;
            if (!name.StartsWith("IFQ_", StringComparison.Ordinal)) return false;
            if (!name.EndsWith("_THRESHOLD", StringComparison.Ordinal)) return false;
            if (name.Length <= "IFQ__THRESHOLD".Length) return false;
            return !EnvSurface.Stage1Static.Contains(name);
        }

        /// <summary>
        /// The one definition of "this channel is genuinely frozen", applied to a
        /// raw value rather than to a UI control. Everything else -- the gate's
        /// policy lines, the launch seal's re-derivation, the status labels --
        /// funnels through here so they cannot drift apart.
        ///
        /// H2: an absent, blank, non-numeric, zero or negative value is NOT a
        /// frozen channel. Zero in particular is the dangerous one, because the
        /// engine would make every pixel a candidate rather than refuse.
        /// </summary>
        public static bool IsFrozenValue(string raw, out double parsed)
        {
            parsed = 0.0;
            string value = (raw ?? "").Trim();
            if (value.Length == 0) return false;
            if (!Double.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture,
                                 out parsed))
                return false;
            if (Double.IsNaN(parsed) || Double.IsInfinity(parsed) || parsed <= 0.0)
                return false;
            return true;
        }

        /// The exact policy line the run record carries for one channel, derived
        /// from a value. The gate builds it from the UI; the seal builds it from
        /// the environment; a mismatch between the two is what stops a run.
        public static string PolicyLine(
            PanelDef panel, ChannelDef channel, HashSet<string> engineThresholdMarkers,
            string raw)
        {
            if (!EngineReads(panel, channel, engineThresholdMarkers))
                return channel.Marker + "=adaptive_otsu_exploratory(no_env_variable_exists)";
            string value = (raw ?? "").Trim();
            if (value.Length == 0)
                return channel.Marker + "=adaptive_otsu_exploratory";
            double parsed;
            if (!IsFrozenValue(value, out parsed))
                return channel.Marker + "=INVALID(" + value + ")";
            return channel.Marker + "=fixed_predeclared(" +
                   parsed.ToString("R", CultureInfo.InvariantCulture) + ")";
        }
    }

    // =================================================================
    // 1b. CUSTOM PANELS  (IFQ_PANEL_CONFIG)
    // =================================================================

    /// <summary>
    /// A deliberately small, strict JSON reader.
    ///
    /// System.Web.Extensions' JavaScriptSerializer is not referenced here: this
    /// file is compiled a second time, without WinForms and without that
    /// assembly, into the verification harnesses, and the whole point of those
    /// harnesses is that they run THIS code rather than a copy of it.
    ///
    /// Strict on purpose. Anything it will not parse becomes a hard block
    /// upstream, which is the correct answer -- the engine parses the same file
    /// with JsonSlurper and calls failRun on anything malformed, so a file this
    /// reader rejects is a run that would have aborted anyway.
    /// </summary>
    internal static class MiniJson
    {
        private const int MaxDepth = 64;

        public static object Parse(string text)
        {
            if (text == null) throw new FormatException("empty document");
            int index = 0;
            object value = ParseValue(text, ref index, 0);
            SkipWhitespace(text, ref index);
            if (index != text.Length)
                throw new FormatException(
                    "unexpected content after the top-level value, at offset " + index);
            return value;
        }

        private static object ParseValue(string text, ref int index, int depth)
        {
            if (depth > MaxDepth)
                throw new FormatException("nesting deeper than " + MaxDepth + " levels");
            SkipWhitespace(text, ref index);
            if (index >= text.Length) throw new FormatException("unexpected end of document");
            char c = text[index];
            if (c == '{') return ParseObject(text, ref index, depth);
            if (c == '[') return ParseArray(text, ref index, depth);
            if (c == '"') return ParseString(text, ref index);
            if (Literal(text, ref index, "true")) return true;
            if (Literal(text, ref index, "false")) return false;
            if (Literal(text, ref index, "null")) return null;
            return ParseNumber(text, ref index);
        }

        private static Dictionary<string, object> ParseObject(
            string text, ref int index, int depth)
        {
            Dictionary<string, object> map =
                new Dictionary<string, object>(StringComparer.Ordinal);
            index++;                                    // '{'
            SkipWhitespace(text, ref index);
            if (index < text.Length && text[index] == '}') { index++; return map; }
            while (true)
            {
                SkipWhitespace(text, ref index);
                if (index >= text.Length || text[index] != '"')
                    throw new FormatException("expected a quoted key at offset " + index);
                string key = ParseString(text, ref index);
                SkipWhitespace(text, ref index);
                if (index >= text.Length || text[index] != ':')
                    throw new FormatException("expected ':' at offset " + index);
                index++;
                object value = ParseValue(text, ref index, depth + 1);
                if (map.ContainsKey(key))
                    throw new FormatException("duplicate key '" + key + "'");
                map[key] = value;
                SkipWhitespace(text, ref index);
                if (index >= text.Length)
                    throw new FormatException("unterminated object");
                if (text[index] == ',') { index++; continue; }
                if (text[index] == '}') { index++; return map; }
                throw new FormatException("expected ',' or '}' at offset " + index);
            }
        }

        private static List<object> ParseArray(string text, ref int index, int depth)
        {
            List<object> list = new List<object>();
            index++;                                    // '['
            SkipWhitespace(text, ref index);
            if (index < text.Length && text[index] == ']') { index++; return list; }
            while (true)
            {
                list.Add(ParseValue(text, ref index, depth + 1));
                SkipWhitespace(text, ref index);
                if (index >= text.Length)
                    throw new FormatException("unterminated array");
                if (text[index] == ',') { index++; continue; }
                if (text[index] == ']') { index++; return list; }
                throw new FormatException("expected ',' or ']' at offset " + index);
            }
        }

        private static string ParseString(string text, ref int index)
        {
            index++;                                    // opening quote
            StringBuilder builder = new StringBuilder();
            while (true)
            {
                if (index >= text.Length) throw new FormatException("unterminated string");
                char c = text[index++];
                if (c == '"') return builder.ToString();
                if (c != '\\') { builder.Append(c); continue; }
                if (index >= text.Length) throw new FormatException("unterminated escape");
                char escape = text[index++];
                switch (escape)
                {
                    case '"': builder.Append('"'); break;
                    case '\\': builder.Append('\\'); break;
                    case '/': builder.Append('/'); break;
                    case 'b': builder.Append('\b'); break;
                    case 'f': builder.Append('\f'); break;
                    case 'n': builder.Append('\n'); break;
                    case 'r': builder.Append('\r'); break;
                    case 't': builder.Append('\t'); break;
                    case 'u':
                        if (index + 4 > text.Length)
                            throw new FormatException("truncated \\u escape");
                        builder.Append((char)Int32.Parse(
                            text.Substring(index, 4), NumberStyles.HexNumber,
                            CultureInfo.InvariantCulture));
                        index += 4;
                        break;
                    default:
                        throw new FormatException("unsupported escape '\\" + escape + "'");
                }
            }
        }

        private static object ParseNumber(string text, ref int index)
        {
            int start = index;
            if (index < text.Length && (text[index] == '-' || text[index] == '+')) index++;
            while (index < text.Length &&
                   ("0123456789.eE+-".IndexOf(text[index]) >= 0)) index++;
            string raw = text.Substring(start, index - start);
            double parsed;
            if (raw.Length == 0 ||
                !Double.TryParse(raw, NumberStyles.Float, CultureInfo.InvariantCulture, out parsed))
                throw new FormatException("'" + raw + "' is not a value, at offset " + start);
            return parsed;
        }

        private static bool Literal(string text, ref int index, string word)
        {
            if (index + word.Length > text.Length) return false;
            if (string.CompareOrdinal(text, index, word, 0, word.Length) != 0) return false;
            index += word.Length;
            return true;
        }

        private static void SkipWhitespace(string text, ref int index)
        {
            while (index < text.Length)
            {
                char c = text[index];
                if (c == ' ' || c == '\t' || c == '\r' || c == '\n') index++;
                else return;
            }
        }
    }

    /// <summary>
    /// marker token -> default_role, read out of config/lung_marker_registry.json.
    ///
    /// A custom panel channel may leave `role` out; the engine then fills it
    /// from the registry (IF_Quant_Pipeline.groovy:722-725) and calls failRun
    /// if there is still no role. The launcher has to resolve roles the same
    /// way or it cannot tell a nuclear channel from an analysis channel, and
    /// "which channels are analysis channels" is the whole of H2.
    /// </summary>
    internal static class MarkerRoleDefaults
    {
        public static Dictionary<string, string> ParseFromRegistry(string registryJson)
        {
            Dictionary<string, string> roles =
                new Dictionary<string, string>(StringComparer.Ordinal);
            object document = MiniJson.Parse(registryJson);
            Dictionary<string, object> root = document as Dictionary<string, object>;
            if (root == null) return roles;
            object markersValue;
            if (!root.TryGetValue("markers", out markersValue)) return roles;
            Dictionary<string, object> markers = markersValue as Dictionary<string, object>;
            if (markers == null) return roles;

            foreach (KeyValuePair<string, object> entry in markers)
            {
                Dictionary<string, object> profile = entry.Value as Dictionary<string, object>;
                if (profile == null) continue;
                object roleValue;
                if (!profile.TryGetValue("default_role", out roleValue)) continue;
                string role = roleValue as string;
                if (string.IsNullOrEmpty(role)) continue;

                Remember(roles, entry.Key, role);
                object aliasValue;
                if (profile.TryGetValue("aliases", out aliasValue))
                {
                    List<object> aliases = aliasValue as List<object>;
                    if (aliases != null)
                        foreach (object alias in aliases)
                            Remember(roles, alias as string, role);
                }
            }
            return roles;
        }

        private static void Remember(
            Dictionary<string, string> roles, string marker, string role)
        {
            string token = PanelRegistry.NormalizeMarkerToken(marker);
            if (token.Length == 0) return;
            roles[token] = role;
        }
    }

    internal sealed class CustomPanelParse
    {
        public readonly Dictionary<string, PanelDef> Panels =
            new Dictionary<string, PanelDef>(StringComparer.OrdinalIgnoreCase);
        /// null exactly when the whole file parsed and validated.
        public string Error;
        public bool Ok { get { return Error == null; } }
    }

    /// <summary>
    /// Reads IFQ_PANEL_CONFIG the way the engine reads it
    /// (IF_Quant_Pipeline.groovy:688-743 and the shared validation at 758-800),
    /// so a custom panel gets a real threshold grid instead of being treated as
    /// "no channels, therefore nothing to freeze".
    ///
    /// It is deliberately all-or-nothing. The engine's failRun aborts the run
    /// on the FIRST malformed panel in the file, so a file this method rejects
    /// is a run that could not have started; reporting a partial channel list
    /// would be worse than reporting none, because the gate would then think it
    /// had checked every channel.
    /// </summary>
    internal static class CustomPanelRegistry
    {
        private static readonly Regex PanelKeyShape = new Regex(
            "^[A-Za-z0-9][A-Za-z0-9_.-]*$", RegexOptions.CultureInvariant);

        /// IF_Quant_Pipeline.groovy:745-746.
        public static readonly string[] AllowedRoles = new string[]
        {
            "nuclear", "cyto", "membrane", "nuc_marker",
            "nuc_ratio", "apical_cilia", "regional_area"
        };

        public static CustomPanelParse Parse(
            string configJson, Dictionary<string, string> defaultRolesByToken,
            ICollection<string> builtInPanelKeys, bool channelsAreThresholdable)
        {
            CustomPanelParse result = new CustomPanelParse();
            if (string.IsNullOrEmpty((configJson ?? "").Trim()))
            {
                result.Error = "the custom panel file is empty";
                return result;
            }

            object document;
            try { document = MiniJson.Parse(configJson); }
            catch (Exception ex)
            {
                result.Error = "the custom panel JSON could not be read: " + ex.Message;
                return result;
            }

            Dictionary<string, object> root = document as Dictionary<string, object>;
            if (root == null)
            {
                result.Error = "the custom panel JSON's top level is not an object";
                return result;
            }
            object panelsValue;
            Dictionary<string, object> panels = null;
            if (root.TryGetValue("panels", out panelsValue))
                panels = panelsValue as Dictionary<string, object>;
            if (panels == null || panels.Count == 0)
            {
                result.Error =
                    "IFQ_PANEL_CONFIG must contain a non-empty 'panels' object " +
                    "(IF_Quant_Pipeline.groovy:701-703); the engine calls failRun otherwise";
                return result;
            }

            foreach (KeyValuePair<string, object> entry in panels)
            {
                string key = entry.Key;
                if (!PanelKeyShape.IsMatch(key ?? ""))
                {
                    result.Error = "invalid custom panel key '" + key + "'";
                    return result;
                }
                if (builtInPanelKeys != null && Contains(builtInPanelKeys, key))
                {
                    result.Error =
                        "custom panel key '" + key + "' would replace a built-in panel, " +
                        "which the engine refuses (IF_Quant_Pipeline.groovy:710-712)";
                    return result;
                }
                Dictionary<string, object> body = entry.Value as Dictionary<string, object>;
                if (body == null)
                {
                    result.Error = "custom panel '" + key + "' is not an object";
                    return result;
                }
                object channelsValue;
                List<object> channels = null;
                if (body.TryGetValue("channels", out channelsValue))
                    channels = channelsValue as List<object>;
                if (channels == null || channels.Count == 0)
                {
                    result.Error =
                        "custom panel '" + key + "' needs a non-empty 'channels' array";
                    return result;
                }

                PanelDef panel = new PanelDef();
                panel.Key = key;
                panel.IsCustom = true;
                panel.ChannelsAreThresholdable = channelsAreThresholdable;
                panel.SourceDescription = "custom panel JSON (IFQ_PANEL_CONFIG)";
                object labelValue;
                panel.Label = body.TryGetValue("label", out labelValue) && labelValue is string
                    ? (string)labelValue
                    : key;

                foreach (object rawChannel in channels)
                {
                    Dictionary<string, object> channelMap =
                        rawChannel as Dictionary<string, object>;
                    if (channelMap == null)
                    {
                        result.Error =
                            "every channel in custom panel '" + key + "' must be an object";
                        return result;
                    }

                    ChannelDef channel = new ChannelDef();

                    object idxValue;
                    if (!channelMap.TryGetValue("idx", out idxValue) || !(idxValue is double) ||
                        (double)idxValue < 1.0 ||
                        (double)idxValue != Math.Floor((double)idxValue))
                    {
                        result.Error =
                            "custom panel '" + key + "' has a channel without a whole idx >= 1";
                        return result;
                    }
                    channel.Idx = (int)(double)idxValue;

                    object markerValue;
                    string marker = null;
                    if (channelMap.TryGetValue("marker", out markerValue))
                        marker = markerValue as string;
                    if (string.IsNullOrEmpty((marker ?? "").Trim()))
                    {
                        result.Error =
                            "custom panel '" + key + "' has a channel without a marker";
                        return result;
                    }
                    channel.Marker = marker.Trim();

                    object roleValue;
                    string role = null;
                    if (channelMap.TryGetValue("role", out roleValue))
                        role = roleValue as string;
                    if (string.IsNullOrEmpty((role ?? "").Trim()))
                    {
                        // The engine's own fallback: the registry's default_role.
                        if (defaultRolesByToken == null ||
                            !defaultRolesByToken.TryGetValue(channel.Token, out role) ||
                            string.IsNullOrEmpty((role ?? "").Trim()))
                        {
                            result.Error =
                                "custom panel '" + key + "' channel '" + channel.Marker +
                                "' has no role and the marker registry has no default_role " +
                                "for it, so the launcher cannot tell whether it is a nuclear " +
                                "channel or an analysis channel. The engine calls failRun on " +
                                "the same input (IF_Quant_Pipeline.groovy:768-771). Give the " +
                                "channel an explicit \"role\".";
                            return result;
                        }
                    }
                    role = role.Trim();
                    if (!Contains(AllowedRoles, role))
                    {
                        result.Error =
                            "custom panel '" + key + "' channel '" + channel.Marker +
                            "' has role '" + role + "', which is not one of " +
                            string.Join(", ", AllowedRoles);
                        return result;
                    }
                    channel.Role = role;

                    object areaValue;
                    channel.AreaMarker =
                        string.Equals(role, "regional_area", StringComparison.Ordinal) ||
                        (channelMap.TryGetValue("areaMarker", out areaValue) &&
                         areaValue is bool && (bool)areaValue);

                    panel.Channels.Add(channel);
                }

                result.Panels[panel.Key] = panel;
            }
            return result;
        }

        private static bool Contains(ICollection<string> values, string candidate)
        {
            foreach (string value in values)
                if (string.Equals(value, candidate, StringComparison.OrdinalIgnoreCase))
                    return true;
            return false;
        }
    }

    // =================================================================
    // 2. ENVIRONMENT SURFACE
    // =================================================================

    internal enum EnvKind { Stage1Static, Stage2Static, MarkerFamily, UnknownMarker, Unknown }

    internal sealed class EnvClassification
    {
        public EnvKind Kind;
        public string Stage;
        public string Detail;
    }

    /// <summary>
    /// The IFQ_* names the two front ends actually read.
    ///
    /// This exists because of one property of the engine: envOr / envInt /
    /// envDouble fall back SILENTLY when a variable is absent or empty. A
    /// typo'd IFQ_KRT_5_THRESHOLD is not an error -- the engine simply uses
    /// adaptive Otsu and the run looks configured. v1.7.2's Advanced box
    /// (IFQuantLauncher.cs ParseAdvancedEnvironment) validates only the shape
    /// ^IFQ_[A-Z0-9_]+$, so today that typo is accepted.
    /// </summary>
    internal static class EnvSurface
    {
        public static readonly HashSet<string> Stage1Static =
            new HashSet<string>(StringComparer.Ordinal)
            {
                "IFQ_WSI_AGER_CHANNEL", "IFQ_WSI_AGER_THRESHOLD", "IFQ_WSI_CHANNEL_PATTERNS",
                "IFQ_WSI_COMPRESSION", "IFQ_WSI_CORE_PX", "IFQ_WSI_DAMAGE_CUTOFF",
                "IFQ_WSI_DAMAGE_SIGMA_UM", "IFQ_WSI_DRY_RUN", "IFQ_WSI_EXPECT_CHANNELS",
                "IFQ_WSI_FILL_INTERIOR_RINGS", "IFQ_WSI_HALO_PX", "IFQ_WSI_INPUT",
                "IFQ_WSI_MAX_PIXEL_UM", "IFQ_WSI_MAX_TILES_PER_SLIDE", "IFQ_WSI_MIN_FRAGMENT_MM2",
                "IFQ_WSI_MIN_TILE_TISSUE_UM2", "IFQ_WSI_OUTPUT", "IFQ_WSI_PANEL",
                "IFQ_WSI_PARALLEL", "IFQ_WSI_PARTITION_DAMAGE", "IFQ_WSI_RESUME",
                "IFQ_WSI_ROI_COMPARTMENT", "IFQ_WSI_ROI_NAME", "IFQ_WSI_ROI_NAME_DAMAGED",
                "IFQ_WSI_ROI_NAME_INTACT", "IFQ_WSI_SLIDE_METADATA", "IFQ_WSI_TISSUE_BLUR_SIGMA",
                "IFQ_WSI_TISSUE_CLOSE_RADIUS", "IFQ_WSI_TISSUE_DOWNSAMPLE",
                "IFQ_WSI_TISSUE_OPEN_RADIUS", "IFQ_WSI_WRITE_TILE_PX"
            };

        public static readonly HashSet<string> Stage2Static =
            new HashSet<string>(StringComparer.Ordinal)
            {
                "IFQ_ACTUB_CILIA_DENSITY_RADIUS_UM", "IFQ_ACTUB_CILIA_MIN_LOCAL_DENSITY",
                "IFQ_ACTUB_CILIA_SEED_PERCENTILE", "IFQ_ACTUB_MAX_COMPONENT_DISTANCE_UM",
                "IFQ_ACTUB_MAX_PATCH_AREA_UM2", "IFQ_ACTUB_MIN_COMPONENT_BOUNDARY_DISTANCE_UM",
                "IFQ_ACTUB_MIN_PATCH_AREA_UM2", "IFQ_ACTUB_MIN_SUPPORT_FRACTION",
                "IFQ_ACTUB_SUPPORT_EXPAND_UM", "IFQ_ALLOW_NONEMPTY_OUTPUT",
                "IFQ_COMPARTMENT_MODE", "IFQ_DAPI_BACKGROUND_RADIUS_UM", "IFQ_DAPI_BLUR_SIGMA_PX",
                "IFQ_DAPI_CONTRAST_SATURATION", "IFQ_DAPI_LOCAL_RADIUS_UM", "IFQ_DAPI_METHOD",
                "IFQ_DISPLAY_GAMMA", "IFQ_DISPLAY_HIGH_PERCENTILE", "IFQ_DISPLAY_LOW_PERCENTILE",
                "IFQ_DISPLAY_PREVIEW_ONLY", "IFQ_DISPLAY_SCALE_BAR_UM", "IFQ_DISPLAY_SCALE_BAR_THICKNESS_PX",
                "IFQ_EXPORT_DISPLAY_CHANNELS", "IFQ_INCLUDE_REGEX", "IFQ_CANONICAL_MANIFEST_PATH",
                "IFQ_INPUT_DIR", "IFQ_MARKER_REGISTRY", "IFQ_MAX_IMAGES", "IFQ_MIN_INCLUDED_NUCLEI",
                "IFQ_MIN_NUCLEUS_AREA_UM2", "IFQ_MORPHOLOGY_PRIMARY", "IFQ_MRAGE_MIN_RING_FRACTION",
                "IFQ_OUTPUT_DIR", "IFQ_PANEL", "IFQ_PANEL_CONFIG", "IFQ_PANEL_MAP_PATH",
                "IFQ_PROJECTION", "IFQ_RECURSIVE", "IFQ_RING_EXPAND_UM", "IFQ_SEGMENTER",
                "IFQ_SINGLE_PLANE", "IFQ_T1A_MIN_RING_FRACTION", "IFQ_TISSUE_MODE",
                "IFQ_WHOLE_FIELD_COMPARTMENT", "IFQ_Z_APICAL_PLANES", "IFQ_Z_APICAL_RANGE",
                "IFQ_Z_CELL_BODY_PLANES", "IFQ_Z_CELL_BODY_RANGE", "IFQ_Z_NUCLEAR_RANGE",
                "IFQ_MORPHOLOGY_PRIMARY", "IFQ_STARDIST_PROB", "IFQ_STARDIST_NMS"
            };

        public static readonly Dictionary<string, string> MarkerSuffixes =
            new Dictionary<string, string>(StringComparer.Ordinal)
            {
                { "_THRESHOLD", "candidate-pixel intensity cutoff" },
                { "_MIN_POSITIVE_FRACTION", "morphology support fraction" },
                { "_MIN_LARGEST_COMPONENT_SHARE", "connected-pattern share" },
                { "_MIN_NUCLEAR_ENRICHMENT", "applied only when the resolved rule already has the key" },
                { "_MIN_NUC_CYTO_RATIO", "applied only when the resolved rule already has the key" }
            };

        /// Owned by the launcher UI. The Advanced box may never set these.
        /// v1.7.2's nineteen, plus the six this build takes ownership of.
        public static readonly HashSet<string> ProtectedKeys =
            new HashSet<string>(StringComparer.OrdinalIgnoreCase)
            {
                // inherited verbatim from v1.7.2
                "IFQ_INPUT_DIR", "IFQ_OUTPUT_DIR", "IFQ_PANEL",
                "IFQ_MARKER_REGISTRY", "IFQ_PANEL_CONFIG", "IFQ_PANEL_MAP_PATH",
                "IFQ_RECURSIVE", "IFQ_INCLUDE_REGEX", "IFQ_MAX_IMAGES",
                "IFQ_SEGMENTER", "IFQ_PROJECTION", "IFQ_SINGLE_PLANE",
                "IFQ_EXPORT_DISPLAY_CHANNELS", "IFQ_DISPLAY_PREVIEW_ONLY",
                "IFQ_DISPLAY_SCALE_BAR_UM", "IFQ_DISPLAY_SCALE_BAR_THICKNESS_PX",
                "IFQ_TISSUE_MODE", "IFQ_COMPARTMENT_MODE",
                "IFQ_WHOLE_FIELD_COMPARTMENT",
                "IFQ_ALLOW_NONEMPTY_OUTPUT", "IFQ_MORPHOLOGY_PRIMARY",
                // new in v1.8.0
                // H3: the sparse-region floor now has a first-class control, so
                // it must not also be settable behind the UI's back.
                "IFQ_MIN_INCLUDED_NUCLEI",
                // Stage 1 identity: one panel control writes both panel names.
                "IFQ_WSI_PANEL", "IFQ_WSI_INPUT", "IFQ_WSI_OUTPUT"
            };

        public static EnvClassification Classify(string name, HashSet<string> panelTokens)
        {
            EnvClassification result = new EnvClassification();
            if (Stage1Static.Contains(name))
            {
                result.Kind = EnvKind.Stage1Static; result.Stage = "stage1"; result.Detail = "";
                return result;
            }
            if (Stage2Static.Contains(name))
            {
                result.Kind = EnvKind.Stage2Static; result.Stage = "stage2"; result.Detail = "";
                return result;
            }
            foreach (KeyValuePair<string, string> pair in MarkerSuffixes)
            {
                if (!name.StartsWith("IFQ_", StringComparison.Ordinal)) continue;
                if (!name.EndsWith(pair.Key, StringComparison.Ordinal)) continue;
                string token = name.Substring(4, name.Length - 4 - pair.Key.Length);
                if (token.Length == 0) continue;
                result.Stage = "stage2";
                if (panelTokens == null || panelTokens.Contains(token))
                {
                    result.Kind = EnvKind.MarkerFamily;
                    result.Detail = pair.Value;
                }
                else
                {
                    result.Kind = EnvKind.UnknownMarker;
                    result.Detail =
                        "marker token '" + token + "' is not a channel of the selected panel. " +
                        "The engine would ignore this variable silently. " + pair.Value;
                }
                return result;
            }
            result.Kind = EnvKind.Unknown;
            result.Stage = null;
            result.Detail =
                "not part of either front end's environment surface. The engine ignores " +
                "unknown IFQ_* names without complaint, so this would be a no-op that " +
                "looks configured.";
            return result;
        }
    }

    // =================================================================
    // 3. ROUTE MODEL
    // =================================================================

    internal enum ImageRoute
    {
        IfConfocal = 1,
        IfSlideScanner = 2,
        HeBrightfield = 3,
        LegacyFiji172 = 4
    }

    internal sealed class StageSpec
    {
        public string Id;
        public string Title;
        public string Tool;
        public string Artifact;

        public StageSpec(string id, string title, string tool, string artifact)
        {
            Id = id; Title = title; Tool = tool; Artifact = artifact;
        }
    }

    internal sealed class RouteSpec
    {
        public ImageRoute Route;
        public string DisplayName;
        public string OneLine;
        public List<StageSpec> Stages = new List<StageSpec>();
        public bool RequiresFiji;
        public bool RequiresQuPath;
        public bool RequiresPython;
        public bool ProducesQuantitativeNumbers;

        /// false => the route is visible but NOT selectable, and the gate blocks it.
        public bool Available = true;
        /// non-null exactly when Available is false.
        public string UnavailableReason;

        /// true  => a missing fixed threshold is flagged EXPLORATORY (H2/H5)
        /// false => a missing fixed threshold is a hard block
        public bool ThresholdsMayBeOmitted;

        /// true  => the launcher writes IFQ_MIN_INCLUDED_NUCLEI explicitly (H3)
        /// false => route 4, which must not write what v1.7.2 never wrote
        public bool WritesMinIncludedNuclei;
    }

    internal static class RouteCatalog
    {
        public static RouteSpec Describe(ImageRoute route)
        {
            RouteSpec spec = new RouteSpec();
            spec.Route = route;
            switch (route)
            {
                case ImageRoute.IfConfocal:
                    spec.DisplayName = "1. IF - confocal / field images (Fiji only)";
                    spec.OneLine =
                        "Small calibrated multichannel fields. The frozen Fiji engine reads them " +
                        "directly. One run_summary.csv row per (image, region).";
                    spec.Stages.Add(new StageSpec(
                        "stage2", "Measure with the frozen Fiji engine", "fiji",
                        "run_summary.csv + run_summary.xlsx + run_manifest.json status=complete"));
                    spec.RequiresFiji = true;
                    spec.ProducesQuantitativeNumbers = true;
                    spec.ThresholdsMayBeOmitted = true;
                    spec.WritesMinIncludedNuclei = true;
                    break;

                case ImageRoute.IfSlideScanner:
                    spec.DisplayName = "2. IF - slide scanner (.vsi whole slide)";
                    spec.OneLine =
                        "QuPath reads and tiles the slide; the SAME frozen Fiji engine measures " +
                        "the tiles; stage 3 reconciles tiles back to one slide. QuPath never " +
                        "measures anything.";
                    spec.Stages.Add(new StageSpec(
                        "stage1", "Tile the slide (QuPath 0.7+, headless)", "qupath",
                        "tiles/*.ome.tif + tiles/*_RoiSet.zip + tiles/samplesheet.csv + stage1_manifest.json"));
                    spec.Stages.Add(new StageSpec(
                        "stage2", "Measure every tile (Fiji)", "fiji",
                        "analysis/run_summary.csv, one row per tile per region"));
                    spec.Stages.Add(new StageSpec(
                        "stage3", "Reconcile tiles to slide (Python)", "python",
                        "stats/slide_level_summary.csv"));
                    spec.RequiresFiji = true;
                    spec.RequiresQuPath = true;
                    spec.RequiresPython = true;
                    spec.ProducesQuantitativeNumbers = true;
                    spec.ThresholdsMayBeOmitted = false;   // hard block: ~370 tiles
                    spec.WritesMinIncludedNuclei = true;
                    break;

                case ImageRoute.HeBrightfield:
                    spec.DisplayName =
                        "3. H&E / brightfield" +
                        (LauncherBuild.BrightfieldRouteEnabled
                            ? " (brightfield morphometry)"
                            : "   [R1 QC ONLY - ANALYSIS NOT AVAILABLE IN v" + LauncherBuild.Version + "]");
                    spec.OneLine =
                        "H&E image QC is approved for the current cohort; pathology measurement " +
                        "remains review-gated and deliberately unavailable in this build.";
                    spec.RequiresQuPath = true;
                    spec.ProducesQuantitativeNumbers = false;
                    spec.ThresholdsMayBeOmitted = true;
                    spec.WritesMinIncludedNuclei = false;
                    if (!LauncherBuild.BrightfieldRouteEnabled)
                    {
                        spec.Available = false;
                        spec.UnavailableReason = LauncherBuild.BrightfieldDisabledReason;
                        // No stages are listed: there is nothing to run.
                    }
                    else
                    {
                        // Flipping the flag makes the route selectable. It does
                        // not conjure an engine: RunEnvironment.BuildStage2 still
                        // refuses until the brightfield module is wired in, so a
                        // half-finished re-enable fails at the Run button with a
                        // named cause instead of producing an empty run.
                        spec.Stages.Add(new StageSpec(
                            "stage2he", "Measure with the brightfield morphometry module",
                            "qupath", "run_summary.csv (brightfield endpoints)"));
                    }
                    break;

                case ImageRoute.LegacyFiji172:
                    spec.DisplayName = "4. Fiji-only legacy mode (reproduce v1.7.2 exactly)";
                    spec.OneLine =
                        "Byte-for-byte the v1.7.2 environment and the v1.7.2 Fiji command line " +
                        "for the field/confocal case, so an analysis run before v" +
                        LauncherBuild.Version + " stays reproducible.";
                    spec.Stages.Add(new StageSpec(
                        "stage2legacy", "Measure with the frozen Fiji engine (v1.7.2 environment)",
                        "fiji", "run_summary.csv + run_summary.xlsx + run_manifest.json"));
                    spec.RequiresFiji = true;
                    spec.ProducesQuantitativeNumbers = true;
                    spec.ThresholdsMayBeOmitted = true;
                    spec.WritesMinIncludedNuclei = false;  // v1.7.2 never wrote it
                    break;

                default:
                    // A route id this build does not define. Without this case
                    // the fresh RouteSpec kept its field initialisers --
                    // Available=true, no stages, ThresholdsMayBeOmitted=false
                    // but nothing to compare it against -- so the gate stayed
                    // silent, RunEnvironment.BuildStage2 produced a complete
                    // fluorescence environment and PreStartAssertions passed.
                    // An unknown route is the one case where the launcher knows
                    // nothing at all about what would run, so it is the last
                    // place that may fail open.
                    spec.DisplayName = "Unknown route " + (int)route;
                    spec.OneLine =
                        "This build defines no stages, no tools and no hazard rules for this " +
                        "route id.";
                    spec.Available = false;
                    spec.UnavailableReason =
                        "Route id " + (int)route + " is not a route this build defines.\r\n\r\n" +
                        "The launcher will not start a run it cannot describe: with no stage " +
                        "list it does not know which engine would measure, and with no route " +
                        "policy it does not know whether an omitted threshold is a flag or a " +
                        "hard stop. Reaching this means a caller invented a route id, or a " +
                        "route was added to ImageRoute without being added to " +
                        "RouteCatalog.Describe.";
                    spec.ProducesQuantitativeNumbers = false;
                    // Fail closed on every axis: no tool is assumed present, an
                    // omitted threshold is a hard stop, and nothing is written.
                    spec.ThresholdsMayBeOmitted = false;
                    spec.WritesMinIncludedNuclei = false;
                    break;
            }
            return spec;
        }

        public static ImageRoute[] All()
        {
            return new ImageRoute[]
            {
                ImageRoute.IfConfocal, ImageRoute.IfSlideScanner,
                ImageRoute.HeBrightfield, ImageRoute.LegacyFiji172
            };
        }

        public static bool IsAvailable(ImageRoute route)
        {
            return Describe(route).Available;
        }
    }

    // =================================================================
    // 4. THE RUN REQUEST
    // =================================================================

    internal enum RunTier
    {
        Dry = 0,
        Exploratory = 1,
        Confirmatory = 2
    }

    /// How the Fiji engine is started.
    internal enum FijiInvocation
    {
        /// v1.7.2: fiji-windows-*.exe --headless --console --run "script".
        /// Route 4 is hardwired to this and may never use anything else.
        LauncherExe = 0,
        /// The bundled JVM, argument-for-argument as scripts/Invoke-Stage2Sharded.ps1
        /// starts it. Needed because the Fiji launcher .exe is unreliable on
        /// win-arm64, where it is the only .exe present.
        BundledJvm = 1
    }

    internal sealed class RunRequest
    {
        public ImageRoute Route = ImageRoute.IfConfocal;
        public RunTier Tier = RunTier.Exploratory;
        public FijiInvocation Invocation = FijiInvocation.LauncherExe;

        public string InputDirectory;
        public string OutputBase;
        public string RunName;
        public string FijiPath;
        public string QuPathExecutable;
        public string PythonExecutable;

        // Route 2 only
        public string WsiInput;
        public string WsiOutput;
        public string SlideMetadataCsv;
        public bool WsiResume = true;
        public bool WsiPartitionDamage;
        public int WsiMaxTilesPerSlide;

        // Shared analysis settings (AUTO is resolved before the gate runs)
        public string PanelKey;
        public bool PanelWasAuto;
        public string PanelConfigJson;

        /// <summary>
        /// H1/H2. Set by the caller when PanelKey names a custom panel and the
        /// selected IFQ_PANEL_CONFIG could not be turned into a channel list --
        /// unreadable JSON, the key missing from the file, a channel with no
        /// resolvable role. Non-null means the engine would abort or, worse,
        /// the launcher would be guessing about which channels exist, so it is
        /// a hard block rather than a flag.
        /// </summary>
        public string PanelResolutionError;
        public string Segmenter = "classic";
        public string Projection = "layer_aware";
        public int SinglePlane = -1;
        public string TissueMode = "auto";
        public string CompartmentMode = "optional";
        public string WholeFieldCompartment = "unassigned";
        public bool Recursive = true;
        public string IncludeRegex = ".*";
        public int MaxImages;
        public bool PreviewOnly;

        /// H2. marker token -> threshold as typed. Absent or blank = adaptive Otsu.
        public readonly Dictionary<string, string> Thresholds =
            new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

        /// H3. Always written explicitly on routes 1 and 2; never on route 4.
        public int MinIncludedNuclei;

        /// H1. Non-interpretable pilot panels are opt-in.
        public bool PilotPanelsUnlocked;

        /// H4. Filled by the caller from the file system immediately before the
        /// gate runs, so the gate itself stays a pure function.
        public bool OutputDirectoryExistsAndIsNonEmpty;

        public string AdvancedText = "";
        public string TypedConfirmation = "";
    }

    // =================================================================
    // 5. THE FAIL-CLOSED GATE
    // =================================================================

    internal enum Severity
    {
        Block = 3,
        Confirm = 2,
        Warn = 1,
        Note = 0
    }

    internal sealed class GateFinding
    {
        public Severity Severity;
        public string Code;
        public string Message;
        /// Suffix appended to the run folder name so a flagged run is
        /// identifiable from Explorer without opening anything.
        public string FolderStamp;

        public GateFinding(Severity severity, string code, string message)
        {
            Severity = severity; Code = code; Message = message;
        }

        public GateFinding(Severity severity, string code, string message, string folderStamp)
            : this(severity, code, message)
        {
            FolderStamp = folderStamp;
        }
    }

    internal sealed class GateResult
    {
        public readonly List<GateFinding> Findings = new List<GateFinding>();

        /// Every phrase the user must type before this run may start.
        ///
        /// A LIST, not a single string. Two independent hazards can apply at
        /// once -- panel T with no frozen thresholds is both non-interpretable
        /// AND exploratory -- and with one slot the second rule to be evaluated
        /// silently overwrote the first, so the user acknowledged one
        /// consequence and never saw the other. Each phrase gets its own box.
        public readonly List<string> RequiredPhrases = new List<string>();

        public bool NeedsConfirmation { get { return RequiredPhrases.Count > 0; } }

        public void RequirePhrase(string phrase)
        {
            if (string.IsNullOrEmpty(phrase)) return;
            if (!RequiredPhrases.Contains(phrase)) RequiredPhrases.Add(phrase);
        }

        /// H5. True when the run's thresholds are not all frozen, i.e. at least
        /// one analysis channel will be measured with per-region adaptive Otsu.
        public bool Exploratory;
        /// Human-readable per-channel threshold provenance, for launcher_run.txt.
        public readonly List<string> ThresholdPolicy = new List<string>();

        /// H3 on route 4 only. Legacy mode writes no IFQ_MIN_INCLUDED_NUCLEI,
        /// so the floor is whatever the v1.7.2 Advanced box set, or the engine
        /// default of 1. Recorded here so launcher_run.txt can state the number
        /// that will actually apply instead of leaving it to be inferred.
        public string LegacyMinIncludedNuclei;

        public bool Blocked
        {
            get
            {
                foreach (GateFinding f in Findings)
                    if (f.Severity == Severity.Block) return true;
                return false;
            }
        }

        public List<string> FolderStamps()
        {
            List<string> stamps = new List<string>();
            foreach (GateFinding f in Findings)
                if (!string.IsNullOrEmpty(f.FolderStamp) && !stamps.Contains(f.FolderStamp))
                    stamps.Add(f.FolderStamp);
            return stamps;
        }

        public List<GateFinding> OfSeverity(Severity s)
        {
            List<GateFinding> list = new List<GateFinding>();
            foreach (GateFinding f in Findings)
                if (f.Severity == s) list.Add(f);
            return list;
        }

        public string FirstBlockMessage()
        {
            foreach (GateFinding f in Findings)
                if (f.Severity == Severity.Block) return f.Message;
            return null;
        }
    }

    /// <summary>
    /// Every rule that makes a silent-garbage run impossible by accident.
    /// A pure function of (request, panel, tool inventory) -> findings: no UI,
    /// no file-system access, no side effects, so it is unit-testable and is
    /// unit-tested by RoutingSelfTest.
    /// </summary>
    internal static class FailClosedGate
    {
        public const string ExploratoryPhrase = "EXPLORATORY";
        public const string PilotPhrase = "NOT INTERPRETABLE";
        public const string ExploratoryStamp = "_EXPLORATORY";
        public const string PilotStamp = "_PILOT_NOT_INTERPRETABLE";
        public const string ExploratoryMarkerFileName = "EXPLORATORY_DO_NOT_AGGREGATE.txt";

        public static GateResult Evaluate(
            RunRequest request,
            PanelDef panel,
            HashSet<string> engineThresholdMarkers,
            ToolInventory tools)
        {
            GateResult result = new GateResult();
            RouteSpec spec = RouteCatalog.Describe(request.Route);

            // ---------------------------------------------------------
            // R0. Route availability.  R3 lands here.
            // ---------------------------------------------------------
            if (!spec.Available)
            {
                result.Findings.Add(new GateFinding(
                    Severity.Block, "ROUTE_NOT_AVAILABLE",
                    spec.DisplayName + "\r\n\r\n" + spec.UnavailableReason));
                // Deliberately keep evaluating: the review text should show the
                // whole picture, not stop at the first problem. Blocked is
                // already true, and nothing below can clear it.
            }

            if (spec.RequiresFiji && tools != null && !tools.FijiPresent)
                result.Findings.Add(new GateFinding(
                    Severity.Block, "FIJI_MISSING",
                    "This route measures with Fiji, and no Fiji/ImageJ executable was resolved " +
                    "from the selected path."));
            if (spec.RequiresFiji && request.Invocation == FijiInvocation.BundledJvm &&
                tools != null && !tools.BundledJvmPresent)
                result.Findings.Add(new GateFinding(
                    Severity.Block, "FIJI_JVM_MISSING",
                    "Direct JVM invocation was requested but the bundled runtime could not be " +
                    "located under the Fiji folder. Both java.exe (under java\\) and " +
                    "ij1-patcher-*.jar (under jars\\) are required; the patcher is what makes " +
                    "headless ImageJ1 work at all."));
            if (spec.RequiresFiji && request.Route != ImageRoute.LegacyFiji172 &&
                request.Invocation == FijiInvocation.LauncherExe && tools != null &&
                string.Equals(tools.WindowsArchitecture, "ARM64", StringComparison.OrdinalIgnoreCase))
                result.Findings.Add(new GateFinding(
                    Severity.Block, "FIJI_ARM64_LAUNCHER_UNRELIABLE",
                    "Fiji's Windows ARM64 launcher executable can exit 0 without running the " +
                    "analysis script or writing run_manifest.json. Select 'bundled JVM' under " +
                    "How to start Fiji; this installation already contains the Java runtime " +
                    "and ij1-patcher required for that path."));
            if (spec.RequiresQuPath && tools != null && !tools.QuPathPresent)
                result.Findings.Add(new GateFinding(
                    Severity.Block, "QUPATH_MISSING",
                    "This route needs the QuPath 0.7+ console build to read the .vsi. Fiji's " +
                    "Bio-Formats cannot decode the JPEG-2000 .ets pyramid, so there is no " +
                    "fallback."));
            if (spec.RequiresPython && tools != null && !tools.PythonPresent)
                result.Findings.Add(new GateFinding(
                    Severity.Block, "PYTHON_MISSING",
                    "Stage 3 (aggregate_tiles_to_slide.py) is the only path from tile rows to a " +
                    "slide-level number. Without Python the run would stop at unreconciled tiles."));

            // Strict gating needs an independently supplied anatomical label.
            // whole_field always names its sole ROI "whole_field", so leaving the
            // override unassigned makes every image fail after the expensive import.
            if (request.Route != ImageRoute.LegacyFiji172 &&
                string.Equals(request.TissueMode, "whole_field", StringComparison.OrdinalIgnoreCase) &&
                string.Equals(request.CompartmentMode, "required", StringComparison.OrdinalIgnoreCase) &&
                string.Equals(request.WholeFieldCompartment, "unassigned", StringComparison.OrdinalIgnoreCase))
            {
                result.Findings.Add(new GateFinding(
                    Severity.Block, "ANATOMY_REQUIRED_BUT_UNASSIGNED",
                    "Strict anatomical gating cannot be combined with Tissue boundary = whole_field " +
                    "and Whole-image tissue type = unassigned. Choose the reviewed tissue type, or " +
                    "set Anatomical gate to optional for an exploratory context-unresolved run."));
            }

            // ---------------------------------------------------------
            // H1. IFQ_PANEL defaults to "T"
            //     IF_Quant_Pipeline.groovy:154  envOr("IFQ_PANEL", "T")
            // ---------------------------------------------------------
            if (string.IsNullOrEmpty(request.PanelKey) || request.PanelKey.Trim().Length == 0)
            {
                result.Findings.Add(new GateFinding(
                    Severity.Block, "H1_PANEL_UNSET",
                    "No staining panel is selected.\r\n\r\n" +
                    "The launcher will not start a run without writing IFQ_PANEL, because the " +
                    "engine's fallback is panel T -- a pilot plumbing panel whose nuclear " +
                    "channel is index 4 rather than 1 and whose green/red channels are smFISH " +
                    "probes from a SKIN sample. Forgetting the panel produces numbers, not an " +
                    "error."));
            }
            else if (string.Equals(request.PanelKey, "T", StringComparison.OrdinalIgnoreCase))
            {
                if (!request.PilotPanelsUnlocked)
                {
                    result.Findings.Add(new GateFinding(
                        Severity.Block, "H1_PILOT_PANEL_LOCKED",
                        "Panel T is a plumbing test, not a study panel. Its channel map points " +
                        "at an unrelated skin smFISH sample, so any positivity it reports is " +
                        "meaningless. Tick 'Enable non-interpretable pilot panels' under " +
                        "Advanced study options if you really are testing the plumbing."));
                }
                else
                {
                    result.RequirePhrase(PilotPhrase);
                    result.Findings.Add(new GateFinding(
                        Severity.Confirm, "H1_PILOT_PANEL",
                        "Panel T is a plumbing test. Nothing it reports is biologically " +
                        "interpretable, and no number from this run may be aggregated or shown " +
                        "as a result.",
                        PilotStamp));
                }
            }
            // ---------------------------------------------------------
            // N4. `!request.PanelWasAuto` is load-bearing.
            //
            // AUTO resolves a panel PER IMAGE, so there is deliberately no
            // channel list before the run; `panel == null` here means "decided
            // later", not "unknown key". Without this guard the caller that
            // resolves AUTO first (ReadAndValidateConfiguration sets
            // request.PanelKey to the detected panel) hit this branch and was
            // refused with "Panel LEFT is not one of the panels declared in the
            // embedded IF_Quant_Pipeline.groovy" -- a statement that is false
            // about a key AUTO had just detected FROM that same file, and that
            // made AUTO unusable on every route. The honest rule for AUTO is
            // the unresolved-panel branch below, which flags the run
            // EXPLORATORY because nothing can be frozen in advance.
            // ---------------------------------------------------------
            else if (panel == null && !request.PanelWasAuto &&
                     string.IsNullOrEmpty(request.PanelConfigJson))
            {
                result.Findings.Add(new GateFinding(
                    Severity.Block, "H1_PANEL_UNKNOWN",
                    "Panel '" + request.PanelKey + "' is not one of the panels declared in the " +
                    "embedded IF_Quant_Pipeline.groovy, and no validated custom panel JSON is " +
                    "selected. The engine aborts on an unknown IFQ_PANEL " +
                    "(IF_Quant_Pipeline.groovy:866-868)."));
            }

            // N4. AUTO detects BUILT-IN panels only: DetectBuiltInPanels reads
            // the embedded pipeline's own table and never opens the custom panel
            // JSON, so the pair is not a configuration the launcher can honour.
            // ReadAndValidateConfiguration already threw on it at Run time; the
            // live gate said nothing, went AMBER and left Run enabled, so the
            // first sight of the problem was an error dialog after the review
            // dialog had been accepted. Same rule, stated in one place.
            if (request.PanelWasAuto && !string.IsNullOrEmpty(request.PanelConfigJson))
            {
                result.Findings.Add(new GateFinding(
                    Severity.Block, "H1_AUTO_WITH_CUSTOM_PANEL",
                    "AUTO detects built-in panels only, and a custom panel JSON is selected. " +
                    "AUTO reads the channel map out of the embedded IF_Quant_Pipeline.groovy " +
                    "and never opens IFQ_PANEL_CONFIG, so it cannot detect the custom panel and " +
                    "would silently fall back to a built-in one. Choose the custom panel key " +
                    "explicitly."));
            }

            // A custom panel key WITH a JSON that the launcher could not turn
            // into a channel list. Before v1.8.0 this fell through every H2
            // rule below -- the block was guarded on `panel != null` -- so a
            // custom panel plus zero thresholds went green and the run record
            // said THRESHOLDS_FROZEN. The engine calls failRun on the same
            // input, so this is a block, not a flag.
            if (!string.IsNullOrEmpty(request.PanelResolutionError))
            {
                result.Findings.Add(new GateFinding(
                    Severity.Block, "H1_PANEL_CONFIG_INVALID",
                    "The custom panel selected for '" + (request.PanelKey ?? "") +
                    "' could not be resolved to a channel list, so the launcher cannot know " +
                    "which channels this run measures or which of them are frozen:\r\n\r\n" +
                    request.PanelResolutionError));
            }

            // ---------------------------------------------------------
            // H2. A missing fixed threshold means per-region adaptive Otsu.
            //     IF_Quant_Pipeline.groovy:2132-2140
            //       double t = fixed ? ... : autoThresholdInRoi(..., "Otsu")
            //       chThreshSource[c.marker] = fixed ? "fixed_predeclared"
            //                                        : "adaptive_otsu_exploratory"
            //     Measured consequence: an uninfected control read 4.95% KRT5+
            //     at an Otsu threshold of 54.6, inside that channel's noise
            //     floor; a background-dominated tile can report
            //     KRT5_pod_area_frac ~0.89.
            // ---------------------------------------------------------
            // Route 4 writes no IFQ_*_THRESHOLD of its own and never will --
            // v1.7.2 wrote none. The ONLY way a channel can carry a fixed
            // cutoff on that route is the v1.7.2 Advanced box, which route 4
            // still overlays verbatim, so that is where the answer is. Parsed
            // with a null sink here: the findings are raised by the Advanced
            // pass further down, and raising them twice would double every
            // message in the run record.
            Dictionary<string, string> advancedValues =
                ParseAdvanced(request.AdvancedText, null);
            bool legacyRoute = request.Route == ImageRoute.LegacyFiji172;

            List<string> adaptive = new List<string>();
            List<string> adaptiveArea = new List<string>();
            List<string> notThresholdable = new List<string>();
            string unresolvedPanelReason = null;
            string unresolvedPolicyTag = null;

            if (panel != null)
            {
                foreach (ChannelDef channel in panel.AnalysisChannels)
                {
                    bool engineReadsIt = ThresholdSurface.EngineReads(
                        panel, channel, engineThresholdMarkers);

                    string raw;
                    if (legacyRoute)
                        advancedValues.TryGetValue(channel.ThresholdEnvName, out raw);
                    else
                        request.Thresholds.TryGetValue(channel.Token, out raw);
                    string value = (raw ?? "").Trim();

                    if (!engineReadsIt)
                    {
                        // Not a launcher failure. The engine simply has no
                        // IFQ_<TOKEN>_THRESHOLD for this marker, so the channel
                        // is adaptive by construction and no box can change it.
                        notThresholdable.Add(channel.Marker);
                        result.ThresholdPolicy.Add(ThresholdSurface.PolicyLine(
                            panel, channel, engineThresholdMarkers, value));
                        adaptive.Add(channel.Marker);
                        if (channel.AreaMarker) adaptiveArea.Add(channel.Marker);
                        if (value.Length > 0)
                            result.Findings.Add(new GateFinding(
                                // Route 4 must accept whatever v1.7.2 accepted in
                                // the Advanced box, and v1.7.2 checked shape only.
                                // A no-op variable cannot change a number, so on
                                // that route this is a warning, not a stop.
                                legacyRoute ? Severity.Warn : Severity.Block,
                                "H2_THRESHOLD_NOT_READ",
                                "A threshold was typed for " + channel.Marker + ", but the engine " +
                                "reads IFQ_<MARKER>_THRESHOLD only for a closed list of markers " +
                                "(IF_Quant_Pipeline.groovy:189-199) and " + channel.Marker +
                                " is not on it. Setting " + channel.ThresholdEnvName + " would be " +
                                "a silent no-op: the channel would still use adaptive Otsu while " +
                                "the run record claimed a fixed cutoff."));
                        continue;
                    }

                    if (value.Length == 0)
                    {
                        adaptive.Add(channel.Marker);
                        if (channel.AreaMarker) adaptiveArea.Add(channel.Marker);
                        result.ThresholdPolicy.Add(ThresholdSurface.PolicyLine(
                            panel, channel, engineThresholdMarkers, value));
                        continue;
                    }

                    double parsed;
                    if (!ThresholdSurface.IsFrozenValue(value, out parsed))
                    {
                        result.Findings.Add(new GateFinding(
                            legacyRoute ? Severity.Warn : Severity.Block,
                            "H2_THRESHOLD_INVALID",
                            "The threshold for " + channel.Marker + " ('" + value + "') is not a " +
                            "positive number. The engine parses it with parseDoubleSetting and " +
                            "would abort, or worse, a value of 0 would make every pixel a " +
                            "candidate." +
                            (legacyRoute
                                ? " On legacy route 4 this is a warning rather than a stop, " +
                                  "because v1.7.2 shape-checked the Advanced box and nothing " +
                                  "else; the engine still refuses the value, so no number can " +
                                  "come out of it."
                                : "")));
                        result.ThresholdPolicy.Add(ThresholdSurface.PolicyLine(
                            panel, channel, engineThresholdMarkers, value));
                        // An unusable value is not a frozen channel. Count it as
                        // adaptive so the run cannot be recorded THRESHOLDS_FROZEN
                        // on the strength of a value the engine will reject.
                        adaptive.Add(channel.Marker);
                        if (channel.AreaMarker) adaptiveArea.Add(channel.Marker);
                        continue;
                    }
                    result.ThresholdPolicy.Add(ThresholdSurface.PolicyLine(
                        panel, channel, engineThresholdMarkers, value));
                }
            }

            else
            {
                // ---------------------------------------------------------
                // D1. NO CHANNEL LIST.
                //
                // Before v1.8.0 the whole H2 block above was guarded on
                // `panel != null`, so this branch did not exist: a panel the
                // launcher could not resolve skipped every threshold rule,
                // Exploratory stayed false, and RunRecord wrote
                // run_classification=THRESHOLDS_FROZEN for a run in which
                // every single channel would use adaptive Otsu. A custom panel
                // key plus a custom panel JSON plus confirmatory tier plus zero
                // thresholds went GREEN.
                //
                // Not knowing which channels exist is strictly worse than
                // knowing they are unfrozen, so it is treated at least as
                // strictly as AUTO: exploratory always, hard stop wherever an
                // omitted threshold is a hard stop.
                // ---------------------------------------------------------
                if (request.PanelWasAuto)
                {
                    unresolvedPanelReason =
                        "Panel selection is AUTO, so the panel is decided per image and no " +
                        "channel threshold can be frozen before the run. Every channel will " +
                        "use per-region adaptive Otsu.";
                    unresolvedPolicyTag = "panel resolved per image by AUTO";
                }
                else
                {
                    unresolvedPanelReason =
                        "The launcher has no channel list for panel '" +
                        (request.PanelKey ?? "") + "', so it cannot show a threshold box for " +
                        "any channel and cannot know that a single one is frozen. Every " +
                        "channel will use per-region adaptive Otsu unless the engine finds an " +
                        "IFQ_<MARKER>_THRESHOLD the launcher never wrote.";
                    unresolvedPolicyTag =
                        "no channel list for panel '" + (request.PanelKey ?? "") + "'";
                }
                result.ThresholdPolicy.Add(
                    "ALL_CHANNELS=adaptive_otsu_exploratory(" + unresolvedPolicyTag + ")");
            }

            // ---------------------------------------------------------
            // N3. A PANEL WITH NO ANALYSIS CHANNEL AT ALL.
            //
            // `adaptive.Count > 0 || unresolvedPanelReason != null` is false
            // when the panel resolved fine and simply has nothing non-nuclear
            // in it: CustomPanelRegistry.Parse accepts a nuclear-only JSON
            // (the engine does too), so a DAPI-only custom panel produced a
            // GREEN confirmatory run whose record said THRESHOLDS_FROZEN --
            // beside a [threshold_policy] block reading "(no analysis channels
            // resolved)". Zero frozen channels out of zero is not "frozen"; it
            // is a run with no marker endpoint in it at all, and any aggregator
            // that greps thresholds_frozen=true would have pooled it with real
            // confirmatory runs.
            // ---------------------------------------------------------
            bool noAnalysisChannels = panel != null && panel.AnalysisChannels.Count == 0;

            // H5. Unfrozen OR unknown OR nothing to freeze. None of the three
            // may ever serialise as frozen.
            result.Exploratory =
                adaptive.Count > 0 || unresolvedPanelReason != null || noAnalysisChannels;

            if (noAnalysisChannels)
            {
                string emptyPanelReason =
                    "Panel '" + (panel.Key ?? "") + "' (" +
                    (panel.SourceDescription ?? "source unknown") +
                    ") declares no analysis channel: every channel in it is nuclear.\r\n\r\n" +
                    "There is no positivity and no area endpoint to freeze, so this run " +
                    "measures nuclei and tissue area and nothing else. Zero frozen channels " +
                    "out of zero is not a frozen run, and recording it as one would let it be " +
                    "pooled with runs whose cutoffs came from stained controls.";
                if (request.Tier == RunTier.Confirmatory)
                {
                    result.Findings.Add(new GateFinding(
                        Severity.Block, "H2_NO_ANALYSIS_CHANNELS_IN_CONFIRMATORY",
                        "Confirmatory tier needs every analysis channel frozen against stained " +
                        "controls, and this panel has no analysis channel to freeze.\r\n\r\n" +
                        emptyPanelReason));
                }
                else if (!spec.ThresholdsMayBeOmitted)
                {
                    result.Findings.Add(new GateFinding(
                        Severity.Block, "H2_NO_ANALYSIS_CHANNELS_ON_WHOLE_SLIDE",
                        "This route reconciles hundreds of tiles into one slide-level number, " +
                        "and this panel produces no marker number to reconcile.\r\n\r\n" +
                        emptyPanelReason));
                }
                else
                {
                    result.RequirePhrase(ExploratoryPhrase);
                    result.Findings.Add(new GateFinding(
                        Severity.Confirm, "H2_NO_ANALYSIS_CHANNELS",
                        emptyPanelReason, ExploratoryStamp));
                }
            }

            if (unresolvedPanelReason != null)
            {
                bool auto = request.PanelWasAuto;
                if (request.Tier == RunTier.Confirmatory)
                {
                    result.Findings.Add(new GateFinding(
                        Severity.Block,
                        auto ? "H2_AUTO_IN_CONFIRMATORY" : "H2_PANEL_UNRESOLVED_IN_CONFIRMATORY",
                        "Confirmatory tier needs every analysis channel frozen against stained " +
                        "controls, and this run cannot name its analysis channels at all.\r\n\r\n" +
                        unresolvedPanelReason));
                }
                else if (!spec.ThresholdsMayBeOmitted)
                {
                    result.Findings.Add(new GateFinding(
                        Severity.Block,
                        auto ? "H2_AUTO_ON_WHOLE_SLIDE" : "H2_PANEL_UNRESOLVED_ON_WHOLE_SLIDE",
                        "This route measures hundreds of tiles and chooses the threshold per " +
                        "region, so an unfrozen channel does not give an uncertain endpoint, it " +
                        "gives a wrong one. The launcher cannot even list this run's " +
                        "channels.\r\n\r\n" + unresolvedPanelReason));
                }
                else
                {
                    result.RequirePhrase(ExploratoryPhrase);
                    result.Findings.Add(new GateFinding(
                        Severity.Confirm,
                        auto ? "H2_AUTO_ADAPTIVE" : "H2_PANEL_UNRESOLVED",
                        unresolvedPanelReason + "\r\n\r\nThe run is usable for looking, never " +
                        "for reporting.",
                        ExploratoryStamp));
                }
            }

            if (adaptive.Count > 0)
            {
                string channelList = string.Join(", ", adaptive.ToArray());
                string areaNote = adaptiveArea.Count > 0
                    ? "\r\n\r\n" + string.Join(", ", adaptiveArea.ToArray()) +
                      " contribute AREA endpoints. Adaptive Otsu on an area endpoint is the " +
                      "failure that has already happened here: an uninfected control read 4.95% " +
                      "KRT5+ at an Otsu threshold of 54.6, inside that channel's noise floor, " +
                      "and a background-dominated tile can report KRT5_pod_area_frac ~0.89."
                    : "";
                string unavailableNote = notThresholdable.Count > 0
                    ? "\r\n\r\nNo IFQ_<MARKER>_THRESHOLD variable exists for: " +
                      string.Join(", ", notThresholdable.ToArray()) +
                      ". Those channels cannot be frozen from this launcher at all."
                    : "";

                if (legacyRoute)
                {
                    // ---------------------------------------------------------
                    // D2. Route 4 used to skip H2 entirely, so Exploratory
                    // stayed false and the run record emitted
                    // run_classification=THRESHOLDS_FROZEN and
                    // thresholds_frozen=true -- contradicting its own
                    // threshold_policy block two lines later, and pooling
                    // unfrozen legacy runs with frozen ones in any aggregator
                    // that greps that field.
                    //
                    // The ENVIRONMENT is what legacy fidelity constrains, and
                    // it is untouched: route 4 still writes no
                    // IFQ_*_THRESHOLD. What changes is the launcher's own
                    // record, its folder name and its marker file, none of
                    // which the engine ever sees.
                    // ---------------------------------------------------------
                    result.RequirePhrase(ExploratoryPhrase);
                    result.Findings.Add(new GateFinding(
                        Severity.Confirm, "H2_LEGACY_ADAPTIVE",
                        "Legacy mode reproduces the v1.7.2 environment, and v1.7.2 wrote no " +
                        "IFQ_*_THRESHOLD, so a channel is frozen here only if the Advanced box " +
                        "sets its variable. These are not: " + channelList + ".\r\n" +
                        "They will use per-region adaptive Otsu, which the engine records as " +
                        "'adaptive_otsu_exploratory'. That is exactly what the original v1.7.2 " +
                        "run did -- which is the point of this mode, and the reason its output " +
                        "must not be pooled with frozen runs." + areaNote + unavailableNote,
                        ExploratoryStamp));
                }
                else if (request.Tier == RunTier.Confirmatory)
                {
                    result.Findings.Add(new GateFinding(
                        Severity.Block, "H2_ADAPTIVE_IN_CONFIRMATORY",
                        "Confirmatory tier: every analysis channel must carry a fixed, " +
                        "control-derived threshold. These do not: " + channelList +
                        areaNote + unavailableNote));
                }
                else if (!spec.ThresholdsMayBeOmitted)
                {
                    result.Findings.Add(new GateFinding(
                        Severity.Block, "H2_ADAPTIVE_ON_WHOLE_SLIDE",
                        "Whole-slide runs measure hundreds of tiles, and the threshold is chosen " +
                        "per region. Tiles that are mostly background get a threshold from " +
                        "background. That does not give an uncertain endpoint, it gives a wrong " +
                        "one. No fixed threshold for: " + channelList + areaNote + unavailableNote));
                }
                else
                {
                    result.RequirePhrase(ExploratoryPhrase);
                    result.Findings.Add(new GateFinding(
                        Severity.Confirm, "H2_ADAPTIVE_EXPLORATORY",
                        "No fixed threshold for: " + channelList + ".\r\n" +
                        "Those channels will use per-region adaptive Otsu, which the engine " +
                        "itself records as 'adaptive_otsu_exploratory'. The run is usable for " +
                        "looking, never for reporting." + areaNote + unavailableNote,
                        ExploratoryStamp));
                }
            }

            // ---------------------------------------------------------
            // H3. IFQ_MIN_INCLUDED_NUCLEI defaults to 1
            //     IF_Quant_Pipeline.groovy:329  envInt(..., 1)
            //     A region below the floor throws inside the region loop and
            //     kills the whole image: its tissue area AND its numerator
            //     vanish from run_summary.csv. Pooled fractions then quietly
            //     lose their sparse regions from the denominator.
            // ---------------------------------------------------------
            if (spec.WritesMinIncludedNuclei)
            {
                if (request.MinIncludedNuclei < 0)
                {
                    result.Findings.Add(new GateFinding(
                        Severity.Block, "H3_NEGATIVE",
                        "Minimum nuclei per region must be zero or a positive integer; the " +
                        "engine rejects anything else (IF_Quant_Pipeline.groovy:395)."));
                }
                else if (request.MinIncludedNuclei > 0)
                {
                    bool hasAreaEndpoint = panel != null && panel.AreaMarkers.Count > 0;
                    if (!spec.ThresholdsMayBeOmitted)
                    {
                        result.Findings.Add(new GateFinding(
                            Severity.Block, "H3_FLOOR_ON_WHOLE_SLIDE",
                            "Minimum nuclei per region is " + request.MinIncludedNuclei +
                            ". On a tiled slide the sparse tiles are exactly the edge and " +
                            "alveolar tiles, and dropping them removes their tissue area from " +
                            "the slide total while their neighbours keep theirs. Set it to 0."));
                    }
                    else if (hasAreaEndpoint)
                    {
                        result.Findings.Add(new GateFinding(
                            Severity.Block, "H3_FLOOR_WITH_AREA_ENDPOINT",
                            "Minimum nuclei per region is " + request.MinIncludedNuclei +
                            ", and this panel has area endpoints (" +
                            DescribeMarkers(panel.AreaMarkers) + "). A dropped region takes its " +
                            "tissue area out of the denominator but leaves the other regions' " +
                            "numerators in, so the reported fraction rises for a reason that has " +
                            "nothing to do with biology. Set it to 0."));
                    }
                    else
                    {
                        result.Findings.Add(new GateFinding(
                            Severity.Warn, "H3_FLOOR_SET",
                            "Minimum nuclei per region is " + request.MinIncludedNuclei +
                            ". Regions with fewer accepted nuclei are dropped, and their tissue " +
                            "area is dropped with them."));
                    }
                }
            }
            else if (legacyRoute)
            {
                // D3. The floor still exists on route 4; it just comes from the
                // Advanced box, which is exactly where v1.7.2 users set it. Say
                // which number will actually apply rather than always claiming
                // the engine default.
                string legacyFloor;
                advancedValues.TryGetValue("IFQ_MIN_INCLUDED_NUCLEI", out legacyFloor);
                legacyFloor = (legacyFloor ?? "").Trim();
                result.LegacyMinIncludedNuclei = legacyFloor.Length > 0
                    ? legacyFloor + " (from the Advanced box, as in v1.7.2)"
                    : "1 (engine default; neither v1.7.2 nor legacy mode writes the variable)";
                if (legacyFloor.Length == 0)
                {
                    result.Findings.Add(new GateFinding(
                        Severity.Note, "H3_LEGACY_ENGINE_DEFAULT",
                        "Legacy mode does not write IFQ_MIN_INCLUDED_NUCLEI, because v1.7.2 never " +
                        "wrote it. The engine default of 1 therefore applies -- which is exactly " +
                        "what the original run did, and is the point of this mode."));
                }
                else
                {
                    result.Findings.Add(new GateFinding(
                        Severity.Warn, "H3_LEGACY_ADVANCED_FLOOR",
                        "IFQ_MIN_INCLUDED_NUCLEI=" + legacyFloor + " comes from the Advanced box, " +
                        "which is how v1.7.2 set it and is therefore accepted here. Regions with " +
                        "fewer accepted nuclei are DROPPED and their tissue area is dropped with " +
                        "them, so a pooled fraction loses its sparse regions from the " +
                        "denominator while the other regions keep their numerators."));
                }
            }

            // ---------------------------------------------------------
            // H4. IFQ_OUTPUT_DIR must be empty or the engine aborts.
            //     IF_Quant_Pipeline.groovy:3479-3484
            // ---------------------------------------------------------
            if (request.OutputDirectoryExistsAndIsNonEmpty)
            {
                result.Findings.Add(new GateFinding(
                    Severity.Block, "H4_OUTPUT_NOT_EMPTY",
                    "The run output folder already contains files. The engine aborts on a " +
                    "non-empty IFQ_OUTPUT_DIR, because stale masks and cell tables from an " +
                    "earlier run would otherwise be mixed into this one. The launcher always " +
                    "creates a fresh timestamped folder, so seeing this means something else " +
                    "wrote into it."));
            }
            if (string.IsNullOrEmpty(request.OutputBase))
            {
                result.Findings.Add(new GateFinding(
                    Severity.Block, "H4_OUTPUT_BASE_UNSET",
                    "Choose the output parent folder. A timestamped run folder is created " +
                    "inside it."));
            }

            // ---------------------------------------------------------
            // Route-2 specific pre-flight
            // ---------------------------------------------------------
            if (request.Route == ImageRoute.IfSlideScanner)
            {
                if (string.IsNullOrEmpty(request.WsiInput))
                    result.Findings.Add(new GateFinding(
                        Severity.Block, "WSI_INPUT_UNSET",
                        "Choose the .vsi slide file, or a folder containing them."));
                if (string.IsNullOrEmpty(request.WsiOutput))
                    result.Findings.Add(new GateFinding(
                        Severity.Block, "WSI_OUTPUT_UNSET",
                        "Choose the stage 1 output root. Tiles, ROI sets and " +
                        "stage1_manifest.json are written there and are read back by stages 2 " +
                        "and 3."));
                if (request.PanelWasAuto)
                    result.Findings.Add(new GateFinding(
                        Severity.Block, "WSI_AUTO_PANEL",
                        "AUTO panel detection is not offered on the whole-slide route. Stage 1 " +
                        "writes the panel into every tile's samplesheet row before Fiji sees a " +
                        "tile, and 'panel' is a grouping key downstream -- two panels for one " +
                        "animal silently split it into two rows."));
                if (string.IsNullOrEmpty(request.SlideMetadataCsv))
                    result.Findings.Add(new GateFinding(
                        Severity.Warn, "WSI_NO_SLIDE_METADATA",
                        "No slide metadata CSV. Stage 1 will not be able to stamp mouse_id, " +
                        "genotype and condition into the tile samplesheet, and the mouse-level " +
                        "aggregation would have to be done by hand later."));
            }

            // ---------------------------------------------------------
            // Advanced free-text keys (v1.7.2 checked shape only)
            // ---------------------------------------------------------
            // D3. On route 4 the v1.7.2 rule IS the contract. v1.7.2's
            // ParseAdvancedEnvironment accepted ANY key matching
            // ^IFQ_[A-Z0-9_]+$ with a non-empty value that was not one of its
            // NINETEEN ProtectedEnvironmentKeys, and handed it to Fiji. Under
            // v1.7.2 the Advanced box was also the ONLY way to set the sparse-
            // region nuclei floor at all.
            //
            // v1.8.0 ran the full gate on every route, so route 4 blocked
            // IFQ_MIN_INCLUDED_NUCLEI as ADV_PROTECTED (it is one of the four
            // keys v1.8.0 newly took ownership of) and blocked anything outside
            // the EnvSurface tables as ADV_UNKNOWN_KEY. Verified: route 4 plus
            // Advanced IFQ_MIN_INCLUDED_NUCLEI=3 gave runButton.Enabled=False,
            // where v1.7.2 emitted 20 variables and ran. That is not legacy.
            //
            // So on route 4 the four SHAPE rules in ParseAdvanced still block
            // (v1.7.2 threw on all four), the NINETEEN v1.7.2-protected keys
            // still block, and every judgement v1.8.0 added is a warning. The
            // warnings are kept, and surfaced in the run record, because a
            // no-op variable is still worth saying out loud.
            HashSet<string> panelTokens = panel == null ? null : panel.Tokens;
            Severity legacyAdvisory = legacyRoute ? Severity.Warn : Severity.Block;
            foreach (KeyValuePair<string, string> item in ParseAdvanced(request.AdvancedText, result))
            {
                bool protectedByLauncher = EnvSurface.ProtectedKeys.Contains(item.Key);
                bool protectedByV172 = LegacyProfile.ProtectedKeys.Contains(item.Key);
                if (protectedByLauncher)
                {
                    if (!legacyRoute || protectedByV172)
                    {
                        result.Findings.Add(new GateFinding(
                            Severity.Block, "ADV_PROTECTED",
                            item.Key + " is controlled by the launcher interface and cannot be " +
                            "overridden in Advanced settings."));
                        continue;
                    }
                    result.Findings.Add(new GateFinding(
                        Severity.Warn, "ADV_PROTECTED_LEGACY_EXEMPT",
                        item.Key + " is controlled by the launcher interface on routes 1 and 2, " +
                        "but v1.7.2 did not protect it and the Advanced box was the only way to " +
                        "set it. Legacy mode therefore accepts it and passes it to Fiji exactly " +
                        "as v1.7.2 did. Value: " + item.Value));
                    continue;
                }

                // ---------------------------------------------------------
                // N1. THE THRESHOLD GRID OWNS IFQ_<CHANNEL>_THRESHOLD.
                //
                // EnvSurface.Classify returns MarkerFamily for a threshold
                // variable whose token IS a channel of the selected panel, and
                // MarkerFamily emitted no finding at all -- so the Advanced box
                // could set the very variable the grid had just validated, and
                // RunEnvironment.BuildStage2 applies the Advanced overlay LAST.
                // Verified: panel LEFT, every grid box 500, confirmatory tier,
                // Advanced "IFQ_KRT5_THRESHOLD=0" gave a green bar, "3/3
                // thresholds fixed", run_classification=THRESHOLDS_FROZEN and a
                // policy line "KRT5=fixed_predeclared(500)" -- while the engine
                // received IFQ_KRT5_THRESHOLD=0, which makes every pixel a
                // candidate. The grid path rejects <=0 as H2_THRESHOLD_INVALID;
                // the Advanced path for the SAME variable was unchecked, which
                // made routes 1 and 2 LESS strict than legacy mode on identical
                // input.
                //
                // One value, one control. Route 4 is exempt because v1.7.2 had
                // no grid and its Advanced box was the only way to set a cutoff
                // at all; the launch seal still refuses to start a route 4 run
                // whose record and environment disagree.
                // ---------------------------------------------------------
                ChannelDef overridden =
                    ThresholdSurface.ChannelForThresholdName(panel, item.Key);
                if (!legacyRoute && overridden != null)
                {
                    result.Findings.Add(new GateFinding(
                        Severity.Block, "ADV_THRESHOLD_OVERRIDE",
                        item.Key + " is the fixed cutoff for " + overridden.Marker +
                        ", which is an analysis channel of panel " + panel.Key +
                        " and therefore has its own validated box in the threshold grid " +
                        "above. Setting it here would overwrite the value the launcher " +
                        "checked and recorded, without being checked itself: the run record " +
                        "would state one cutoff and the engine would receive another. Type " +
                        "the value into the " + overridden.Marker + " box instead."));
                    continue;
                }

                EnvClassification cls = EnvSurface.Classify(item.Key, panelTokens);
                if (cls.Kind == EnvKind.Unknown)
                    result.Findings.Add(new GateFinding(
                        legacyAdvisory, "ADV_UNKNOWN_KEY",
                        item.Key + " is " + cls.Detail));
                else if (cls.Kind == EnvKind.UnknownMarker)
                    result.Findings.Add(new GateFinding(
                        legacyAdvisory, "ADV_UNKNOWN_MARKER",
                        item.Key + ": " + cls.Detail));
                else if (cls.Kind == EnvKind.Stage1Static &&
                         request.Route != ImageRoute.IfSlideScanner)
                    result.Findings.Add(new GateFinding(
                        legacyAdvisory, "ADV_STAGE1_ON_FIJI_ROUTE",
                        item.Key + " is a stage 1 (QuPath) setting and this route never runs " +
                        "stage 1, so it would have no effect."));
            }

            // ---------------------------------------------------------
            // v1.7.2 behaviours that are kept
            // ---------------------------------------------------------
            if (panel != null && HasChannel(panel, "YAP") &&
                !string.Equals(request.Projection, "single", StringComparison.OrdinalIgnoreCase) &&
                !string.Equals(request.Projection, "layer_aware", StringComparison.OrdinalIgnoreCase))
                result.Findings.Add(new GateFinding(
                    Severity.Block, "YAP_NEEDS_SINGLE_PLANE",
                    "This panel contains YAP nuclear-to-cytoplasmic analysis, which requires " +
                    "Z-stack handling = single or layer_aware. A maximum-intensity projection " +
                    "corrupts the ratio."));

            if (panel != null && HasChannel(panel, "AcTub") &&
                !string.Equals(request.WholeFieldCompartment, "airway",
                               StringComparison.OrdinalIgnoreCase))
                result.Findings.Add(new GateFinding(
                    Severity.Warn, "ACTUB_NO_AIRWAY_ROI",
                    "Without an independently assigned airway ROI, only nuclei meeting the " +
                    "strict bright/locally dense/size-bounded apical ciliary rule can be " +
                    "exploratory positive. All other AcTub calls stay indeterminate, not " +
                    "negative."));

            if (request.Tier == RunTier.Dry)
                result.Findings.Add(new GateFinding(
                    Severity.Warn, "TIER_DRY",
                    "Dry tier: this is a smoke test. Nothing it produces may be aggregated."));

            // A Block outranks a Confirm: if the run cannot start, no phrase
            // should be requested, or the dialog implies it can be unlocked.
            if (result.Blocked)
                result.RequiredPhrases.Clear();

            return result;
        }

        private static bool HasChannel(PanelDef panel, string marker)
        {
            string token = PanelRegistry.NormalizeMarkerToken(marker);
            foreach (ChannelDef c in panel.Channels)
                if (string.Equals(c.Token, token, StringComparison.Ordinal)) return true;
            return false;
        }

        private static string DescribeMarkers(List<ChannelDef> channels)
        {
            List<string> names = new List<string>();
            foreach (ChannelDef c in channels) names.Add(c.Marker);
            return string.Join(", ", names.ToArray());
        }

        /// Shape-parses the Advanced box. Shape problems become Block findings
        /// rather than exceptions so the live gate summary can show them while
        /// the user is still typing.
        public static Dictionary<string, string> ParseAdvanced(string text, GateResult sink)
        {
            Dictionary<string, string> values =
                new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            string[] lines = (text ?? "").Replace("\r\n", "\n").Replace('\r', '\n').Split('\n');
            for (int index = 0; index < lines.Length; index++)
            {
                string line = lines[index].Trim();
                if (line.Length == 0 || line.StartsWith("#", StringComparison.Ordinal)) continue;
                int equals = line.IndexOf('=');
                if (equals <= 0)
                {
                    if (sink != null)
                        sink.Findings.Add(new GateFinding(
                            Severity.Block, "ADV_SHAPE",
                            "Advanced setting line " + (index + 1) + " must use KEY=VALUE."));
                    continue;
                }
                string key = line.Substring(0, equals).Trim().ToUpperInvariant();
                string value = line.Substring(equals + 1).Trim();
                if (!Regex.IsMatch(key, "^IFQ_[A-Z0-9_]+$"))
                {
                    if (sink != null)
                        sink.Findings.Add(new GateFinding(
                            Severity.Block, "ADV_KEY_SHAPE",
                            "Advanced setting line " + (index + 1) +
                            " has an invalid IFQ key: " + key));
                    continue;
                }
                if (value.Length == 0)
                {
                    if (sink != null)
                        sink.Findings.Add(new GateFinding(
                            Severity.Block, "ADV_EMPTY_VALUE",
                            key + " has an empty value. The engine treats an empty variable as " +
                            "absent and falls back silently, so this line would look like " +
                            "configuration and do nothing."));
                    continue;
                }
                values[key] = value;
            }
            return values;
        }
    }

    // =================================================================
    // 6. TOOL INVENTORY
    // =================================================================

    /// <summary>
    /// What is actually installed. Resolution only; no process is started here.
    /// </summary>
    internal sealed class ToolInventory
    {
        public string FijiExecutable;
        public string FijiDirectory;
        public string JavaExecutable;
        public string Ij1PatcherJar;
        public string WindowsArchitecture;
        public string QuPathExecutable;
        public string PythonExecutable;

        public bool FijiPresent { get { return !string.IsNullOrEmpty(FijiExecutable); } }
        public bool BundledJvmPresent
        {
            get
            {
                return !string.IsNullOrEmpty(JavaExecutable) &&
                       !string.IsNullOrEmpty(Ij1PatcherJar);
            }
        }
        public bool QuPathPresent { get { return !string.IsNullOrEmpty(QuPathExecutable); } }
        public bool PythonPresent { get { return !string.IsNullOrEmpty(PythonExecutable); } }

        public static ToolInventory Resolve(
            string fijiPath, string quPathPath, string pythonPath, string windowsArchitecture)
        {
            ToolInventory tools = new ToolInventory();
            tools.WindowsArchitecture = windowsArchitecture;
            tools.FijiExecutable = ResolveFiji(fijiPath, windowsArchitecture);
            tools.FijiDirectory = ResolveFijiDirectory(fijiPath, tools.FijiExecutable);
            if (tools.FijiDirectory != null)
            {
                tools.JavaExecutable = FindFirst(
                    Path.Combine(tools.FijiDirectory, "java"), "java.exe");
                tools.Ij1PatcherJar = FindFirst(
                    Path.Combine(tools.FijiDirectory, "jars"), "ij1-patcher-*.jar");
            }
            tools.QuPathExecutable = ResolveQuPath(quPathPath);
            tools.PythonExecutable = ResolvePython(pythonPath);
            return tools;
        }

        private static string ResolveFijiDirectory(string fijiPath, string resolvedExe)
        {
            if (!string.IsNullOrEmpty(fijiPath) && Directory.Exists(fijiPath))
                return Path.GetFullPath(fijiPath);
            if (!string.IsNullOrEmpty(resolvedExe))
                return Path.GetDirectoryName(Path.GetFullPath(resolvedExe));
            return null;
        }

        /// Identical preference order to v1.7.2's ResolveFijiExecutable.
        public static string ResolveFiji(string path, string architecture)
        {
            if (string.IsNullOrEmpty(path)) return null;
            if (File.Exists(path) && path.EndsWith(".exe", StringComparison.OrdinalIgnoreCase))
                return Path.GetFullPath(path);
            if (!Directory.Exists(path)) return null;

            string[] preferred;
            if (string.Equals(architecture, "ARM64", StringComparison.OrdinalIgnoreCase))
                preferred = new string[]
                {
                    "fiji-windows-arm64.exe", "fiji-windows-x64.exe",
                    "ImageJ-win64.exe", "fiji-windows.exe", "ImageJ.exe"
                };
            else
                preferred = new string[]
                {
                    "fiji-windows-x64.exe", "ImageJ-win64.exe",
                    "fiji-windows.exe", "ImageJ.exe", "fiji-windows-arm64.exe"
                };
            foreach (string name in preferred)
            {
                string candidate = Path.Combine(path, name);
                if (File.Exists(candidate)) return candidate;
            }
            string[] executables = Directory.GetFiles(path, "*.exe", SearchOption.TopDirectoryOnly);
            Array.Sort(executables, StringComparer.OrdinalIgnoreCase);
            foreach (string candidate in executables)
            {
                string name = Path.GetFileName(candidate);
                if (name.IndexOf("fiji", StringComparison.OrdinalIgnoreCase) >= 0 ||
                    name.IndexOf("imagej", StringComparison.OrdinalIgnoreCase) >= 0)
                    return candidate;
            }
            return null;
        }

        public static string ResolveQuPath(string path)
        {
            if (string.IsNullOrEmpty(path)) return null;
            if (File.Exists(path) && path.EndsWith(".exe", StringComparison.OrdinalIgnoreCase))
                return Path.GetFullPath(path);
            if (!Directory.Exists(path)) return null;
            // The console build is required: the windowed .exe detaches and the
            // launcher would see an immediate exit 0 with no script output.
            string[] executables = Directory.GetFiles(path, "*.exe", SearchOption.TopDirectoryOnly);
            Array.Sort(executables, StringComparer.OrdinalIgnoreCase);
            foreach (string candidate in executables)
            {
                string name = Path.GetFileName(candidate);
                if (name.IndexOf("qupath", StringComparison.OrdinalIgnoreCase) >= 0 &&
                    name.IndexOf("console", StringComparison.OrdinalIgnoreCase) >= 0)
                    return candidate;
            }
            return null;
        }

        public static string ResolvePython(string path)
        {
            if (string.IsNullOrEmpty(path)) return null;
            if (File.Exists(path)) return Path.GetFullPath(path);
            return null;
        }

        private static string FindFirst(string root, string pattern)
        {
            try
            {
                if (!Directory.Exists(root)) return null;
                string[] hits = Directory.GetFiles(root, pattern, SearchOption.AllDirectories);
                if (hits.Length == 0) return null;
                Array.Sort(hits, StringComparer.OrdinalIgnoreCase);
                return hits[0];
            }
            catch { return null; }
        }
    }

    // =================================================================
    // 7. FIJI COMMAND LINES
    // =================================================================

    /// <summary>
    /// The single place where a run environment is handed to a child process.
    ///
    /// v1.7.2 did this inline in StartFijiRun: strip every inherited IFQ_*,
    /// then copy ours in. It is factored out here for one reason -- the legacy
    /// equivalence harness runs THIS code, so what it proves is what the
    /// launcher actually does, not what a copy of it does.
    ///
    /// v1.8.0: Apply no longer accepts a Dictionary. It accepts a RunSeal, and
    /// a RunSeal cannot be constructed -- only issued by RunSeal.Issue, which
    /// validates the merged environment. That is the whole point: the previous
    /// two defect rounds were both "a path into the child environment that did
    /// not pass validation", and no amount of validating INPUTS fixed that.
    /// This validates the OUTPUT, and the type system makes it unskippable.
    /// </summary>
    internal static class EnvironmentApply
    {
        /// <summary>
        /// Windows environment blocks can legally contain duplicate names that
        /// differ only by case (for example Path and PATH). .NET Framework's
        /// ProcessStartInfo.EnvironmentVariables materializes them into a
        /// case-insensitive StringDictionary and throws before a child can be
        /// started. Normalize only duplicate spellings in the current process;
        /// values and all non-duplicate keys are preserved.
        /// </summary>
        public static int NormalizeDuplicateKeyCasing()
        {
            System.Collections.IDictionary raw = Environment.GetEnvironmentVariables();
            Dictionary<string, List<string>> spellings =
                new Dictionary<string, List<string>>(StringComparer.OrdinalIgnoreCase);
            Dictionary<string, string> values =
                new Dictionary<string, string>(StringComparer.Ordinal);

            foreach (System.Collections.DictionaryEntry entry in raw)
            {
                string key = entry.Key == null ? null : entry.Key.ToString();
                if (string.IsNullOrEmpty(key)) continue;
                string value = entry.Value == null ? "" : entry.Value.ToString();
                List<string> keys;
                if (!spellings.TryGetValue(key, out keys))
                {
                    keys = new List<string>();
                    spellings[key] = keys;
                }
                keys.Add(key);
                values[key] = value;
            }

            int normalized = 0;
            foreach (KeyValuePair<string, List<string>> group in spellings)
            {
                if (group.Value.Count < 2) continue;
                string canonical = group.Value[0];
                foreach (string spelling in group.Value)
                {
                    if (string.Equals(spelling, "Path", StringComparison.Ordinal))
                    {
                        canonical = spelling;
                        break;
                    }
                }
                string value = values[canonical];
                foreach (string spelling in group.Value)
                    Environment.SetEnvironmentVariable(spelling, null);
                Environment.SetEnvironmentVariable(canonical, value);
                normalized++;
            }
            return normalized;
        }

        /// v1.7.2's ClearIfqEnvironment, verbatim in behaviour: a stale IFQ_*
        /// inherited from the shell must never reach the engine, because the
        /// engine reads every one of them and falls back silently.
        public static void ClearIfq(System.Collections.Specialized.StringDictionary environment)
        {
            List<string> stale = new List<string>();
            foreach (string key in environment.Keys)
                if (key != null && key.StartsWith("IFQ_", StringComparison.OrdinalIgnoreCase))
                    stale.Add(key);
            foreach (string key in stale)
                environment.Remove(key);
        }

        /// <summary>
        /// The ONE statement in this program that writes a variable into a child
        /// process's environment. build.ps1 scans the three shipping sources and
        /// discards the binary if a second one appears, because a second one is
        /// exactly how rounds 1 and 2 happened.
        /// </summary>
        public static void Apply(
            System.Diagnostics.ProcessStartInfo psi, RunSeal seal)
        {
            if (psi == null) throw new ArgumentNullException("psi");
            if (seal == null)
                throw new ArgumentNullException(
                    "seal",
                    "A run environment may only reach a child process through a RunSeal, " +
                    "which is issued by RunSeal.Issue after the merged environment has been " +
                    "re-checked against the run record. The run was not started.");
            NormalizeDuplicateKeyCasing();
            ClearIfq(psi.EnvironmentVariables);
            foreach (KeyValuePair<string, string> item in seal.Variables())
                psi.EnvironmentVariables[item.Key] = item.Value;
        }
    }

    internal static class FijiCommand
    {
        public static string Quote(string value)
        {
            return "\"" + (value ?? "").Replace("\"", "\\\"") + "\"";
        }

        /// v1.7.2, verbatim (IFQuantLauncher.cs StartFijiRun):
        ///   psi.Arguments = "--headless --console --run " + QuoteArgument(script)
        /// Route 4 uses this and only this.
        public static string LauncherExeArguments(string scriptPath)
        {
            return "--headless --console --run " + Quote(scriptPath);
        }

        /// The bundled JVM, argument-for-argument as scripts/Invoke-Stage2Sharded.ps1
        /// starts it. The ij1-patcher javaagent is what makes headless ImageJ1
        /// work; without it the engine throws on the first IJ call.
        public static string BundledJvmArguments(
            string fijiDirectory, string ij1PatcherJar, string scriptPath, string maxHeap)
        {
            string classPath =
                Path.Combine(fijiDirectory, "jars\\*") + ";" +
                Path.Combine(fijiDirectory, "plugins\\*");
            StringBuilder args = new StringBuilder();
            args.Append("--add-opens=java.base/java.lang=ALL-UNNAMED");
            args.Append(" -javaagent:").Append(Quote(ij1PatcherJar + "=init"));
            args.Append(" -Djava.awt.headless=true");
            args.Append(" -Dplugins.dir=").Append(Quote(fijiDirectory));
            args.Append(" -Xmx").Append(string.IsNullOrEmpty(maxHeap) ? "8g" : maxHeap);
            args.Append(" -cp ").Append(Quote(classPath));
            args.Append(" net.imagej.Main --headless --run ").Append(Quote(scriptPath));
            return args.ToString();
        }
    }

    // =================================================================
    // 8. ENVIRONMENT BUILDERS (routes 1 and 2)
    // =================================================================

    internal static class RunEnvironment
    {
        /// <summary>
        /// The stage 2 (Fiji) environment for routes 1 and 2.
        /// Route 4 does NOT come through here: see LegacyProfile.
        /// </summary>
        public static Dictionary<string, string> BuildStage2(
            RunRequest request, PanelDef panel, HashSet<string> engineThresholdMarkers,
            string registryPath, string outputDirectory, string inputDirectory,
            string autoPanelMapPath, bool previewOnly)
        {
            // R3 fail-closed. Reaching this with route 3 means the UI veto and
            // the gate were both bypassed programmatically. Throw before a
            // single variable is assigned, so no partial environment can escape
            // and no process can be started from it.
            if (request.Route == ImageRoute.HeBrightfield)
            {
                if (!LauncherBuild.BrightfieldRouteEnabled)
                    throw new InvalidOperationException(
                        "Route 3 (H&E / brightfield) is not available in this build and no run " +
                        "environment will be produced for it.\r\n\r\n" +
                        LauncherBuild.BrightfieldDisabledReason);
                // The flag is on but nothing has been wired to it yet. Refuse
                // here rather than fall through and emit a fluorescence
                // environment for a brightfield slide.
                throw new NotImplementedException(
                    "LauncherBuild.BrightfieldRouteEnabled is true, but no brightfield " +
                    "measurement module is wired into RunEnvironment.BuildStage2. Build the " +
                    "brightfield environment here before shipping a build with the flag on.");
            }
            if (request.Route == ImageRoute.LegacyFiji172)
                throw new InvalidOperationException(
                    "Route 4 must be built by LegacyProfile.BuildEnvironment, which is a " +
                    "transcription of v1.7.2. Building it here would add variables v1.7.2 " +
                    "never wrote and the run would no longer be legacy.");

            // Anything else the catalog will not vouch for. This is what caught
            // an undefined ImageRoute value, which used to fall through
            // RouteCatalog.Describe with Available=true and get a complete
            // 21-variable fluorescence environment built for it.
            RouteSpec buildSpec = RouteCatalog.Describe(request.Route);
            if (!buildSpec.Available)
                throw new InvalidOperationException(
                    "No run environment will be produced for " + buildSpec.DisplayName +
                    ".\r\n\r\n" + buildSpec.UnavailableReason);

            // H1. IFQ_PANEL is written first and is never optional.
            string panelKey = panel != null ? panel.Key : (request.PanelKey ?? "");
            if (panelKey.Trim().Length == 0)
                throw new InvalidOperationException(
                    "Refusing to build a run environment without IFQ_PANEL. The engine's " +
                    "fallback is panel T (IF_Quant_Pipeline.groovy:154), which measures an " +
                    "unrelated skin sample's channel map and reports numbers for it.");

            Dictionary<string, string> env =
                new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

            env["IFQ_INPUT_DIR"] = Path.GetFullPath(inputDirectory);
            env["IFQ_OUTPUT_DIR"] = outputDirectory;
            env["IFQ_PANEL"] = panelKey;
            env["IFQ_MARKER_REGISTRY"] = registryPath;
            if (!string.IsNullOrEmpty(autoPanelMapPath))
                env["IFQ_PANEL_MAP_PATH"] = autoPanelMapPath;
            if (!string.IsNullOrEmpty(request.PanelConfigJson))
                env["IFQ_PANEL_CONFIG"] = Path.GetFullPath(request.PanelConfigJson);
            env["IFQ_RECURSIVE"] = request.Recursive ? "true" : "false";
            env["IFQ_INCLUDE_REGEX"] =
                string.IsNullOrEmpty(request.IncludeRegex) ? ".*" : request.IncludeRegex;
            env["IFQ_MAX_IMAGES"] = request.MaxImages.ToString(CultureInfo.InvariantCulture);
            env["IFQ_SEGMENTER"] = request.Segmenter;
            env["IFQ_PROJECTION"] = request.Projection;
            env["IFQ_SINGLE_PLANE"] = request.SinglePlane.ToString(CultureInfo.InvariantCulture);
            env["IFQ_EXPORT_DISPLAY_CHANNELS"] = "true";
            env["IFQ_DISPLAY_PREVIEW_ONLY"] = previewOnly ? "true" : "false";
            env["IFQ_DISPLAY_SCALE_BAR_UM"] = "100";
            env["IFQ_DISPLAY_SCALE_BAR_THICKNESS_PX"] = "6";
            env["IFQ_TISSUE_MODE"] = request.TissueMode;
            env["IFQ_COMPARTMENT_MODE"] = request.CompartmentMode;
            env["IFQ_WHOLE_FIELD_COMPARTMENT"] = request.WholeFieldCompartment;
            // H4. Never settable from the UI or the Advanced box; a non-empty
            // output folder must abort the engine rather than be merged into.
            env["IFQ_ALLOW_NONEMPTY_OUTPUT"] = "false";
            env["IFQ_MORPHOLOGY_PRIMARY"] = "true";

            // H3. Written explicitly on every route that is allowed to write it,
            // so the engine's default of 1 can never apply by omission.
            env["IFQ_MIN_INCLUDED_NUCLEI"] =
                request.MinIncludedNuclei.ToString(CultureInfo.InvariantCulture);

            // H2. One variable per analysis channel that has a value AND that the
            // engine actually reads. A marker outside the engine's closed
            // threshold list gets no variable at all, so the run record cannot
            // claim a fixed cutoff the engine never saw.
            if (panel != null)
            {
                foreach (ChannelDef channel in panel.AnalysisChannels)
                {
                    // Same question, same answer as the gate. If these two ever
                    // disagreed the record would claim a fixed cutoff the engine
                    // never received, or drop one it would have honoured.
                    if (!ThresholdSurface.EngineReads(panel, channel, engineThresholdMarkers))
                        continue;
                    string value;
                    if (request.Thresholds.TryGetValue(channel.Token, out value) &&
                        !string.IsNullOrEmpty((value ?? "").Trim()))
                        env[channel.ThresholdEnvName] = value.Trim();
                }
            }

            // Route 2 forces the tile-appropriate settings. Tiles are single
            // plane, in one flat folder, and every one of them must be measured
            // or the slide area no longer adds up.
            if (request.Route == ImageRoute.IfSlideScanner)
            {
                env["IFQ_PROJECTION"] = "max";
                env["IFQ_SINGLE_PLANE"] = "-1";
                env["IFQ_RECURSIVE"] = "false";
                env["IFQ_INCLUDE_REGEX"] = ".*";
                env["IFQ_MAX_IMAGES"] = "0";
                env["IFQ_MIN_INCLUDED_NUCLEI"] = "0";
            }

            // Advanced free text last, and only for names the launcher does not
            // own. The gate has already blocked unknown and protected names.
            foreach (KeyValuePair<string, string> item in
                     FailClosedGate.ParseAdvanced(request.AdvancedText, null))
            {
                if (EnvSurface.ProtectedKeys.Contains(item.Key)) continue;
                env[item.Key] = item.Value;
            }
            return env;
        }

        /// Stage 1 (QuPath) environment for route 2.
        public static Dictionary<string, string> BuildStage1(RunRequest request, string panelKey)
        {
            // The same fail-closed guard BuildStage2 has always carried, and for
            // a sharper reason: route 3 is the only route besides 2 that
            // declares RequiresQuPath, so a future brightfield wiring reaches
            // THIS builder first. Without this it returned a complete seven-
            // variable stage-1 environment for a route-3 request and QuPath
            // would have been started on a brightfield slide with the
            // fluorescence tiler's settings.
            if (request.Route == ImageRoute.HeBrightfield)
            {
                if (!LauncherBuild.BrightfieldRouteEnabled)
                    throw new InvalidOperationException(
                        "Route 3 (H&E / brightfield) is not available in this build and no " +
                        "stage 1 environment will be produced for it.\r\n\r\n" +
                        LauncherBuild.BrightfieldDisabledReason);
                throw new NotImplementedException(
                    "LauncherBuild.BrightfieldRouteEnabled is true, but no brightfield tiling " +
                    "stage is wired into RunEnvironment.BuildStage1. qupath_wsi_tile_export.groovy " +
                    "is a fluorescence tiler: it thresholds channels to find tissue and writes " +
                    "IFQ_WSI_* channel patterns. Build the brightfield stage 1 here before " +
                    "shipping a build with the flag on.");
            }
            if (request.Route != ImageRoute.IfSlideScanner)
                throw new InvalidOperationException(
                    "Stage 1 exists only on route 2 (IF - slide scanner). " +
                    RouteCatalog.Describe(request.Route).DisplayName +
                    " has no stage 1, so producing a stage 1 environment for it would start " +
                    "QuPath on inputs no stage of this route ever reads.");

            Dictionary<string, string> env =
                new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            env["IFQ_WSI_INPUT"] = Path.GetFullPath(request.WsiInput);
            env["IFQ_WSI_OUTPUT"] = Path.GetFullPath(request.WsiOutput);
            // One panel control writes both names. 'panel' is a grouping key
            // downstream, so a mismatch splits one animal into two rows.
            env["IFQ_WSI_PANEL"] = panelKey;
            env["IFQ_WSI_RESUME"] = request.WsiResume ? "true" : "false";
            env["IFQ_WSI_PARTITION_DAMAGE"] = request.WsiPartitionDamage ? "true" : "false";
            env["IFQ_WSI_DRY_RUN"] = request.Tier == RunTier.Dry ? "true" : "false";
            if (request.WsiMaxTilesPerSlide > 0)
                env["IFQ_WSI_MAX_TILES_PER_SLIDE"] =
                    request.WsiMaxTilesPerSlide.ToString(CultureInfo.InvariantCulture);
            if (!string.IsNullOrEmpty(request.SlideMetadataCsv))
                env["IFQ_WSI_SLIDE_METADATA"] = Path.GetFullPath(request.SlideMetadataCsv);

            foreach (KeyValuePair<string, string> item in
                     FailClosedGate.ParseAdvanced(request.AdvancedText, null))
            {
                if (EnvSurface.ProtectedKeys.Contains(item.Key)) continue;
                if (EnvSurface.Stage1Static.Contains(item.Key)) env[item.Key] = item.Value;
            }
            return env;
        }
    }

    // =================================================================
    // 9. LEGACY MODE -- byte-for-byte v1.7.2
    // =================================================================

    /// <summary>
    /// Route 4 exists so an analysis produced by IFQuantLauncher-v1.7.2.exe can
    /// be reproduced after the launcher gains routes. It is defined by an exact,
    /// testable property, not by intent:
    ///
    ///   For the same inputs, the set of environment variables the launcher
    ///   passes to Fiji, and the Fiji command line, are IDENTICAL to v1.7.2.
    ///
    /// BuildEnvironment below is a transcript of IFQuantLauncher.cs (v1.7.2)
    /// ReadAndValidateConfiguration lines 1394-1424, with nothing added. In
    /// particular it does NOT write IFQ_MIN_INCLUDED_NUCLEI and does NOT write
    /// any IFQ_*_THRESHOLD, because v1.7.2 never wrote them either -- and both
    /// change the numbers.
    /// </summary>
    internal static class LegacyProfile
    {
        public const string FrozenVersion = "1.7.2.0";

        /// Exactly the nineteen keys v1.7.2 writes, in the order it writes them.
        /// IFQ_PANEL_MAP_PATH and IFQ_PANEL_CONFIG are conditional there too.
        public static readonly string[] KeyOrder = new string[]
        {
            "IFQ_INPUT_DIR", "IFQ_OUTPUT_DIR", "IFQ_PANEL", "IFQ_MARKER_REGISTRY",
            "IFQ_PANEL_MAP_PATH", "IFQ_PANEL_CONFIG", "IFQ_RECURSIVE", "IFQ_INCLUDE_REGEX",
            "IFQ_MAX_IMAGES", "IFQ_SEGMENTER", "IFQ_PROJECTION", "IFQ_SINGLE_PLANE",
            "IFQ_EXPORT_DISPLAY_CHANNELS", "IFQ_DISPLAY_PREVIEW_ONLY", "IFQ_TISSUE_MODE",
            "IFQ_COMPARTMENT_MODE", "IFQ_WHOLE_FIELD_COMPARTMENT",
            "IFQ_ALLOW_NONEMPTY_OUTPUT", "IFQ_MORPHOLOGY_PRIMARY"
        };

        /// <summary>
        /// v1.7.2's ProtectedEnvironmentKeys (launcher/IFQuantLauncher.cs:107-118
        /// @ dfa3cfa), which is the same nineteen names it writes, in the same
        /// set. It is deliberately DERIVED from KeyOrder rather than retyped:
        /// the two lists are the same nineteen keys in v1.7.2 and the
        /// equivalence harness already proves KeyOrder against the real file, so
        /// deriving means one proof covers both.
        ///
        /// v1.8.0 took ownership of four more names -- IFQ_MIN_INCLUDED_NUCLEI,
        /// IFQ_WSI_PANEL, IFQ_WSI_INPUT, IFQ_WSI_OUTPUT (EnvSurface.ProtectedKeys)
        /// -- and route 4 must NOT enforce those, because v1.7.2 did not and the
        /// Advanced box was the only way a v1.7.2 user could set the nuclei floor.
        /// </summary>
        public static readonly HashSet<string> ProtectedKeys =
            BuildProtectedKeys();

        private static HashSet<string> BuildProtectedKeys()
        {
            HashSet<string> keys = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (string key in KeyOrder) keys.Add(key);
            return keys;
        }

        public static Dictionary<string, string> BuildEnvironment(
            string inputDirectory, string outputDirectory, string panelKey, string registryPath,
            string panelMapPath, string panelConfigPath, bool recursive, string includeRegex,
            int maxImages, string segmenter, string projection, int singlePlane,
            bool previewOnly, string tissueMode, string compartmentMode,
            string wholeFieldCompartment)
        {
            Dictionary<string, string> env =
                new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            env["IFQ_INPUT_DIR"] = Path.GetFullPath(inputDirectory);
            env["IFQ_OUTPUT_DIR"] = outputDirectory;
            env["IFQ_PANEL"] = panelKey;
            env["IFQ_MARKER_REGISTRY"] = registryPath;
            if (!string.IsNullOrEmpty(panelMapPath))
                env["IFQ_PANEL_MAP_PATH"] = panelMapPath;
            if (!string.IsNullOrEmpty(panelConfigPath))
                env["IFQ_PANEL_CONFIG"] = Path.GetFullPath(panelConfigPath);
            env["IFQ_RECURSIVE"] = recursive ? "true" : "false";
            env["IFQ_INCLUDE_REGEX"] = includeRegex;
            env["IFQ_MAX_IMAGES"] = maxImages.ToString(CultureInfo.InvariantCulture);
            env["IFQ_SEGMENTER"] = segmenter;
            env["IFQ_PROJECTION"] = projection;
            env["IFQ_SINGLE_PLANE"] = singlePlane.ToString(CultureInfo.InvariantCulture);
            env["IFQ_EXPORT_DISPLAY_CHANNELS"] = "true";   // DisplayChannelExportSetting(_)
            env["IFQ_DISPLAY_PREVIEW_ONLY"] = previewOnly ? "true" : "false";
            env["IFQ_TISSUE_MODE"] = tissueMode;
            env["IFQ_COMPARTMENT_MODE"] = compartmentMode;
            env["IFQ_WHOLE_FIELD_COMPARTMENT"] = wholeFieldCompartment;
            env["IFQ_ALLOW_NONEMPTY_OUTPUT"] = "false";
            env["IFQ_MORPHOLOGY_PRIMARY"] = "true";
            return env;
        }

        /// Canonical serialisation for the equivalence test. Ordinal-sorted, so
        /// dictionary iteration order cannot affect the comparison.
        public static string Canonicalize(Dictionary<string, string> env)
        {
            List<string> keys = new List<string>(env.Keys);
            keys.Sort(StringComparer.Ordinal);
            StringBuilder text = new StringBuilder();
            foreach (string key in keys)
                text.Append(key).Append('=').Append(env[key]).Append('\n');
            return text.ToString();
        }

        public static string Fingerprint(Dictionary<string, string> env)
        {
            using (SHA256 algorithm = SHA256.Create())
            {
                byte[] hash = algorithm.ComputeHash(
                    new UTF8Encoding(false).GetBytes(Canonicalize(env)));
                StringBuilder text = new StringBuilder(hash.Length * 2);
                foreach (byte b in hash)
                    text.Append(b.ToString("x2", CultureInfo.InvariantCulture));
                return text.ToString();
            }
        }

        /// The v1.7.2 Fiji command line, verbatim. Legacy mode keeps the
        /// launcher-exe invocation even where the bundled JVM would be more
        /// robust, because "more robust" is still a behaviour change.
        public static string CommandLine(string scriptPath)
        {
            return FijiCommand.LauncherExeArguments(scriptPath);
        }

        /// Deterministic fixture for the equivalence test. Fixed strings only,
        /// so the fingerprint is machine-independent.
        public static Dictionary<string, string> Fixture()
        {
            return BuildEnvironment(
                @"C:\fixture\input",
                @"C:\fixture\output\IFQ_run_20260807_000000",
                "LEFT",
                @"C:\fixture\runtime\config\lung_marker_registry.json",
                null, null, true, ".*", 0, "classic", "layer_aware", -1, false,
                "auto", "required", "unassigned");
        }

        // -----------------------------------------------------------------
        // Legacy is about NUMBERS, not just variables.
        //
        // The environment is half of what determines a result. The other half is
        // the pipeline and registry embedded at build time and extracted to a
        // version-keyed runtime directory. A v1.8.0 build made from a repo whose
        // IF_Quant_Pipeline.groovy has moved on would give DIFFERENT numbers from
        // an IDENTICAL environment.
        //
        // So route 4 checks the artefacts it is about to run and downgrades
        // itself honestly when they are not the v1.7.2 ones. It does not refuse:
        // refusing would make the mode useless after the first engine change.
        // -----------------------------------------------------------------

        public const string V172PipelineSha256 =
            "defffe6703e6da10ef7810977cb16473c0f36bf81ffa75fbbf6b81066de140ec";
        public const string V172RegistrySha256 =
            "20c6859d8b7d1114b5476eefc90d8c92affa78d9a59d1be11d2449ba98460b37";
        public const string V172ExeSha256 =
            "bd8e71764013c2dc7de0d43b76457fca136b50be20fc89e8d19d85fb4cb4a1c4";
        public const string V172ExeArchivePath = "legacy/launchers/IFQuantLauncher-v1.7.2.exe";

        /// <returns>null when the embedded artefacts are the v1.7.2 ones;
        /// otherwise the exact reason legacy mode is only approximate.</returns>
        public static string CheckEmbeddedArtefacts(
            string actualPipelineSha256, string actualRegistrySha256)
        {
            List<string> drift = new List<string>();
            if (!string.Equals(actualPipelineSha256, V172PipelineSha256,
                               StringComparison.OrdinalIgnoreCase))
                drift.Add("IF_Quant_Pipeline.groovy: embedded " + actualPipelineSha256 +
                          ", v1.7.2 shipped " + V172PipelineSha256);
            if (!string.Equals(actualRegistrySha256, V172RegistrySha256,
                               StringComparison.OrdinalIgnoreCase))
                drift.Add("lung_marker_registry.json: embedded " + actualRegistrySha256 +
                          ", v1.7.2 shipped " + V172RegistrySha256);
            if (drift.Count == 0) return null;
            return
                "Legacy mode will pass Fiji the exact v1.7.2 environment, but the embedded " +
                "analysis artefacts are NOT the ones v1.7.2 shipped:\r\n  " +
                string.Join("\r\n  ", drift.ToArray()) + "\r\n\r\n" +
                "The environment is reproduced; the numbers may not be. To reproduce the " +
                "numbers, run the archived binary " + V172ExeArchivePath +
                " (sha256 " + V172ExeSha256 + ") instead.";
        }
    }

    // =================================================================
    // 10. THE RUN RECORD  (H5)
    // =================================================================

    /// <summary>
    /// launcher_run.txt, extended from v1.7.2's WriteLauncherRecord.
    ///
    /// H5: a run whose thresholds are not frozen must be visibly marked
    /// EXPLORATORY in whatever record the launcher writes. Three places carry
    /// it, because any one of them can be lost:
    ///   * the run folder NAME gains _EXPLORATORY (survives being moved);
    ///   * launcher_run.txt carries run_classification and threshold_policy;
    ///   * EXPLORATORY_DO_NOT_AGGREGATE.txt sits in the folder.
    ///
    /// The marker file is written AFTER the engine exits, never before. H4 means
    /// the engine aborts on a non-empty IFQ_OUTPUT_DIR, so a marker file written
    /// up front would break every exploratory run it was meant to label.
    /// </summary>
    internal static class RunRecord
    {
        public static string Build(
            RunRequest request, GateResult gate, Dictionary<string, string> env,
            string launcherVersion, string architecture, string fijiExecutable,
            string invocationDescription, int exitCode, string manifestStatus,
            string pipelineSha256, string registrySha256, string legacyNote)
        {
            RouteSpec spec = RouteCatalog.Describe(request.Route);
            StringBuilder record = new StringBuilder();
            record.AppendLine("IF Quant Launcher run record");
            record.AppendLine("launcher_version=" + launcherVersion);
            record.AppendLine("recorded_at=" +
                DateTimeOffset.Now.ToString("o", CultureInfo.InvariantCulture));
            record.AppendLine("route=" + (int)request.Route + " " + spec.DisplayName);
            record.AppendLine("tier=" + request.Tier.ToString().ToLowerInvariant());

            // H5. The one line an aggregation script can grep for.
            record.AppendLine("run_classification=" +
                (gate.Exploratory ? "EXPLORATORY_DO_NOT_AGGREGATE" : "THRESHOLDS_FROZEN"));
            record.AppendLine("thresholds_frozen=" + (gate.Exploratory ? "false" : "true"));
            record.AppendLine("windows_architecture=" + architecture);
            record.AppendLine("fiji_executable=" + fijiExecutable);
            record.AppendLine("fiji_invocation=" + invocationDescription);
            record.AppendLine("fiji_exit_code=" + exitCode.ToString(CultureInfo.InvariantCulture));
            record.AppendLine("manifest_status=" + manifestStatus);
            record.AppendLine("pipeline_sha256=" + pipelineSha256);
            record.AppendLine("registry_sha256=" + registrySha256);
            if (!string.IsNullOrEmpty(legacyNote))
            {
                record.AppendLine();
                record.AppendLine("[legacy_equivalence]");
                record.AppendLine(legacyNote);
            }

            record.AppendLine();
            record.AppendLine("[threshold_policy]");
            if (request.Route == ImageRoute.LegacyFiji172)
            {
                record.AppendLine(
                    "legacy_mode=v1.7.2 wrote no IFQ_*_THRESHOLD of its own, so a channel is " +
                    "frozen here only where the v1.7.2 Advanced box set its variable");
                // H3 on route 4. The floor is real, it just does not come from a
                // launcher control, so the record states the number that applies
                // instead of leaving a reader to work it out from [environment].
                if (!string.IsNullOrEmpty(gate.LegacyMinIncludedNuclei))
                    record.AppendLine(
                        "legacy_min_included_nuclei=" + gate.LegacyMinIncludedNuclei);
                foreach (string line in gate.ThresholdPolicy)
                    record.AppendLine(line);
            }
            else if (gate.ThresholdPolicy.Count == 0)
                record.AppendLine("(no analysis channels resolved)");
            else
                foreach (string line in gate.ThresholdPolicy)
                    record.AppendLine(line);

            record.AppendLine();
            record.AppendLine("[gate_findings]");
            if (gate.Findings.Count == 0)
                record.AppendLine("(none)");
            else
                foreach (GateFinding f in gate.Findings)
                    record.AppendLine(
                        f.Severity.ToString().ToUpperInvariant() + " " + f.Code + ": " +
                        OneLine(f.Message));

            record.AppendLine();
            record.AppendLine("[environment]");
            List<string> keys = new List<string>(env.Keys);
            keys.Sort(StringComparer.Ordinal);
            foreach (string key in keys)
                record.AppendLine(key + "=" + env[key]);
            return record.ToString();
        }

        public static string ExploratoryMarkerText(RunRequest request, GateResult gate)
        {
            StringBuilder text = new StringBuilder();
            text.AppendLine("EXPLORATORY RUN -- DO NOT AGGREGATE, DO NOT REPORT");
            text.AppendLine();
            text.AppendLine(
                "At least one analysis channel in this run had no fixed, control-derived");
            text.AppendLine(
                "threshold, so the engine chose one per region with adaptive Otsu and");
            text.AppendLine(
                "recorded the source as 'adaptive_otsu_exploratory'. On a region that is");
            text.AppendLine(
                "mostly background the threshold comes from background: an uninfected");
            text.AppendLine(
                "control has read 4.95% KRT5+ at an Otsu threshold of 54.6, inside that");
            text.AppendLine(
                "channel's noise floor, and a background-dominated tile can report a");
            text.AppendLine("KRT5 pod area fraction of about 0.89.");
            text.AppendLine();
            text.AppendLine("Per-channel threshold source:");
            foreach (string line in gate.ThresholdPolicy)
                text.AppendLine("  " + line);
            text.AppendLine();
            text.AppendLine("Route: " + RouteCatalog.Describe(request.Route).DisplayName);
            text.AppendLine("Tier:  " + request.Tier.ToString().ToLowerInvariant());
            text.AppendLine("Panel: " + request.PanelKey);
            text.AppendLine();
            text.AppendLine(
                "Freeze every threshold from stained controls and re-run before any number");
            text.AppendLine("here is used as an endpoint.");
            return text.ToString();
        }

        private static string OneLine(string value)
        {
            return Regex.Replace(value ?? "", "\\s+", " ").Trim();
        }
    }

    // =================================================================
    // 11. PRE-START ASSERTIONS
    //
    // The gate is advisory to the UI. These are the last line: they run
    // between "the user pressed OK" and "a process exists", and they throw.
    // =================================================================

    internal static class PreStartAssertions
    {
        /// H1 + H4, enforced against the environment that is actually about to
        /// be handed to the process, not against the UI state that produced it.
        public static void AssertStage2Environment(
            Dictionary<string, string> env, string outputDirectory, ImageRoute route)
        {
            AssertStage2Environment(env, outputDirectory, route, null);
        }

        /// <summary>
        /// <paramref name="advancedKeys"/> is the set of names the operator typed
        /// into the Advanced box. It exists for exactly one rule: on route 4,
        /// IFQ_MIN_INCLUDED_NUCLEI in the environment is legal if and only if
        /// the operator typed it, because v1.7.2's Advanced box was the only way
        /// to set the nuclei floor and route 4 must accept what v1.7.2 accepted.
        /// The launcher itself still may not write it. Pass null (or use the
        /// three-argument overload) to keep the stricter "never present" rule.
        /// </summary>
        public static void AssertStage2Environment(
            Dictionary<string, string> env, string outputDirectory, ImageRoute route,
            ICollection<string> advancedKeys)
        {
            // Generalised from a route-3 specific check. An undefined ImageRoute
            // used to sail through here: RouteCatalog.Describe returned
            // Available=true for it, so nothing in this method objected.
            RouteSpec spec = RouteCatalog.Describe(route);
            if (!spec.Available)
                throw new InvalidOperationException(
                    spec.DisplayName + " cannot be started by this build.\r\n\r\n" +
                    spec.UnavailableReason + "\r\n\r\nThe run was not started.");

            string panel;
            if (!env.TryGetValue("IFQ_PANEL", out panel) ||
                string.IsNullOrEmpty((panel ?? "").Trim()))
                throw new InvalidOperationException(
                    "H1: IFQ_PANEL is missing from the run environment. The engine would fall " +
                    "back to panel T and report numbers for an unrelated channel map. The run " +
                    "was not started.");

            string allowNonEmpty;
            if (!env.TryGetValue("IFQ_ALLOW_NONEMPTY_OUTPUT", out allowNonEmpty) ||
                !string.Equals(allowNonEmpty, "false", StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException(
                    "H4: IFQ_ALLOW_NONEMPTY_OUTPUT must be false. The launcher never merges a " +
                    "run into an existing output folder. The run was not started.");

            string canonicalManifest;
            if (env.TryGetValue("IFQ_CANONICAL_MANIFEST_PATH", out canonicalManifest) &&
                !string.IsNullOrWhiteSpace(canonicalManifest) &&
                !File.Exists(canonicalManifest))
                throw new InvalidOperationException(
                    "Canonical field manifest does not exist: " + canonicalManifest +
                    "\r\nThe run was not started. Remove IFQ_CANONICAL_MANIFEST_PATH " +
                    "or select the reviewed manifest generated for this cohort.");

            if (route != ImageRoute.LegacyFiji172)
            {
                string floor;
                if (!env.TryGetValue("IFQ_MIN_INCLUDED_NUCLEI", out floor) ||
                    string.IsNullOrEmpty((floor ?? "").Trim()))
                    throw new InvalidOperationException(
                        "H3: IFQ_MIN_INCLUDED_NUCLEI is missing from the run environment. The " +
                        "engine default is 1, which silently drops sparse regions and takes " +
                        "their tissue area out of the total. The run was not started.");
            }
            else if (env.ContainsKey("IFQ_MIN_INCLUDED_NUCLEI") &&
                     !ContainsIgnoreCase(advancedKeys, "IFQ_MIN_INCLUDED_NUCLEI"))
            {
                throw new InvalidOperationException(
                    "Legacy mode wrote IFQ_MIN_INCLUDED_NUCLEI, which v1.7.2 never wrote. That " +
                    "changes which regions are measured, so the run would not be legacy. The " +
                    "run was not started.\r\n\r\n" +
                    "(The Advanced box may still set it, because v1.7.2's Advanced box could. " +
                    "This assertion fired because it did NOT come from there, i.e. the launcher " +
                    "put it in the environment by itself.)");
            }

            AssertOutputDirectoryEmpty(outputDirectory);
        }

        private static bool ContainsIgnoreCase(ICollection<string> values, string candidate)
        {
            if (values == null) return false;
            foreach (string value in values)
                if (string.Equals(value, candidate, StringComparison.OrdinalIgnoreCase))
                    return true;
            return false;
        }

        /// H4. The engine aborts on a non-empty IFQ_OUTPUT_DIR
        /// (IF_Quant_Pipeline.groovy:3479-3484). Catching it here costs
        /// milliseconds instead of a JVM start plus a stack trace.
        public static void AssertOutputDirectoryEmpty(string outputDirectory)
        {
            if (string.IsNullOrEmpty(outputDirectory)) return;
            if (!Directory.Exists(outputDirectory)) return;
            string[] entries = Directory.GetFileSystemEntries(outputDirectory);
            if (entries.Length == 0) return;
            throw new InvalidOperationException(
                "H4: the run output folder is not empty:\r\n" + outputDirectory + "\r\n\r\n" +
                "It contains " + entries.Length + " item(s), starting with '" +
                Path.GetFileName(entries[0]) + "'. The engine refuses a non-empty output " +
                "directory so stale masks and cell tables from an earlier run cannot be mixed " +
                "into this one. The run was not started.");
        }

        /// The v1.7.2 rule, kept: nothing inherited from the parent process's
        /// environment may reach the engine.
        public static List<string> InheritedIfqNames()
        {
            List<string> names = new List<string>();
            foreach (System.Collections.DictionaryEntry entry in
                     Environment.GetEnvironmentVariables())
            {
                string key = Convert.ToString(entry.Key, CultureInfo.InvariantCulture);
                if (key != null && key.StartsWith("IFQ_", StringComparison.OrdinalIgnoreCase))
                    names.Add(key);
            }
            names.Sort(StringComparer.OrdinalIgnoreCase);
            return names;
        }
    }

    // =================================================================
    // 12. THE LAUNCH CHOKE POINT
    //
    // Round 1: the gate was skipped entirely for custom panels.
    // Round 2: the gate was bypassed by the Advanced overlay.
    //
    // Both are the same shape -- a path into the child environment that does
    // not pass validation -- and both were found AFTER a green test run,
    // because both validated INPUTS. Every input has a caller, every caller is
    // a place to forget, and there is no test that proves you enumerated them
    // all.
    //
    // The output has exactly one shape: a Dictionary<string,string> that is
    // about to become a child process's environment. So this validates THAT,
    // after every overlay, on every route, and it is not skippable by writing a
    // new caller:
    //
    //   * EnvironmentApply.Apply takes a RunSeal, not a dictionary.
    //   * RunSeal has one constructor and it is PRIVATE.
    //   * The only code that can call it is RunSeal.Issue, which validates.
    //   * build.ps1 refuses to ship a binary whose sources contain a second
    //     `.EnvironmentVariables[` write or a second `new RunSeal(`.
    //
    // A caller who wants to start a process with an IFQ_* environment has to
    // produce a RunSeal, and producing one runs every rule below.
    // =================================================================

    internal enum LaunchStage
    {
        /// QuPath, route 2 only. Reads the IFQ_WSI_* surface.
        Stage1QuPath = 1,
        /// The frozen Fiji engine. Routes 1, 2 and 4 all end up here.
        Stage2Fiji = 2,
        /// aggregate_tiles_to_slide.py. Reads NO IFQ_* variable at all.
        Stage3Python = 3
    }

    /// Everything the choke point needs in order to decide, gathered by the
    /// caller but judged here. Deliberately a plain carrier: the caller cannot
    /// pre-compute an answer and hand it in.
    internal sealed class SealInput
    {
        public LaunchStage Stage = LaunchStage.Stage2Fiji;
        public RunRequest Request;
        /// The panel whose channel list the RECORD was written from. null means
        /// "the launcher has no channel list", which is never 'frozen'.
        public PanelDef Panel;
        public HashSet<string> EngineThresholdMarkers;
        /// The gate result the run record will be built from. Its Exploratory
        /// flag and its ThresholdPolicy lines are the CLAIM being checked.
        public GateResult Gate;
        /// The final, merged environment -- after the route forcing, after the
        /// Advanced overlay, after everything.
        public Dictionary<string, string> Environment;
        public string OutputDirectory;
        /// The names the operator typed into the Advanced box (route 4's nuclei
        /// floor rule needs it).
        public ICollection<string> AdvancedKeys;
    }

    /// <summary>
    /// Proof that one specific environment dictionary passed the choke point.
    ///
    /// It cannot be constructed. `new RunSeal(...)` compiles in exactly one
    /// place -- inside Issue, below -- because the constructor is private, and
    /// nothing else in the assembly can reach it. Holding one of these is
    /// therefore the same statement as "these variables were re-derived and
    /// agreed with the record".
    /// </summary>
    internal sealed class RunSeal
    {
        private readonly Dictionary<string, string> variables;

        public readonly LaunchStage Stage;
        public readonly ImageRoute Route;
        /// What the ENVIRONMENT says, re-derived here, not copied from the gate.
        public readonly bool EnvironmentSaysFrozen;
        /// The per-channel policy this environment actually implies.
        public readonly List<string> EnvironmentThresholdPolicy;

        private RunSeal(
            LaunchStage stage, ImageRoute route, Dictionary<string, string> environment,
            bool frozen, List<string> policy)
        {
            Stage = stage;
            Route = route;
            variables = new Dictionary<string, string>(
                environment, StringComparer.OrdinalIgnoreCase);
            EnvironmentSaysFrozen = frozen;
            EnvironmentThresholdPolicy = policy;
        }

        /// The run_classification line this environment would honestly carry.
        public string Classification
        {
            get
            {
                return EnvironmentSaysFrozen
                    ? "THRESHOLDS_FROZEN"
                    : "EXPLORATORY_DO_NOT_AGGREGATE";
            }
        }

        public int Count { get { return variables.Count; } }

        /// A copy, so a sealed environment cannot be edited after it was sealed.
        public Dictionary<string, string> Variables()
        {
            return new Dictionary<string, string>(variables, StringComparer.OrdinalIgnoreCase);
        }

        public bool Contains(string key) { return variables.ContainsKey(key); }

        public string Value(string key)
        {
            string value;
            return variables.TryGetValue(key, out value) ? value : null;
        }

        // -------------------------------------------------------------
        // THE GATE ON THE WAY OUT
        // -------------------------------------------------------------

        public static RunSeal Issue(SealInput input)
        {
            if (input == null)
                throw new ArgumentNullException("input", Prefix + "no seal input was supplied.");
            if (input.Request == null)
                throw new InvalidOperationException(Prefix + "no run request was supplied.");
            if (input.Environment == null)
                throw new InvalidOperationException(
                    Prefix + "no environment was supplied, so there is nothing to check.");
            if (input.Gate == null)
                throw new InvalidOperationException(
                    Prefix + "no gate result was supplied, so there is no record to check the " +
                    "environment against.");

            ImageRoute route = input.Request.Route;
            RouteSpec spec = RouteCatalog.Describe(route);
            if (!spec.Available)
                throw new InvalidOperationException(
                    Prefix + spec.DisplayName + " cannot be started by this build.\r\n\r\n" +
                    spec.UnavailableReason);

            // A blocked gate must never reach a process start. This is the
            // backstop for "somebody called the run path without asking the
            // gate first", which is how round 1's custom-panel hole shipped.
            if (input.Gate.Blocked)
                throw new InvalidOperationException(
                    Prefix + "the fail-closed gate refused this run, and the run was started " +
                    "anyway.\r\n\r\n" + (input.Gate.FirstBlockMessage() ?? "(no reason given)"));

            Dictionary<string, string> env = new Dictionary<string, string>(
                input.Environment, StringComparer.OrdinalIgnoreCase);

            bool frozen;
            List<string> policy;
            switch (input.Stage)
            {
                case LaunchStage.Stage3Python:
                    CheckStage3(env, route);
                    frozen = false;
                    policy = new List<string>();
                    break;
                case LaunchStage.Stage1QuPath:
                    CheckStage1(env, input.Request);
                    frozen = false;
                    policy = new List<string>();
                    break;
                case LaunchStage.Stage2Fiji:
                    policy = CheckStage2(input, env, route, out frozen);
                    break;
                default:
                    throw new InvalidOperationException(
                        Prefix + "launch stage " + (int)input.Stage + " is not a stage this " +
                        "build knows how to check, so it will not be started.");
            }

            return new RunSeal(input.Stage, route, env, frozen, policy);
        }

        private const string Prefix = "LAUNCH REFUSED. ";

        // -------------------------------------------------------------
        // Stage 3 -- aggregate_tiles_to_slide.py
        // -------------------------------------------------------------
        private static void CheckStage3(Dictionary<string, string> env, ImageRoute route)
        {
            if (route != ImageRoute.IfSlideScanner)
                throw new InvalidOperationException(
                    Prefix + "stage 3 exists only on route 2 (IF - slide scanner).");
            List<string> stray = IfqNames(env);
            if (stray.Count > 0)
                throw new InvalidOperationException(
                    Prefix + "stage 3 reconciles tiles that stage 2 already measured; it reads " +
                    "no IFQ_* variable, and any that is set here would appear in the run record " +
                    "as if it had configured something. Remove: " +
                    string.Join(", ", stray.ToArray()));
        }

        // -------------------------------------------------------------
        // Stage 1 -- QuPath tiling
        // -------------------------------------------------------------
        private static void CheckStage1(Dictionary<string, string> env, RunRequest request)
        {
            if (request.Route != ImageRoute.IfSlideScanner)
                throw new InvalidOperationException(
                    Prefix + "stage 1 exists only on route 2 (IF - slide scanner).");

            RequireNonEmpty(env, "IFQ_WSI_INPUT",
                "Stage 1 would tile nothing and stage 2 would then measure an empty tiles " +
                "folder and report a complete run with no rows.");
            RequireNonEmpty(env, "IFQ_WSI_OUTPUT",
                "Stage 1 writes the tiles, the ROI sets and stage1_manifest.json there, and " +
                "stages 2 and 3 read all three back from it.");
            RequireNonEmpty(env, "IFQ_WSI_PANEL",
                "'panel' is a grouping key in every downstream table. Stage 1 stamps it into " +
                "each tile's samplesheet row before Fiji sees a tile, so a blank one splits " +
                "one animal into an unnamed group.");

            // One panel control writes both stage names. A stage 1 that tiles
            // under one panel and a stage 2 that measures under another produces
            // two rows for one animal, which is a silent data error, not a crash.
            string declared = (request.PanelKey ?? "").Trim();
            string stage1Panel = (env["IFQ_WSI_PANEL"] ?? "").Trim();
            if (declared.Length > 0 &&
                !string.Equals(declared, stage1Panel, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException(
                    Prefix + "stage 1 would tile under panel '" + stage1Panel +
                    "' while the run is recorded as panel '" + declared +
                    "'. 'panel' is a grouping key downstream, so the two halves of this run " +
                    "would be reconciled as two different animals.");

            foreach (string name in IfqNames(env))
                if (!EnvSurface.Stage1Static.Contains(name))
                    throw new InvalidOperationException(
                        Prefix + name + " is not part of the stage 1 (QuPath) environment " +
                        "surface. The tiler ignores unknown IFQ_* names without complaint, so " +
                        "this would be a no-op that looks like configuration.");
        }

        // -------------------------------------------------------------
        // Stage 2 -- the frozen Fiji engine. This is where the numbers come
        // from, so this is where the re-derivation happens.
        // -------------------------------------------------------------
        private static List<string> CheckStage2(
            SealInput input, Dictionary<string, string> env, ImageRoute route, out bool frozen)
        {
            // H1, H3, H4 against the merged environment. Unchanged rules, moved
            // inside the seal so they cannot be reached around either.
            PreStartAssertions.AssertStage2Environment(
                env, input.OutputDirectory, route, input.AdvancedKeys);

            PanelDef panel = input.Panel;
            bool legacy = route == ImageRoute.LegacyFiji172;

            // The record names a panel; the environment names a panel. If they
            // are not the same panel the entire channel map in the record is
            // about a different set of markers.
            string envPanel = (Read(env, "IFQ_PANEL") ?? "").Trim();
            if (panel != null &&
                !string.Equals(envPanel, panel.Key, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException(
                    Prefix + "the run record describes panel '" + panel.Key +
                    "' and the environment carries IFQ_PANEL=" + envPanel +
                    ". Every channel index, marker name and threshold in the record belongs to " +
                    "the first panel; the engine would measure the second.");

            // -----------------------------------------------------------
            // RE-DERIVE, FROM THE ENVIRONMENT, WHICH CHANNELS ARE FROZEN.
            // Nothing here reads request.Thresholds or the gate's own answer.
            // -----------------------------------------------------------
            List<string> policy = new List<string>();
            if (panel == null)
            {
                // No channel list. Unknown is never frozen -- that was defect
                // round 1, and it is re-stated here so that a caller who
                // reintroduces it cannot also reintroduce THRESHOLDS_FROZEN.
                frozen = false;
            }
            else if (panel.AnalysisChannels.Count == 0)
            {
                // N3. Zero frozen out of zero is not frozen.
                frozen = false;
            }
            else
            {
                bool all = true;
                foreach (ChannelDef channel in panel.AnalysisChannels)
                {
                    string raw = Read(env, channel.ThresholdEnvName);
                    policy.Add(ThresholdSurface.PolicyLine(
                        panel, channel, input.EngineThresholdMarkers, raw));
                    double parsed;
                    bool channelFrozen =
                        ThresholdSurface.EngineReads(
                            panel, channel, input.EngineThresholdMarkers) &&
                        ThresholdSurface.IsFrozenValue(raw, out parsed);
                    if (!channelFrozen) all = false;
                }
                frozen = all;
            }

            // -----------------------------------------------------------
            // H2 ON THE OUTPUT. Routes 1 and 2 own their threshold grid, so a
            // cutoff in the environment that the grid would have rejected is a
            // cutoff that got in some other way. Route 4 is exempt: v1.7.2
            // shape-checked its Advanced box and nothing else, and legacy mode
            // must accept what v1.7.2 accepted.
            // -----------------------------------------------------------
            if (!legacy)
            {
                foreach (string name in IfqNames(env))
                {
                    if (!ThresholdSurface.IsMarkerThresholdName(name)) continue;
                    double parsed;
                    if (!ThresholdSurface.IsFrozenValue(env[name], out parsed))
                        throw new InvalidOperationException(
                            Prefix + name + "=" + env[name] +
                            " is not a positive number. The engine parses it with " +
                            "parseDoubleSetting and would abort -- or, at 0, would make every " +
                            "pixel a candidate. The threshold grid rejects this value; " +
                            "something else put it into the environment.");
                    if (panel != null &&
                        ThresholdSurface.ChannelForThresholdName(panel, name) == null)
                        throw new InvalidOperationException(
                            Prefix + name + " freezes a marker that is not an analysis channel " +
                            "of panel " + panel.Key + ". The engine would ignore it, and the " +
                            "run record's [environment] block would show a cutoff that had no " +
                            "effect on a single number.");
                }
            }

            // -----------------------------------------------------------
            // THE CLAIM vs THE ENVIRONMENT.
            //
            // This is the whole point. RunRecord.Build writes
            //   run_classification = gate.Exploratory ? EXPLORATORY : FROZEN
            // and prints gate.ThresholdPolicy underneath it. Both are checked
            // against what the variables actually say.
            // -----------------------------------------------------------
            bool recordSaysFrozen = !input.Gate.Exploratory;
            if (recordSaysFrozen != frozen)
                throw new InvalidOperationException(
                    Prefix + "the run record and the run environment disagree about whether " +
                    "this run's thresholds are frozen.\r\n\r\n" +
                    "  the record would say : " +
                    (recordSaysFrozen ? "THRESHOLDS_FROZEN" : "EXPLORATORY_DO_NOT_AGGREGATE") +
                    "\r\n" +
                    "  the environment says : " +
                    (frozen ? "THRESHOLDS_FROZEN" : "EXPLORATORY_DO_NOT_AGGREGATE") + "\r\n\r\n" +
                    DescribeEnvironmentPolicy(panel, policy) +
                    "\r\nA run whose thresholds are not frozen must be visibly marked " +
                    "EXPLORATORY (H5), and one that is marked frozen must actually be frozen. " +
                    "The run was not started.");

            if (panel != null && panel.AnalysisChannels.Count > 0)
            {
                string difference = FirstPolicyDifference(input.Gate.ThresholdPolicy, policy);
                if (difference != null)
                    throw new InvalidOperationException(
                        Prefix + "the per-channel threshold policy in the run record does not " +
                        "match the thresholds in the run environment.\r\n\r\n" + difference +
                        "\r\n\r\nThe record is what a reader trusts months later, and the " +
                        "environment is what produced the numbers. They are not allowed to " +
                        "differ. The run was not started.");
            }

            return policy;
        }

        // -------------------------------------------------------------
        // helpers
        // -------------------------------------------------------------

        private static string Read(Dictionary<string, string> env, string key)
        {
            string value;
            return env.TryGetValue(key, out value) ? value : null;
        }

        private static void RequireNonEmpty(
            Dictionary<string, string> env, string key, string consequence)
        {
            string value = Read(env, key);
            if (string.IsNullOrEmpty((value ?? "").Trim()))
                throw new InvalidOperationException(
                    Prefix + key + " is missing or empty. " + consequence);
        }

        private static List<string> IfqNames(Dictionary<string, string> env)
        {
            List<string> names = new List<string>();
            foreach (string key in env.Keys)
                if (key != null && key.StartsWith("IFQ_", StringComparison.OrdinalIgnoreCase))
                    names.Add(key);
            names.Sort(StringComparer.OrdinalIgnoreCase);
            return names;
        }

        private static string DescribeEnvironmentPolicy(PanelDef panel, List<string> policy)
        {
            if (panel == null)
                return "The launcher has no channel list for this run, so it cannot know that " +
                       "a single channel is frozen.\r\n";
            if (panel.AnalysisChannels.Count == 0)
                return "Panel " + panel.Key + " declares no analysis channel at all, so there " +
                       "is nothing in this run that a threshold could freeze.\r\n";
            StringBuilder text = new StringBuilder();
            text.AppendLine("What the environment actually sets, channel by channel:");
            foreach (string line in policy) text.AppendLine("  " + line);
            return text.ToString();
        }

        /// Order-insensitive: the record prints one line per analysis channel and
        /// so does the re-derivation, but neither promises an order.
        private static string FirstPolicyDifference(List<string> recorded, List<string> derived)
        {
            List<string> a = new List<string>(recorded ?? new List<string>());
            List<string> b = new List<string>(derived ?? new List<string>());
            a.Sort(StringComparer.Ordinal);
            b.Sort(StringComparer.Ordinal);
            if (a.Count == b.Count)
            {
                bool same = true;
                for (int i = 0; i < a.Count; i++)
                    if (!string.Equals(a[i], b[i], StringComparison.Ordinal)) same = false;
                if (same) return null;
            }
            StringBuilder text = new StringBuilder();
            text.AppendLine("  the record would say :");
            foreach (string line in a) text.AppendLine("      " + line);
            text.AppendLine("  the environment says :");
            foreach (string line in b) text.AppendLine("      " + line);
            return text.ToString();
        }
    }
}
