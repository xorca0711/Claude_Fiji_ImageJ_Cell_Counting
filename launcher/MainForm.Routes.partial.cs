// =====================================================================
// MainForm.Routes.partial.cs                                     v1.8.0
// ---------------------------------------------------------------------
// The WinForms surface for the multi-route launcher.
//
// It is the second half of `internal sealed partial class MainForm`; the
// first half is launcher/IFQuantLauncher.cs, which keeps every v1.7.2
// control, tooltip and behaviour unchanged. This file adds:
//
//   * the route selector, with route 3 visible and NOT selectable;
//   * the extra tool/location rows routes 2 needs;
//   * the per-channel threshold grid (H2) and the nuclei floor (H3);
//   * the live gate summary that sits directly above the Run button;
//   * the typed-phrase confirmation dialog for a flagged run (H5);
//   * the route-2 stage runner.
//
// Controls that do not apply to a route are HIDDEN, not disabled: a greyed
// box invites "why can't I set this", and the answer is always "it does not
// exist on this route", which absence says better. The one deliberate
// exception is route 3 itself, which stays visible and greyed because the
// user needs to know the option exists and why it is refused.
// =====================================================================

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using IFQuantLauncher.Routing;

namespace IFQuantLauncher
{
    internal sealed partial class MainForm
    {
        // ---- new controls ---------------------------------------------
        private GroupBox routeGroup;
        private ComboBox routeBox;
        private Label routeHelpLabel;
        private Label routeUnavailableLabel;
        private ComboBox tierBox;
        private ComboBox invocationBox;

        private GroupBox toolsGroup;
        private TextBox quPathBox;
        private TextBox pythonBox;
        private TextBox wsiInputBox;
        private TextBox wsiOutputBox;
        private TextBox slideMetadataBox;
        private CheckBox wsiResumeBox;
        private CheckBox wsiPartitionBox;
        private NumericUpDown wsiMaxTilesBox;
        private Control[] wsiOnlyRows;
        private Control[] fijiOnlyRows;

        private GroupBox measurementGroup;
        private TableLayoutPanel thresholdTable;
        private Label thresholdNoteLabel;
        private NumericUpDown minNucleiBox;
        private CheckBox unlockPilotPanelsBox;

        private Label gateSummaryLabel;

        private readonly Dictionary<string, TextBox> thresholdBoxes =
            new Dictionary<string, TextBox>(StringComparer.OrdinalIgnoreCase);
        /// N2. Which panel the boxes in thresholdBoxes currently belong to.
        /// null when there is no grid. A typed cutoff is only ever re-used for
        /// the panel it was typed for.
        private string thresholdGridPanelKey;

        private readonly Dictionary<string, Label> thresholdStatusLabels =
            new Dictionary<string, Label>(StringComparer.OrdinalIgnoreCase);

        private Dictionary<string, PanelDef> enginePanels;
        private HashSet<string> engineThresholdMarkers;
        private string enginePanelsError;

        // Custom panels (IFQ_PANEL_CONFIG). Cached on path+mtime+length because
        // the gate summary is recomputed on every keystroke.
        private CustomPanelParse customPanels;
        private string customPanelsKey;
        private string customPanelError;
        private Dictionary<string, string> markerDefaultRoles;

        private int lastValidRouteIndex;
        private bool routeVetoInProgress;
        private bool suppressGateRefresh;

        private const string PilotPanelItem = "T — pilot plumbing test; NOT interpretable";

        // =================================================================
        // CONSTRUCTION
        // =================================================================

        /// Called near the end of BuildInterface, once every v1.7.2 control
        /// exists.
        ///
        /// It no longer PLACES its group boxes. It builds routeGroup,
        /// toolsGroup and measurementGroup and leaves them parentless; the
        /// caller appends them to the configuration columns, so the reading
        /// order of the whole form is decided by one block of statements
        /// instead of by which method happened to run first. Only the gate
        /// summary is placed here, and it goes into the pinned bottom stack,
        /// because it must never scroll away from the Run button it describes.
        private void BuildRouteInterface()
        {
            // Held until FinishRouteInitialisation, so restoring settings.ini
            // does not re-evaluate the gate once per control.
            suppressGateRefresh = true;
            BuildRouteGroup();
            BuildToolsGroup();
            BuildMeasurementGroup();
            BuildGateSummary();

            // H1: panel T is not in the picker unless it is explicitly unlocked.
            RemovePilotPanelFromPicker();

            HookGateRefresh();
        }

        /// Called once at the end of the MainForm constructor, after
        /// settings.ini has been restored and the v1.7.2 defaults applied.
        private void FinishRouteInitialisation()
        {
            suppressGateRefresh = false;
            OnRouteChanged();
        }

        /// <summary>
        /// --ui-smoke. Constructs nothing new: the whole window already exists
        /// by the time this runs. It forces layout and then drives the route
        /// picker the way a user would, asserting the properties that only
        /// exist at the UI level.
        /// </summary>
        internal int UiSmokeTest()
        {
            // Forces handle creation for the form and every child, which is
            // where a bad TableLayoutPanel cell assignment actually throws.
            IntPtr handle = Handle;
            if (handle == IntPtr.Zero) return 61;
            PerformLayout();

            // Guard against the failure mode this test had while it was being
            // written: on a form that was never shown, Control.Visible is false
            // for every child, so half the assertions below pass for the wrong
            // reason and the other half fail for the wrong reason.
            if (!Visible) return 60;

            // Neutralise whatever settings.ini happened to contain, so the
            // smoke test measures the interface and not the operator's last
            // session. A stub Fiji folder keeps tool resolution architecture
            // independent.
            string sandbox = Path.Combine(
                Path.GetTempPath(), "IFQuantLauncher-uismoke-" + Guid.NewGuid().ToString("N"));
            try
            {
                Directory.CreateDirectory(sandbox);
                foreach (string stub in new string[]
                         { "fiji-windows-x64.exe", "fiji-windows-arm64.exe" })
                    File.WriteAllBytes(Path.Combine(sandbox, stub), new byte[0]);
                Directory.CreateDirectory(Path.Combine(sandbox, "java", "stub", "bin"));
                File.WriteAllBytes(
                    Path.Combine(sandbox, "java", "stub", "bin", "java.exe"), new byte[0]);
                Directory.CreateDirectory(Path.Combine(sandbox, "jars"));
                File.WriteAllBytes(
                    Path.Combine(sandbox, "jars", "ij1-patcher-smoke.jar"), new byte[0]);
                fijiBox.Text = sandbox;
                outputBaseBox.Text = sandbox;
                inputBox.Text = sandbox;
                advancedBox.Text = "";
                panelConfigBox.Text = "";
                return UiSmokeBody(sandbox);
            }
            finally
            {
                try { if (Directory.Exists(sandbox)) Directory.Delete(sandbox, true); }
                catch { }
            }
        }

        private int UiSmokeBody(string sandbox)
        {

            if (routeBox == null || routeBox.Items.Count != 4) return 62;
            if (gateSummaryLabel == null || measurementGroup == null ||
                toolsGroup == null || thresholdTable == null) return 62;

            // The v1.7.2 controls must still be present and reachable.
            if (inputBox == null || fijiBox == null || outputBaseBox == null ||
                panelBox == null || runButton == null || logBox == null ||
                inputScopeGroup == null || includeRegexBox == null ||
                validatedLungScopeButton == null || maxImagesBox == null ||
                recursiveBox == null) return 63;

            // File scope must remain outside the scrolling configuration pane.
            // It is impossible to repair a bad AUTO scan with a control that
            // the same broken scroll layout prevents the operator from seeing.
            if (inputScopeGroup.Parent != rootTable ||
                !IsDescendantOf(includeRegexBox, inputScopeGroup) ||
                IsDescendantOf(includeRegexBox, configScroll)) return 78;
            includeRegexBox.Text = ".*";

            // v1.9.2 regression: both column-top groups must size to their full
            // table content. v1.9.1 could leave their final rows below the
            // GroupBox border even though the surrounding pane scrolled.
            PerformLayout();
            configScroll.PerformLayout();
            if (!GroupContainsItsContent(routeGroup) ||
                !GroupContainsItsContent(analysisSettingsGroup)) return 79;
            validatedLungScopeButton.PerformClick();
            if (!string.Equals(includeRegexBox.Text, @".*20x 2k.*\.oir",
                               StringComparison.Ordinal)) return 78;
            includeRegexBox.Text = ".*";

            // H1: panel T must not be in the picker while pilot panels are locked.
            foreach (object item in panelBox.Items)
            {
                string text = Convert.ToString(item, CultureInfo.InvariantCulture);
                if (text != null &&
                    (text.Trim() == "T" || text.StartsWith("T —", StringComparison.Ordinal)))
                    return 64;
            }
            unlockPilotPanelsBox.Checked = true;
            bool pilotAppeared = false;
            foreach (object item in panelBox.Items)
                if (string.Equals(Convert.ToString(item, CultureInfo.InvariantCulture),
                                  PilotPanelItem, StringComparison.Ordinal))
                    pilotAppeared = true;
            if (!pilotAppeared) return 64;
            unlockPilotPanelsBox.Checked = false;

            // R3: every route index is selectable EXCEPT the unavailable ones,
            // which bounce back to the previously selected route.
            for (int index = 0; index < routeBox.Items.Count; index++)
            {
                ImageRoute route = RouteCatalog.All()[index];
                bool available = RouteCatalog.IsAvailable(route);
                int before = routeBox.SelectedIndex;
                routeBox.SelectedIndex = index;
                if (available)
                {
                    if (routeBox.SelectedIndex != index) return 65;
                    if (SelectedRoute != route) return 65;
                }
                else
                {
                    // The veto must have restored the previous selection and
                    // said why, without a modal dialog.
                    if (routeBox.SelectedIndex == index) return 66;
                    if (routeBox.SelectedIndex != before) return 66;
                    if (routeHelpLabel.Text.IndexOf("NOT AVAILABLE",
                                                    StringComparison.OrdinalIgnoreCase) < 0 &&
                        routeHelpLabel.Text.IndexOf("not available",
                                                    StringComparison.OrdinalIgnoreCase) < 0)
                        return 66;
                }
            }

            // The permanent explanation for the greyed entry must be shown --
            // exactly when there IS a greyed entry. Written against the route
            // catalog rather than against "route 3 is always off", so flipping
            // LauncherBuild.BrightfieldRouteEnabled does not fail --ui-smoke
            // and get the binary discarded by build.ps1.
            bool anyUnavailable = false;
            foreach (ImageRoute route in RouteCatalog.All())
                if (!RouteCatalog.IsAvailable(route)) anyUnavailable = true;
            if (routeUnavailableLabel.Visible != anyUnavailable) return 67;
            if (anyUnavailable && routeUnavailableLabel.Text.Length == 0) return 67;

            // Route 4 hides the measurement controls and pins the invocation.
            routeBox.SelectedIndex = 3;
            if (SelectedRoute != ImageRoute.LegacyFiji172) return 68;
            if (measurementGroup.Visible) return 68;
            if (SelectedInvocation != FijiInvocation.LauncherExe) return 68;
            if (tierBox.Enabled) return 68;

            // Route 2 shows the whole-slide tools and forces the tile settings.
            routeBox.SelectedIndex = 1;
            if (!toolsGroup.Visible) return 69;
            if (projectionBox.Enabled || maxImagesBox.Enabled || inputBox.Enabled) return 69;

            // Route 1 with an explicit panel must build a threshold row per
            // analysis channel, each naming the engine's own variable.
            routeBox.SelectedIndex = 0;
            SelectChoice(panelBox, "LEFT");
            SelectChoice(tissueModeBox, "auto");
            SelectChoice(compartmentModeBox, "optional");
            SelectChoice(wholeCompartmentBox, "unassigned");
            OnPanelChanged();
            PanelDef left = ResolveSelectedPanel();
            if (left == null) return 70;
            if (thresholdBoxes.Count != left.AnalysisChannels.Count) return 70;
            foreach (ChannelDef channel in left.AnalysisChannels)
                if (!thresholdBoxes.ContainsKey(channel.Token)) return 70;

            // Blank boxes must leave the gate summary flagged, not green.
            RefreshGateSummary();
            if (!runButton.Enabled) return 71;
            if (!previewButton.Enabled) return 71;
            if (gateSummaryLabel.Text.IndexOf("FLAGGED", StringComparison.Ordinal) < 0) return 71;

            // Filling every box must turn it green.
            foreach (KeyValuePair<string, TextBox> pair in thresholdBoxes)
                pair.Value.Text = "500";
            RefreshGateSummary();
            if (gateSummaryLabel.Text.IndexOf("Ready.", StringComparison.Ordinal) < 0) return 72;

            // A non-zero nuclei floor on an area-endpoint panel must disable Run.
            minNucleiBox.Value = 5;
            RefreshGateSummary();
            if (runButton.Enabled) return 73;
            if (gateSummaryLabel.Text.IndexOf("Cannot start", StringComparison.Ordinal) < 0)
                return 73;
            minNucleiBox.Value = 0;

            // ---------------------------------------------------------------
            // 74 A CUSTOM PANEL AT THE UI LEVEL.
            //
            // ResolveSelectedPanel returned null for every custom panel key,
            // and the gate's whole H2 block was guarded on `panel != null`, so
            // this exact sequence -- custom key, custom JSON, zero thresholds,
            // confirmatory tier -- produced an EMPTY threshold grid and a GREEN
            // "Ready. Route 1, panel MYCUSTOM, 0/0 thresholds fixed, tier
            // confirmatory" bar. It is the UI half of the same defect the
            // packaged check 44 covers at the gate level.
            // ---------------------------------------------------------------
            string customPath = Path.Combine(sandbox, "custom_panel.json");
            File.WriteAllText(
                customPath,
                "{\"panels\":{\"UISMOKE\":{\"label\":\"ui smoke\",\"channels\":[" +
                "{\"idx\":1,\"marker\":\"DAPI\",\"role\":\"nuclear\"}," +
                "{\"idx\":2,\"marker\":\"KRT5\",\"role\":\"cyto\",\"areaMarker\":true}]}}}",
                new UTF8Encoding(false));

            panelConfigBox.Text = customPath;
            SelectChoice(panelBox, "UISMOKE");
            OnPanelChanged();

            PanelDef custom = ResolveSelectedPanel();
            if (custom == null || !custom.IsCustom) return 74;
            if (thresholdBoxes.Count != 1) return 74;
            if (!thresholdBoxes.ContainsKey("KRT5")) return 74;

            // ---------------------------------------------------------------
            // N2. THE KRT5 BOX MUST BE EMPTY HERE.
            //
            // Every threshold box was filled with 500 for panel LEFT a few lines
            // above, and UISMOKE also has a KRT5 channel. The grid used to carry
            // a typed value across a panel change whenever the marker token
            // matched, so this box came back reading 500 -- for a panel the
            // operator had never set a cutoff for -- and ReadRunRequest read it
            // straight into request.Thresholds. This test used to CLEAR the box
            // to work around that; clearing it is what hid the defect, so the
            // workaround is now the assertion.
            if ((thresholdBoxes["KRT5"].Text ?? "").Trim().Length != 0) return 75;
            RunRequest afterSwitch = ReadRunRequest();
            string carried;
            if (afterSwitch.Thresholds.TryGetValue("KRT5", out carried) &&
                (carried ?? "").Trim().Length != 0) return 75;

            // Blank box: flagged, never green.
            RefreshGateSummary();
            if (!runButton.Enabled) return 74;
            if (gateSummaryLabel.Text.IndexOf("FLAGGED", StringComparison.Ordinal) < 0) return 74;

            // Confirmatory tier with nothing frozen: refused outright.
            SelectChoice(tierBox, "confirmatory");
            RefreshGateSummary();
            if (runButton.Enabled) return 74;
            SelectChoice(tierBox, "exploratory");

            // Frozen: green, and it really is the custom channel that was frozen.
            thresholdBoxes["KRT5"].Text = "480";
            RefreshGateSummary();
            if (gateSummaryLabel.Text.IndexOf("Ready.", StringComparison.Ordinal) < 0) return 74;

            // A custom key whose JSON does not declare it must be refused, not
            // silently treated as a panel with no channels.
            SelectChoice(panelBox, "NOT_IN_THE_FILE");
            OnPanelChanged();
            RefreshGateSummary();
            if (runButton.Enabled) return 74;
            if (thresholdBoxes.Count != 0) return 74;

            panelConfigBox.Text = "";
            SelectChoice(panelBox, "LEFT");
            OnPanelChanged();

            // ---------------------------------------------------------------
            // 76 N2, the other direction: LEFT -> A -> LEFT loses the values.
            //     A cutoff is a per-panel, control-derived number. Coming back
            //     to a panel must not silently re-arm a stale one.
            // ---------------------------------------------------------------
            foreach (KeyValuePair<string, TextBox> pair in thresholdBoxes)
                pair.Value.Text = "777";
            SelectChoice(panelBox, "A");
            OnPanelChanged();
            foreach (KeyValuePair<string, TextBox> pair in thresholdBoxes)
                if ((pair.Value.Text ?? "").Trim().Length != 0) return 76;
            RefreshGateSummary();
            if (gateSummaryLabel.Text.IndexOf("Ready.", StringComparison.Ordinal) >= 0)
                return 76;
            SelectChoice(panelBox, "LEFT");
            OnPanelChanged();
            foreach (KeyValuePair<string, TextBox> pair in thresholdBoxes)
                if ((pair.Value.Text ?? "").Trim().Length != 0) return 76;

            // ...but a route change, which rebuilds the same grid for the same
            // panel, must still keep them, or every route toggle would silently
            // unfreeze the run.
            foreach (KeyValuePair<string, TextBox> pair in thresholdBoxes)
                pair.Value.Text = "500";
            routeBox.SelectedIndex = 1;
            routeBox.SelectedIndex = 0;
            if (thresholdBoxes.Count == 0) return 76;
            foreach (KeyValuePair<string, TextBox> pair in thresholdBoxes)
                if ((pair.Value.Text ?? "").Trim() != "500") return 76;

            // ---------------------------------------------------------------
            // 77 N4 AUTO IS USABLE, AND THE LIVE GATE AGREES WITH THE RUN PATH.
            //
            //     AUTO gave "Cannot start: Panel 'AUTO' is not one of the panels
            //     declared in the embedded IF_Quant_Pipeline.groovy" -- and on
            //     the run path, after AUTO had resolved, the same false sentence
            //     about the panel it had just detected. With a custom panel JSON
            //     selected it did the opposite: amber, Run enabled, and a throw
            //     after the review dialog was accepted.
            // ---------------------------------------------------------------
            foreach (KeyValuePair<string, TextBox> pair in thresholdBoxes)
                pair.Value.Text = "";
            SelectChoice(panelBox, "AUTO");
            OnPanelChanged();
            RefreshGateSummary();
            if (!runButton.Enabled) return 77;
            if (gateSummaryLabel.Text.IndexOf("FLAGGED", StringComparison.Ordinal) < 0) return 77;
            if (gateSummaryLabel.Text.IndexOf("not one of the panels",
                                              StringComparison.Ordinal) >= 0) return 77;

            // The run path resolves AUTO to a concrete key and re-gates on it.
            // Same verdict, or the live bar was lying.
            RunRequest autoRequest = ReadRunRequest();
            if (!autoRequest.PanelWasAuto) return 77;
            autoRequest.PanelKey = "LEFT";                 // as ReadAndValidateConfiguration does
            GateResult autoAfterResolve = EvaluateGate(autoRequest);
            if (autoAfterResolve.Blocked) return 77;
            if (!autoAfterResolve.Exploratory) return 77;

            // Confirmatory tier refuses AUTO on both sides.
            SelectChoice(tierBox, "confirmatory");
            RefreshGateSummary();
            if (runButton.Enabled) return 77;
            SelectChoice(tierBox, "exploratory");

            // AUTO plus a custom panel JSON: refused by the LIVE bar, not by an
            // exception after the review dialog.
            panelConfigBox.Text = customPath;
            RefreshGateSummary();
            if (runButton.Enabled) return 77;
            panelConfigBox.Text = "";

            SelectChoice(panelBox, "LEFT");
            OnPanelChanged();
            RefreshGateSummary();

            return 0;
        }

        private static bool IsDescendantOf(Control control, Control ancestor)
        {
            for (Control current = control; current != null; current = current.Parent)
                if (current == ancestor) return true;
            return false;
        }

        private static bool GroupContainsItsContent(GroupBox group)
        {
            if (group == null || group.Controls.Count == 0) return false;
            Control content = group.Controls[0];
            int requiredBottom = content.Top + content.GetPreferredSize(
                new Size(Math.Max(1, content.Width), 0)).Height;
            return group.ClientSize.Height >= requiredBottom + group.Padding.Bottom;
        }

        private void BuildRouteGroup()
        {
            routeGroup = new GroupBox();
            routeGroup.Text = "Step 1 — what kind of images are these?";
            routeGroup.Dock = DockStyle.Top;
            routeGroup.AutoSize = true;
            routeGroup.AutoSizeMode = AutoSizeMode.GrowAndShrink;
            routeGroup.Padding = new Padding(10);

            TableLayoutPanel table = new TableLayoutPanel();
            table.Dock = DockStyle.Top;
            table.AutoSize = true;
            table.ColumnCount = 2;
            table.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, ScaledF(175F)));
            table.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100F));
            routeGroup.Controls.Add(table);

            List<string> items = new List<string>();
            foreach (ImageRoute route in RouteCatalog.All())
                items.Add(RouteCatalog.Describe(route).DisplayName);

            routeBox = new ComboBox();
            routeBox.Dock = DockStyle.Fill;
            routeBox.DropDownStyle = ComboBoxStyle.DropDownList;
            routeBox.Items.AddRange(items.ToArray());
            // Owner draw is the only way to grey one item of a DropDownList.
            routeBox.DrawMode = DrawMode.OwnerDrawFixed;
            routeBox.DrawItem += new DrawItemEventHandler(RouteBoxDrawItem);
            routeBox.SelectedIndex = 0;
            lastValidRouteIndex = 0;

            table.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            table.Controls.Add(MakeLabel("Image type / route"), 0, 0);
            table.Controls.Add(routeBox, 1, 0);

            routeHelpLabel = new Label();
            routeHelpLabel.AutoSize = true;
            routeHelpLabel.MaximumSize = new Size(Scaled(980), 0);
            routeHelpLabel.ForeColor = Color.FromArgb(75, 75, 75);
            routeHelpLabel.Padding = new Padding(4, 4, 4, 4);
            table.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            table.Controls.Add(routeHelpLabel, 1, 1);

            // Permanently visible, so the greyed entry is never a mystery.
            routeUnavailableLabel = new Label();
            routeUnavailableLabel.AutoSize = true;
            routeUnavailableLabel.MaximumSize = new Size(Scaled(980), 0);
            routeUnavailableLabel.ForeColor = Color.FromArgb(150, 60, 0);
            routeUnavailableLabel.Padding = new Padding(4, 0, 4, 6);
            routeUnavailableLabel.Text = DescribeUnavailableRoutes();
            routeUnavailableLabel.Visible = routeUnavailableLabel.Text.Length > 0;
            table.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            table.Controls.Add(routeUnavailableLabel, 1, 2);

            tierBox = MakeCombo(
                new string[]
                {
                    "exploratory — numbers may be inspected, never reported as an endpoint",
                    "dry — smoke test; caps the work and forbids aggregation",
                    "confirmatory — every analysis channel must carry a frozen threshold"
                },
                "exploratory — numbers may be inspected, never reported as an endpoint",
                false);
            table.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            table.Controls.Add(MakeLabel("Run tier"), 0, 3);
            table.Controls.Add(tierBox, 1, 3);
            toolTips.SetToolTip(tierBox,
                "The tier decides how hard the launcher fails. Confirmatory refuses to start " +
                "unless every analysis channel carries a frozen, control-derived threshold. " +
                "Exploratory allows adaptive Otsu behind a typed confirmation and stamps the " +
                "run folder. Dry is a smoke test.");

            string launcherInvocation =
                "launcher exe — fiji-windows-*.exe --headless --console --run (v1.7.2)";
            string bundledJvmInvocation =
                "bundled JVM — java.exe + ij1-patcher agent (as scripts/Invoke-Stage2Sharded.ps1)";
            invocationBox = MakeCombo(
                new string[] { launcherInvocation, bundledJvmInvocation },
                GetWindowsArchitecture() == "ARM64"
                    ? bundledJvmInvocation
                    : launcherInvocation,
                false);
            table.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            table.Controls.Add(MakeLabel("How to start Fiji"), 0, 4);
            table.Controls.Add(invocationBox, 1, 4);
            toolTips.SetToolTip(invocationBox,
                "Legacy mode always uses the launcher exe, because that is what v1.7.2 used. " +
                "On win-arm64 the Fiji launcher exe is the only one present and is unreliable; " +
                "the bundled JVM path starts the same engine directly with the ij1-patcher " +
                "agent, which is how the whole-slide shard script has always run it. Whichever " +
                "is used is written into launcher_run.txt.");

            routeBox.SelectedIndexChanged += delegate { OnRouteSelectionChanged(); };
            tierBox.SelectedIndexChanged += delegate { RefreshGateSummary(); };
            invocationBox.SelectedIndexChanged += delegate { RefreshGateSummary(); };
        }

        private static string DescribeUnavailableRoutes()
        {
            StringBuilder text = new StringBuilder();
            foreach (ImageRoute route in RouteCatalog.All())
            {
                RouteSpec spec = RouteCatalog.Describe(route);
                if (spec.Available) continue;
                if (text.Length > 0) text.Append("\r\n");
                text.Append(spec.DisplayName)
                    .Append(" is listed but cannot be selected. ")
                    .Append(FirstParagraph(spec.UnavailableReason));
            }
            return text.ToString();
        }

        private static string FirstParagraph(string value)
        {
            if (string.IsNullOrEmpty(value)) return "";
            int cut = value.IndexOf("\r\n\r\n", StringComparison.Ordinal);
            return cut > 0 ? value.Substring(0, cut) : value;
        }

        private void RouteBoxDrawItem(object sender, DrawItemEventArgs e)
        {
            if (e.Index < 0) return;
            ImageRoute route = RouteCatalog.All()[e.Index];
            bool available = RouteCatalog.IsAvailable(route);
            string text = Convert.ToString(routeBox.Items[e.Index], CultureInfo.InvariantCulture);

            // Never paint an unselectable item with the selection highlight.
            if (available)
                e.DrawBackground();
            else
                using (SolidBrush background = new SolidBrush(SystemColors.Window))
                    e.Graphics.FillRectangle(background, e.Bounds);

            Color foreground = !available
                ? SystemColors.GrayText
                : ((e.State & DrawItemState.Selected) == DrawItemState.Selected
                    ? SystemColors.HighlightText
                    : SystemColors.WindowText);
            TextRenderer.DrawText(
                e.Graphics, text, e.Font, e.Bounds, foreground,
                TextFormatFlags.Left | TextFormatFlags.VerticalCenter |
                TextFormatFlags.NoPrefix | TextFormatFlags.EndEllipsis);
            if (available) e.DrawFocusRectangle();
        }

        /// The selection veto. An unavailable route bounces back to the last
        /// valid one and explains itself in place, without a modal box, so
        /// arrowing through the list is not punished.
        private void OnRouteSelectionChanged()
        {
            if (routeVetoInProgress) return;
            int index = Math.Max(0, routeBox.SelectedIndex);
            RouteSpec spec = RouteCatalog.Describe(RouteCatalog.All()[index]);
            if (!spec.Available)
            {
                routeVetoInProgress = true;
                try { routeBox.SelectedIndex = lastValidRouteIndex; }
                finally { routeVetoInProgress = false; }
                routeHelpLabel.ForeColor = Color.DarkRed;
                routeHelpLabel.Text = spec.DisplayName + "\r\n\r\n" + spec.UnavailableReason;
                return;
            }
            lastValidRouteIndex = index;
            OnRouteChanged();
        }

        private void BuildToolsGroup()
        {
            toolsGroup = new GroupBox();
            toolsGroup.Text = "Whole-slide tools and locations";
            toolsGroup.Dock = DockStyle.Top;
            toolsGroup.AutoSize = true;
            toolsGroup.Padding = new Padding(10);

            TableLayoutPanel table = new TableLayoutPanel();
            table.Dock = DockStyle.Top;
            table.AutoSize = true;
            table.ColumnCount = 3;
            table.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, ScaledF(175F)));
            table.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100F));
            table.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, ScaledF(95F)));
            toolsGroup.Controls.Add(table);

            quPathBox = AddBrowseRow(table, 0, "QuPath console executable", false);
            wsiInputBox = AddBrowseRow(table, 1, "Slide (.vsi) file or folder", false);
            wsiOutputBox = AddBrowseRow(table, 2, "Stage 1 output root", true);
            slideMetadataBox = AddBrowseRow(table, 3, "Slide metadata CSV", false);
            pythonBox = AddBrowseRow(table, 4, "Python executable", false);

            toolTips.SetToolTip(quPathBox,
                "The CONSOLE build. The windowed QuPath detaches immediately, so the launcher " +
                "would see exit 0 with no script output and call a failed tiling run a success.");
            toolTips.SetToolTip(wsiInputBox,
                "A single .vsi, or a folder of them. Fiji's Bio-Formats cannot decode the " +
                "JPEG-2000 .ets pyramid at all, which is why QuPath does the reading.");
            toolTips.SetToolTip(wsiOutputBox,
                "Tiles, per-tile ROI sets, samplesheet.csv and stage1_manifest.json are written " +
                "here, and read back by stages 2 and 3.");
            toolTips.SetToolTip(slideMetadataBox,
                "vsi_filename,mouse_id,genotype,condition. Without it the mouse-level " +
                "aggregation has to be assembled by hand afterwards.");

            table.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            wsiResumeBox = new CheckBox();
            wsiResumeBox.Text = "Resume an existing stage 1 root instead of starting over";
            wsiResumeBox.Checked = true;
            wsiResumeBox.AutoSize = true;
            table.Controls.Add(wsiResumeBox, 1, 5);

            table.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            wsiPartitionBox = new CheckBox();
            wsiPartitionBox.Text = "Partition tissue into damaged / intact ROIs during tiling";
            wsiPartitionBox.AutoSize = true;
            table.Controls.Add(wsiPartitionBox, 1, 6);

            table.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            table.Controls.Add(MakeLabel("Tile cap per slide (0 = all)"), 0, 7);
            wsiMaxTilesBox = new NumericUpDown();
            wsiMaxTilesBox.Minimum = 0;
            wsiMaxTilesBox.Maximum = 1000000;
            wsiMaxTilesBox.Value = 0;
            wsiMaxTilesBox.Dock = DockStyle.Fill;
            table.Controls.Add(wsiMaxTilesBox, 1, 7);
            toolTips.SetToolTip(wsiMaxTilesBox,
                "A cap makes stage 1 record coverage as incomplete, which makes stage 3 refuse " +
                "to write a slide summary. That is intended: a partially tiled slide has no " +
                "slide-level denominator.");

            wsiOnlyRows = new Control[]
            {
                quPathBox, wsiInputBox, wsiOutputBox, slideMetadataBox, pythonBox,
                wsiResumeBox, wsiPartitionBox, wsiMaxTilesBox
            };
            fijiOnlyRows = new Control[0];
        }

        private TextBox AddBrowseRow(TableLayoutPanel table, int row, string label, bool folder)
        {
            table.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            table.Controls.Add(MakeLabel(label), 0, row);
            TextBox box = new TextBox();
            box.Dock = DockStyle.Fill;
            table.Controls.Add(box, 1, row);
            Button browse = new Button();
            browse.Text = "Browse...";
            browse.Dock = DockStyle.Fill;
            bool pickFolder = folder;
            browse.Click += delegate
            {
                if (pickFolder) BrowseFolder(box);
                else BrowseAnyFile(box);
            };
            table.Controls.Add(browse, 2, row);
            return box;
        }

        private void BrowseAnyFile(TextBox target)
        {
            using (OpenFileDialog dialog = new OpenFileDialog())
            {
                dialog.Title = "Select a file";
                dialog.Filter =
                    "Programs and data (*.exe;*.csv;*.vsi)|*.exe;*.csv;*.vsi|All files (*.*)|*.*";
                dialog.CheckFileExists = true;
                string current = (target.Text ?? "").Trim();
                if (File.Exists(current)) dialog.FileName = current;
                else if (Directory.Exists(current)) dialog.InitialDirectory = current;
                if (dialog.ShowDialog(this) == DialogResult.OK)
                    target.Text = dialog.FileName;
            }
        }

        private void BuildMeasurementGroup()
        {
            measurementGroup = new GroupBox();
            measurementGroup.Text =
                "What is being measured — thresholds and the sparse-region floor";
            measurementGroup.Dock = DockStyle.Top;
            measurementGroup.AutoSize = true;
            measurementGroup.Padding = new Padding(10);

            TableLayoutPanel outer = new TableLayoutPanel();
            outer.Dock = DockStyle.Top;
            outer.AutoSize = true;
            outer.ColumnCount = 1;
            measurementGroup.Controls.Add(outer);

            thresholdNoteLabel = new Label();
            thresholdNoteLabel.AutoSize = true;
            thresholdNoteLabel.MaximumSize = new Size(Scaled(980), 0);
            thresholdNoteLabel.Padding = new Padding(0, 0, 0, 6);
            outer.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            outer.Controls.Add(thresholdNoteLabel, 0, 0);

            thresholdTable = new TableLayoutPanel();
            thresholdTable.Dock = DockStyle.Top;
            thresholdTable.AutoSize = true;
            thresholdTable.ColumnCount = 4;
            thresholdTable.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, ScaledF(175F)));
            thresholdTable.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, ScaledF(235F)));
            thresholdTable.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, ScaledF(110F)));
            thresholdTable.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100F));
            outer.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            outer.Controls.Add(thresholdTable, 0, 1);

            TableLayoutPanel floor = new TableLayoutPanel();
            floor.Dock = DockStyle.Top;
            floor.AutoSize = true;
            floor.ColumnCount = 3;
            floor.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, ScaledF(175F)));
            floor.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, ScaledF(110F)));
            floor.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100F));
            outer.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            outer.Controls.Add(floor, 0, 2);

            floor.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            floor.Controls.Add(MakeLabel("Minimum nuclei per region"), 0, 0);
            minNucleiBox = new NumericUpDown();
            minNucleiBox.Minimum = 0;
            minNucleiBox.Maximum = 100000;
            minNucleiBox.Value = 0;              // H3: NOT the engine's default of 1
            minNucleiBox.Dock = DockStyle.Fill;
            floor.Controls.Add(minNucleiBox, 1, 0);
            Label floorHelp = new Label();
            floorHelp.AutoSize = true;
            floorHelp.MaximumSize = new Size(Scaled(680), 0);
            floorHelp.ForeColor = Color.FromArgb(75, 75, 75);
            floorHelp.Text =
                "A region with fewer accepted nuclei is dropped, and its tissue area is dropped " +
                "with it — so the denominator loses exactly the sparse regions while the other " +
                "regions keep their numerators. 0 keeps every region. The engine's own default " +
                "is 1, which is why this launcher always writes the value explicitly.";
            floor.Controls.Add(floorHelp, 2, 0);
            toolTips.SetToolTip(minNucleiBox,
                "Leave at 0 unless a protocol requires otherwise. Any non-zero value is refused " +
                "outright on the whole-slide route and on any panel with an area endpoint.");

            unlockPilotPanelsBox = new CheckBox();
            unlockPilotPanelsBox.Text =
                "Enable non-interpretable pilot panels (adds panel T to the picker)";
            unlockPilotPanelsBox.AutoSize = true;
            unlockPilotPanelsBox.CheckedChanged += delegate { OnPilotUnlockChanged(); };
            outer.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            outer.Controls.Add(unlockPilotPanelsBox, 0, 3);
            toolTips.SetToolTip(unlockPilotPanelsBox,
                "Panel T maps an unrelated skin smFISH sample onto a lung panel shape and its " +
                "nuclear channel is index 4, not 1. It exists to test the plumbing. Selecting " +
                "it requires typing a confirmation phrase and stamps the output folder.");
        }

        private void BuildGateSummary()
        {
            gateSummaryLabel = new Label();
            gateSummaryLabel.AutoSize = true;
            gateSummaryLabel.Dock = DockStyle.Fill;
            // No MaximumSize: the bar spans the whole form, so the verdict
            // wraps at the window edge instead of at a fixed 1180 px.
            gateSummaryLabel.Padding = new Padding(6, 4, 6, 4);
            gateSummaryLabel.BorderStyle = BorderStyle.FixedSingle;
            gateSummaryLabel.Font = new Font(Font, FontStyle.Bold);
            gateSummaryLabel.Text = "Checking...";
            // The pinned bottom stack, not the scrolling pane: the verdict has
            // to be next to the button it is a verdict about.
            rootTable.Controls.Add(gateSummaryLabel, 0, RootRowGate);
        }

        /// Every control that can change a gate outcome refreshes the live
        /// summary, so the review dialog is never the first sight of a problem.
        private void HookGateRefresh()
        {
            EventHandler refresh = delegate { RefreshGateSummary(); };
            panelBox.SelectedIndexChanged += delegate { OnPanelChanged(); };
            panelBox.TextChanged += delegate { OnPanelChanged(); };
            projectionBox.SelectedIndexChanged += refresh;
            tissueModeBox.SelectedIndexChanged += refresh;
            compartmentModeBox.SelectedIndexChanged += refresh;
            wholeCompartmentBox.SelectedIndexChanged += refresh;
            segmenterBox.SelectedIndexChanged += refresh;
            minNucleiBox.ValueChanged += refresh;
            advancedBox.TextChanged += refresh;
            panelConfigBox.TextChanged += refresh;
            inputBox.TextChanged += refresh;
            outputBaseBox.TextChanged += refresh;
            fijiBox.TextChanged += refresh;
            quPathBox.TextChanged += refresh;
            pythonBox.TextChanged += refresh;
            wsiInputBox.TextChanged += refresh;
            wsiOutputBox.TextChanged += refresh;
            slideMetadataBox.TextChanged += refresh;
        }

        // =================================================================
        // ROUTE VISIBILITY
        //
        //  control                          R1 confocal  R2 slide  R4 legacy
        //  --------------------------------------------------------------
        //  Original image folder                shown     HIDDEN     shown
        //  Whole-slide tools group              HIDDEN    shown      HIDDEN
        //  Threshold grid + nuclei floor        shown     shown      HIDDEN*
        //  Run tier                             shown     shown      HIDDEN
        //  How to start Fiji                    shown     shown      HIDDEN**
        //  Z-stack handling / filter / limit    shown     HIDDEN†    shown
        //  Create visual merge panels           shown     HIDDEN     shown
        //
        //   *  HIDDEN in legacy mode BECAUSE v1.7.2 could not set them. Showing
        //      a control that legacy mode must ignore is how the mode stops
        //      being legacy.
        //   ** legacy mode is pinned to the v1.7.2 launcher-exe invocation.
        //   †  forced: tiles are single plane, one flat folder, no limit.
        // =================================================================

        private ImageRoute SelectedRoute
        {
            get
            {
                int index = routeBox == null ? 0 : Math.Max(0, routeBox.SelectedIndex);
                return RouteCatalog.All()[index];
            }
        }

        private RunTier SelectedTier
        {
            get
            {
                string key = ChoiceKey(tierBox);
                if (string.Equals(key, "dry", StringComparison.OrdinalIgnoreCase))
                    return RunTier.Dry;
                if (string.Equals(key, "confirmatory", StringComparison.OrdinalIgnoreCase))
                    return RunTier.Confirmatory;
                return RunTier.Exploratory;
            }
        }

        private FijiInvocation SelectedInvocation
        {
            get
            {
                if (SelectedRoute == ImageRoute.LegacyFiji172)
                    return FijiInvocation.LauncherExe;
                return invocationBox != null && invocationBox.SelectedIndex == 1
                    ? FijiInvocation.BundledJvm
                    : FijiInvocation.LauncherExe;
            }
        }

        private void OnRouteChanged()
        {
            // Every group box below is AutoSize, inside an AutoSize
            // TableLayoutPanel, inside an AutoScroll one. Toggling their
            // visibility one statement at a time re-lays the whole form once
            // per statement, which measured in SECONDS. One pass instead.
            SuspendLayout();
            try { OnRouteChangedCore(); }
            finally { ResumeLayout(true); }
        }

        private void OnRouteChangedCore()
        {
            ImageRoute route = SelectedRoute;
            RouteSpec spec = RouteCatalog.Describe(route);

            routeHelpLabel.ForeColor = Color.FromArgb(75, 75, 75);
            routeHelpLabel.Text = spec.OneLine + "\r\n\r\nStages: " + DescribeStages(spec);

            bool slide = route == ImageRoute.IfSlideScanner;
            bool legacy = route == ImageRoute.LegacyFiji172;

            toolsGroup.Visible = slide;
            measurementGroup.Visible = !legacy;
            tierBox.Visible = !legacy;
            tierBox.Enabled = !legacy;
            invocationBox.Enabled = !legacy;
            if (legacy)
                invocationBox.SelectedIndex = 0;
            else if (GetWindowsArchitecture() == "ARM64")
                invocationBox.SelectedIndex = 1;

            // Route 2 forces these; hiding them is how the user learns they are
            // not negotiable rather than wondering why they were ignored.
            projectionBox.Enabled = !slide;
            singlePlaneBox.Enabled = !slide;
            recursiveBox.Enabled = !slide;
            includeRegexBox.Enabled = !slide;
            maxImagesBox.Enabled = !slide;
            inputBox.Enabled = !slide;
            previewButton.Visible = !slide;

            RebuildThresholdGrid();
            RefreshGateSummary();
        }

        private static string DescribeStages(RouteSpec spec)
        {
            if (spec.Stages.Count == 0) return "(none — this route runs nothing)";
            List<string> parts = new List<string>();
            for (int i = 0; i < spec.Stages.Count; i++)
                parts.Add((i + 1) + ") " + spec.Stages[i].Title + " [" + spec.Stages[i].Tool + "]");
            return string.Join("   ->   ", parts.ToArray());
        }

        private void OnPanelChanged()
        {
            UpdatePanelHelp();
            RebuildThresholdGrid();
            RefreshGateSummary();
        }

        private void OnPilotUnlockChanged()
        {
            if (unlockPilotPanelsBox.Checked)
            {
                if (panelBox.Items.IndexOf(PilotPanelItem) < 0)
                    panelBox.Items.Add(PilotPanelItem);
            }
            else
            {
                RemovePilotPanelFromPicker();
                if (string.Equals(ChoiceKey(panelBox), "T", StringComparison.OrdinalIgnoreCase))
                    SelectChoice(panelBox, "AUTO");
            }
            RefreshGateSummary();
        }

        private void RemovePilotPanelFromPicker()
        {
            for (int index = panelBox.Items.Count - 1; index >= 0; index--)
            {
                string item = Convert.ToString(panelBox.Items[index], CultureInfo.InvariantCulture);
                if (item != null &&
                    (string.Equals(item, PilotPanelItem, StringComparison.Ordinal) ||
                     item.StartsWith("T —", StringComparison.Ordinal) ||
                     string.Equals(item.Trim(), "T", StringComparison.Ordinal)))
                    panelBox.Items.RemoveAt(index);
            }
        }

        // =================================================================
        // THE THRESHOLD GRID  (H2)
        // =================================================================

        /// The panel table is parsed out of the embedded pipeline rather than
        /// retyped in C#, so the grid cannot offer a variable the engine does
        /// not read, and cannot omit one it does.
        private Dictionary<string, PanelDef> EnginePanels
        {
            get
            {
                if (enginePanels == null && enginePanelsError == null)
                {
                    try
                    {
                        RuntimePaths paths = RuntimeBundle.EnsureExtracted();
                        string text = File.ReadAllText(paths.ScriptPath, Encoding.UTF8);
                        enginePanels = PanelRegistry.ParseFromPipeline(text);
                        engineThresholdMarkers = PanelRegistry.ParseThresholdMarkerTokens(text);
                    }
                    catch (Exception ex)
                    {
                        enginePanelsError = ex.Message;
                        enginePanels = new Dictionary<string, PanelDef>(
                            StringComparer.OrdinalIgnoreCase);
                        engineThresholdMarkers = new HashSet<string>(StringComparer.Ordinal);
                    }
                }
                return enginePanels;
            }
        }

        /// <summary>
        /// The built-in table first, then the selected IFQ_PANEL_CONFIG.
        ///
        /// Returning null for a custom panel key -- which is what v1.8.0.0 did,
        /// because only AUTO was special-cased -- made the gate's whole H2 block
        /// unreachable for custom panels, since it was guarded on `panel != null`.
        /// A custom panel key plus a custom panel JSON plus confirmatory tier
        /// plus zero thresholds produced a GREEN bar and a run record saying
        /// run_classification=THRESHOLDS_FROZEN. Custom panels are a supported
        /// engine path (IF_Quant_Pipeline.groovy:688-743), so they get a real
        /// channel list and a real threshold grid.
        ///
        /// customPanelError is set whenever a custom key was asked for and no
        /// channel list came back; the gate turns that into a hard block.
        /// </summary>
        private PanelDef ResolveSelectedPanel()
        {
            return ResolvePanelForKey(ChoiceKey(panelBox));
        }

        /// <summary>
        /// The same resolution, driven by a KEY rather than by the combo box.
        ///
        /// This split exists because of a real disagreement between the live
        /// gate and the run path. ReadAndValidateConfiguration resolves AUTO to
        /// a concrete panel and writes it into request.PanelKey, but the gate
        /// then re-resolved from the COMBO, which still said "AUTO" -- so the
        /// two halves of the launcher were reasoning about different panels.
        /// Every caller now names the key it means.
        /// </summary>
        private PanelDef ResolvePanelForKey(string key)
        {
            customPanelError = null;
            if (string.IsNullOrEmpty(key)) return null;
            if (string.Equals(key, "AUTO", StringComparison.OrdinalIgnoreCase)) return null;
            PanelDef panel;
            if (EnginePanels.TryGetValue(key, out panel)) return panel;
            return ResolveCustomPanel(key);
        }

        /// <summary>
        /// The channel list a REQUEST runs under. The gate, RunEnvironment and
        /// the launch seal all call this, so they cannot be looking at three
        /// different panels.
        ///
        /// AUTO stays deliberately unresolved even after the caller has picked a
        /// fallback key: AUTO decides the panel per image, so there is no single
        /// channel list to freeze thresholds against, and pretending otherwise
        /// would let the launcher record "3/3 thresholds fixed" for a run in
        /// which most images used a different panel.
        /// </summary>
        private PanelDef PanelForRequest(RunRequest request)
        {
            if (request == null) return null;
            if (request.PanelWasAuto)
            {
                customPanelError = null;
                return null;
            }
            return ResolvePanelForKey(request.PanelKey);
        }

        private PanelDef ResolveCustomPanel(string key)
        {
            string path = panelConfigBox == null ? "" : (panelConfigBox.Text ?? "").Trim();
            if (path.Length == 0)
            {
                // H1_PANEL_UNKNOWN already covers this and says it better.
                return null;
            }

            CustomPanelParse parse = LoadCustomPanels(path);
            if (!parse.Ok)
            {
                customPanelError = parse.Error;
                return null;
            }
            PanelDef panel;
            if (parse.Panels.TryGetValue(key, out panel)) return panel;

            List<string> available = new List<string>(parse.Panels.Keys);
            available.Sort(StringComparer.Ordinal);
            customPanelError =
                "panel '" + key + "' is not declared in " + path + ". That file declares: " +
                (available.Count == 0 ? "(nothing)" : string.Join(", ", available.ToArray())) +
                ". The engine aborts on an unknown IFQ_PANEL " +
                "(IF_Quant_Pipeline.groovy:866-868).";
            return null;
        }

        /// <summary>
        /// RefreshGateSummary runs on every keystroke, so the panel JSON is read
        /// from disk only when its path or its last-write time has changed.
        /// </summary>
        private CustomPanelParse LoadCustomPanels(string path)
        {
            string stamp;
            try
            {
                stamp = path + "|" +
                        File.GetLastWriteTimeUtc(path).Ticks.ToString(CultureInfo.InvariantCulture) +
                        "|" + new FileInfo(path).Length.ToString(CultureInfo.InvariantCulture);
            }
            catch (Exception ex)
            {
                CustomPanelParse unreadable = new CustomPanelParse();
                unreadable.Error = "the custom panel file could not be read: " + ex.Message;
                return unreadable;
            }
            if (customPanels != null &&
                string.Equals(customPanelsKey, stamp, StringComparison.Ordinal))
                return customPanels;

            CustomPanelParse parse;
            try
            {
                parse = CustomPanelRegistry.Parse(
                    File.ReadAllText(path, Encoding.UTF8),
                    MarkerDefaultRoles,
                    new List<string>(EnginePanels.Keys),
                    EnginePanelChannelsAreThresholdable);
            }
            catch (Exception ex)
            {
                parse = new CustomPanelParse();
                parse.Error = "the custom panel file could not be read: " + ex.Message;
            }
            customPanelsKey = stamp;
            customPanels = parse;
            return parse;
        }

        /// The engine fills a channel's missing role from the marker registry's
        /// default_role, so the launcher has to as well or it cannot tell a
        /// nuclear channel from an analysis channel in a custom panel.
        private Dictionary<string, string> MarkerDefaultRoles
        {
            get
            {
                if (markerDefaultRoles == null)
                {
                    markerDefaultRoles = new Dictionary<string, string>(StringComparer.Ordinal);
                    try
                    {
                        RuntimePaths paths = RuntimeBundle.EnsureExtracted();
                        markerDefaultRoles = MarkerRoleDefaults.ParseFromRegistry(
                            File.ReadAllText(paths.RegistryPath, Encoding.UTF8));
                    }
                    catch
                    {
                        // Leave it empty: a custom channel without an explicit
                        // role then fails to resolve, which is a block, not a
                        // guess.
                    }
                }
                return markerDefaultRoles;
            }
        }

        private bool EnginePanelChannelsAreThresholdable
        {
            get
            {
                foreach (KeyValuePair<string, PanelDef> pair in EnginePanels)
                    return pair.Value.ChannelsAreThresholdable;
                return false;
            }
        }

        private void RebuildThresholdGrid()
        {
            if (thresholdTable == null) return;
            thresholdTable.SuspendLayout();
            try { RebuildThresholdGridCore(); }
            finally { thresholdTable.ResumeLayout(true); }
        }

        private void RebuildThresholdGridCore()
        {
            // ---------------------------------------------------------------
            // N2. A TYPED CUTOFF BELONGS TO THE PANEL IT WAS TYPED FOR.
            //
            // This method runs on every route change and every panel change, so
            // it has to carry typed values across a rebuild or a route change
            // would silently empty the grid. It used to carry them by MARKER
            // TOKEN alone: panel LEFT with KRT5=777 and AGER=888, then switch to
            // panel A, and the boxes came back showing 777/888 -- ReadRunRequest
            // read them into request.Thresholds, the bar went GREEN "2/2
            // thresholds fixed, confirmatory", and the environment carried
            // IFQ_KRT5_THRESHOLD=777 for a panel the operator never set it for.
            // A threshold is a per-panel, control-derived number; the same
            // marker in a different panel is a different acquisition.
            //
            // So: remember which panel the boxes belonged to, and re-use their
            // values only for that same panel.
            // ---------------------------------------------------------------
            string previousGridPanelKey = thresholdGridPanelKey;
            Dictionary<string, string> retained =
                new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            foreach (KeyValuePair<string, TextBox> pair in thresholdBoxes)
                retained[pair.Key] = pair.Value.Text;
            thresholdGridPanelKey = null;

            thresholdTable.Controls.Clear();
            thresholdTable.RowStyles.Clear();
            thresholdTable.RowCount = 0;
            thresholdBoxes.Clear();
            thresholdStatusLabels.Clear();

            if (SelectedRoute == ImageRoute.LegacyFiji172)
            {
                thresholdNoteLabel.ForeColor = Color.FromArgb(75, 75, 75);
                thresholdNoteLabel.Text =
                    "Legacy mode writes no threshold variables, because v1.7.2 wrote none. " +
                    "Every analysis channel uses the engine's per-region adaptive Otsu, exactly " +
                    "as the original run did.";
                return;
            }
            if (enginePanelsError != null)
            {
                thresholdNoteLabel.ForeColor = Color.DarkRed;
                thresholdNoteLabel.Text =
                    "The embedded pipeline could not be read, so no threshold grid can be " +
                    "built: " + enginePanelsError;
                return;
            }

            PanelDef panel = ResolveSelectedPanel();
            if (panel == null)
            {
                bool auto = string.Equals(ChoiceKey(panelBox), "AUTO",
                                          StringComparison.OrdinalIgnoreCase);
                thresholdNoteLabel.ForeColor = auto ? Color.FromArgb(150, 60, 0) : Color.DarkRed;
                if (auto)
                    thresholdNoteLabel.Text =
                        "AUTO resolves a panel per image, so no channel threshold can be frozen " +
                        "before the run. Every channel will use per-region adaptive Otsu and the " +
                        "run will be marked EXPLORATORY. Select the panel explicitly to freeze " +
                        "thresholds.";
                else if (!string.IsNullOrEmpty(customPanelError))
                    // The grid used to be silently EMPTY here, which read as
                    // "this panel has no channels to freeze" rather than "the
                    // launcher does not know what this panel measures".
                    thresholdNoteLabel.Text =
                        "NO CHANNEL LIST for panel '" + ChoiceKey(panelBox) + "', so no " +
                        "threshold can be frozen and this run cannot start: " + customPanelError;
                else
                    thresholdNoteLabel.Text =
                        "Select a built-in staining panel, or a custom panel key together with " +
                        "its validated panel JSON under Advanced study options, to see its " +
                        "analysis channels.";
                return;
            }

            // N2. Values survive a rebuild only within the same panel.
            bool samePanel = string.Equals(
                previousGridPanelKey, panel.Key, StringComparison.OrdinalIgnoreCase);
            thresholdGridPanelKey = panel.Key;

            thresholdNoteLabel.ForeColor = Color.FromArgb(75, 75, 75);
            thresholdNoteLabel.Text =
                "Marker thresholds for panel " + panel.Key +
                (panel.IsCustom ? " (custom panel, read from the selected panel JSON)" : "") +
                ". An empty box is NOT neutral: " +
                "the engine then picks a cutoff per region with Otsu and records the source as " +
                "'adaptive_otsu_exploratory'. On a background-dominated region the cutoff comes " +
                "from background.";

            int row = 0;
            foreach (ChannelDef channel in panel.AnalysisChannels)
            {
                thresholdTable.RowCount = row + 1;
                thresholdTable.RowStyles.Add(new RowStyle(SizeType.AutoSize));

                string caption = channel.Marker + (channel.AreaMarker ? "  (AREA ENDPOINT)" : "");
                Label name = MakeLabel(caption);
                if (channel.AreaMarker) name.Font = new Font(Font, FontStyle.Bold);
                thresholdTable.Controls.Add(name, 0, row);

                bool engineReadsIt = ThresholdSurface.EngineReads(
                    panel, channel, engineThresholdMarkers);
                thresholdTable.Controls.Add(MakeLabel(channel.ThresholdEnvName), 1, row);

                TextBox box = new TextBox();
                box.Dock = DockStyle.Fill;
                box.Enabled = engineReadsIt;
                string previous;
                if (samePanel && retained.TryGetValue(channel.Token, out previous))
                    box.Text = previous;
                thresholdTable.Controls.Add(box, 2, row);
                thresholdBoxes[channel.Token] = box;

                Label status = new Label();
                status.AutoSize = true;
                status.MaximumSize = new Size(Scaled(520), 0);
                status.Anchor = AnchorStyles.Left;
                thresholdTable.Controls.Add(status, 3, row);
                thresholdStatusLabels[channel.Token] = status;

                box.TextChanged += delegate { RefreshGateSummary(); };
                row++;
            }
            UpdateThresholdStatusLabels(panel);
        }

        private static void SetStatus(Label label, string text, Color colour)
        {
            if (!string.Equals(label.Text, text, StringComparison.Ordinal)) label.Text = text;
            if (label.ForeColor != colour) label.ForeColor = colour;
        }

        private void UpdateThresholdStatusLabels(PanelDef panel)
        {
            if (panel == null) return;
            foreach (ChannelDef channel in panel.AnalysisChannels)
            {
                Label status;
                TextBox box;
                if (!thresholdStatusLabels.TryGetValue(channel.Token, out status)) continue;
                if (!thresholdBoxes.TryGetValue(channel.Token, out box)) continue;

                if (!ThresholdSurface.EngineReads(panel, channel, engineThresholdMarkers))
                {
                    SetStatus(status,
                        "NO VARIABLE — the engine reads no threshold for this marker; it is " +
                        "adaptive by construction", Color.FromArgb(150, 60, 0));
                    continue;
                }
                string value = (box.Text ?? "").Trim();
                if (value.Length == 0)
                {
                    SetStatus(status, "ADAPTIVE (per-region Otsu)" +
                        (channel.AreaMarker ? " — on an AREA endpoint" : ""), Color.DarkRed);
                    continue;
                }
                double parsed;
                if (!Double.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture,
                                     out parsed) || parsed <= 0.0)
                {
                    SetStatus(status, "NOT A POSITIVE NUMBER", Color.DarkRed);
                    continue;
                }
                SetStatus(status, "fixed_predeclared", Color.FromArgb(0, 110, 40));
            }
        }

        // =================================================================
        // READING THE UI INTO A RUN REQUEST
        // =================================================================

        private RunRequest ReadRunRequest()
        {
            RunRequest request = new RunRequest();
            request.Route = SelectedRoute;
            request.Tier = SelectedRoute == ImageRoute.LegacyFiji172
                ? RunTier.Exploratory
                : SelectedTier;
            request.Invocation = SelectedInvocation;

            request.InputDirectory = inputBox.Text.Trim();
            request.OutputBase = outputBaseBox.Text.Trim();
            request.RunName = runNameBox.Text.Trim();
            request.FijiPath = fijiBox.Text.Trim();
            request.QuPathExecutable = quPathBox.Text.Trim();
            request.PythonExecutable = pythonBox.Text.Trim();

            request.WsiInput = wsiInputBox.Text.Trim();
            request.WsiOutput = wsiOutputBox.Text.Trim();
            request.SlideMetadataCsv = slideMetadataBox.Text.Trim();
            request.WsiResume = wsiResumeBox.Checked;
            request.WsiPartitionDamage = wsiPartitionBox.Checked;
            request.WsiMaxTilesPerSlide = Decimal.ToInt32(wsiMaxTilesBox.Value);

            string panelKey = ChoiceKey(panelBox);
            request.PanelWasAuto =
                string.Equals(panelKey, "AUTO", StringComparison.OrdinalIgnoreCase);
            request.PanelKey = panelKey;
            request.PanelConfigJson = panelConfigBox.Text.Trim();
            request.Segmenter = ChoiceKey(segmenterBox);
            request.Projection = ChoiceKey(projectionBox);
            request.SinglePlane = Decimal.ToInt32(singlePlaneBox.Value);
            request.TissueMode = ChoiceKey(tissueModeBox);
            request.CompartmentMode = ChoiceKey(compartmentModeBox);
            request.WholeFieldCompartment = ChoiceKey(wholeCompartmentBox);
            request.Recursive = recursiveBox.Checked;
            request.IncludeRegex = includeRegexBox.Text.Trim();
            if (request.IncludeRegex.Length == 0) request.IncludeRegex = ".*";
            request.MaxImages = Decimal.ToInt32(maxImagesBox.Value);
            request.MinIncludedNuclei = Decimal.ToInt32(minNucleiBox.Value);
            request.PilotPanelsUnlocked = unlockPilotPanelsBox.Checked;
            request.AdvancedText = advancedBox.Text;
            // H4. The launcher always mints a fresh timestamped directory
            // (MakeUniqueDirectory), so at this point the answer is always no.
            // The authoritative check is PreStartAssertions.AssertOutputDirectoryEmpty,
            // which runs after the directory exists and immediately before the
            // process starts. The gate rule stays because it is what the legacy
            // harness exercises, and because a future caller might know better.
            request.OutputDirectoryExistsAndIsNonEmpty = false;

            foreach (KeyValuePair<string, TextBox> pair in thresholdBoxes)
                request.Thresholds[pair.Key] = pair.Value.Text;

            return request;
        }

        private ToolInventory cachedTools;
        private string cachedToolsKey;

        /// <summary>
        /// Resolving the tools walks the Fiji tree for java.exe and
        /// ij1-patcher-*.jar. That costs a few hundred milliseconds, and the
        /// live gate summary runs on every keystroke -- so without this cache
        /// every character typed into the filename filter paid for a recursive
        /// directory scan. The key is every input the resolution depends on.
        /// </summary>
        private ToolInventory ResolveTools(RunRequest request)
        {
            string key =
                (request.FijiPath ?? "") + "|" + (request.QuPathExecutable ?? "") + "|" +
                (request.PythonExecutable ?? "") + "|" + GetWindowsArchitecture();
            if (cachedTools != null &&
                string.Equals(cachedToolsKey, key, StringComparison.Ordinal))
                return cachedTools;
            cachedToolsKey = key;
            cachedTools = ToolInventory.Resolve(
                request.FijiPath, request.QuPathExecutable, request.PythonExecutable,
                GetWindowsArchitecture());
            return cachedTools;
        }

        /// <summary>
        /// The gate is now the single authority on "this run cannot name its
        /// analysis channels". The AUTO special case that used to live HERE, as
        /// a patch applied after Evaluate returned, is gone: it only ever fired
        /// in the UI, so a request that reached the gate any other way -- and a
        /// custom panel key, which resolves to no channel list for a different
        /// reason -- skipped H2 entirely. FailClosedGate.Evaluate handles both
        /// cases now, and reports AUTO with the same finding codes it always
        /// used (H2_AUTO_ADAPTIVE / H2_AUTO_IN_CONFIRMATORY).
        /// </summary>
        private GateResult EvaluateGate(RunRequest request)
        {
            // From the REQUEST, never from the combo. ReadAndValidateConfiguration
            // resolves AUTO into request.PanelKey before calling this, and the
            // combo still reads "AUTO" at that moment: resolving from the combo
            // meant the run path was gated on a panel it was not going to run,
            // and AUTO was refused with "Panel LEFT is not one of the panels
            // declared in the embedded IF_Quant_Pipeline.groovy" -- about a key
            // AUTO had just detected from that very file.
            PanelDef panel = PanelForRequest(request);
            request.PanelResolutionError = customPanelError;
            return FailClosedGate.Evaluate(
                request, panel, engineThresholdMarkers, ResolveTools(request));
        }

        /// Assigning Text or BackColor on an AutoSize label inside an AutoSize
        /// table triggers a layout pass even when the value has not changed,
        /// and this method runs on every keystroke. Only assign what differs.
        private void SetGateSummary(string text, Color fore, Color back)
        {
            if (!string.Equals(gateSummaryLabel.Text, text, StringComparison.Ordinal))
                gateSummaryLabel.Text = text;
            if (gateSummaryLabel.ForeColor != fore) gateSummaryLabel.ForeColor = fore;
            if (gateSummaryLabel.BackColor != back) gateSummaryLabel.BackColor = back;
        }

        private void RefreshGateSummary()
        {
            if (suppressGateRefresh || gateSummaryLabel == null) return;
            try
            {
                UpdateThresholdStatusLabels(ResolveSelectedPanel());
                RunRequest request = ReadRunRequest();
                GateResult gate = EvaluateGate(request);

                if (gate.Blocked)
                {
                    SetGateSummary("Cannot start:  " + Summarise(gate, Severity.Block),
                                   Color.White, Color.FromArgb(160, 30, 30));
                    runButton.Enabled = false;
                    // Do not derive Enabled from Control.Visible here. During
                    // startup WinForms can report a child's effective Visible
                    // state as false until its parent form has been shown. That
                    // left the preview button disabled even though the same gate
                    // correctly enabled the full analysis button.
                    previewButton.Enabled = !HasHardToolBlock(gate);
                }
                else if (gate.NeedsConfirmation)
                {
                    SetGateSummary(
                        "Will run FLAGGED (" + string.Join(" ", gate.FolderStamps().ToArray()) +
                        "):  " + Summarise(gate, Severity.Confirm),
                        Color.Black, Color.FromArgb(250, 220, 150));
                    runButton.Enabled = true;
                    previewButton.Enabled = true;
                }
                else
                {
                    SetGateSummary("Ready.  " + DescribeReadyState(request, gate),
                                   Color.White, Color.FromArgb(30, 110, 60));
                    runButton.Enabled = true;
                    previewButton.Enabled = true;
                }
            }
            catch (Exception ex)
            {
                SetGateSummary("Cannot start:  " + ex.Message,
                               Color.White, Color.FromArgb(160, 30, 30));
                runButton.Enabled = false;
            }
        }

        private static bool HasHardToolBlock(GateResult gate)
        {
            foreach (GateFinding f in gate.OfSeverity(Severity.Block))
                if (f.Code == "FIJI_MISSING" || f.Code == "FIJI_JVM_MISSING" ||
                    f.Code == "ROUTE_NOT_AVAILABLE")
                    return true;
            return false;
        }

        private static string Summarise(GateResult gate, Severity severity)
        {
            List<string> parts = new List<string>();
            foreach (GateFinding f in gate.OfSeverity(severity))
                parts.Add(FirstSentence(f.Message));
            if (parts.Count == 0) return "(no detail)";
            return string.Join("   |   ", parts.ToArray());
        }

        private static string FirstSentence(string value)
        {
            string text = (value ?? "").Replace("\r\n", " ").Replace('\n', ' ').Trim();
            int stop = text.IndexOf(". ", StringComparison.Ordinal);
            if (stop > 0 && stop < 180) return text.Substring(0, stop + 1);
            return text.Length > 200 ? text.Substring(0, 200) + "..." : text;
        }

        private string DescribeReadyState(RunRequest request, GateResult gate)
        {
            PanelDef panel = ResolveSelectedPanel();
            int total = panel == null ? 0 : panel.AnalysisChannels.Count;
            int frozen = 0;
            if (panel != null)
                foreach (ChannelDef channel in panel.AnalysisChannels)
                {
                    string value;
                    if (request.Thresholds.TryGetValue(channel.Token, out value) &&
                        (value ?? "").Trim().Length > 0)
                        frozen++;
                }
            StringBuilder text = new StringBuilder();
            text.Append("Route ").Append((int)request.Route).Append(", panel ")
                .Append(request.PanelKey);
            if (request.Route != ImageRoute.LegacyFiji172)
            {
                text.Append(", ").Append(frozen).Append('/').Append(total)
                    .Append(" thresholds fixed, minimum nuclei per region ")
                    .Append(request.MinIncludedNuclei)
                    .Append(", tier ").Append(request.Tier.ToString().ToLowerInvariant());
            }
            else
            {
                text.Append(", v1.7.2 environment reproduced exactly");
            }
            int warnings = gate.OfSeverity(Severity.Warn).Count;
            if (warnings > 0) text.Append("  (").Append(warnings).Append(" warning(s))");
            return text.ToString();
        }

        // =================================================================
        // REVIEW AND CONFIRMATION
        // =================================================================

        private string BuildReviewText(RunConfiguration config)
        {
            RunRequest request = config.Request;
            GateResult gate = config.Gate;
            RouteSpec spec = RouteCatalog.Describe(request.Route);

            StringBuilder text = new StringBuilder();
            text.AppendLine("ROUTE");
            text.AppendLine("  " + spec.DisplayName);
            text.AppendLine("  " + DescribeStages(spec));
            if (request.Route != ImageRoute.LegacyFiji172)
                text.AppendLine("  tier: " + request.Tier.ToString().ToLowerInvariant());
            text.AppendLine("  fiji: " + config.FijiExecutable);
            text.AppendLine("  start: " + config.InvocationDescription);
            text.AppendLine();

            text.AppendLine("INPUT");
            text.AppendLine("  " + config.InputDirectory);
            text.AppendLine();

            text.AppendLine("PANEL");
            text.AppendLine("  " + DescribePanelAllocation(config));
            text.AppendLine();

            text.AppendLine("THRESHOLDS");
            if (gate.ThresholdPolicy.Count == 0)
                text.AppendLine("  (none — legacy mode writes no threshold variables)");
            else
                foreach (string line in gate.ThresholdPolicy)
                    text.AppendLine("  " + line);
            text.AppendLine();

            if (spec.WritesMinIncludedNuclei)
            {
                text.AppendLine("SPARSE-REGION FLOOR");
                text.AppendLine("  IFQ_MIN_INCLUDED_NUCLEI=" + request.MinIncludedNuclei +
                                (request.MinIncludedNuclei == 0
                                    ? "  (every region is kept)"
                                    : "  (regions below the floor are DROPPED, with their area)"));
                text.AppendLine();
            }

            List<GateFinding> warnings = gate.OfSeverity(Severity.Warn);
            if (warnings.Count > 0)
            {
                text.AppendLine("WARNINGS");
                foreach (GateFinding f in warnings) text.AppendLine("  * " + f.Message);
                text.AppendLine();
            }
            List<GateFinding> confirms = gate.OfSeverity(Severity.Confirm);
            if (confirms.Count > 0)
            {
                text.AppendLine("FLAGS");
                foreach (GateFinding f in confirms) text.AppendLine("  * " + f.Message);
                text.AppendLine();
            }
            List<GateFinding> notes = gate.OfSeverity(Severity.Note);
            if (notes.Count > 0)
            {
                text.AppendLine("NOTES");
                foreach (GateFinding f in notes) text.AppendLine("  * " + f.Message);
                text.AppendLine();
            }
            if (!string.IsNullOrEmpty(config.LegacyArtefactNote))
            {
                text.AppendLine("LEGACY EQUIVALENCE");
                text.AppendLine("  " + config.LegacyArtefactNote);
                text.AppendLine();
            }

            text.AppendLine("NEW RESULT FOLDER");
            text.AppendLine("  " + config.OutputDirectory);
            return text.ToString();
        }

        private bool ConfirmRouteRun(RunConfiguration config)
        {
            string body = BuildReviewText(config);
            if (!config.Gate.NeedsConfirmation)
            {
                return MessageBox.Show(
                    this, body + "\r\n\r\nStart this run now?", "Review the run",
                    MessageBoxButtons.OKCancel, MessageBoxIcon.Question) == DialogResult.OK;
            }
            using (PhraseConfirmDialog dialog = new PhraseConfirmDialog(
                       body, config.Gate.RequiredPhrases, config.Gate.FolderStamps()))
            {
                return dialog.ShowDialog(this) == DialogResult.OK;
            }
        }

        // =================================================================
        // ROUTE 2 — the staged runner
        //
        // Each stage is gated on the terminal state the stage itself already
        // defines. The launcher invents no new success criterion, and in
        // particular does not read stage 2's exit code as "did it work":
        // exit 1 there means "at least one image failed", not "no results",
        // because outputs are written before the terminal failRun.
        // =================================================================

        private void StartSlideScannerRun(RunConfiguration config)
        {
            SetRunningState(true);
            SetProgressPreparing();
            ThreadPool.QueueUserWorkItem(delegate
            {
                string failure = null;
                int lastExit = 0;
                try
                {
                    AppendLog("Stage 1 — tiling the slide with QuPath.");
                    lastExit = RunStage(
                        config.QuPathExecutable,
                        "script " + QuoteArgument(config.Stage1ScriptPath),
                        config.RuntimeDirectory, config.Stage1Seal);
                    if (lastExit != 0)
                        failure = "Stage 1 (QuPath tiling) exited " + lastExit +
                                  ". No tiles can be trusted, so stage 2 was not started.";

                    string manifest = failure == null
                        ? Path.Combine(config.Request.WsiOutput, "stage1_manifest.json")
                        : null;
                    if (failure == null && !File.Exists(manifest))
                        failure = "Stage 1 exited 0 but wrote no stage1_manifest.json at " +
                                  manifest + ". Without it there is no record of whether tissue " +
                                  "coverage was complete, so stage 2 was not started.";

                    if (failure == null)
                    {
                        AppendLog("Stage 2 — measuring every tile with the frozen Fiji engine.");
                        lastExit = RunStage(
                            config.FijiExecutable, config.FijiArguments,
                            config.RuntimeDirectory, config.Stage2Seal);
                        string runManifest =
                            Path.Combine(config.OutputDirectory, "run_manifest.json");
                        if (!File.Exists(runManifest))
                            failure = "Stage 2 wrote no run_manifest.json. Its exit code (" +
                                      lastExit + ") is not a success criterion — the manifest is.";
                    }

                    if (failure == null && config.PythonExecutable != null)
                    {
                        AppendLog("Stage 3 — reconciling tiles to a slide.");
                        lastExit = RunStage(
                            config.PythonExecutable,
                            QuoteArgument(config.Stage3ScriptPath) +
                            " --slide-root " + QuoteArgument(config.Request.WsiOutput),
                            config.RuntimeDirectory, config.Stage3Seal);
                        string slideSummary = Path.Combine(
                            config.Request.WsiOutput, "stats", "slide_level_summary.csv");
                        if (lastExit != 0 || !File.Exists(slideSummary))
                            failure = "Stage 3 did not write stats/slide_level_summary.csv " +
                                      "(exit " + lastExit + "). It refuses to write one on a " +
                                      "missing tile or an area mismatch, so this is the stage " +
                                      "telling you the slide does not add up.";
                    }
                }
                catch (Exception ex)
                {
                    failure = ex.Message;
                }

                int finalExit = lastExit;
                string finalFailure = failure;
                BeginInvoke(new Action(delegate
                {
                    FinishAnalysis(config, finalExit, finalFailure);
                }));
            });
        }

        /// <summary>
        /// Runs one stage to completion, streaming both pipes into the log.
        /// Both pipes must be drained or a full buffer deadlocks the child.
        ///
        /// It takes a RunSeal, not a Dictionary. This method used to strip and
        /// copy the environment inline -- a second, independent way into the
        /// child environment that no validation covered. There is now exactly
        /// one, EnvironmentApply.Apply, and it only accepts a seal.
        /// </summary>
        private int RunStage(
            string fileName, string arguments, string workingDirectory, RunSeal seal)
        {
            ProcessStartInfo psi = new ProcessStartInfo();
            psi.FileName = fileName;
            psi.Arguments = arguments;
            psi.WorkingDirectory = workingDirectory;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;
            EnvironmentApply.Apply(psi, seal);

            Process process = new Process();
            process.StartInfo = psi;
            process.EnableRaisingEvents = true;
            process.OutputDataReceived += delegate(object sender, DataReceivedEventArgs e)
            {
                if (!string.IsNullOrEmpty(e.Data)) HandleFijiLine(e.Data, false);
            };
            process.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e)
            {
                if (!string.IsNullOrEmpty(e.Data)) HandleFijiLine(e.Data, true);
            };
            lock (processLock) { runningProcess = process; }
            process.Start();
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();
            process.WaitForExit();
            int exitCode = process.ExitCode;
            lock (processLock)
            {
                if (ReferenceEquals(runningProcess, process)) runningProcess = null;
            }
            return exitCode;
        }
    }

    // =================================================================
    // THE TYPED-PHRASE CONFIRMATION  (H2 / H5)
    //
    // A checkbox gets ticked without being read. The phrase is the word that
    // will appear in the output folder name, so typing it is also reading the
    // consequence. AcceptButton is deliberately null: Enter must not bypass it.
    // =================================================================

    internal sealed class PhraseConfirmDialog : Form
    {
        private readonly Button okButton;
        private readonly List<TextBox> entries = new List<TextBox>();
        private readonly List<string> requiredPhrases;

        public PhraseConfirmDialog(string body, List<string> phrases, List<string> folderStamps)
        {
            requiredPhrases = phrases;
            Text = "This run will be flagged";
            StartPosition = FormStartPosition.CenterParent;
            MinimizeBox = false;
            MaximizeBox = false;
            FormBorderStyle = FormBorderStyle.Sizable;
            ClientSize = new Size(MainForm.Scaled(900), MainForm.Scaled(620));
            Font = new Font("Segoe UI", 9F);

            TableLayoutPanel root = new TableLayoutPanel();
            root.Dock = DockStyle.Fill;
            root.Padding = new Padding(12);
            root.ColumnCount = 1;
            root.RowCount = 4;
            root.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
            root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            Controls.Add(root);

            TextBox review = new TextBox();
            review.Multiline = true;
            review.ReadOnly = true;
            review.ScrollBars = ScrollBars.Vertical;
            review.Dock = DockStyle.Fill;
            review.Font = new Font("Consolas", 9F);
            review.Text = body;
            root.Controls.Add(review, 0, 0);

            Label consequence = new Label();
            consequence.AutoSize = true;
            consequence.MaximumSize = new Size(MainForm.Scaled(860), 0);
            consequence.ForeColor = Color.FromArgb(150, 60, 0);
            consequence.Padding = new Padding(0, 8, 0, 8);
            consequence.Text =
                "The output folder will be named ..." +
                string.Join("", folderStamps.ToArray()) +
                " and will contain " + FailClosedGate.ExploratoryMarkerFileName +
                " once the run finishes. Nothing from this run may be aggregated or " +
                "reported as an endpoint.";
            root.Controls.Add(consequence, 0, 1);

            // One box per phrase. Two hazards means two acknowledgements: a
            // single combined box would let one word stand in for both.
            FlowLayoutPanel entryRows = new FlowLayoutPanel();
            entryRows.AutoSize = true;
            entryRows.FlowDirection = FlowDirection.TopDown;
            entryRows.WrapContents = false;
            foreach (string phrase in requiredPhrases)
            {
                FlowLayoutPanel row = new FlowLayoutPanel();
                row.AutoSize = true;
                row.FlowDirection = FlowDirection.LeftToRight;
                Label prompt = new Label();
                prompt.AutoSize = true;
                prompt.Padding = new Padding(0, 6, 6, 0);
                prompt.Text = "Type  " + phrase + "  to continue:";
                row.Controls.Add(prompt);
                TextBox entry = new TextBox();
                entry.Width = MainForm.Scaled(260);
                entry.Tag = phrase;
                entry.TextChanged += delegate { UpdateOkButton(); };
                row.Controls.Add(entry);
                entries.Add(entry);
                entryRows.Controls.Add(row);
            }
            root.Controls.Add(entryRows, 0, 2);

            FlowLayoutPanel buttons = new FlowLayoutPanel();
            buttons.AutoSize = true;
            buttons.FlowDirection = FlowDirection.RightToLeft;
            buttons.Dock = DockStyle.Fill;
            Button cancel = new Button();
            cancel.Text = "Cancel";
            cancel.DialogResult = DialogResult.Cancel;
            cancel.AutoSize = true;
            cancel.Padding = new Padding(10, 4, 10, 4);
            buttons.Controls.Add(cancel);
            okButton = new Button();
            okButton.Text = "Run anyway";
            okButton.DialogResult = DialogResult.OK;
            okButton.Enabled = false;
            okButton.AutoSize = true;
            okButton.Padding = new Padding(10, 4, 10, 4);
            buttons.Controls.Add(okButton);
            root.Controls.Add(buttons, 0, 3);

            CancelButton = cancel;
            AcceptButton = null;   // Enter must not confirm a flagged run.
            UpdateOkButton();
        }

        private void UpdateOkButton()
        {
            bool all = entries.Count > 0;
            foreach (TextBox entry in entries)
            {
                string expected = Convert.ToString(entry.Tag, CultureInfo.InvariantCulture);
                // Case sensitive on purpose: the phrase is the word that will
                // appear in the folder name.
                if (!string.Equals(entry.Text, expected, StringComparison.Ordinal))
                    all = false;
            }
            okButton.Enabled = all;
        }
    }
}
