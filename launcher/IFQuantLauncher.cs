using System;
using System.Collections.Generic;
using System.Collections.Specialized;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using IFQuantLauncher.Routing;

[assembly: AssemblyTitle("IF Quant Launcher")]
[assembly: AssemblyDescription("Windows launcher for the Fiji morphology-primary IF quantification pipeline")]
[assembly: AssemblyCompany("IF Quant Pipeline")]
[assembly: AssemblyProduct("IF Quant Launcher")]
[assembly: AssemblyCopyright("Research software")]
[assembly: AssemblyVersion("1.9.5.0")]
[assembly: AssemblyFileVersion("1.9.5.0")]

namespace IFQuantLauncher
{
    internal static class Program
    {
        [STAThread]
        private static void Main(string[] args)
        {
            if (args != null && args.Length > 0 &&
                string.Equals(args[0], "--self-test", StringComparison.OrdinalIgnoreCase))
            {
                Environment.ExitCode = RuntimeBundle.SelfTest();
                return;
            }

            // Builds the whole window, forces layout, walks every route and
            // exits without showing anything. It exists because the v1.8.0
            // layout inserts four new rows into the v1.7.2 table and because
            // route 3's non-selectability is a UI behaviour: neither can be
            // checked by a pure-logic test. Exit 0 = the interface constructs
            // and the route veto holds.
            if (args != null && args.Length > 0 &&
                string.Equals(args[0], "--ui-smoke", StringComparison.OrdinalIgnoreCase))
            {
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                try
                {
                    using (MainForm form = new MainForm())
                    {
                        // The form must really be SHOWN. Control.Visible reports
                        // EFFECTIVE visibility, so on a form that was never shown
                        // every child reads false and every visibility assertion
                        // passes vacuously in one direction and fails in the
                        // other. Minimised + fully transparent + off the taskbar
                        // is shown without being seen.
                        form.WindowState = FormWindowState.Minimized;
                        form.ShowInTaskbar = false;
                        form.Opacity = 0;
                        form.Show();
                        Application.DoEvents();
                        Environment.ExitCode = form.UiSmokeTest();
                        form.Hide();
                    }
                }
                catch (Exception ex)
                {
                    Console.Error.WriteLine("UI SMOKE FAILED: " + ex);
                    Environment.ExitCode = 60;
                }
                return;
            }

            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new MainForm());
        }
    }

    // partial: the route selector, the threshold grid, the fail-closed gate
    // wiring and the route-2 stage runner live in MainForm.Routes.partial.cs.
    // Everything in THIS half is v1.7.2 behaviour and is deliberately left as
    // it was, so the field/confocal path stays compatible.
    internal sealed partial class MainForm : Form
    {
        private TextBox inputBox;
        private TextBox fijiBox;
        private TextBox outputBaseBox;
        private TextBox runNameBox;
        private TextBox includeRegexBox;
        private TextBox panelConfigBox;
        private TextBox advancedBox;
        private ComboBox panelBox;
        private ComboBox segmenterBox;
        private ComboBox projectionBox;
        private ComboBox tissueModeBox;
        private ComboBox compartmentModeBox;
        private ComboBox wholeCompartmentBox;
        private CheckBox recursiveBox;
        private NumericUpDown maxImagesBox;
        private NumericUpDown singlePlaneBox;
        private Button runButton;
        private Button previewButton;
        private Button cancelButton;
        private Button openOutputButton;
        private Button openSummaryButton;
        private TextBox logBox;
        private Label statusLabel;
        private Label progressDetailLabel;
        private Label panelHelpLabel;
        private ProgressBar progressBar;
        private GroupBox advancedGroup;
        private GroupBox analysisSettingsGroup;
        private CheckBox showAdvancedBox;
        private ToolTip toolTips;

        // Layout skeleton. rootTable is the whole form; configScroll is
        // the only part of it that ever scrolls.
        private TableLayoutPanel rootTable;
        private Panel configScroll;
        private TableLayoutPanel configStack;
        private TableLayoutPanel configColumns;
        private TableLayoutPanel configLeft;
        private TableLayoutPanel configRight;
        private Label introLabel;
        private GroupBox inputScopeGroup;
        private Button validatedLungScopeButton;
        private FlowLayoutPanel actionsPanel;
        private TableLayoutPanel progressStack;
        private bool adjustingConfigPane;

        private Process runningProcess;
        private string lastRunDirectory;
        private string lastSummaryPath;
        private bool cancellationRequested;
        private bool runningPreview;
        private readonly object processLock = new object();

        private static readonly Dictionary<string, string> PanelDescriptions =
            new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            {
                { "LEFT", "Priority project left panel: DAPI, KRT5-488, AGER-555, T1alpha-647 (channels 1-4). Per-marker final-positive counts remain primary; KRT5 pod area and AGER/T1alpha membrane areas are also exported." },
                { "RIGHT", "Priority project right panel: DAPI, Pro-SPC-488, AGER-555, KRT8-647 (channels 1-4). Per-marker final-positive counts remain primary; co-expression classes are descriptive research endpoints." },
                { "ALI1", "20x ALI Z-stack: DAPI, SCGB3A2-488, tdTOM, p63-647 (channels 1-4). Channel 4 p63 is the primary endpoint and uses the nuclear range. ALI tdTOM uses a permissive candidate-pixel threshold, with morphology still deciding the final call." },
                { "ALI2", "20x ALI Z-stack: DAPI, KRT5-488, tdTOM, acetylated-tubulin-647 (channels 1-4). Channel 4 AcTub is the primary endpoint. Its independent apical slab is filtered to bright, locally dense, size-bounded ciliary tufts so stable cytoplasmic microtubules are suppressed." },
                { "ALI3", "20x ALI Z-stack: DAPI, KRT5-488, tdTOM, MUC5AC-647 (channels 1-4). Channel 4 MUC5AC is the primary endpoint and uses apical area/cluster analysis. ALI tdTOM remains a secondary morphology-gated reporter endpoint." },
                { "ALI1_MAP", "4x ALI mapping subset: DAPI, SCGB3A2-488, tdTOM (channels 1-3). The named p63 channel is absent from the mapping acquisition and is not analyzed." },
                { "ALI23_MAP", "4x ALI mapping subset: DAPI, KRT5-488, tdTOM (channels 1-3). The named AcTub/MUC5AC channel is absent from the mapping acquisition and is not analyzed." },
                { "E", "20x airway panel: DAPI, CC10, tdTOM, acetylated tubulin (channels 1-4). AcTub uses bright, locally dense, size-bounded apical ciliary components with unique nucleus ownership. Strict positive evidence can be retained when context is unresolved; a negative still requires an airway ROI." },
                { "R", "20x alveolar panel: DAPI, T1alpha/PDPN, tdTOM, mRAGE (channels 1-4). T1alpha and mRAGE negatives require an alveolar ROI; strict evidence in unresolved context is reported separately." },
                { "M", "4x mapping panel: DAPI, CC10, tdTOM (channels 1-3)." },
                { "A", "DAPI, KRT5, AGER (channels 1-3)." },
                { "B", "DAPI, KRT5, ProSPC (channels 1-3)." },
                { "C", "DAPI, KRT5, CD8 (channels 1-3)." },
                { "D", "DAPI, KRT5, CD4 (channels 1-3)." },
                { "P", "DAPI, KRT5, PDPN/T1alpha (channels 1-3)." },
                { "S", "DAPI, KRT5, Sox2 (channels 1-3)." },
                { "S2", "DAPI, KRT5, p63, YAP (channels 1-4); use a single z-plane for YAP." },
                { "T", "Pilot plumbing test only; not valid for biological interpretation." }
            };

        private static readonly HashSet<string> ProtectedEnvironmentKeys =
            new HashSet<string>(StringComparer.OrdinalIgnoreCase)
            {
                "IFQ_INPUT_DIR", "IFQ_OUTPUT_DIR", "IFQ_PANEL",
                "IFQ_MARKER_REGISTRY", "IFQ_PANEL_CONFIG", "IFQ_PANEL_MAP_PATH",
                "IFQ_RECURSIVE", "IFQ_INCLUDE_REGEX", "IFQ_MAX_IMAGES",
                "IFQ_SEGMENTER", "IFQ_PROJECTION", "IFQ_SINGLE_PLANE",
                "IFQ_EXPORT_DISPLAY_CHANNELS", "IFQ_DISPLAY_PREVIEW_ONLY",
                "IFQ_DISPLAY_SCALE_BAR_UM", "IFQ_DISPLAY_SCALE_BAR_THICKNESS_PX",
                "IFQ_TISSUE_MODE", "IFQ_COMPARTMENT_MODE",
                "IFQ_WHOLE_FIELD_COMPARTMENT",
                "IFQ_ALLOW_NONEMPTY_OUTPUT", "IFQ_MORPHOLOGY_PRIMARY"
            };

        private static readonly HashSet<string> SupportedImageExtensions =
            new HashSet<string>(StringComparer.OrdinalIgnoreCase)
            {
                ".czi", ".lif", ".nd2", ".oir", ".oib", ".oif",
                ".ics", ".tif", ".tiff"
            };

        public MainForm()
        {
            Text = "IF Quant Launcher";
            StartPosition = FormStartPosition.CenterScreen;
            FormBorderStyle = FormBorderStyle.Sizable;
            AutoScroll = true;
            AutoScaleMode = AutoScaleMode.Dpi;
            Font = new Font("Segoe UI", 9F);
            ApplyWindowMetrics();
            WindowState = FormWindowState.Maximized;
            toolTips = new ToolTip();
            toolTips.AutoPopDelay = 12000;
            toolTips.InitialDelay = 400;
            toolTips.ReshowDelay = 150;

            BuildInterface();
            LoadSavedSettings();
            ApplyFirstRunDefaults();
            UpdateAdvancedVisibility();
            UpdatePanelHelp();
            FinishRouteInitialisation();

            FormClosing += delegate(object sender, FormClosingEventArgs e)
            {
                lock (processLock)
                {
                    if (runningProcess != null && !runningProcess.HasExited)
                    {
                        DialogResult result = MessageBox.Show(
                            this,
                            "Fiji is still running. Cancel the analysis and close?",
                            "Analysis in progress",
                            MessageBoxButtons.YesNo,
                            MessageBoxIcon.Warning);
                        if (result != DialogResult.Yes)
                        {
                            e.Cancel = true;
                            return;
                        }
                        CancelRunningProcess();
                    }
                }
                SaveSettings();
            };
        }

        private void BuildInterface()
        {
            // ---------------------------------------------------------------
            // THE LAYOUT. v1.7.2 had 7 rows and v1.8.0 grew it to 11, all of
            // them AutoSize, all in ONE column, inside a form that scrolled
            // only in theory: the last row was Percent, so the table never
            // reported a preferred height larger than its client area and no
            // scrollbar ever appeared. Measured natural client height at 96 dpi
            // was 1127 px on route 1, 1280 px with Advanced open and 1423 px on
            // route 2, against roughly 689 px of usable client height on a
            // 1366x768 laptop and 1001 px on 1920x1080 -- so the bottom of the
            // form was simply cut off, on every screen, at every scaling.
            //
            // The shape is now:
            //
            //   header                       AutoSize   (quick start)
            //   input scope                  AutoSize   (always reachable)
            //   configuration pane           computed   TWO columns, AutoScroll
            //   gate summary                 AutoSize  \
            //   action buttons               AutoSize   |  pinned: these four
            //   progress                     AutoSize   |  can never scroll
            //   log                          Percent   /   out of reach
            //
            // Two properties are deliberate. (1) Only the CONFIGURATION half
            // scrolls; the gate verdict, the Run button, the progress bar and
            // the log are always on screen, because those are what a run in
            // progress needs. (2) The pane's row height is computed in
            // UpdateConfigPaneHeight so it takes its natural height when there
            // is room -- the scrollbar is the safety net, not the design.
            //
            // Nothing here changes what any control DOES, and no control was
            // put inside a container that is ever hidden, so Control.Visible
            // still answers the per-route questions --ui-smoke asks of it.
            // ---------------------------------------------------------------
            rootTable = new TableLayoutPanel();
            rootTable.Dock = DockStyle.Fill;
            rootTable.Padding = new Padding(12);
            rootTable.ColumnCount = 1;
            rootTable.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100F));
            rootTable.RowCount = RootRowCount;
            rootTable.RowStyles.Add(new RowStyle(SizeType.AutoSize));            // header
            rootTable.RowStyles.Add(new RowStyle(SizeType.AutoSize));            // input scope
            rootTable.RowStyles.Add(new RowStyle(SizeType.Absolute, ScaledF(320F)));// config
            rootTable.RowStyles.Add(new RowStyle(SizeType.AutoSize));            // gate
            rootTable.RowStyles.Add(new RowStyle(SizeType.AutoSize));            // actions
            rootTable.RowStyles.Add(new RowStyle(SizeType.AutoSize));            // progress
            rootTable.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));       // log
            Controls.Add(rootTable);

            introLabel = new Label();
            introLabel.AutoSize = true;
            // No MaximumSize: this row is the full width of the form, so the
            // paragraph should wrap at the window edge rather than at a fixed
            // 980 px. On a 1920-wide window that is two lines instead of four.
            introLabel.Padding = new Padding(0, 0, 0, 6);
            introLabel.Text =
                "Quick start: (1) choose the folder containing your original microscope files, " +
                "(2) choose the Fiji installation and where results should be saved, " +
                "(3) leave panel selection on AUTO when the complete marker panel is named in the file/folder path, then create visual merge panels or run analysis. " +
                "Recommended settings can normally be left unchanged. Research use only.";
            rootTable.Controls.Add(introLabel, 0, RootRowHeader);

            // File scope is a run-defining input, not an expert afterthought.
            // Keep it outside the scrolling configuration pane so an operator
            // can always see which files AUTO must classify. This also gives
            // the validated lung cohort a named preset instead of requiring a
            // regular expression to be recalled from documentation.
            inputScopeGroup = new GroupBox();
            inputScopeGroup.Text = "Input scope — which microscope files are included";
            inputScopeGroup.Dock = DockStyle.Top;
            inputScopeGroup.AutoSize = true;
            inputScopeGroup.Padding = new Padding(10);

            TableLayoutPanel inputScope = new TableLayoutPanel();
            inputScope.Dock = DockStyle.Top;
            inputScope.AutoSize = true;
            inputScope.ColumnCount = 4;
            inputScope.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, ScaledF(155F)));
            inputScope.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50F));
            inputScope.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, ScaledF(155F)));
            inputScope.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50F));
            inputScopeGroup.Controls.Add(inputScope);

            includeRegexBox = new TextBox();
            includeRegexBox.Text = ".*";
            includeRegexBox.Dock = DockStyle.Fill;
            AddSetting(inputScope, 0, 0, "Filename filter", includeRegexBox);

            validatedLungScopeButton = new Button();
            validatedLungScopeButton.Text = "Use validated 20x 2k .oir fields";
            validatedLungScopeButton.AutoSize = true;
            validatedLungScopeButton.Dock = DockStyle.Fill;
            validatedLungScopeButton.Click += delegate
            {
                includeRegexBox.Text = @".*20x 2k.*\.oir";
            };
            inputScope.Controls.Add(validatedLungScopeButton, 2, 0);
            inputScope.SetColumnSpan(validatedLungScopeButton, 2);

            maxImagesBox = new NumericUpDown();
            maxImagesBox.Minimum = 0;
            maxImagesBox.Maximum = 1000000;
            maxImagesBox.Value = 0;
            maxImagesBox.Dock = DockStyle.Fill;
            AddSetting(inputScope, 1, 0, "Image limit (0 = all)", maxImagesBox);

            recursiveBox = new CheckBox();
            recursiveBox.Text = "Search subfolders";
            recursiveBox.Checked = true;
            recursiveBox.AutoSize = true;
            recursiveBox.Anchor = AnchorStyles.Left;
            inputScope.Controls.Add(MakeLabel("Subfolders"), 2, 1);
            inputScope.Controls.Add(recursiveBox, 3, 1);

            toolTips.SetToolTip(includeRegexBox,
                "Full-path regular expression. Leave .* to include every supported microscope image.");
            toolTips.SetToolTip(validatedLungScopeButton,
                "Selects the independently validated 20x 2k .oir lung fields and excludes 4x navigation acquisitions.");
            toolTips.SetToolTip(maxImagesBox,
                "0 analyzes all matching images. Use 1 for a quick pilot run.");
            rootTable.Controls.Add(inputScopeGroup, 0, RootRowInputScope);

            // The scrolling configuration pane and its two columns. Groups are
            // appended to the columns at the END of this method, once the route
            // half of the form has been built too, so that reading order is
            // decided in one place instead of by construction order.
            configScroll = new Panel();
            configScroll.Dock = DockStyle.Fill;
            configScroll.AutoScroll = true;
            configScroll.Margin = new Padding(0, 0, 0, 4);
            configScroll.MinimumSize = new Size(0, Scaled(MinimumConfigPaneHeight));
            rootTable.Controls.Add(configScroll, 0, RootRowConfig);

            configStack = new TableLayoutPanel();
            configStack.Dock = DockStyle.Top;
            configStack.AutoSize = true;
            configStack.AutoSizeMode = AutoSizeMode.GrowAndShrink;
            configStack.ColumnCount = 1;
            configStack.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100F));
            configStack.RowCount = 2;
            configStack.RowStyles.Add(new RowStyle(SizeType.AutoSize));   // two columns
            configStack.RowStyles.Add(new RowStyle(SizeType.AutoSize));   // full width
            configScroll.Controls.Add(configStack);

            configColumns = new TableLayoutPanel();
            configColumns.Dock = DockStyle.Top;
            configColumns.AutoSize = true;
            configColumns.AutoSizeMode = AutoSizeMode.GrowAndShrink;
            configColumns.ColumnCount = 2;
            configColumns.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50F));
            configColumns.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50F));
            configColumns.RowCount = 1;
            configColumns.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            configStack.Controls.Add(configColumns, 0, 0);

            configLeft = NewConfigColumn(new Padding(0, 0, 6, 0));
            configRight = NewConfigColumn(new Padding(6, 0, 0, 0));
            configColumns.Controls.Add(configLeft, 0, 0);
            configColumns.Controls.Add(configRight, 1, 0);

            GroupBox pathsGroup = new GroupBox();
            pathsGroup.Text = "Required locations";
            pathsGroup.Dock = DockStyle.Top;
            pathsGroup.AutoSize = true;
            pathsGroup.Padding = new Padding(10);

            TableLayoutPanel paths = new TableLayoutPanel();
            paths.Dock = DockStyle.Top;
            paths.AutoSize = true;
            paths.ColumnCount = 3;
            paths.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, ScaledF(175F)));
            paths.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100F));
            paths.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, ScaledF(95F)));
            pathsGroup.Controls.Add(paths);

            inputBox = AddPathRow(paths, 0, "Original image folder", true, false);
            fijiBox = AddPathRow(paths, 1, "Fiji executable or folder", false, true);
            outputBaseBox = AddPathRow(paths, 2, "Output parent folder", true, false);
            toolTips.SetToolTip(inputBox, "Choose the folder containing the unedited CZI, LIF, ND2, OIR/OIB/OIF, ICS, TIF, or TIFF files.");
            toolTips.SetToolTip(fijiBox, "Choose Fiji's installation folder or its executable. The launcher selects the correct Windows ARM64/x64 executable.");
            toolTips.SetToolTip(outputBaseBox, "A new timestamped run folder will be created here. Existing analysis folders are not overwritten.");

            paths.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            paths.Controls.Add(MakeLabel("Run name (optional)"), 0, 3);
            runNameBox = new TextBox();
            runNameBox.Dock = DockStyle.Fill;
            paths.Controls.Add(runNameBox, 1, 3);
            Label runHint = new Label();
            runHint.AutoSize = true;
            runHint.Text = "Fresh folder";
            runHint.Anchor = AnchorStyles.Left;
            paths.Controls.Add(runHint, 2, 3);
            toolTips.SetToolTip(runNameBox, "Optional readable name such as Mouse12_CC10_AcTub. A timestamp is added automatically.");

            analysisSettingsGroup = new GroupBox();
            analysisSettingsGroup.Text = "Analysis settings — recommended defaults are appropriate for most first runs";
            analysisSettingsGroup.Dock = DockStyle.Top;
            analysisSettingsGroup.AutoSize = true;
            analysisSettingsGroup.AutoSizeMode = AutoSizeMode.GrowAndShrink;
            analysisSettingsGroup.Padding = new Padding(10);

            TableLayoutPanel settings = new TableLayoutPanel();
            settings.Dock = DockStyle.Top;
            settings.AutoSize = true;
            settings.ColumnCount = 4;
            settings.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, ScaledF(155F)));
            settings.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50F));
            settings.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, ScaledF(155F)));
            settings.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50F));
            analysisSettingsGroup.Controls.Add(settings);

            panelBox = MakeCombo(
                new string[] {
                    "AUTO",
                    "LEFT — priority: KRT5-488 + AGER-555 + T1alpha-647",
                    "RIGHT — priority: Pro-SPC-488 + AGER-555 + KRT8-647",
                    "ALI1",
                    "ALI2",
                    "ALI3",
                    "ALI1_MAP — 4x DAPI + SCGB3A2 + tdTOM subset",
                    "ALI23_MAP — 4x DAPI + KRT5 + tdTOM subset",
                    "E — 20x CC10 + tdTOM + acetylated tubulin",
                    "R — 20x T1alpha + tdTOM + mRAGE",
                    "M — 4x CC10 + tdTOM mapping",
                    "A — KRT5 + AGER",
                    "B — KRT5 + ProSPC",
                    "C — KRT5 + CD8",
                    "D — KRT5 + CD4",
                    "P — KRT5 + PDPN",
                    "S — KRT5 + Sox2",
                    "S2 — KRT5 + p63 + YAP",
                    "T — pilot test only"
                },
                "AUTO",
                true);
            segmenterBox = MakeCombo(
                new string[] {
                    "classic — Recommended; built into Fiji",
                    "stardist — Requires the StarDist plugin"
                },
                "classic — Recommended; built into Fiji",
                false);
            projectionBox = MakeCombo(
                new string[] {
                    "layer_aware — Marker-specific Z slabs; recommended for multichannel stacks",
                    "max — Whole-stack maximum intensity; legacy/global mode",
                    "single — One z-plane; required for YAP ratio",
                    "avg — Average intensity",
                    "sum — Sum intensity"
                },
                "layer_aware — Marker-specific Z slabs; recommended for multichannel stacks",
                false);
            tissueModeBox = MakeCombo(
                new string[] {
                    "auto — Automatically identify tissue",
                    "whole_field — Analyze the entire image"
                },
                "auto — Automatically identify tissue",
                false);
            compartmentModeBox = MakeCombo(
                new string[] {
                    "required — Strict compartment gating",
                    "optional — Allow unassigned cells"
                },
                "optional — Allow unassigned cells",
                false);
            wholeCompartmentBox = MakeCombo(
                new string[] { "unassigned", "airway", "alveolar", "tumor", "fibrotic", "stromal", "vascular", "immune", "ambiguous" },
                "unassigned",
                false);

            // This group now occupies half the form's width instead of all of
            // it, so the five long descriptive combo boxes span the remaining
            // three columns and only the five SHORT controls still pair up.
            // Same controls, same tooltips, same order of ideas -- they simply
            // no longer have to share 155-px-plus-half-a-column with a
            // sentence-long item like "layer_aware — Marker-specific Z slabs".
            AddWideSetting(settings, 0, "Staining panel", panelBox);

            panelHelpLabel = new Label();
            panelHelpLabel.AutoSize = true;
            panelHelpLabel.ForeColor = Color.FromArgb(75, 75, 75);
            panelHelpLabel.Padding = new Padding(4, 2, 4, 7);
            settings.Controls.Add(panelHelpLabel, 0, 1);
            settings.SetColumnSpan(panelHelpLabel, 4);

            AddWideSetting(settings, 2, "Nucleus detection", segmenterBox);
            AddWideSetting(settings, 3, "Z-stack handling", projectionBox);
            AddWideSetting(settings, 4, "Tissue boundary", tissueModeBox);
            AddWideSetting(settings, 5, "Anatomical gate", compartmentModeBox);

            singlePlaneBox = new NumericUpDown();
            singlePlaneBox.Minimum = -1;
            singlePlaneBox.Maximum = 10000;
            singlePlaneBox.Value = -1;
            singlePlaneBox.Dock = DockStyle.Fill;
            AddSetting(settings, 6, 0, "Whole-image tissue type", wholeCompartmentBox);
            AddSetting(settings, 6, 2, "Z-plane (-1 = middle)", singlePlaneBox);

            panelBox.SelectedIndexChanged += delegate { UpdatePanelHelp(); };
            panelBox.TextChanged += delegate { UpdatePanelHelp(); };
            toolTips.SetToolTip(panelBox, "AUTO assigns each matching image independently from marker names in its file/folder path, then applies that built-in panel's fixed acquisition channel order. Multiple recognized panels may share one run. Unknown images stop for manual review; stains are not inferred from colors or intensity.");
            toolTips.SetToolTip(segmenterBox, "Classic is the safest first choice. Choose StarDist only when that Fiji installation has the plugin and model.");
            toolTips.SetToolTip(projectionBox, "Layer-aware mode keeps DAPI across the stack, selects a DAPI-guided cell-body slab, and selects a marker-guided apical slab. Review the saved Z profile and freeze explicit ranges before confirmatory analysis.");
            toolTips.SetToolTip(singlePlaneBox, "Used only when Z-stack handling is single. -1 asks the pipeline to use the middle plane.");
            toolTips.SetToolTip(tissueModeBox, "Auto excludes empty background. Whole field is appropriate only when the entire image should be analyzed.");
            toolTips.SetToolTip(compartmentModeBox, "Required protects the negative denominator. Strict marker evidence may be retained when anatomy is unresolved, but a negative requires a compatible compartment; a known incompatible compartment remains indeterminate.");
            toolTips.SetToolTip(wholeCompartmentBox, "Use this only when the whole image contains one known tissue compartment.");

            TableLayoutPanel advancedContainer = new TableLayoutPanel();
            advancedContainer.Dock = DockStyle.Top;
            advancedContainer.AutoSize = true;
            advancedContainer.ColumnCount = 1;
            advancedContainer.RowCount = 2;
            advancedContainer.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            advancedContainer.RowStyles.Add(new RowStyle(SizeType.AutoSize));

            showAdvancedBox = new CheckBox();
            showAdvancedBox.Text = "Show advanced study options";
            showAdvancedBox.AutoSize = true;
            showAdvancedBox.Padding = new Padding(0, 5, 0, 2);
            showAdvancedBox.CheckedChanged += delegate { UpdateAdvancedVisibility(); };
            advancedContainer.Controls.Add(showAdvancedBox, 0, 0);
            toolTips.SetToolTip(showAdvancedBox, "Most users should leave this closed. Open it only for a validated custom panel or predeclared IFQ settings.");

            advancedGroup = new GroupBox();
            advancedGroup.Text = "Advanced study configuration — leave blank unless your study protocol requires it";
            advancedGroup.Dock = DockStyle.Top;
            advancedGroup.AutoSize = true;
            advancedGroup.Padding = new Padding(10);
            advancedContainer.Controls.Add(advancedGroup, 0, 1);

            TableLayoutPanel optional = new TableLayoutPanel();
            optional.Dock = DockStyle.Top;
            optional.AutoSize = true;
            optional.ColumnCount = 3;
            optional.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, ScaledF(175F)));
            optional.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100F));
            optional.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, ScaledF(95F)));
            advancedGroup.Controls.Add(optional);

            optional.Controls.Add(MakeLabel("Validated custom panel file"), 0, 0);
            panelConfigBox = new TextBox();
            panelConfigBox.Dock = DockStyle.Fill;
            optional.Controls.Add(panelConfigBox, 1, 0);
            Button panelBrowse = new Button();
            panelBrowse.Text = "Browse...";
            panelBrowse.Dock = DockStyle.Fill;
            panelBrowse.Click += delegate { BrowseJsonFile(panelConfigBox); };
            optional.Controls.Add(panelBrowse, 2, 0);

            optional.Controls.Add(MakeLabel("Predeclared IFQ settings"), 0, 1);
            advancedBox = new TextBox();
            advancedBox.Dock = DockStyle.Fill;
            advancedBox.Multiline = true;
            advancedBox.ScrollBars = ScrollBars.Vertical;
            advancedBox.Height = Scaled(76);
            advancedBox.AcceptsReturn = true;
            optional.Controls.Add(advancedBox, 1, 1);
            optional.SetColumnSpan(advancedBox, 2);
            toolTips.SetToolTip(panelConfigBox, "Expert use: a study-owned JSON file that defines marker-to-channel mappings not included in the built-in panels.");
            toolTips.SetToolTip(advancedBox, "Expert use: one validated IFQ_KEY=VALUE setting per line. Do not invent thresholds during an analysis run.");

            FlowLayoutPanel actions = new FlowLayoutPanel();
            actions.Dock = DockStyle.Top;
            actions.AutoSize = true;
            actions.AutoSizeMode = AutoSizeMode.GrowAndShrink;
            actions.FlowDirection = FlowDirection.LeftToRight;
            actions.Padding = new Padding(0, 4, 0, 4);
            actionsPanel = actions;
            rootTable.Controls.Add(actions, 0, RootRowActions);

            Button helpButton = new Button();
            helpButton.Text = "First-time help";
            helpButton.AutoSize = true;
            helpButton.Padding = new Padding(8, 5, 8, 5);
            helpButton.Click += delegate { ShowFirstTimeHelp(); };
            actions.Controls.Add(helpButton);

            Button defaultsButton = new Button();
            defaultsButton.Text = "Restore recommended settings";
            defaultsButton.AutoSize = true;
            defaultsButton.Padding = new Padding(8, 5, 8, 5);
            defaultsButton.Click += delegate { RestoreRecommendedSettings(); };
            actions.Controls.Add(defaultsButton);

            runButton = new Button();
            runButton.Text = "Review and run analysis";
            runButton.AutoSize = true;
            runButton.Padding = new Padding(12, 5, 12, 5);
            runButton.Click += delegate { StartAnalysis(); };
            actions.Controls.Add(runButton);

            previewButton = new Button();
            previewButton.Text = "Create visual merge panels";
            previewButton.AutoSize = true;
            previewButton.Padding = new Padding(12, 5, 12, 5);
            previewButton.Click += delegate { StartDisplayPreview(); };
            actions.Controls.Add(previewButton);
            toolTips.SetToolTip(
                previewButton,
                "Creates the primary visual merge panel and supporting enhanced channel views for every image in the configured run scope, " +
                "with a calibrated thin 100 \u00b5m scale bar drawn inside the image. " +
                "No segmentation, cell calls, masks, CSV, Excel, parameters, or analysis manifest are produced.");

            cancelButton = new Button();
            cancelButton.Text = "Cancel";
            cancelButton.Enabled = false;
            cancelButton.AutoSize = true;
            cancelButton.Padding = new Padding(8, 5, 8, 5);
            cancelButton.Click += delegate { CancelRunningProcess(); };
            actions.Controls.Add(cancelButton);

            openOutputButton = new Button();
            openOutputButton.Text = "Open output folder";
            openOutputButton.Enabled = false;
            openOutputButton.AutoSize = true;
            openOutputButton.Padding = new Padding(8, 5, 8, 5);
            openOutputButton.Click += delegate { OpenPath(lastRunDirectory, true); };
            actions.Controls.Add(openOutputButton);

            openSummaryButton = new Button();
            openSummaryButton.Text = "Open summary Excel";
            openSummaryButton.Enabled = false;
            openSummaryButton.AutoSize = true;
            openSummaryButton.Padding = new Padding(8, 5, 8, 5);
            openSummaryButton.Click += delegate { OpenPath(lastSummaryPath, false); };
            actions.Controls.Add(openSummaryButton);

            TableLayoutPanel progressPanel = new TableLayoutPanel();
            progressPanel.Dock = DockStyle.Top;
            progressPanel.AutoSize = true;
            progressPanel.AutoSizeMode = AutoSizeMode.GrowAndShrink;
            progressPanel.ColumnCount = 1;
            progressPanel.RowCount = 3;
            progressPanel.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            progressPanel.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            progressPanel.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            progressStack = progressPanel;
            rootTable.Controls.Add(progressPanel, 0, RootRowProgress);

            statusLabel = new Label();
            statusLabel.Text = "Ready — choose the three required locations and staining panel";
            statusLabel.AutoSize = true;
            statusLabel.Padding = new Padding(0, 2, 0, 2);
            statusLabel.Font = new Font(Font, FontStyle.Bold);
            progressPanel.Controls.Add(statusLabel, 0, 0);

            progressBar = new ProgressBar();
            progressBar.Dock = DockStyle.Top;
            progressBar.Height = Scaled(24);
            progressBar.Minimum = 0;
            progressBar.Maximum = 100;
            progressBar.Value = 0;
            progressBar.Style = ProgressBarStyle.Continuous;
            progressPanel.Controls.Add(progressBar, 0, 1);

            progressDetailLabel = new Label();
            progressDetailLabel.Text = "Not started";
            progressDetailLabel.AutoSize = true;
            progressDetailLabel.ForeColor = Color.FromArgb(80, 80, 80);
            progressDetailLabel.Padding = new Padding(0, 2, 0, 2);
            progressPanel.Controls.Add(progressDetailLabel, 0, 2);

            logBox = new TextBox();
            logBox.Dock = DockStyle.Fill;
            logBox.Multiline = true;
            logBox.ReadOnly = true;
            logBox.ScrollBars = ScrollBars.Both;
            logBox.WordWrap = false;
            logBox.Font = new Font("Consolas", 9F);
            logBox.MinimumSize = new Size(0, Scaled(MinimumLogHeight));
            rootTable.Controls.Add(logBox, 0, RootRowLog);

            // v1.8.0: the route selector and everything that depends on it.
            // Built last, so every v1.7.2 control it reads already exists. It
            // no longer places its own group boxes -- it builds them, and the
            // one statement below decides where they sit.
            BuildRouteInterface();

            // ---- reading order of the configuration pane -------------------
            // LEFT  what is being analysed and where it lives.
            // RIGHT how it is analysed.
            // FULL  what is being measured -- the threshold grid needs the full
            //       width, or its per-marker status sentences wrap to four
            //       lines each and the group becomes taller than the two
            //       columns it was meant to fit beside.
            AppendToConfigColumn(configLeft, routeGroup);
            AppendToConfigColumn(configLeft, pathsGroup);
            AppendToConfigColumn(configLeft, toolsGroup);
            AppendToConfigColumn(configRight, analysisSettingsGroup);
            AppendToConfigColumn(configRight, advancedContainer);
            configStack.Controls.Add(measurementGroup, 0, 1);
        }

        // =================================================================
        // LAYOUT PLUMBING
        // =================================================================

        private const int RootRowHeader = 0;
        private const int RootRowInputScope = 1;
        private const int RootRowConfig = 2;
        private const int RootRowGate = 3;
        private const int RootRowActions = 4;
        private const int RootRowProgress = 5;
        private const int RootRowLog = 6;
        private const int RootRowCount = 7;

        /// The log is the only thing a running analysis writes to, so it keeps
        /// a floor of its own and the configuration pane is never allowed to
        /// eat it.
        private const int MinimumLogHeight = 120;

        /// Below this the configuration pane is pure scrollbar, which is worse
        /// than useless; the form's MinimumSize is set so it is not reached.
        private const int MinimumConfigPaneHeight = 140;

        /// <summary>
        /// The old values were MinimumSize 960x720 and Size 1280x1000,
        /// both raw pixels and neither clamped to the screen. The work area of
        /// a 1366x768 laptop is about 1366x720, so a 720-px minimum HEIGHT
        /// could not be reduced to fit even in principle; and the manifest
        /// declares dpiAware, so at 125% or 150% scaling every control grew by
        /// that factor while the 720 stayed 720 physical pixels.
        ///
        /// Both are now scaled by the real DPI and then clamped to the work
        /// area, so the window can always be made to fit the screen it is on.
        /// </summary>
        private void ApplyWindowMetrics()
        {
            Rectangle work = Screen.PrimaryScreen.WorkingArea;
            int minWidth = Math.Min(Scaled(820), Math.Max(600, work.Width));
            int minHeight = Math.Min(Scaled(600), Math.Max(400, work.Height));
            MinimumSize = new Size(minWidth, minHeight);
            Size = new Size(
                Math.Max(minWidth, Math.Min(Scaled(1280), work.Width)),
                Math.Max(minHeight, Math.Min(Scaled(900), work.Height)));
        }

        /// <summary>
        /// Physical pixels per 96 logical pixels on the display this process
        /// starts on.
        ///
        /// app.manifest declares dpiAware, so at 125% or 150% scaling every
        /// font-driven control really is 1.25x or 1.5x taller -- but a
        /// hardcoded "175F" label column stayed 175 PHYSICAL pixels, i.e. 117
        /// logical, and the label in it wrapped onto a second line. Measured at
        /// 150%: eight two-line labels that are one line at 100%, costing about
        /// 150 px of height for nothing. Every fixed layout pixel in this form
        /// now goes through Scaled().
        /// </summary>
        private static readonly float LayoutScale = MeasureLayoutScale();

        private static float MeasureLayoutScale()
        {
            try
            {
                using (Graphics probe = Graphics.FromHwnd(IntPtr.Zero))
                    if (probe.DpiY > 0f) return Math.Max(1f, probe.DpiY / 96f);
            }
            catch { }
            return 1f;
        }

        internal static int Scaled(int pixels)
        {
            return (int)Math.Round(pixels * LayoutScale);
        }

        internal static float ScaledF(float pixels)
        {
            return pixels * LayoutScale;
        }

        private static TableLayoutPanel NewConfigColumn(Padding margin)
        {
            TableLayoutPanel column = new TableLayoutPanel();
            column.Dock = DockStyle.Top;
            column.AutoSize = true;
            column.AutoSizeMode = AutoSizeMode.GrowAndShrink;
            column.ColumnCount = 1;
            column.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100F));
            column.RowCount = 0;
            column.Margin = margin;
            return column;
        }

        private static void AppendToConfigColumn(TableLayoutPanel column, Control control)
        {
            int row = column.RowCount;
            column.RowCount = row + 1;
            column.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            column.Controls.Add(control, 0, row);
        }

        protected override void OnLayout(LayoutEventArgs levent)
        {
            base.OnLayout(levent);
            UpdateConfigPaneHeight();
        }

        /// <summary>
        /// Gives the configuration pane exactly the height it needs, up to the
        /// height that is left once the header, the gate bar, the buttons, the
        /// progress row and the log's floor have been paid for. Beyond that the
        /// pane scrolls internally, which is the whole point: the four pinned
        /// rows below it cannot be pushed off the bottom of the screen no
        /// matter how tall the configuration becomes.
        ///
        /// Layout-only. It reads sizes and writes one RowStyle height.
        /// </summary>
        private void UpdateConfigPaneHeight()
        {
            if (adjustingConfigPane) return;
            if (rootTable == null || configScroll == null || configStack == null) return;
            if (rootTable.RowStyles.Count <= RootRowLog) return;

            // WinForms can under-measure an AutoSize GroupBox whose only child
            // is a docked AutoSize TableLayoutPanel. The child then paints below
            // the group border and the following group appears to cut off the
            // final rows. This was visible in v1.9.1 in both Step 1 and Analysis
            // settings. Give those groups a width-aware floor derived from the
            // complete child table before measuring the surrounding pane.
            StabilizeGroupHeight(routeGroup);
            StabilizeGroupHeight(analysisSettingsGroup);

            int available = rootTable.ClientSize.Height - rootTable.Padding.Vertical;
            if (available <= 0) return;

            // Every height below is asked for with GetPreferredSize AT A STATED
            // WIDTH; none of it is read out of Control.Height.
            //
            // Reading Height was a real defect. Widening the window from 1350
            // to 1904 runs this handler before the children have re-measured,
            // so the pinned rows still reported their narrow -- taller, more
            // wrapped -- heights, and the pane was sized from a content height
            // of 1262 px that was already history. Measured consequence at
            // 1904x1001: the log's bottom landed at 1032, thirty-one pixels
            // below the client, and nothing re-laid it out afterwards. A
            // preferred size at a stated width has no such memory.
            int rowWidth = Math.Max(
                1, rootTable.ClientSize.Width - rootTable.Padding.Horizontal);

            int reserved =
                PreferredOuterHeight(introLabel, rowWidth) +
                PreferredOuterHeight(inputScopeGroup, rowWidth) +
                PreferredOuterHeight(gateSummaryLabel, rowWidth) +
                PreferredOuterHeight(actionsPanel, rowWidth) +
                PreferredOuterHeight(progressStack, rowWidth) +
                Scaled(MinimumLogHeight) + (logBox == null ? 0 : logBox.Margin.Vertical) +
                configScroll.Margin.Vertical;

            // Measure the content at the width it would have WITH a scrollbar,
            // never at the width it has right now. Measuring the current width
            // is self-referential: showing the bar narrows the pane, the narrow
            // pane rewraps a label, the taller content still does not fit, and
            // the bar stays up forever four pixels short of fitting. Measured:
            // 514 px wanted without the bar, 518 px with it, so a 1920x1080
            // screen with 633 px of room still showed a scrollbar.
            int paneWidth = configScroll.Width - SystemInformation.VerticalScrollBarWidth - 2;
            if (paneWidth < 120) paneWidth = Math.Max(1, configScroll.Width);

            // A TableLayoutPanel that is itself AutoSize gives every Percent
            // column its CONTENT width first and only shares out what is left,
            // so "50% / 50%" silently became 74% / 26% and the right-hand
            // column was drawn off the edge of the window. Capping each
            // column's preferred width at half the pane is what makes the two
            // percentages mean what they say.
            int half = (paneWidth - configLeft.Margin.Horizontal
                                  - configRight.Margin.Horizontal) / 2;
            if (half < Scaled(160)) half = Scaled(160);
            if (configLeft.MaximumSize.Width != half)
                configLeft.MaximumSize = new Size(half, 0);
            if (configRight.MaximumSize.Width != half)
                configRight.MaximumSize = new Size(half, 0);
            // GetPreferredSize on nested AutoSize tables measured 4 px under the
            // height the same content actually laid out to, so round up. Being
            // a few pixels generous costs a few pixels of dead space; being a
            // few pixels mean costs a permanent scrollbar on a screen that had
            // 100 px to spare -- measured: 514 wanted, 518 needed, scrollbar.
            int wanted = configStack.GetPreferredSize(new Size(paneWidth, 0)).Height + 12;

            int room = available - reserved;
            if (room < Scaled(MinimumConfigPaneHeight)) room = Scaled(MinimumConfigPaneHeight);
            int target = wanted > room ? room : wanted;
            if (target < Scaled(MinimumConfigPaneHeight)) target = Scaled(MinimumConfigPaneHeight);

            // THE OUTER SAFETY NET. A work area can be shorter than even the
            // pinned rows need: a 1366x768 laptop at 150% scaling leaves about
            // 649 px of client height, and the header, gate bar, buttons,
            // progress row and the log's own floor want more than that.
            //
            // Two statements, because one is not enough. rootTable.MinimumSize
            // stops the table being squeezed -- but rootTable is Dock=Fill, and
            // a docked child does NOT contribute to a form's scrollable area
            // (measured: root 762 px inside a 689-px client, and the form
            // reported DisplayRectangle 689 with no scrollbar, so the bottom 73
            // px was simply clipped). AutoScrollMinSize is what actually creates
            // the scroll region, and the docked table is then laid out into the
            // taller display rectangle instead of the client.
            //
            // The dead band matters: a scrollbar narrows the client, which
            // rewraps the header, which moves the floor. Eight pixels of slack
            // stops that from ringing.
            int floorHeight = reserved + Scaled(MinimumConfigPaneHeight) +
                              rootTable.Padding.Vertical;
            RowStyle style = rootTable.RowStyles[RootRowConfig];
            bool rowIsCurrent = style.SizeType == SizeType.Absolute &&
                                (int)style.Height == target;
            bool floorIsCurrent = Math.Abs(rootTable.MinimumSize.Height - floorHeight) < 8 &&
                                  Math.Abs(AutoScrollMinSize.Height - floorHeight) < 8;
            if (rowIsCurrent && floorIsCurrent) return;

            adjustingConfigPane = true;
            try
            {
                if (!rowIsCurrent)
                {
                    style.SizeType = SizeType.Absolute;
                    style.Height = target;
                }
                if (!floorIsCurrent)
                {
                    rootTable.MinimumSize = new Size(0, floorHeight);
                    AutoScrollMinSize = new Size(0, floorHeight);
                }
            }
            finally { adjustingConfigPane = false; }
        }

        private static void StabilizeGroupHeight(GroupBox group)
        {
            if (group == null || !group.Visible || group.Controls.Count == 0) return;

            Control content = group.Controls[0];
            int width = group.ClientSize.Width - group.Padding.Horizontal;
            if (width < Scaled(120))
                width = Math.Max(Scaled(120), group.Width - group.Padding.Horizontal);

            int contentHeight = content.GetPreferredSize(new Size(width, 0)).Height;
            int captionAllowance = Math.Max(group.Font.Height, Scaled(12));
            int required = contentHeight + group.Padding.Vertical + captionAllowance;
            if (required < 1) required = 1;

            if (group.MinimumSize.Height != required)
                group.MinimumSize = new Size(0, required);
        }

        /// The height a pinned row needs at a given width, plus its margins.
        /// Preferred size rather than current size, so a resize cannot be
        /// budgeted against the previous width's wrapping.
        private static int PreferredOuterHeight(Control control, int width)
        {
            if (control == null || !control.Visible) return 0;
            int inner = Math.Max(1, width - control.Margin.Horizontal);
            int height = control.GetPreferredSize(new Size(inner, 0)).Height;
            if (height < control.MinimumSize.Height) height = control.MinimumSize.Height;
            return height + control.Margin.Vertical;
        }

        private TextBox AddPathRow(TableLayoutPanel table, int row, string label, bool folder, bool executable)
        {
            table.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            table.Controls.Add(MakeLabel(label), 0, row);
            TextBox box = new TextBox();
            box.Dock = DockStyle.Fill;
            table.Controls.Add(box, 1, row);
            Button browse = new Button();
            browse.Text = "Browse...";
            browse.Dock = DockStyle.Fill;
            browse.Click += delegate
            {
                if (executable)
                    BrowseFiji(box);
                else if (folder)
                    BrowseFolder(box);
            };
            table.Controls.Add(browse, 2, row);
            return box;
        }

        private static Label MakeLabel(string text)
        {
            Label label = new Label();
            label.Text = text;
            label.AutoSize = true;
            label.Anchor = AnchorStyles.Left;
            label.Padding = new Padding(0, 4, 0, 4);
            return label;
        }

        private static ComboBox MakeCombo(string[] values, string selected, bool editable)
        {
            ComboBox combo = new ComboBox();
            combo.Dock = DockStyle.Fill;
            combo.DropDownStyle = editable ? ComboBoxStyle.DropDown : ComboBoxStyle.DropDownList;
            combo.Items.AddRange(values);
            combo.Text = selected;
            return combo;
        }

        private static void AddSetting(TableLayoutPanel table, int row, int column, string label, Control control)
        {
            while (table.RowCount <= row)
            {
                table.RowCount++;
                table.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            }
            table.Controls.Add(MakeLabel(label), column, row);
            control.Dock = DockStyle.Fill;
            table.Controls.Add(control, column + 1, row);
        }

        /// The same row, with the control taking every remaining column of the
        /// four-column analysis grid. Used for the combo boxes whose items are
        /// whole explanatory sentences.
        private static void AddWideSetting(
            TableLayoutPanel table, int row, string label, Control control)
        {
            AddSetting(table, row, 0, label, control);
            table.SetColumnSpan(control, table.ColumnCount - 1);
        }

        private static string ChoiceKey(ComboBox combo)
        {
            string text = (combo.Text ?? "").Trim();
            int separator = text.IndexOf(" — ", StringComparison.Ordinal);
            return separator > 0 ? text.Substring(0, separator).Trim() : text;
        }

        private static void SelectChoice(ComboBox combo, string key)
        {
            string requested = (key ?? "").Trim();
            for (int index = 0; index < combo.Items.Count; index++)
            {
                string item = Convert.ToString(combo.Items[index], CultureInfo.InvariantCulture);
                if (string.Equals(item, requested, StringComparison.OrdinalIgnoreCase) ||
                    item.StartsWith(requested + " — ", StringComparison.OrdinalIgnoreCase))
                {
                    combo.SelectedIndex = index;
                    return;
                }
            }
            if (combo.DropDownStyle != ComboBoxStyle.DropDownList)
                combo.Text = requested;
        }

        private void UpdatePanelHelp()
        {
            if (panelHelpLabel == null || panelBox == null)
                return;
            string key = ChoiceKey(panelBox);
            string description;
            if (string.Equals(key, "AUTO", StringComparison.OrdinalIgnoreCase))
                description =
                    "Automatic mode scans matching analytical image paths for marker names. " +
                    "Each recognized image receives its own built-in panel/channel map, so multiple panels and marker subsets may share one run. Unknown images are rejected.";
            else if (!PanelDescriptions.TryGetValue(key, out description))
                description = "Custom panel key. Select a validated custom panel file under Advanced study options.";
            panelHelpLabel.Text =
                description + " Verify marker identity and acquisition channel order before running; colors in a displayed composite are not sufficient.";
            panelHelpLabel.ForeColor = string.Equals(key, "T", StringComparison.OrdinalIgnoreCase)
                ? Color.DarkRed
                : Color.FromArgb(75, 75, 75);
        }

        internal static string InferBuiltInPanelFromText(string sourceText)
        {
            string upper = (sourceText ?? "").ToUpperInvariant().Replace("Α", "ALPHA");
            string text = Regex.Replace(upper, "[^A-Z0-9]+", "");
            bool krt5 = text.Contains("KRT5");
            bool krt8 = text.Contains("KRT8");
            // The validated lung cohort encodes AGER as mRAGE_555 in its raw
            // filenames. After punctuation removal this is "MRAGE", which
            // does not contain "AGER" in the same order. Treat the explicit
            // antibody synonym as AGER; do not accept generic "RAGE", which
            // could occur inside unrelated words such as "average".
            bool ager = text.Contains("AGER") || text.Contains("MRAGE");
            bool tdtom = text.Contains("TDTOM");
            bool t1a = text.Contains("T1ALPHA") || text.Contains("T1A") ||
                       text.Contains("PDPN") || text.Contains("PODOPLANIN");
            bool prospc = text.Contains("PROSPC") || text.Contains("SFTPC");
            bool actub = text.Contains("ACTUB") || text.Contains("ACETYL");
            bool muc5ac = text.Contains("MUC5AC") || text.Contains("MU5AC");
            bool cc10 = text.Contains("CC10") || text.Contains("SCGB1A1");
            bool p63 = text.Contains("P63") || text.Contains("TP63");
            bool mapping4x = text.Contains("4X") && text.Contains("MAPPING");

            // Resolve the most specific four-channel panels before the shorter
            // universal marker combinations.
            // Olympus 4x map acquisitions contain only DAPI, the 488 marker,
            // and tdTOM even when their folder names retain the omitted 647
            // marker. Route these validated three-channel subsets first.
            if (mapping4x && cc10 && tdtom) return "M";
            if (mapping4x && text.Contains("SCGB3A2") && tdtom) return "ALI1_MAP";
            if (mapping4x && krt5 && tdtom) return "ALI23_MAP";
            if (krt5 && ager && t1a) return "LEFT";
            if (prospc && ager && krt8) return "RIGHT";
            if (text.Contains("SCGB3A2") && tdtom && p63) return "ALI1";
            // CC10 identifies the airway panel even when the mouse genotype
            // prefix contains "krt5-creERT2".
            if (cc10 && tdtom && actub) return "E";
            if (krt5 && tdtom && actub) return "ALI2";
            if (krt5 && tdtom && muc5ac) return "ALI3";
            if (t1a && tdtom && text.Contains("MRAGE")) return "R";
            if (krt5 && p63 && text.Contains("YAP")) return "S2";
            if (krt5 && text.Contains("SOX2")) return "S";
            if (krt5 && text.Contains("CD8")) return "C";
            if (krt5 && text.Contains("CD4")) return "D";
            if (krt5 && prospc) return "B";
            if (krt5 && ager) return "A";
            if (krt5 && t1a) return "P";
            return null;
        }

        private static string InferBuiltInPanelFromPath(
            string inputDirectory,
            string imagePath)
        {
            string context = Path.GetFileName(imagePath);
            string panel = InferBuiltInPanelFromText(context);
            if (panel != null)
                return panel;

            string root = Path.GetFullPath(inputDirectory)
                .TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            DirectoryInfo directory = new FileInfo(imagePath).Directory;
            while (directory != null &&
                   directory.FullName.StartsWith(root, StringComparison.OrdinalIgnoreCase))
            {
                context = directory.Name + " " + context;
                panel = InferBuiltInPanelFromText(context);
                if (panel != null)
                    return panel;
                if (string.Equals(
                        directory.FullName.TrimEnd(
                            Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar),
                        root,
                        StringComparison.OrdinalIgnoreCase))
                    break;
                directory = directory.Parent;
            }
            return null;
        }

        internal static PanelDetectionResult DetectBuiltInPanels(
            string inputDirectory,
            string includeRegex,
            bool recursive)
        {
            Regex includePattern = new Regex(
                "^(?:" + includeRegex + ")$",
                RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);
            SearchOption option = recursive ? SearchOption.AllDirectories : SearchOption.TopDirectoryOnly;
            List<string> analyticalFiles;
            try
            {
                analyticalFiles = Directory.EnumerateFiles(inputDirectory, "*", option)
                    .Where(delegate(string path)
                    {
                        return SupportedImageExtensions.Contains(Path.GetExtension(path)) &&
                               includePattern.IsMatch(Path.GetFullPath(path)) &&
                               !Regex.IsMatch(
                                   Path.GetFileName(path),
                                   @"^Map_A\d+\.(oir|oib|oif)$",
                                   RegexOptions.IgnoreCase);
                    })
                    .OrderBy(delegate(string path) { return path; }, StringComparer.OrdinalIgnoreCase)
                    .ToList();
            }
            catch (Exception ex)
            {
                throw new InvalidOperationException(
                    "Automatic panel detection could not scan the input folder:\r\n" + ex.Message);
            }

            if (analyticalFiles.Count == 0)
                throw new InvalidOperationException(
                    "Automatic panel detection found no matching analytical image files. " +
                    "Check the filename filter and subfolder option, or select the panel manually.");

            PanelDetectionResult result = new PanelDetectionResult();
            result.AnalyticalImageCount = analyticalFiles.Count;
            List<string> unknown = new List<string>();
            SamplesheetPanelAssignments samplesheet =
                LoadSamplesheetPanels(inputDirectory);
            string fullRoot = Path.GetFullPath(inputDirectory)
                .TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar) +
                Path.DirectorySeparatorChar;
            foreach (string path in analyticalFiles)
            {
                string fullPath = Path.GetFullPath(path);
                if (!fullPath.StartsWith(fullRoot, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidOperationException(
                        "Automatic panel detection found a path outside the selected input folder: " + fullPath);
                string relative = fullPath.Substring(fullRoot.Length).Replace('\\', '/');
                string panel;
                if (!samplesheet.ByRelativePath.TryGetValue(relative, out panel) &&
                    !samplesheet.ByFilename.TryGetValue(Path.GetFileName(path), out panel))
                {
                    panel = InferBuiltInPanelFromPath(inputDirectory, path);
                }
                if (panel == null)
                {
                    unknown.Add(path);
                    continue;
                }
                string canonicalPanel = PanelDescriptions.Keys.FirstOrDefault(
                    delegate(string key)
                    {
                        return string.Equals(key, panel, StringComparison.OrdinalIgnoreCase);
                    });
                if (canonicalPanel == null)
                {
                    throw new InvalidOperationException(
                        "AUTO found panel '" + panel + "' for " + relative +
                        ", but that panel is not built in. Select a validated custom panel " +
                        "or use its samplesheet outside AUTO mode.");
                }
                panel = canonicalPanel;
                result.PanelByRelativePath[relative] = panel;
                int count;
                result.PanelCounts.TryGetValue(panel, out count);
                result.PanelCounts[panel] = count + 1;
            }

            if (unknown.Count > 0)
            {
                string examples = string.Join(
                    "\r\n",
                    unknown.Take(3).Select(delegate(string path) { return "• " + Path.GetFileName(path); }));
                throw new InvalidOperationException(
                    "Automatic panel detection could not identify " + unknown.Count +
                    " of " + analyticalFiles.Count + " matching image(s):\r\n" + examples +
                    "\r\n\r\nNarrow the always-visible Input scope before choosing one panel " +
                    "manually. For the validated lung cohort, click 'Use validated 20x " +
                    "2k .oir fields'. Do not choose one manual panel for a folder that " +
                    "contains both LEFT and RIGHT acquisitions. No image was assigned " +
                    "by color alone.");
            }
            result.FallbackPanel = result.PanelCounts
                .OrderByDescending(delegate(KeyValuePair<string, int> pair) { return pair.Value; })
                .ThenBy(delegate(KeyValuePair<string, int> pair) { return pair.Key; },
                        StringComparer.OrdinalIgnoreCase)
                .First().Key;
            return result;
        }

        private static SamplesheetPanelAssignments LoadSamplesheetPanels(
            string inputDirectory)
        {
            SamplesheetPanelAssignments assignments =
                new SamplesheetPanelAssignments();
            string path = Path.Combine(inputDirectory, "samplesheet.csv");
            if (!File.Exists(path))
                return assignments;

            string[] lines = File.ReadAllLines(path, Encoding.UTF8);
            if (lines.Length == 0)
                return assignments;
            List<string> header = ParseCsvLine(lines[0]);
            if (header.Count > 0)
                header[0] = header[0].TrimStart('\uFEFF');
            int filenameIndex = header.FindIndex(
                delegate(string value)
                {
                    return string.Equals(value.Trim(), "filename",
                                         StringComparison.OrdinalIgnoreCase);
                });
            int relativeIndex = header.FindIndex(
                delegate(string value)
                {
                    return string.Equals(value.Trim(), "relative_path",
                                         StringComparison.OrdinalIgnoreCase);
                });
            int panelIndex = header.FindIndex(
                delegate(string value)
                {
                    return string.Equals(value.Trim(), "panel",
                                         StringComparison.OrdinalIgnoreCase);
                });
            if (panelIndex < 0 || (filenameIndex < 0 && relativeIndex < 0))
                return assignments;

            HashSet<string> duplicateFilenames =
                new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            for (int lineIndex = 1; lineIndex < lines.Length; lineIndex++)
            {
                string line = lines[lineIndex].Trim();
                if (line.Length == 0 || line.StartsWith("#", StringComparison.Ordinal))
                    continue;
                List<string> values = ParseCsvLine(lines[lineIndex]);
                string panel = panelIndex < values.Count ? values[panelIndex].Trim() : "";
                if (panel.Length == 0 ||
                    string.Equals(panel, "NA", StringComparison.OrdinalIgnoreCase))
                    continue;
                string relative = relativeIndex >= 0 && relativeIndex < values.Count
                    ? values[relativeIndex].Trim().Replace('\\', '/')
                    : "";
                if (relative.Length > 0 &&
                    !string.Equals(relative, "NA", StringComparison.OrdinalIgnoreCase))
                {
                    if (assignments.ByRelativePath.ContainsKey(relative))
                        throw new InvalidOperationException(
                            "samplesheet.csv repeats relative_path '" + relative + "'.");
                    assignments.ByRelativePath[relative] = panel;
                }

                string filename = filenameIndex >= 0 && filenameIndex < values.Count
                    ? values[filenameIndex].Trim()
                    : "";
                if (filename.Length == 0 ||
                    string.Equals(filename, "NA", StringComparison.OrdinalIgnoreCase))
                    continue;
                if (assignments.ByFilename.ContainsKey(filename))
                {
                    assignments.ByFilename.Remove(filename);
                    duplicateFilenames.Add(filename);
                }
                else if (!duplicateFilenames.Contains(filename))
                {
                    assignments.ByFilename[filename] = panel;
                }
            }
            return assignments;
        }

        private static List<string> ParseCsvLine(string line)
        {
            List<string> values = new List<string>();
            StringBuilder field = new StringBuilder();
            bool quoted = false;
            for (int index = 0; index < (line ?? "").Length; index++)
            {
                char value = line[index];
                if (value == '"')
                {
                    if (quoted && index + 1 < line.Length && line[index + 1] == '"')
                    {
                        field.Append('"');
                        index++;
                    }
                    else
                    {
                        quoted = !quoted;
                    }
                }
                else if (value == ',' && !quoted)
                {
                    values.Add(field.ToString());
                    field.Length = 0;
                }
                else
                {
                    field.Append(value);
                }
            }
            if (quoted)
                throw new InvalidOperationException("Unclosed quoted field in samplesheet.csv.");
            values.Add(field.ToString());
            return values;
        }

        internal static string WriteAutoPanelMap(
            PanelDetectionResult detection,
            string runtimeDirectory)
        {
            string directory = Path.Combine(runtimeDirectory, "auto_panel_maps");
            Directory.CreateDirectory(directory);
            string path = Path.Combine(
                directory,
                "panel_map_" + Guid.NewGuid().ToString("N") + ".csv");
            StringBuilder csv = new StringBuilder();
            csv.AppendLine("relative_path,panel");
            foreach (KeyValuePair<string, string> assignment in
                     detection.PanelByRelativePath.OrderBy(
                         delegate(KeyValuePair<string, string> pair) { return pair.Key; },
                         StringComparer.OrdinalIgnoreCase))
            {
                csv.Append(EscapeCsv(assignment.Key))
                   .Append(',')
                   .Append(EscapeCsv(assignment.Value))
                   .AppendLine();
            }
            File.WriteAllText(path, csv.ToString(), new UTF8Encoding(false));
            return path;
        }

        private static string EscapeCsv(string value)
        {
            string text = value ?? "";
            if (text.IndexOfAny(new char[] { ',', '"', '\r', '\n' }) >= 0)
                return "\"" + text.Replace("\"", "\"\"") + "\"";
            return text;
        }

        private static void DeleteTemporaryPanelMap(RunConfiguration config)
        {
            if (config == null || string.IsNullOrWhiteSpace(config.AutoPanelMapPath))
                return;
            try
            {
                if (File.Exists(config.AutoPanelMapPath))
                    File.Delete(config.AutoPanelMapPath);
            }
            catch
            {
                // The generated map contains paths and panel keys only. A stale
                // cache file must never change the completed analysis status.
            }
        }

        private void UpdateAdvancedVisibility()
        {
            if (advancedGroup == null || showAdvancedBox == null)
                return;
            bool hasSavedAdvanced =
                (panelConfigBox != null && !string.IsNullOrWhiteSpace(panelConfigBox.Text)) ||
                (advancedBox != null && !string.IsNullOrWhiteSpace(advancedBox.Text));
            if (hasSavedAdvanced && !showAdvancedBox.Checked)
                showAdvancedBox.Checked = true;
            advancedGroup.Visible = showAdvancedBox.Checked;
        }

        private void ShowFirstTimeHelp()
        {
            MessageBox.Show(
                this,
                "1. Original image folder: choose unedited microscope files.\r\n\r\n" +
                "2. Fiji: choose the Fiji folder or executable. The launcher automatically selects ARM64 or x64.\r\n\r\n" +
                "3. Output parent folder: choose where a new timestamped result folder should be created.\r\n\r\n" +
                "4. Staining panel: leave AUTO selected when marker names are present in the image or folder names. " +
                "AUTO allocates every recognized image independently, so different built-in panels and marker subsets may share one batch. " +
                "Unknown images stop rather than being guessed. Each allocation uses a preset's fixed acquisition channel order; AUTO does not discover marker identity from fluorescence colors. " +
                "Use a manual/custom choice when naming is insufficient or channel order differs.\r\n\r\n" +
                "5. For a first pilot, set Image limit to 1. Leave the other recommended settings unchanged.\r\n\r\n" +
                "ALI panels treat channel 4 as the primary experimental endpoint: p63, acetylated tubulin, or MUC5AC. " +
                "The channel map comes from the selected preset or AUTO path-name match, never from displayed colors.\r\n\r\n" +
                "6. Click Create visual merge panels to generate the merged marker presentation for every image in the configured run scope. " +
                "This visual-only operation performs no segmentation, cell calls, masks, CSV, Excel, or manifest export.\r\n\r\n" +
                "7. When the marker allocation and merge presentation are correct, click Review and run analysis. " +
                "The full run creates a visual merge panel for every analyzed image as well as the quantitative results. " +
                "Watch the progress bar and status text; a successful analysis enables Open summary Excel.\r\n\r\n" +
                "Always inspect the QC overlays. The software quantifies fluorescence patterns for research and does not make a diagnosis.",
                "First-time guide",
                MessageBoxButtons.OK,
                MessageBoxIcon.Information);
        }

        private void RestoreRecommendedSettings()
        {
            if (invocationBox != null && SelectedRoute != ImageRoute.LegacyFiji172)
                invocationBox.SelectedIndex = GetWindowsArchitecture() == "ARM64" ? 1 : 0;
            SelectChoice(panelBox, "AUTO");
            SelectChoice(segmenterBox, "classic");
            SelectChoice(projectionBox, "layer_aware");
            singlePlaneBox.Value = -1;
            SelectChoice(tissueModeBox, "auto");
            SelectChoice(compartmentModeBox, "optional");
            SelectChoice(wholeCompartmentBox, "unassigned");
            recursiveBox.Checked = true;
            includeRegexBox.Text = ".*";
            maxImagesBox.Value = 0;
            MessageBox.Show(
                this,
                "Recommended processing settings were restored. Panel selection is now AUTO; folder locations were left unchanged. AUTO still requires you to confirm the detected marker names and channel order before the run starts.",
                "Recommended settings restored",
                MessageBoxButtons.OK,
                MessageBoxIcon.Information);
        }

        private void BrowseFolder(TextBox target)
        {
            using (FolderBrowserDialog dialog = new FolderBrowserDialog())
            {
                dialog.ShowNewFolderButton = true;
                if (Directory.Exists(target.Text))
                    dialog.SelectedPath = target.Text;
                if (dialog.ShowDialog(this) == DialogResult.OK)
                    target.Text = dialog.SelectedPath;
            }
        }

        private void BrowseFiji(TextBox target)
        {
            using (OpenFileDialog dialog = new OpenFileDialog())
            {
                dialog.Title = "Select the Fiji or ImageJ executable";
                dialog.Filter = "Fiji/ImageJ executable (*.exe)|*.exe|All files (*.*)|*.*";
                dialog.CheckFileExists = true;
                string current = target.Text.Trim();
                if (File.Exists(current))
                    dialog.FileName = current;
                else if (Directory.Exists(current))
                    dialog.InitialDirectory = current;
                if (dialog.ShowDialog(this) == DialogResult.OK)
                    target.Text = dialog.FileName;
            }
        }

        private void BrowseJsonFile(TextBox target)
        {
            using (OpenFileDialog dialog = new OpenFileDialog())
            {
                dialog.Title = "Select a study panel JSON file";
                dialog.Filter = "JSON files (*.json)|*.json|All files (*.*)|*.*";
                dialog.CheckFileExists = true;
                if (File.Exists(target.Text))
                    dialog.FileName = target.Text;
                if (dialog.ShowDialog(this) == DialogResult.OK)
                    target.Text = dialog.FileName;
            }
        }

        private void ApplyFirstRunDefaults()
        {
            if (string.IsNullOrWhiteSpace(fijiBox.Text))
            {
                string defaultFiji = @"X:\Fiji";
                if (Directory.Exists(defaultFiji))
                    fijiBox.Text = defaultFiji;
            }
            if (string.IsNullOrWhiteSpace(outputBaseBox.Text))
                outputBaseBox.Text = ChooseDefaultOutputBase();
        }

        /// First-run default for the output folder.
        ///
        /// v1.7.2 hardcoded MyDocuments\IFQuantResults, which is on the system
        /// drive. One confocal batch is several GB and a whole-slide route 2 run
        /// is tens of GB, so that default fills C: after a handful of runs and
        /// the failure surfaces as a half-written run, not as a clear message.
        ///
        /// So: prefer a fixed drive that can actually hold the output, and
        /// prefer one already being used for results. This only ever fills an
        /// empty box, and the value is visible in the UI before anything starts,
        /// so it proposes a location rather than imposing one.
        private static string ChooseDefaultOutputBase()
        {
            string fallback = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments),
                "IFQuantResults");

            try
            {
                DriveInfo best = null;
                DriveInfo bestExisting = null;

                foreach (DriveInfo d in DriveInfo.GetDrives())
                {
                    // Fixed only: a removable drive may be absent next session,
                    // and a network drive's free space is not ours to spend.
                    if (!d.IsReady || d.DriveType != DriveType.Fixed) continue;

                    if (best == null || d.AvailableFreeSpace > best.AvailableFreeSpace)
                        best = d;

                    if (Directory.Exists(Path.Combine(d.RootDirectory.FullName, "IFQ_Runs")) &&
                        (bestExisting == null ||
                         d.AvailableFreeSpace > bestExisting.AvailableFreeSpace))
                        bestExisting = d;
                }

                // An existing IFQ_Runs is a deliberate choice by whoever set this
                // machine up; honour it ahead of raw free space.
                DriveInfo chosen = bestExisting != null ? bestExisting : best;
                if (chosen == null) return fallback;

                return Path.Combine(chosen.RootDirectory.FullName, "IFQ_Runs");
            }
            catch (Exception)
            {
                // Drive enumeration can throw on an unusual mount. A default is
                // a convenience, never a correctness requirement, so degrade to
                // the v1.7.2 location rather than failing to open the window.
                return fallback;
            }
        }

        private void StartAnalysis()
        {
            StartFijiRun(false);
        }

        private void StartDisplayPreview()
        {
            StartFijiRun(true);
        }

        private void StartFijiRun(bool previewOnly)
        {
            RunConfiguration config;
            try
            {
                config = ReadAndValidateConfiguration(previewOnly);
            }
            catch (Exception ex)
            {
                MessageBox.Show(
                    this,
                    ex.Message,
                    previewOnly ? "Cannot create visual merge panels" : "Cannot start analysis",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error);
                return;
            }

            if (!(previewOnly ? ConfirmDisplayPreview(config) : ConfirmRouteRun(config)))
                return;

            Directory.CreateDirectory(config.OutputDirectory);
            try
            {
                // H4, for real this time: the folder now exists, so check what
                // is in it rather than what was in it a moment ago.
                PreStartAssertions.AssertOutputDirectoryEmpty(config.OutputDirectory);
            }
            catch (Exception ex)
            {
                MessageBox.Show(this, ex.Message, "Cannot start",
                                MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }
            SaveSettings();
            logBox.Clear();
            lastRunDirectory = config.OutputDirectory;
            lastSummaryPath = null;
            cancellationRequested = false;
            runningPreview = previewOnly;
            openOutputButton.Enabled = true;
            openSummaryButton.Enabled = false;
            SetProgressPreparing();

            AppendLog("IF Quant Launcher " + Assembly.GetExecutingAssembly().GetName().Version);
            AppendLog("Route:  " + RouteCatalog.Describe(config.Request.Route).DisplayName);
            AppendLog("Windows architecture: " + GetWindowsArchitecture());
            AppendLog("Input:  " + config.InputDirectory);
            AppendLog("Output: " + config.OutputDirectory);
            AppendLog("Fiji:   " + config.FijiExecutable);
            AppendLog("Start:  " + config.InvocationDescription);
            if (config.Gate.Exploratory)
                AppendLog(
                    "EXPLORATORY: at least one analysis channel has no fixed threshold and " +
                    "will use per-region adaptive Otsu. Nothing from this run may be aggregated.");
            AppendLog(config.PanelWasAutoDetected
                ? "Panels: " + string.Join(
                    ", ",
                    config.AutoPanelCounts.OrderBy(
                        delegate(KeyValuePair<string, int> pair) { return pair.Key; },
                        StringComparer.OrdinalIgnoreCase)
                    .Select(delegate(KeyValuePair<string, int> pair)
                    {
                        return pair.Key + "=" + pair.Value;
                    }))
                : "Panel:  " + config.Environment["IFQ_PANEL"]);
            string canonicalManifest;
            if (config.Environment.TryGetValue(
                    "IFQ_CANONICAL_MANIFEST_PATH", out canonicalManifest) &&
                !string.IsNullOrWhiteSpace(canonicalManifest))
                AppendLog("Canonical cohort manifest: " + canonicalManifest);

            AppendLog(previewOnly
                ? "Mode:   visual merge panels only; configured image scope; no quantification"
                : "Mode:   full analysis with a visual merge panel for every analyzed image");
            // Route 2 is a chain of processes, not one; it has its own runner.
            if (config.Request.Route == ImageRoute.IfSlideScanner && !previewOnly)
            {
                StartSlideScannerRun(config);
                return;
            }

            AppendLog("Starting Fiji...");

            ProcessStartInfo psi = new ProcessStartInfo();
            psi.FileName = config.FijiExecutable;
            // v1.7.2 built this string inline. It is now built by
            // FijiCommand/LegacyProfile so the legacy equivalence harness can
            // execute the same code the launcher runs.
            psi.Arguments = config.FijiArguments;
            psi.WorkingDirectory = config.RuntimeDirectory;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;

            // v1.7.2 stripped and applied inline here. It is now one call into
            // EnvironmentApply so the legacy equivalence harness can execute the
            // very same code path -- and it takes the SEAL, not the dictionary,
            // so this line cannot start a process whose environment was never
            // re-checked against the record.
            EnvironmentApply.Apply(psi, config.Stage2Seal);

            Process process = new Process();
            process.StartInfo = psi;
            process.EnableRaisingEvents = true;
            process.OutputDataReceived += delegate(object sender, DataReceivedEventArgs e)
            {
                if (!string.IsNullOrEmpty(e.Data))
                    HandleFijiLine(e.Data, false);
            };
            process.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e)
            {
                if (!string.IsNullOrEmpty(e.Data))
                    HandleFijiLine(e.Data, true);
            };

            try
            {
                lock (processLock)
                {
                    runningProcess = process;
                    process.Start();
                }
                process.BeginOutputReadLine();
                process.BeginErrorReadLine();
            }
            catch (Exception ex)
            {
                lock (processLock) { runningProcess = null; }
                if (!config.PreviewOnly)
                    WriteLauncherRecord(config, -1, "failed_to_start: " + ex.Message);
                DeleteTemporaryPanelMap(config);
                MessageBox.Show(this, ex.Message, "Fiji could not start", MessageBoxButtons.OK, MessageBoxIcon.Error);
                SetRunningState(false);
                SetProgressTerminal("Fiji could not start", false, false);
                return;
            }

            SetRunningState(true);
            ThreadPool.QueueUserWorkItem(delegate
            {
                int exitCode = -1;
                string waitError = null;
                try
                {
                    process.WaitForExit();
                    exitCode = process.ExitCode;
                }
                catch (Exception ex)
                {
                    waitError = ex.Message;
                }
                finally
                {
                    lock (processLock)
                    {
                        if (ReferenceEquals(runningProcess, process))
                            runningProcess = null;
                    }
                }

                BeginInvoke(new Action(delegate
                {
                    if (config.PreviewOnly)
                        FinishDisplayPreview(config, exitCode, waitError);
                    else
                        FinishAnalysis(config, exitCode, waitError);
                }));
            });
        }

        private string DescribePanelAllocation(RunConfiguration config)
        {
            if (config.PanelWasAutoDetected && config.AutoPanelCounts != null)
            {
                StringBuilder text = new StringBuilder();
                text.Append("AUTO per-image allocation:\r\n");
                foreach (KeyValuePair<string, int> pair in
                         config.AutoPanelCounts.OrderBy(
                             delegate(KeyValuePair<string, int> item) { return item.Key; },
                             StringComparer.OrdinalIgnoreCase))
                {
                    string description;
                    if (!PanelDescriptions.TryGetValue(pair.Key, out description))
                        description = pair.Key;
                    text.Append("  ").Append(pair.Key).Append(": ")
                        .Append(pair.Value).Append(" image(s) — ")
                        .Append(description).Append("\r\n");
                }
                return text.ToString().TrimEnd();
            }

            string panelKey = config.Environment["IFQ_PANEL"];
            string panelDescription;
            if (PanelDescriptions.TryGetValue(panelKey, out panelDescription))
                return panelDescription;

            // A custom panel now has a real channel list rather than being an
            // opaque key, so the review says what it will actually measure.
            PanelDef custom = ResolveSelectedPanel();
            if (custom != null && custom.IsCustom)
            {
                StringBuilder text = new StringBuilder();
                text.Append(panelKey).Append(" — ").Append(custom.SourceDescription)
                    .Append("\r\n");
                foreach (ChannelDef channel in custom.Channels)
                    text.Append("  channel ").Append(channel.Idx).Append(": ")
                        .Append(channel.Marker).Append(" (").Append(channel.Role)
                        .Append(channel.AreaMarker ? ", AREA ENDPOINT" : "").Append(")\r\n");
                return text.ToString().TrimEnd();
            }
            return "Custom validated panel: " + panelKey;
        }

        private bool ConfirmDisplayPreview(RunConfiguration config)
        {
            DialogResult result = MessageBox.Show(
                this,
                "Create visual merge panels only?\r\n\r\n" +
                "Input:\r\n" + config.InputDirectory + "\r\n\r\n" +
                "Detected/selected marker-channel allocation:\r\n" +
                DescribePanelAllocation(config) + "\r\n\r\n" +
                "Z-stack handling: " + config.Environment["IFQ_PROJECTION"] + "\r\n" +
                "Images: " + (config.Environment["IFQ_MAX_IMAGES"] == "0"
                    ? "all matching analytical images"
                    : "up to " + config.Environment["IFQ_MAX_IMAGES"] + " matching analytical image(s)") + "\r\n\r\n" +
                "Output folder:\r\n" + config.OutputDirectory + "\r\n\r\n" +
                "This operation writes the primary visual merge panel and supporting enhanced channel PNGs, " +
                "with a calibrated thin 100 \u00b5m scale bar drawn inside each primary panel. " +
                "It will not run segmentation or create cell counts, masks, CSV, Excel, " +
                "parameter files, Z-profile tables, or an analysis manifest.\r\n\r\n" +
                "Continue?",
                "Review visual merge panel generation",
                MessageBoxButtons.OKCancel,
                MessageBoxIcon.Information);
            return result == DialogResult.OK;
        }

        // v1.7.2's ConfirmRun was replaced by ConfirmRouteRun/BuildReviewText in
        // MainForm.Routes.partial.cs, which shows the route, the per-channel
        // threshold source and the gate findings, and swaps the plain OK/Cancel
        // box for a typed-phrase dialog when the run will be flagged.

        private RunConfiguration ReadAndValidateConfiguration(bool previewOnly)
        {
            // Route 3 can only be reached here by a caller that bypassed both
            // the picker veto and the gate. Refuse before touching anything.
            if (SelectedRoute == ImageRoute.HeBrightfield &&
                !LauncherBuild.BrightfieldRouteEnabled)
                throw new InvalidOperationException(
                    RouteCatalog.Describe(ImageRoute.HeBrightfield).DisplayName + "\r\n\r\n" +
                    LauncherBuild.BrightfieldDisabledReason);

            // Route 2 measures the tiles stage 1 writes, not a folder the user
            // picked; the "original image folder" row is hidden for it.
            string input;
            if (SelectedRoute == ImageRoute.IfSlideScanner)
            {
                string stage1Root = wsiOutputBox.Text.Trim();
                if (stage1Root.Length == 0)
                    throw new InvalidOperationException(
                        "Choose the stage 1 output root. The tiles measured in stage 2 are read " +
                        "from its tiles\\ subfolder.");
                input = Path.Combine(Path.GetFullPath(stage1Root), "tiles");
                Directory.CreateDirectory(input);
            }
            else
            {
                input = inputBox.Text.Trim();
                if (!Directory.Exists(input))
                    throw new InvalidOperationException(
                        "The original image folder does not exist:\r\n" + input);
            }

            string fiji = ResolveFijiExecutable(fijiBox.Text.Trim());
            if (fiji == null)
                throw new InvalidOperationException(
                    "Could not find a Fiji/ImageJ executable. Select the executable itself or its installation folder.");

            string outputBase = outputBaseBox.Text.Trim();
            if (outputBase.Length == 0)
                throw new InvalidOperationException("Choose an output parent folder.");
            Directory.CreateDirectory(outputBase);

            string includeRegex = includeRegexBox.Text.Trim();
            if (includeRegex.Length == 0)
                includeRegex = ".*";
            try { new Regex(includeRegex); }
            catch (Exception ex)
            {
                throw new InvalidOperationException("The filename include regex is invalid:\r\n" + ex.Message);
            }

            string panelConfig = panelConfigBox.Text.Trim();
            if (panelConfig.Length > 0 && !File.Exists(panelConfig))
                throw new InvalidOperationException("The custom panel JSON does not exist:\r\n" + panelConfig);
            string panelKey = ChoiceKey(panelBox);
            if (panelKey.Length == 0)
                throw new InvalidOperationException("Choose the staining panel that matches the marker names and acquisition channel order.");
            bool panelWasAutoDetected =
                string.Equals(panelKey, "AUTO", StringComparison.OrdinalIgnoreCase);
            int panelDetectionImageCount = 0;
            PanelDetectionResult autoDetection = null;
            if (panelWasAutoDetected)
            {
                if (SelectedRoute == ImageRoute.IfSlideScanner)
                    throw new InvalidOperationException(
                        "AUTO panel detection is not offered on the whole-slide route. Stage 1 " +
                        "writes the panel into every tile's samplesheet row before Fiji sees a " +
                        "tile, and 'panel' is a grouping key downstream, so two panels for one " +
                        "animal would silently split it into two rows. Select the panel " +
                        "explicitly.");
                if (panelConfig.Length > 0)
                    throw new InvalidOperationException(
                        "AUTO detects built-in panels only. Choose the custom panel key explicitly when a custom panel JSON is selected.");
                autoDetection = DetectBuiltInPanels(
                    input, includeRegex, recursiveBox.Checked);
                panelDetectionImageCount = autoDetection.AnalyticalImageCount;
                panelKey = autoDetection.FallbackPanel;
            }
            string builtInPanelKey = PanelDescriptions.Keys.FirstOrDefault(
                delegate(string key) { return string.Equals(key, panelKey, StringComparison.OrdinalIgnoreCase); });
            if (builtInPanelKey != null)
                panelKey = builtInPanelKey;
            if (!PanelDescriptions.ContainsKey(panelKey) && panelConfig.Length == 0)
                throw new InvalidOperationException(
                    "Panel '" + panelKey + "' is not built in. Select its validated custom panel JSON under Advanced study options.");
            bool includesS2 = string.Equals(panelKey, "S2", StringComparison.OrdinalIgnoreCase) ||
                (autoDetection != null && autoDetection.PanelCounts.ContainsKey("S2"));
            if (includesS2 &&
                !string.Equals(ChoiceKey(projectionBox), "single", StringComparison.OrdinalIgnoreCase) &&
                !string.Equals(ChoiceKey(projectionBox), "layer_aware", StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException(
                    "Panel S2 contains YAP nuclear-to-cytoplasmic analysis and requires either Z-stack handling = single " +
                    "or layer_aware, which assigns YAP a single-plane policy. Confirm the intended plane in the saved Z profile.");

            RuntimePaths runtime = RuntimeBundle.EnsureExtracted();
            string autoPanelMapPath = autoDetection == null ? null :
                WriteAutoPanelMap(autoDetection, runtime.RuntimeDirectory);

            // ---------------------------------------------------------
            // v1.8.0: the fail-closed gate runs BEFORE anything is created
            // on disk, so a blocked run leaves no trace at all.
            // ---------------------------------------------------------
            ImageRoute route = SelectedRoute;
            RunRequest request = ReadRunRequest();
            request.Route = route;
            request.PanelKey = panelKey;          // AUTO already resolved above
            request.PreviewOnly = previewOnly;
            GateResult gate = EvaluateGate(request);
            if (gate.Blocked)
            {
                List<string> reasons = new List<string>();
                foreach (GateFinding finding in gate.OfSeverity(Severity.Block))
                    reasons.Add("• " + finding.Message);
                throw new InvalidOperationException(
                    "This run was refused before anything was created:\r\n\r\n" +
                    string.Join("\r\n\r\n", reasons.ToArray()));
            }

            string runStem = SanitizeFileName(runNameBox.Text.Trim());
            if (runStem.Length == 0)
                runStem = "IFQ_run";
            if (previewOnly)
                runStem += "_visual_merge_panels";
            // H5: a flagged run says so in its folder name, which survives the
            // folder being moved, renamed in a lab notebook, or zipped up.
            foreach (string stamp in gate.FolderStamps())
                runStem += stamp;
            string timestamp = DateTime.Now.ToString("yyyyMMdd_HHmmss", CultureInfo.InvariantCulture);
            string outputDirectory = MakeUniqueDirectory(Path.Combine(outputBase, runStem + "_" + timestamp));

            Dictionary<string, string> env;
            string invocationDescription;
            string fijiArguments;
            string legacyNote = null;

            if (route == ImageRoute.LegacyFiji172)
            {
                // ---- ROUTE 4 -------------------------------------------------
                // The nineteen v1.7.2 keys, from the transcription in
                // LegacyProfile, followed by the v1.7.2 Advanced overlay applied
                // exactly as v1.7.2 applied it. Nothing else. In particular no
                // IFQ_MIN_INCLUDED_NUCLEI and no IFQ_*_THRESHOLD, because
                // v1.7.2 wrote neither and both change the numbers.
                env = LegacyProfile.BuildEnvironment(
                    input, outputDirectory, panelKey, runtime.RegistryPath,
                    autoPanelMapPath, panelConfig.Length > 0 ? panelConfig : null,
                    recursiveBox.Checked, includeRegex,
                    Decimal.ToInt32(maxImagesBox.Value), ChoiceKey(segmenterBox),
                    ChoiceKey(projectionBox), Decimal.ToInt32(singlePlaneBox.Value),
                    previewOnly, ChoiceKey(tissueModeBox), ChoiceKey(compartmentModeBox),
                    ChoiceKey(wholeCompartmentBox));
                foreach (KeyValuePair<string, string> item in
                         ParseAdvancedEnvironment(advancedBox.Text))
                    env[item.Key] = item.Value;

                fijiArguments = LegacyProfile.CommandLine(runtime.ScriptPath);
                invocationDescription =
                    "launcher_exe (v1.7.2): " + Path.GetFileName(fiji) + " " + fijiArguments;
                legacyNote = LegacyProfile.CheckEmbeddedArtefacts(
                    runtime.PipelineSha256, runtime.RegistrySha256);
                if (legacyNote == null)
                    legacyNote =
                        "Environment, command line, embedded pipeline and embedded registry all " +
                        "match v" + LegacyProfile.FrozenVersion + ".";
            }
            else
            {
                // ---- ROUTES 1 and 2 -----------------------------------------
                // PanelForRequest, not ResolveSelectedPanel: the gate, this
                // builder and the launch seal must all be looking at the same
                // channel list, and after AUTO is resolved the combo no longer
                // holds the key this request will run under.
                env = RunEnvironment.BuildStage2(
                    request, PanelForRequest(request), engineThresholdMarkers,
                    runtime.RegistryPath, outputDirectory, input, autoPanelMapPath,
                    previewOnly);

                if (request.Invocation == FijiInvocation.BundledJvm)
                {
                    ToolInventory tools = ResolveTools(request);
                    fijiArguments = FijiCommand.BundledJvmArguments(
                        tools.FijiDirectory, tools.Ij1PatcherJar, runtime.ScriptPath, "8g");
                    fiji = tools.JavaExecutable;
                    invocationDescription =
                        "bundled_jvm: " + fiji + " " + fijiArguments;
                }
                else
                {
                    fijiArguments = FijiCommand.LauncherExeArguments(runtime.ScriptPath);
                    invocationDescription =
                        "launcher_exe: " + Path.GetFileName(fiji) + " " + fijiArguments;
                }
            }

            RunConfiguration config = new RunConfiguration();
            config.InputDirectory = Path.GetFullPath(input);
            config.OutputDirectory = outputDirectory;
            config.FijiExecutable = fiji;
            config.RuntimeDirectory = runtime.RuntimeDirectory;
            config.ScriptPath = runtime.ScriptPath;
            config.RegistryPath = runtime.RegistryPath;
            config.Environment = env;
            config.PanelWasAutoDetected = panelWasAutoDetected;
            config.PanelDetectionImageCount = panelDetectionImageCount;
            config.AutoPanelCounts = autoDetection == null ? null : autoDetection.PanelCounts;
            config.AutoPanelMapPath = autoPanelMapPath;
            config.PreviewOnly = previewOnly;
            config.Request = request;
            config.Gate = gate;
            config.FijiArguments = fijiArguments;
            config.InvocationDescription = invocationDescription;
            config.LegacyArtefactNote = legacyNote;

            if (route == ImageRoute.IfSlideScanner)
            {
                ToolInventory tools = ResolveTools(request);
                config.QuPathExecutable = tools.QuPathExecutable;
                config.PythonExecutable = tools.PythonExecutable;
                config.Stage1ScriptPath = runtime.Stage1ScriptPath;
                config.Stage3ScriptPath = runtime.Stage3ScriptPath;
                config.Stage1Environment = RunEnvironment.BuildStage1(request, panelKey);
            }

            // -------------------------------------------------------------
            // THE LAUNCH CHOKE POINT.
            //
            // Every process this run will start gets a seal here, and nothing
            // downstream can start a process without one: EnvironmentApply.Apply
            // takes a RunSeal and RunSeal's constructor is private. The seal
            // re-derives, from the FINAL merged environment, which channels are
            // genuinely frozen, and throws if that disagrees with the record
            // this run is about to write.
            //
            // H1/H3/H4 moved inside it, so they too are checked against the
            // environment that will actually be handed to the process rather
            // than against the UI that produced it. The Advanced key set is
            // passed for one rule: on route 4 IFQ_MIN_INCLUDED_NUCLEI is legal
            // in the environment if and only if the operator typed it, because
            // v1.7.2's Advanced box was the only way to set the nuclei floor.
            // -------------------------------------------------------------
            List<string> advancedKeys =
                new List<string>(ParseAdvancedEnvironment(advancedBox.Text).Keys);
            PanelDef sealPanel = PanelForRequest(request);

            SealInput stage2 = new SealInput();
            stage2.Stage = LaunchStage.Stage2Fiji;
            stage2.Request = request;
            stage2.Panel = sealPanel;
            stage2.EngineThresholdMarkers = engineThresholdMarkers;
            stage2.Gate = gate;
            stage2.Environment = env;
            stage2.OutputDirectory = outputDirectory;
            stage2.AdvancedKeys = advancedKeys;
            config.Stage2Seal = RunSeal.Issue(stage2);

            if (route == ImageRoute.IfSlideScanner)
            {
                SealInput stage1 = new SealInput();
                stage1.Stage = LaunchStage.Stage1QuPath;
                stage1.Request = request;
                stage1.Panel = sealPanel;
                stage1.EngineThresholdMarkers = engineThresholdMarkers;
                stage1.Gate = gate;
                stage1.Environment = config.Stage1Environment;
                stage1.OutputDirectory = null;   // stage 1 writes its own root
                stage1.AdvancedKeys = advancedKeys;
                config.Stage1Seal = RunSeal.Issue(stage1);

                // Stage 3 reads no IFQ_* at all. It still gets a seal, because
                // "this stage needs no environment" is a claim worth checking
                // once rather than a reason to skip the choke point.
                SealInput stage3 = new SealInput();
                stage3.Stage = LaunchStage.Stage3Python;
                stage3.Request = request;
                stage3.Panel = sealPanel;
                stage3.EngineThresholdMarkers = engineThresholdMarkers;
                stage3.Gate = gate;
                stage3.Environment = new Dictionary<string, string>(
                    StringComparer.OrdinalIgnoreCase);
                stage3.AdvancedKeys = advancedKeys;
                config.Stage3Seal = RunSeal.Issue(stage3);
            }
            return config;
        }

        internal static string DisplayChannelExportSetting(bool previewOnly)
        {
            // Kept as a mode-aware policy seam so the packaged self-test can
            // verify that both launcher operations request companion views.
            return "true";
        }

        private static Dictionary<string, string> ParseAdvancedEnvironment(string text)
        {
            Dictionary<string, string> values = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            string[] lines = (text ?? "").Replace("\r\n", "\n").Replace('\r', '\n').Split('\n');
            for (int index = 0; index < lines.Length; index++)
            {
                string line = lines[index].Trim();
                if (line.Length == 0 || line.StartsWith("#", StringComparison.Ordinal))
                    continue;
                int equals = line.IndexOf('=');
                if (equals <= 0)
                    throw new InvalidOperationException(
                        "Advanced setting line " + (index + 1) + " must use KEY=VALUE.");
                string key = line.Substring(0, equals).Trim().ToUpperInvariant();
                string value = line.Substring(equals + 1).Trim();
                if (!Regex.IsMatch(key, @"^IFQ_[A-Z0-9_]+$"))
                    throw new InvalidOperationException(
                        "Advanced setting line " + (index + 1) + " has an invalid IFQ key: " + key);
                if (ProtectedEnvironmentKeys.Contains(key))
                    throw new InvalidOperationException(
                        key + " is controlled by the launcher interface and cannot be overridden in Advanced settings.");
                if (value.Length == 0)
                    throw new InvalidOperationException(key + " cannot have an empty value.");
                values[key] = value;
            }
            return values;
        }

        // v1.7.2's ClearIfqEnvironment used to be re-exported here as a private
        // shim so the route-2 stage runner could strip-and-copy inline. That was
        // a second way into a child process's environment, which is precisely
        // the shape of both defect rounds, so the shim is gone: everything now
        // goes through EnvironmentApply.Apply, which takes a RunSeal. The
        // stripping itself still lives in EnvironmentApply.ClearIfq, which the
        // legacy equivalence harness executes directly.

        private void HandleFijiLine(string line, bool isError)
        {
            AppendLog(isError ? "ERROR: " + line : line);

            Match progress = Regex.Match(
                line,
                @"\[IFQ_PROGRESS\]\s+(\d+)\s*/\s*(\d+)\s*(.*)",
                RegexOptions.IgnoreCase);
            if (progress.Success)
            {
                int current;
                int total;
                if (Int32.TryParse(progress.Groups[1].Value, out current) &&
                    Int32.TryParse(progress.Groups[2].Value, out total))
                {
                    UpdateImageProgress(current, total, progress.Groups[3].Value.Trim());
                }
            }
            else if (line.IndexOf("DONE. Wrote run_summary.csv", StringComparison.OrdinalIgnoreCase) >= 0 ||
                     line.IndexOf("VISUAL MERGE PANELS COMPLETE", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                UpdateFinalizingProgress();
            }
        }

        private void SetProgressPreparing()
        {
            progressBar.Style = ProgressBarStyle.Marquee;
            progressBar.MarqueeAnimationSpeed = 28;
            progressDetailLabel.Text = runningPreview
                ? "Preparing Fiji to create visual merge panels..."
                : "Preparing the embedded pipeline and starting Fiji...";
            statusLabel.Text = runningPreview
                ? "Starting — visual merge panels are being prepared"
                : "Starting — Fiji is being prepared";
            statusLabel.ForeColor = Color.DarkBlue;
        }

        private void UpdateImageProgress(int current, int total, string fileName)
        {
            if (InvokeRequired)
            {
                BeginInvoke(new Action<int, int, string>(UpdateImageProgress), current, total, fileName);
                return;
            }
            total = Math.Max(1, total);
            current = Math.Max(1, Math.Min(total, current));
            progressBar.MarqueeAnimationSpeed = 0;
            progressBar.Style = ProgressBarStyle.Continuous;
            progressBar.Minimum = 0;
            progressBar.Maximum = total;
            progressBar.Value = Math.Max(0, current - 1);
            statusLabel.Text = runningPreview
                ? "Merging — image " + current + " of " + total
                : "Running — processing image " + current + " of " + total;
            statusLabel.ForeColor = Color.DarkBlue;
            progressDetailLabel.Text = fileName.Length > 0
                ? (runningPreview ? "Currently creating a visual merge panel for: " : "Currently analyzing: ") + fileName
                : (runningPreview ? "Fiji visual merge panel generation is ongoing." : "Fiji analysis is ongoing.");
        }

        private void UpdateFinalizingProgress()
        {
            if (InvokeRequired)
            {
                BeginInvoke(new Action(UpdateFinalizingProgress));
                return;
            }
            progressBar.MarqueeAnimationSpeed = 25;
            progressBar.Style = ProgressBarStyle.Marquee;
            statusLabel.Text = runningPreview
                ? "Finalizing — checking visual merge panels"
                : "Finalizing — writing summary and run record";
            statusLabel.ForeColor = Color.DarkBlue;
            progressDetailLabel.Text = runningPreview
                ? "Visual merge generation finished; checking PNG outputs."
                : "Image processing finished; checking required output files.";
        }

        private void SetProgressTerminal(string detail, bool succeeded, bool cancelled)
        {
            progressBar.MarqueeAnimationSpeed = 0;
            progressBar.Style = ProgressBarStyle.Continuous;
            progressBar.Minimum = 0;
            progressBar.Maximum = 100;
            progressBar.Value = succeeded ? 100 : 0;
            if (cancelled)
            {
                statusLabel.Text = "Cancelled — Fiji was terminated";
                statusLabel.ForeColor = Color.DarkOrange;
            }
            else if (succeeded)
            {
                statusLabel.Text = "Complete — summary and QC outputs are ready";
                statusLabel.ForeColor = Color.DarkGreen;
            }
            else
            {
                statusLabel.Text = "Stopped with a problem — review the log and manifest";
                statusLabel.ForeColor = Color.DarkRed;
            }
            progressDetailLabel.Text = detail;
        }

        private void FinishDisplayPreview(RunConfiguration config, int exitCode, string waitError)
        {
            SetRunningState(false);
            string[] allFiles = Directory.Exists(config.OutputDirectory)
                ? Directory.GetFiles(config.OutputDirectory, "*", SearchOption.AllDirectories)
                : new string[0];
            string[] previewPngs = allFiles.Where(delegate(string path)
            {
                string name = Path.GetFileName(path);
                return path.EndsWith(".png", StringComparison.OrdinalIgnoreCase) &&
                       (name.IndexOf("__DISPLAY_ONLY__", StringComparison.OrdinalIgnoreCase) >= 0 ||
                        name.IndexOf("__VISUAL_MERGE_PANEL__", StringComparison.OrdinalIgnoreCase) >= 0);
            }).ToArray();
            int mergedCount = previewPngs.Count(delegate(string path)
            {
                return Path.GetFileName(path).EndsWith(
                    "__VISUAL_MERGE_PANEL__merged_enhanced.png",
                    StringComparison.OrdinalIgnoreCase);
            });
            string[] unexpectedFiles = allFiles.Where(delegate(string path)
            {
                string name = Path.GetFileName(path);
                return !path.EndsWith(".png", StringComparison.OrdinalIgnoreCase) ||
                       (name.IndexOf("__DISPLAY_ONLY__", StringComparison.OrdinalIgnoreCase) < 0 &&
                        name.IndexOf("__VISUAL_MERGE_PANEL__", StringComparison.OrdinalIgnoreCase) < 0);
            }).ToArray();

            AppendLog("");
            AppendLog("Fiji visual merge panel exit code: " + exitCode);
            AppendLog("Visual merge/supporting PNGs: " + previewPngs.Length +
                      " across " + mergedCount + " source image(s).");
            if (unexpectedFiles.Length > 0)
                AppendLog("Unexpected non-visual files: " + unexpectedFiles.Length + ".");
            if (waitError != null)
                AppendLog("Process wait error: " + waitError);

            lastSummaryPath = null;
            openSummaryButton.Enabled = false;
            openOutputButton.Enabled = Directory.Exists(config.OutputDirectory);
            bool complete = exitCode == 0 && waitError == null &&
                            mergedCount > 0 &&
                            unexpectedFiles.Length == 0;
            if (cancellationRequested)
            {
                SetProgressTerminal(
                    "Visual merge panel generation was cancelled. Any PNGs already written remain available for inspection.",
                    false,
                    true);
            }
            else if (complete)
            {
                SetProgressTerminal(
                    "Created visual merge panels for " + mergedCount +
                    " image(s). No segmentation or quantitative outputs were generated.",
                    true,
                    false);
                statusLabel.Text = "Complete — visual merge panels are ready";
                progressDetailLabel.Text =
                    "Open the output folder to review the labeled per-channel and merged PNGs.";
            }
            else
            {
                SetProgressTerminal(
                    "Visual merge panel generation did not complete cleanly. Review the Fiji log; no output is valid for quantification.",
                    false,
                    false);
                statusLabel.Text = "Visual merge panels stopped with a problem — review the log";
            }
            DeleteTemporaryPanelMap(config);
            runningPreview = false;
        }

        private void FinishAnalysis(RunConfiguration config, int exitCode, string waitError)
        {
            SetRunningState(false);
            string manifestPath = Path.Combine(config.OutputDirectory, "run_manifest.json");
            string summaryCsvPath = Path.Combine(config.OutputDirectory, "run_summary.csv");
            string summaryWorkbookPath = Path.Combine(config.OutputDirectory, "run_summary.xlsx");
            string manifestStatus = "missing";
            string successCount = "?";
            string skippedCount = "?";
            string failureCount = "?";
            string outputFailureCount = "?";

            if (File.Exists(manifestPath))
            {
                try
                {
                    JavaScriptSerializer json = new JavaScriptSerializer();
                    Dictionary<string, object> manifest =
                        json.Deserialize<Dictionary<string, object>>(File.ReadAllText(manifestPath, Encoding.UTF8));
                    manifestStatus = GetDictionaryValue(manifest, "status", "unknown");
                    successCount = GetDictionaryValue(manifest, "success_count", "?");
                    skippedCount = GetDictionaryValue(manifest, "skipped_count", "0");
                    failureCount = GetDictionaryValue(manifest, "failure_count", "?");
                    outputFailureCount = GetDictionaryValue(manifest, "output_failure_count", "0");
                }
                catch (Exception ex)
                {
                    AppendLog("Could not read run manifest: " + ex.Message);
                    manifestStatus = "unreadable";
                }
            }

            WriteLauncherRecord(config, exitCode, manifestStatus);
            AppendLog("");
            AppendLog("Fiji exit code: " + exitCode);
            AppendLog("Manifest status: " + manifestStatus);
            AppendLog("Analytical images: " + successCount + " successful; " + failureCount + " failed.");
            AppendLog("Non-analysis acquisitions deliberately skipped: " + skippedCount + ".");
            if (!string.Equals(outputFailureCount, "0", StringComparison.OrdinalIgnoreCase))
                AppendLog("Output-generation failures: " + outputFailureCount + ".");

            if (waitError != null)
                AppendLog("Process wait error: " + waitError);

            lastSummaryPath = File.Exists(summaryWorkbookPath)
                ? summaryWorkbookPath
                : (File.Exists(summaryCsvPath) ? summaryCsvPath : null);
            openSummaryButton.Enabled = lastSummaryPath != null;
            openOutputButton.Enabled = Directory.Exists(config.OutputDirectory);

            bool complete = exitCode == 0 &&
                string.Equals(manifestStatus, "complete", StringComparison.OrdinalIgnoreCase) &&
                File.Exists(summaryCsvPath) &&
                File.Exists(summaryWorkbookPath);
            if (cancellationRequested)
            {
                SetProgressTerminal(
                    "Analysis was cancelled. Partial outputs are retained only for troubleshooting and must not be aggregated.",
                    false,
                    true);
            }
            else if (complete)
            {
                SetProgressTerminal(
                    "Finished successfully: " + successCount + " analytical image(s) processed, " +
                    skippedCount + " non-analysis acquisition(s) skipped, " + failureCount + " failed.",
                    true,
                    false);
                AppendLog("Excel summary: " + summaryWorkbookPath);
                AppendLog("Region-level CSV: " + summaryCsvPath);
            }
            else
            {
                SetProgressTerminal(
                    "Analysis terminated or was incomplete: " + successCount + " image(s) succeeded, " + failureCount +
                    " failed; " + skippedCount + " non-analysis acquisition(s) were skipped. " +
                    "Check the log below and run_manifest.json.",
                    false,
                    false);
            }
            DeleteTemporaryPanelMap(config);
        }

        private static string GetDictionaryValue(Dictionary<string, object> values, string key, string fallback)
        {
            object value;
            if (values != null && values.TryGetValue(key, out value) && value != null)
                return Convert.ToString(value, CultureInfo.InvariantCulture);
            return fallback;
        }

        private void SetRunningState(bool running)
        {
            runButton.Enabled = !running;
            previewButton.Enabled = !running;
            cancelButton.Enabled = running;
            if (running)
            {
                statusLabel.Text = runningPreview
                    ? "Running Fiji visual merge panel generation..."
                    : "Running Fiji analysis...";
                statusLabel.ForeColor = Color.DarkBlue;
            }
        }

        private void CancelRunningProcess()
        {
            Process process = null;
            lock (processLock)
            {
                process = runningProcess;
            }
            if (process == null)
                return;

            DialogResult result = MessageBox.Show(
                this,
                runningPreview
                    ? "Cancel visual merge panel generation? Any PNGs already written will remain in the output folder."
                    : "Cancel the running Fiji analysis? Partial outputs will be retained for diagnosis and must not be aggregated.",
                runningPreview ? "Cancel visual merge panels" : "Cancel analysis",
                MessageBoxButtons.YesNo,
                MessageBoxIcon.Warning);
            if (result != DialogResult.Yes)
                return;

            cancellationRequested = true;
            progressBar.MarqueeAnimationSpeed = 22;
            progressBar.Style = ProgressBarStyle.Marquee;
            statusLabel.Text = "Cancelling — terminating Fiji";
            statusLabel.ForeColor = Color.DarkOrange;
            progressDetailLabel.Text = "Please wait while the Fiji process and its child processes close.";

            try
            {
                if (!process.HasExited)
                {
                    ProcessStartInfo taskKill = new ProcessStartInfo();
                    taskKill.FileName = "taskkill.exe";
                    taskKill.Arguments = "/PID " + process.Id + " /T /F";
                    taskKill.UseShellExecute = false;
                    taskKill.CreateNoWindow = true;
                    using (Process killer = Process.Start(taskKill))
                    {
                        killer.WaitForExit(10000);
                    }
                }
                AppendLog("Cancellation requested.");
            }
            catch (Exception ex)
            {
                AppendLog("Cancellation error: " + ex.Message);
                try { if (!process.HasExited) process.Kill(); } catch { }
            }
        }

        private void AppendLog(string message)
        {
            if (logBox.InvokeRequired)
            {
                logBox.BeginInvoke(new Action<string>(AppendLog), message);
                return;
            }
            logBox.AppendText((message ?? "") + Environment.NewLine);
        }

        /// <summary>
        /// H5. Three independent carriers of "this run is exploratory", because
        /// any single one can be lost: the folder NAME (stamped in
        /// ReadAndValidateConfiguration), launcher_run.txt, and a marker file.
        ///
        /// The marker file is written HERE, after the engine has exited, and
        /// never before it starts. H4 means the engine aborts on a non-empty
        /// IFQ_OUTPUT_DIR, so a marker file written up front would break every
        /// exploratory run it was meant to label.
        /// </summary>
        private static void WriteLauncherRecord(RunConfiguration config, int exitCode, string status)
        {
            try
            {
                string record = RunRecord.Build(
                    config.Request, config.Gate, config.Environment,
                    Convert.ToString(Assembly.GetExecutingAssembly().GetName().Version,
                                     CultureInfo.InvariantCulture),
                    GetWindowsArchitecture(), config.FijiExecutable,
                    config.InvocationDescription, exitCode, status,
                    ComputeSha256(config.ScriptPath), ComputeSha256(config.RegistryPath),
                    config.LegacyArtefactNote);
                if (config.Stage1Environment != null)
                {
                    StringBuilder stage1 = new StringBuilder(record);
                    stage1.AppendLine();
                    stage1.AppendLine("[stage1_environment]");
                    foreach (KeyValuePair<string, string> item in
                             config.Stage1Environment.OrderBy(
                                 delegate(KeyValuePair<string, string> pair) { return pair.Key; }))
                        stage1.AppendLine(item.Key + "=" + item.Value);
                    record = stage1.ToString();
                }
                File.WriteAllText(
                    Path.Combine(config.OutputDirectory, "launcher_run.txt"),
                    record,
                    new UTF8Encoding(false));
            }
            catch
            {
                // The Fiji outputs remain authoritative if this convenience record cannot be written.
            }

            try
            {
                if (config.Gate != null && config.Gate.Exploratory &&
                    Directory.Exists(config.OutputDirectory))
                    File.WriteAllText(
                        Path.Combine(config.OutputDirectory,
                                     FailClosedGate.ExploratoryMarkerFileName),
                        RunRecord.ExploratoryMarkerText(config.Request, config.Gate),
                        new UTF8Encoding(false));
            }
            catch
            {
                // The folder-name stamp and launcher_run.txt still carry it.
            }
        }

        private static string ComputeSha256(string path)
        {
            using (SHA256 algorithm = SHA256.Create())
            using (FileStream stream = File.OpenRead(path))
            {
                byte[] hash = algorithm.ComputeHash(stream);
                StringBuilder text = new StringBuilder(hash.Length * 2);
                foreach (byte value in hash)
                    text.Append(value.ToString("x2", CultureInfo.InvariantCulture));
                return text.ToString();
            }
        }

        internal static string ResolveFijiExecutable(string path)
        {
            if (File.Exists(path) && path.EndsWith(".exe", StringComparison.OrdinalIgnoreCase))
                return Path.GetFullPath(path);
            if (!Directory.Exists(path))
                return null;

            string architecture = GetWindowsArchitecture();
            string[] preferred;
            if (architecture == "ARM64")
            {
                preferred = new string[]
                {
                    "fiji-windows-arm64.exe",
                    "fiji-windows-x64.exe",
                    "ImageJ-win64.exe",
                    "fiji-windows.exe",
                    "ImageJ.exe"
                };
            }
            else
            {
                preferred = new string[]
                {
                    "fiji-windows-x64.exe",
                    "ImageJ-win64.exe",
                    "fiji-windows.exe",
                    "ImageJ.exe",
                    "fiji-windows-arm64.exe"
                };
            }
            foreach (string name in preferred)
            {
                string candidate = Path.Combine(path, name);
                if (File.Exists(candidate))
                    return candidate;
            }

            string[] executables = Directory.GetFiles(path, "*.exe", SearchOption.TopDirectoryOnly);
            foreach (string candidate in executables.OrderBy(delegate(string value) { return value; }))
            {
                string name = Path.GetFileName(candidate);
                if (name.IndexOf("fiji", StringComparison.OrdinalIgnoreCase) >= 0 ||
                    name.IndexOf("imagej", StringComparison.OrdinalIgnoreCase) >= 0)
                    return candidate;
            }
            return null;
        }

        internal static string GetWindowsArchitecture()
        {
            // An AnyCPU .NET Framework launcher can run under x86/x64 emulation
            // on Windows ARM64. In that case the process-scoped variables can
            // describe the emulation layer (AMD64) instead of the native OS
            // (ARM64). The machine-scoped value comes from the system registry
            // and remains native, so it must win when available.
            string machineArchitecture = Environment.GetEnvironmentVariable(
                "PROCESSOR_ARCHITECTURE", EnvironmentVariableTarget.Machine);
            string wowArchitecture = Environment.GetEnvironmentVariable("PROCESSOR_ARCHITEW6432");
            string processArchitecture = Environment.GetEnvironmentVariable("PROCESSOR_ARCHITECTURE");
            return ClassifyWindowsArchitecture(
                machineArchitecture, wowArchitecture, processArchitecture);
        }

        internal static string ClassifyWindowsArchitecture(
            string machineArchitecture, string wowArchitecture, string processArchitecture)
        {
            string architecture = !string.IsNullOrWhiteSpace(machineArchitecture)
                ? machineArchitecture
                : (!string.IsNullOrWhiteSpace(wowArchitecture)
                    ? wowArchitecture
                    : processArchitecture);
            architecture = (architecture ?? "unknown").Trim().ToUpperInvariant();
            if (architecture.Contains("ARM64"))
                return "ARM64";
            if (architecture.Contains("AMD64") || architecture.Contains("X86_64"))
                return "X64";
            if (architecture.Contains("86"))
                return "X86";
            return architecture;
        }

        private static string QuoteArgument(string value)
        {
            return "\"" + value.Replace("\"", "\\\"") + "\"";
        }

        private static string SanitizeFileName(string value)
        {
            if (string.IsNullOrWhiteSpace(value))
                return "";
            string sanitized = value.Trim();
            foreach (char invalid in Path.GetInvalidFileNameChars())
                sanitized = sanitized.Replace(invalid, '_');
            sanitized = Regex.Replace(sanitized, @"\s+", "_");
            return sanitized.Trim('_', '.', ' ');
        }

        private static string MakeUniqueDirectory(string candidate)
        {
            string path = candidate;
            int suffix = 2;
            while (Directory.Exists(path) || File.Exists(path))
            {
                path = candidate + "_" + suffix.ToString(CultureInfo.InvariantCulture);
                suffix++;
            }
            return path;
        }

        private static void OpenPath(string path, bool directory)
        {
            if (string.IsNullOrWhiteSpace(path))
                return;
            try
            {
                if (directory)
                {
                    if (Directory.Exists(path))
                        Process.Start("explorer.exe", QuoteArgument(path));
                }
                else if (File.Exists(path))
                {
                    ProcessStartInfo open = new ProcessStartInfo(path);
                    open.UseShellExecute = true;
                    Process.Start(open);
                }
            }
            catch (Exception ex)
            {
                MessageBox.Show(ex.Message, "Could not open path", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }

        private string SettingsPath
        {
            get
            {
                return Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "IFQuantLauncher",
                    "settings.ini");
            }
        }

        private void SaveSettings()
        {
            try
            {
                string directory = Path.GetDirectoryName(SettingsPath);
                Directory.CreateDirectory(directory);
                Dictionary<string, string> settings = new Dictionary<string, string>();
                settings["input"] = inputBox.Text;
                settings["fiji"] = fijiBox.Text;
                settings["output"] = outputBaseBox.Text;
                settings["run_name"] = runNameBox.Text;
                settings["panel"] = ChoiceKey(panelBox);
                settings["segmenter"] = ChoiceKey(segmenterBox);
                settings["projection"] = ChoiceKey(projectionBox);
                settings["single_plane"] = singlePlaneBox.Value.ToString(CultureInfo.InvariantCulture);
                settings["tissue"] = ChoiceKey(tissueModeBox);
                settings["compartment_mode"] = ChoiceKey(compartmentModeBox);
                settings["whole_compartment"] = ChoiceKey(wholeCompartmentBox);
                settings["recursive"] = recursiveBox.Checked ? "true" : "false";
                settings["include_regex_b64"] = Convert.ToBase64String(Encoding.UTF8.GetBytes(includeRegexBox.Text));
                settings["max_images"] = maxImagesBox.Value.ToString(CultureInfo.InvariantCulture);
                settings["panel_config"] = panelConfigBox.Text;
                settings["advanced_b64"] = Convert.ToBase64String(Encoding.UTF8.GetBytes(advancedBox.Text));

                StringBuilder content = new StringBuilder();
                foreach (KeyValuePair<string, string> item in settings)
                    content.AppendLine(item.Key + "=" + (item.Value ?? ""));
                File.WriteAllText(SettingsPath, content.ToString(), new UTF8Encoding(false));
            }
            catch
            {
                // Settings persistence is optional and must never block a run.
            }
        }

        private void LoadSavedSettings()
        {
            try
            {
                if (!File.Exists(SettingsPath))
                    return;
                Dictionary<string, string> values = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
                foreach (string line in File.ReadAllLines(SettingsPath, Encoding.UTF8))
                {
                    int equals = line.IndexOf('=');
                    if (equals > 0)
                        values[line.Substring(0, equals)] = line.Substring(equals + 1);
                }
                inputBox.Text = GetValue(values, "input", inputBox.Text);
                fijiBox.Text = GetValue(values, "fiji", fijiBox.Text);
                outputBaseBox.Text = GetValue(values, "output", outputBaseBox.Text);
                runNameBox.Text = GetValue(values, "run_name", runNameBox.Text);
                SelectChoice(panelBox, GetValue(values, "panel", "AUTO"));
                SelectChoice(segmenterBox, GetValue(values, "segmenter", "classic"));
                SelectChoice(projectionBox, GetValue(values, "projection", "layer_aware"));
                SetNumeric(singlePlaneBox, GetValue(values, "single_plane", "-1"));
                SelectChoice(tissueModeBox, GetValue(values, "tissue", "auto"));
                SelectChoice(compartmentModeBox, GetValue(values, "compartment_mode", "optional"));
                SelectChoice(wholeCompartmentBox, GetValue(values, "whole_compartment", "unassigned"));
                recursiveBox.Checked = string.Equals(GetValue(values, "recursive", "true"), "true", StringComparison.OrdinalIgnoreCase);
                includeRegexBox.Text = DecodeBase64(GetValue(values, "include_regex_b64", ""), ".*");
                SetNumeric(maxImagesBox, GetValue(values, "max_images", "0"));
                panelConfigBox.Text = GetValue(values, "panel_config", "");
                advancedBox.Text = DecodeBase64(GetValue(values, "advanced_b64", ""), "");
            }
            catch
            {
                // Ignore malformed previous settings and retain safe defaults.
            }
        }

        private static string GetValue(Dictionary<string, string> values, string key, string fallback)
        {
            string value;
            return values.TryGetValue(key, out value) ? value : fallback;
        }

        private static string DecodeBase64(string value, string fallback)
        {
            if (string.IsNullOrEmpty(value))
                return fallback;
            try { return Encoding.UTF8.GetString(Convert.FromBase64String(value)); }
            catch { return fallback; }
        }

        private static void SetNumeric(NumericUpDown control, string text)
        {
            decimal value;
            if (Decimal.TryParse(text, NumberStyles.Number, CultureInfo.InvariantCulture, out value))
            {
                value = Math.Max(control.Minimum, Math.Min(control.Maximum, value));
                control.Value = value;
            }
        }
    }

    internal sealed class PanelDetectionResult
    {
        public int AnalyticalImageCount;
        public string FallbackPanel;
        public Dictionary<string, string> PanelByRelativePath =
            new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        public Dictionary<string, int> PanelCounts =
            new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);

        public bool IsMixed
        {
            get { return PanelCounts.Count > 1; }
        }

        public string CountSummary()
        {
            return string.Join(
                ", ",
                PanelCounts.OrderBy(delegate(KeyValuePair<string, int> pair) { return pair.Key; },
                                    StringComparer.OrdinalIgnoreCase)
                           .Select(delegate(KeyValuePair<string, int> pair)
                           {
                               return pair.Key + "=" + pair.Value;
                           }));
        }
    }

    internal sealed class SamplesheetPanelAssignments
    {
        public Dictionary<string, string> ByRelativePath =
            new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        public Dictionary<string, string> ByFilename =
            new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
    }

    internal sealed class RunConfiguration
    {
        public string InputDirectory;
        public string OutputDirectory;
        public string FijiExecutable;
        public string RuntimeDirectory;
        public string ScriptPath;
        public string RegistryPath;
        public bool PanelWasAutoDetected;
        public int PanelDetectionImageCount;
        public Dictionary<string, int> AutoPanelCounts;
        public string AutoPanelMapPath;
        public bool PreviewOnly;
        public Dictionary<string, string> Environment;

        // ---- v1.8.0 ----
        public RunRequest Request;
        public GateResult Gate;
        /// The exact argument string handed to the engine. For route 4 this is
        /// LegacyProfile.CommandLine and nothing else.
        public string FijiArguments;
        public string InvocationDescription;
        public string LegacyArtefactNote;
        // Route 2 only
        public string QuPathExecutable;
        public string PythonExecutable;
        public string Stage1ScriptPath;
        public string Stage3ScriptPath;
        public Dictionary<string, string> Stage1Environment;

        // ---- the launch choke point ----
        //
        // One seal per process this run will start. A seal is the ONLY thing
        // EnvironmentApply.Apply accepts, and RunSeal's constructor is private,
        // so a stage that has no seal here simply cannot be started. That is
        // deliberate: the previous two defect rounds were both a caller reaching
        // the child environment without passing validation, and adding a field
        // to this class is not enough to do that any more.
        public RunSeal Stage1Seal;
        public RunSeal Stage2Seal;
        public RunSeal Stage3Seal;
    }

    internal sealed class RuntimePaths
    {
        public string RuntimeDirectory;
        public string ScriptPath;
        public string RegistryPath;
        public string Stage1ScriptPath;
        public string Stage3ScriptPath;
        public string PipelineSha256;
        public string RegistrySha256;
    }

    internal static class RuntimeBundle
    {
        private const string ScriptResource = "IFQuant.IF_Quant_Pipeline.groovy";
        private const string RegistryResource = "IFQuant.lung_marker_registry.json";
        // v1.8.0: route 2 needs stage 1 and stage 3 on an analysis machine that
        // has no repository checkout, for the same reason v1.7.2 embedded the
        // pipeline. Both are optional at run time so a Fiji-only build of this
        // launcher still starts.
        private const string Stage1Resource = "IFQuant.qupath_wsi_tile_export.groovy";
        private const string Stage3Resource = "IFQuant.aggregate_tiles_to_slide.py";

        public static RuntimePaths EnsureExtracted()
        {
            Version version = Assembly.GetExecutingAssembly().GetName().Version;
            string root = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "IFQuantLauncher",
                "runtime",
                version.ToString());
            string config = Path.Combine(root, "config");
            Directory.CreateDirectory(config);

            string script = Path.Combine(root, "IF_Quant_Pipeline.groovy");
            string registry = Path.Combine(config, "lung_marker_registry.json");
            ExtractResource(ScriptResource, script);
            ExtractResource(RegistryResource, registry);

            string stage1 = Path.Combine(root, "qupath_wsi_tile_export.groovy");
            string stage3 = Path.Combine(root, "aggregate_tiles_to_slide.py");
            if (!TryExtractResource(Stage1Resource, stage1)) stage1 = null;
            if (!TryExtractResource(Stage3Resource, stage3)) stage3 = null;

            RuntimePaths paths = new RuntimePaths();
            paths.RuntimeDirectory = root;
            paths.ScriptPath = script;
            paths.RegistryPath = registry;
            paths.Stage1ScriptPath = stage1;
            paths.Stage3ScriptPath = stage3;
            paths.PipelineSha256 = ComputeSha256(script);
            paths.RegistrySha256 = ComputeSha256(registry);
            return paths;
        }

        /// <summary>
        /// The v1.8.0 half of --self-test. Exit codes 30-45 so they never
        /// collide with v1.7.2's 10-28. Every check here is a property the
        /// build must not ship without; build.ps1 runs this and discards the
        /// binary on a non-zero result.
        /// </summary>
        private static int RouteSelfTest(string pipelineText, RuntimePaths paths)
        {
            // 30 R3 is present in the catalog, and its availability, its reason
            //    and its stage list all agree with LauncherBuild's flag.
            //
            //    This check used to open with `if (BrightfieldRouteEnabled)
            //    return 30;`, which meant flipping the one line advertised to
            //    the user as "re-enabling it is one line" made --self-test
            //    return 30 and build.ps1 delete the binary. Every assertion here
            //    is now written against the flag's VALUE, so the claim is true.
            RouteSpec he = RouteCatalog.Describe(ImageRoute.HeBrightfield);
            if (he.Available != LauncherBuild.BrightfieldRouteEnabled) return 30;
            if (!he.Available && string.IsNullOrEmpty(he.UnavailableReason)) return 30;
            if (he.Available && !string.IsNullOrEmpty(he.UnavailableReason)) return 30;
            if (he.Available && he.Stages.Count == 0) return 30;
            if (!he.Available && he.Stages.Count != 0) return 30;
            if (RouteCatalog.All().Length != 4) return 30;

            // 31 R3 fails closed in the environment builder: no partial
            //    environment, no run, a named reason.
            RunRequest heRequest = new RunRequest();
            heRequest.Route = ImageRoute.HeBrightfield;
            heRequest.PanelKey = "LEFT";
            heRequest.WsiInput = Path.GetTempPath();
            heRequest.WsiOutput = Path.GetTempPath();
            bool refused = false;
            try
            {
                RunEnvironment.BuildStage2(
                    heRequest, null, null, "r", "o", Path.GetTempPath(), null, false);
            }
            catch (InvalidOperationException) { refused = true; }
            catch (NotImplementedException) { refused = true; }
            if (!refused) return 31;

            // 31b R3 also fails closed in the STAGE 1 builder, which is the one
            //     a brightfield wiring reaches first: route 3 is the only route
            //     besides 2 that declares RequiresQuPath. It had no guard at all
            //     and returned a complete seven-variable stage-1 environment.
            bool stage1Refused = false;
            try { RunEnvironment.BuildStage1(heRequest, "LEFT"); }
            catch (InvalidOperationException) { stage1Refused = true; }
            catch (NotImplementedException) { stage1Refused = true; }
            if (!stage1Refused) return 31;

            // 32 R3 is blocked by the gate even with everything else perfect,
            //    and while the flag is off the block names the route itself
            //    rather than happening to trip over some other rule.
            GateResult heGate = FailClosedGate.Evaluate(heRequest, null, null, null);
            if (!heGate.Blocked || heGate.NeedsConfirmation) return 32;
            if (!LauncherBuild.BrightfieldRouteEnabled && !HasCode(heGate, "ROUTE_NOT_AVAILABLE"))
                return 32;

            // 33 the panel table and the threshold whitelist really parse.
            Dictionary<string, PanelDef> panels = PanelRegistry.ParseFromPipeline(pipelineText);
            HashSet<string> thresholdMarkers =
                PanelRegistry.ParseThresholdMarkerTokens(pipelineText);
            PanelDef left;
            if (!panels.TryGetValue("LEFT", out left)) return 33;
            if (left.AnalysisChannels.Count != 3) return 33;
            if (!thresholdMarkers.Contains("KRT5") || !thresholdMarkers.Contains("T1A")) return 33;
            // The engine declares LEFT's podoplanin channel as "T1A", so the
            // variable is IFQ_T1A_THRESHOLD. IFQ_PDPN_THRESHOLD does nothing.
            bool sawT1A = false;
            foreach (ChannelDef channel in left.AnalysisChannels)
                if (channel.ThresholdEnvName == "IFQ_T1A_THRESHOLD") sawT1A = true;
            if (!sawT1A) return 33;

            // 34 H1: no panel is a block, panel T is a block until unlocked.
            RunRequest bare = new RunRequest();
            bare.Route = ImageRoute.IfConfocal;
            bare.OutputBase = "out";
            if (!FailClosedGate.Evaluate(bare, null, thresholdMarkers, null).Blocked) return 34;
            bare.PanelKey = "T";
            if (!FailClosedGate.Evaluate(bare, null, thresholdMarkers, null).Blocked) return 34;
            bare.PilotPanelsUnlocked = true;
            GateResult pilot = FailClosedGate.Evaluate(bare, null, thresholdMarkers, null);
            if (pilot.Blocked || !pilot.RequiredPhrases.Contains(FailClosedGate.PilotPhrase)) return 34;

            // 35 H2: a blank threshold is exploratory on R1 and blocked on R2.
            RunRequest r1 = NewLeftRequest(ImageRoute.IfConfocal);
            GateResult g1 = FailClosedGate.Evaluate(r1, left, thresholdMarkers, null);
            if (g1.Blocked || !g1.Exploratory ||
                !g1.RequiredPhrases.Contains(FailClosedGate.ExploratoryPhrase) ||
                g1.FolderStamps().Count == 0) return 35;
            RunRequest r2 = NewLeftRequest(ImageRoute.IfSlideScanner);
            r2.WsiInput = "in"; r2.WsiOutput = "out";
            if (!FailClosedGate.Evaluate(r2, left, thresholdMarkers, null).Blocked) return 35;

            // 36 H2: every channel frozen clears the flag; a bad value blocks.
            RunRequest frozen = NewLeftRequest(ImageRoute.IfConfocal);
            foreach (ChannelDef channel in left.AnalysisChannels)
                frozen.Thresholds[channel.Token] = "500";
            GateResult frozenGate = FailClosedGate.Evaluate(frozen, left, thresholdMarkers, null);
            if (frozenGate.Blocked || frozenGate.Exploratory ||
                frozenGate.NeedsConfirmation) return 36;
            frozen.Thresholds[left.AnalysisChannels[0].Token] = "0";
            if (!FailClosedGate.Evaluate(frozen, left, thresholdMarkers, null).Blocked) return 36;

            // 37 H3: the floor is written explicitly, and a non-zero value is
            //    refused on a panel with an area endpoint.
            Dictionary<string, string> env = RunEnvironment.BuildStage2(
                NewLeftRequest(ImageRoute.IfConfocal), left, thresholdMarkers,
                "registry", "out", Path.GetTempPath(), null, false);
            if (!env.ContainsKey("IFQ_MIN_INCLUDED_NUCLEI")) return 37;
            if (env["IFQ_MIN_INCLUDED_NUCLEI"] != "0") return 37;
            RunRequest floored = NewLeftRequest(ImageRoute.IfConfocal);
            floored.MinIncludedNuclei = 3;
            if (left.AreaMarkers.Count > 0 &&
                !FailClosedGate.Evaluate(floored, left, thresholdMarkers, null).Blocked)
                return 37;

            // 38 H4: IFQ_ALLOW_NONEMPTY_OUTPUT is hardcoded false and a
            //    non-empty output folder throws before any process starts.
            if (env["IFQ_ALLOW_NONEMPTY_OUTPUT"] != "false") return 38;
            string probe = Path.Combine(
                Path.GetTempPath(), "IFQuantLauncher-h4-" + Guid.NewGuid().ToString("N"));
            bool h4Threw = false;
            try
            {
                Directory.CreateDirectory(probe);
                File.WriteAllText(Path.Combine(probe, "stale.csv"), "x");
                try { PreStartAssertions.AssertOutputDirectoryEmpty(probe); }
                catch (InvalidOperationException) { h4Threw = true; }
            }
            finally
            {
                if (Directory.Exists(probe)) Directory.Delete(probe, true);
            }
            if (!h4Threw) return 38;

            // 39 H5: an exploratory run is marked in the folder name, in the
            //    record, and in the marker file.
            string record = RunRecord.Build(
                r1, g1, env, "1.8.0.0", "X64", "fiji.exe", "launcher_exe", 0, "complete",
                "a", "b", null);
            if (record.IndexOf("run_classification=EXPLORATORY_DO_NOT_AGGREGATE",
                               StringComparison.Ordinal) < 0) return 39;
            if (record.IndexOf("adaptive_otsu_exploratory", StringComparison.Ordinal) < 0) return 39;
            if (RunRecord.ExploratoryMarkerText(r1, g1)
                    .IndexOf("DO NOT AGGREGATE", StringComparison.Ordinal) < 0) return 39;

            // 40 route 4 reproduces the recorded v1.7.2 fixture exactly.
            if (LegacyProfile.Fingerprint(LegacyProfile.Fixture()) != V172FixtureFingerprint)
                return 40;
            Dictionary<string, string> legacy = LegacyProfile.Fixture();
            if (legacy.Count != 17) return 40;            // 19 keys, 2 conditional
            if (legacy.ContainsKey("IFQ_MIN_INCLUDED_NUCLEI")) return 40;
            foreach (string key in legacy.Keys)
                if (key.EndsWith("_THRESHOLD", StringComparison.Ordinal)) return 40;
            if (LegacyProfile.CommandLine("S") != "--headless --console --run \"S\"") return 40;

            // 41 route 4 refuses to be built by the route 1/2 builder.
            RunRequest legacyRequest = NewLeftRequest(ImageRoute.LegacyFiji172);
            bool legacyRefused = false;
            try
            {
                RunEnvironment.BuildStage2(
                    legacyRequest, left, thresholdMarkers, "r", "o", Path.GetTempPath(),
                    null, false);
            }
            catch (InvalidOperationException) { legacyRefused = true; }
            if (!legacyRefused) return 41;

            // 42 the advanced box can no longer hide a typo or a no-op.
            RunRequest typo = NewLeftRequest(ImageRoute.IfConfocal);
            typo.AdvancedText = "IFQ_KRT_5_THRESHOLD=400";
            if (!FailClosedGate.Evaluate(typo, left, thresholdMarkers, null).Blocked) return 42;
            typo.AdvancedText = "IFQ_MIN_INCLUDED_NUCLEI=5";
            if (!FailClosedGate.Evaluate(typo, left, thresholdMarkers, null).Blocked) return 42;
            typo.AdvancedText = "IFQ_RING_EXPAND_UM=";
            if (!FailClosedGate.Evaluate(typo, left, thresholdMarkers, null).Blocked) return 42;

            // 43 the whole-slide stage scripts really were embedded.
            if (paths.Stage1ScriptPath == null || !File.Exists(paths.Stage1ScriptPath)) return 43;
            if (paths.Stage3ScriptPath == null || !File.Exists(paths.Stage3ScriptPath)) return 43;
            string stage1Text = File.ReadAllText(paths.Stage1ScriptPath, Encoding.UTF8);
            if (stage1Text.IndexOf("IFQ_WSI_INPUT", StringComparison.Ordinal) < 0) return 43;

            // ---------------------------------------------------------------
            // 44 H2 CANNOT BE BYPASSED BY A PANEL THE LAUNCHER CANNOT RESOLVE.
            //    v1.8.0.0 guarded the whole H2 block on `panel != null`, so a
            //    custom panel key + a custom panel JSON + confirmatory tier +
            //    zero thresholds went GREEN and recorded THRESHOLDS_FROZEN.
            // ---------------------------------------------------------------
            RunRequest unresolved = NewLeftRequest(ImageRoute.IfConfocal);
            unresolved.PanelKey = "MYCUSTOM";
            unresolved.PanelConfigJson = @"C:\fixture\custom_panel.json";
            unresolved.Tier = RunTier.Confirmatory;
            if (!FailClosedGate.Evaluate(unresolved, null, thresholdMarkers, null).Blocked)
                return 44;

            unresolved.Route = ImageRoute.IfSlideScanner;
            unresolved.Tier = RunTier.Exploratory;
            unresolved.WsiInput = "in";
            unresolved.WsiOutput = "out";
            if (!FailClosedGate.Evaluate(unresolved, null, thresholdMarkers, null).Blocked)
                return 44;

            unresolved.Route = ImageRoute.IfConfocal;
            GateResult unresolvedGate =
                FailClosedGate.Evaluate(unresolved, null, thresholdMarkers, null);
            if (unresolvedGate.Blocked) return 44;
            if (!unresolvedGate.Exploratory) return 44;
            if (!unresolvedGate.RequiredPhrases.Contains(FailClosedGate.ExploratoryPhrase))
                return 44;
            if (!unresolvedGate.FolderStamps().Contains(FailClosedGate.ExploratoryStamp))
                return 44;
            string unresolvedRecord = RunRecord.Build(
                unresolved, unresolvedGate, env, "1.8.0.0", "X64", "fiji.exe", "launcher_exe",
                0, "complete", "a", "b", null);
            if (unresolvedRecord.IndexOf("run_classification=EXPLORATORY_DO_NOT_AGGREGATE",
                                         StringComparison.Ordinal) < 0) return 44;

            // A custom panel that DOES parse gets a real channel list, so it is
            // supported rather than merely blocked.
            string customJson =
                "{\"panels\":{\"MYCUSTOM\":{\"label\":\"probe\",\"channels\":[" +
                "{\"idx\":1,\"marker\":\"DAPI\",\"role\":\"nuclear\"}," +
                "{\"idx\":2,\"marker\":\"KRT5\",\"role\":\"cyto\",\"areaMarker\":true}]}}}";
            Dictionary<string, string> defaultRoles = MarkerRoleDefaults.ParseFromRegistry(
                File.ReadAllText(paths.RegistryPath, Encoding.UTF8));
            CustomPanelParse customParse = CustomPanelRegistry.Parse(
                customJson, defaultRoles, new List<string>(panels.Keys), left.ChannelsAreThresholdable);
            if (!customParse.Ok) return 44;
            PanelDef custom;
            if (!customParse.Panels.TryGetValue("MYCUSTOM", out custom)) return 44;
            if (custom.AnalysisChannels.Count != 1) return 44;
            if (custom.AreaMarkers.Count != 1) return 44;
            if (custom.AnalysisChannels[0].ThresholdEnvName != "IFQ_KRT5_THRESHOLD") return 44;
            RunRequest customRequest = NewLeftRequest(ImageRoute.IfConfocal);
            customRequest.PanelKey = "MYCUSTOM";
            customRequest.PanelConfigJson = @"C:\fixture\custom_panel.json";
            GateResult customBlank =
                FailClosedGate.Evaluate(customRequest, custom, thresholdMarkers, null);
            if (customBlank.Blocked || !customBlank.Exploratory) return 44;
            customRequest.Thresholds[custom.AnalysisChannels[0].Token] = "480";
            GateResult customFrozen =
                FailClosedGate.Evaluate(customRequest, custom, thresholdMarkers, null);
            if (customFrozen.Blocked || customFrozen.Exploratory ||
                customFrozen.NeedsConfirmation) return 44;
            // A channel of the selected panel is always freezable, custom or
            // not (IF_Quant_Pipeline.groovy:873-882), so the variable must
            // actually be emitted.
            Dictionary<string, string> customEnv = RunEnvironment.BuildStage2(
                customRequest, custom, thresholdMarkers, "reg", "out", Path.GetTempPath(),
                null, false);
            if (!customEnv.ContainsKey("IFQ_KRT5_THRESHOLD")) return 44;
            if (customEnv["IFQ_PANEL"] != "MYCUSTOM") return 44;
            // Malformed input is a block, never a guess.
            if (CustomPanelRegistry.Parse("{ not json", defaultRoles, null, true).Ok) return 44;
            if (CustomPanelRegistry.Parse("{\"panels\":{}}", defaultRoles, null, true).Ok) return 44;
            if (CustomPanelRegistry.Parse(
                    "{\"panels\":{\"X\":{\"channels\":[{\"idx\":1,\"marker\":\"NOT_A_MARKER\"}]}}}",
                    defaultRoles, null, true).Ok) return 44;

            // ---------------------------------------------------------------
            // 45 ROUTE 4 DOES NOT CALL ITSELF FROZEN.
            //    It writes no IFQ_*_THRESHOLD by construction, so every channel
            //    is adaptive unless the v1.7.2 Advanced box froze it. v1.8.0.0
            //    skipped H2 on route 4 and emitted THRESHOLDS_FROZEN.
            // ---------------------------------------------------------------
            RunRequest legacyBlank = NewLeftRequest(ImageRoute.LegacyFiji172);
            GateResult legacyBlankGate =
                FailClosedGate.Evaluate(legacyBlank, left, thresholdMarkers, null);
            if (legacyBlankGate.Blocked || !legacyBlankGate.Exploratory) return 45;
            if (!legacyBlankGate.FolderStamps().Contains(FailClosedGate.ExploratoryStamp))
                return 45;
            string legacyRecord = RunRecord.Build(
                legacyBlank, legacyBlankGate, LegacyProfile.Fixture(), "1.8.0.0", "X64",
                "fiji.exe", "launcher_exe", 0, "complete", "a", "b", null);
            if (legacyRecord.IndexOf("run_classification=EXPLORATORY_DO_NOT_AGGREGATE",
                                     StringComparison.Ordinal) < 0) return 45;
            if (legacyRecord.IndexOf("thresholds_frozen=false", StringComparison.Ordinal) < 0)
                return 45;

            // Freezing every channel through the Advanced box, the only place
            // route 4 has, must clear the flag.
            RunRequest legacyFrozen = NewLeftRequest(ImageRoute.LegacyFiji172);
            StringBuilder legacyAdvanced = new StringBuilder();
            foreach (ChannelDef channel in left.AnalysisChannels)
                legacyAdvanced.AppendLine(channel.ThresholdEnvName + "=500");
            legacyFrozen.AdvancedText = legacyAdvanced.ToString();
            GateResult legacyFrozenGate =
                FailClosedGate.Evaluate(legacyFrozen, left, thresholdMarkers, null);
            if (legacyFrozenGate.Blocked || legacyFrozenGate.Exploratory) return 45;
            string legacyFrozenRecord = RunRecord.Build(
                legacyFrozen, legacyFrozenGate, LegacyProfile.Fixture(), "1.8.0.0", "X64",
                "fiji.exe", "launcher_exe", 0, "complete", "a", "b", null);
            if (legacyFrozenRecord.IndexOf("run_classification=THRESHOLDS_FROZEN",
                                           StringComparison.Ordinal) < 0) return 45;

            // ---------------------------------------------------------------
            // 46 ROUTE 4 ACCEPTS EXACTLY WHAT v1.7.2 ACCEPTED IN ADVANCED.
            //    v1.7.2's rule: KEY=VALUE, ^IFQ_[A-Z0-9_]+$, non-empty value,
            //    not one of ITS nineteen protected keys. Nothing else.
            // ---------------------------------------------------------------
            RunRequest legacyAdv = NewLeftRequest(ImageRoute.LegacyFiji172);
            string[] v172Accepts = new string[]
            {
                "IFQ_MIN_INCLUDED_NUCLEI=3",      // the v1.7.2 nuclei floor
                "IFQ_TOTALLY_UNKNOWN_KEY=1",      // v1.7.2 checked shape only
                "IFQ_WSI_HALO_PX=64",             // a stage 1 name on a Fiji route
                "IFQ_PDPN_THRESHOLD=400",         // a marker LEFT does not have
                "IFQ_WSI_PANEL=LEFT"              // v1.8.0-owned, v1.7.2 was not
            };
            foreach (string line in v172Accepts)
            {
                legacyAdv.AdvancedText = line;
                if (FailClosedGate.Evaluate(legacyAdv, left, thresholdMarkers, null).Blocked)
                    return 46;
            }
            // ...and still refuses exactly what v1.7.2 refused.
            string[] v172Refuses = new string[]
            {
                "no equals sign here",            // v1.7.2: must use KEY=VALUE
                "NOT_AN_IFQ_KEY=1",               // v1.7.2: invalid IFQ key
                "IFQ_RING_EXPAND_UM=",            // v1.7.2: empty value
                "IFQ_PANEL=T",                    // v1.7.2 protected key
                "IFQ_ALLOW_NONEMPTY_OUTPUT=true"  // v1.7.2 protected key
            };
            foreach (string line in v172Refuses)
            {
                legacyAdv.AdvancedText = line;
                if (!FailClosedGate.Evaluate(legacyAdv, left, thresholdMarkers, null).Blocked)
                    return 46;
            }
            // The floor typed into Advanced reaches the process on route 4,
            // exactly as it did in v1.7.2, and is named in the run record.
            legacyAdv.AdvancedText = "IFQ_MIN_INCLUDED_NUCLEI=3";
            GateResult legacyFloorGate =
                FailClosedGate.Evaluate(legacyAdv, left, thresholdMarkers, null);
            if (legacyFloorGate.Blocked) return 46;
            if (RunRecord.Build(legacyAdv, legacyFloorGate, LegacyProfile.Fixture(), "1.8.0.0",
                                "X64", "f", "i", 0, "complete", "a", "b", null)
                    .IndexOf("legacy_min_included_nuclei=3", StringComparison.Ordinal) < 0)
                return 46;
            Dictionary<string, string> legacyFloorEnv = LegacyProfile.Fixture();
            legacyFloorEnv["IFQ_MIN_INCLUDED_NUCLEI"] = "3";
            try
            {
                PreStartAssertions.AssertStage2Environment(
                    legacyFloorEnv, null, ImageRoute.LegacyFiji172,
                    new string[] { "IFQ_MIN_INCLUDED_NUCLEI" });
            }
            catch (InvalidOperationException) { return 46; }
            // But the LAUNCHER writing it by itself is still refused.
            bool launcherWroteFloor = false;
            try
            {
                PreStartAssertions.AssertStage2Environment(
                    legacyFloorEnv, null, ImageRoute.LegacyFiji172, new string[0]);
            }
            catch (InvalidOperationException) { launcherWroteFloor = true; }
            if (!launcherWroteFloor) return 46;

            // ---------------------------------------------------------------
            // 47 AN UNDEFINED ROUTE ID FAILS CLOSED EVERYWHERE.
            //    RouteCatalog.Describe used to fall out of its switch with the
            //    RouteSpec field initialisers intact: Available=true, no stages,
            //    no reason. The gate stayed silent, BuildStage2 produced 21
            //    variables and PreStartAssertions passed.
            // ---------------------------------------------------------------
            ImageRoute undefinedRoute = (ImageRoute)7;
            RouteSpec undefinedSpec = RouteCatalog.Describe(undefinedRoute);
            if (undefinedSpec.Available) return 47;
            if (string.IsNullOrEmpty(undefinedSpec.UnavailableReason)) return 47;
            if (undefinedSpec.Stages.Count != 0) return 47;
            RunRequest undefinedRequest = NewLeftRequest(undefinedRoute);
            foreach (ChannelDef channel in left.AnalysisChannels)
                undefinedRequest.Thresholds[channel.Token] = "500";
            if (!FailClosedGate.Evaluate(undefinedRequest, left, thresholdMarkers, null).Blocked)
                return 47;
            bool undefinedRefused = false;
            try
            {
                RunEnvironment.BuildStage2(
                    undefinedRequest, left, thresholdMarkers, "r", "o", Path.GetTempPath(),
                    null, false);
            }
            catch (InvalidOperationException) { undefinedRefused = true; }
            if (!undefinedRefused) return 47;
            undefinedRefused = false;
            try { RunEnvironment.BuildStage1(undefinedRequest, "LEFT"); }
            catch (InvalidOperationException) { undefinedRefused = true; }
            if (!undefinedRefused) return 47;
            undefinedRefused = false;
            try
            {
                PreStartAssertions.AssertStage2Environment(env, null, undefinedRoute);
            }
            catch (InvalidOperationException) { undefinedRefused = true; }
            if (!undefinedRefused) return 47;

            // ---------------------------------------------------------------
            // 48 THE LAUNCH CHOKE POINT.
            //
            //    Rounds 1 and 2 were the same defect twice: a path into the
            //    child environment that did not pass validation. Both validated
            //    INPUTS, and inputs have callers, and callers are places to
            //    forget. This validates the OUTPUT -- the final merged
            //    dictionary -- and it is not skippable, because
            //    EnvironmentApply.Apply takes a RunSeal and RunSeal has no
            //    public constructor.
            // ---------------------------------------------------------------
            if (typeof(RunSeal).GetConstructors(
                    BindingFlags.Public | BindingFlags.Instance).Length != 0) return 48;

            RunRequest sealedRequest = NewLeftRequest(ImageRoute.IfConfocal);
            foreach (ChannelDef channel in left.AnalysisChannels)
                sealedRequest.Thresholds[channel.Token] = "500";
            sealedRequest.Tier = RunTier.Confirmatory;
            GateResult sealedGate =
                FailClosedGate.Evaluate(sealedRequest, left, thresholdMarkers, null);
            if (sealedGate.Blocked || sealedGate.Exploratory) return 48;
            Dictionary<string, string> sealedEnv = RunEnvironment.BuildStage2(
                sealedRequest, left, thresholdMarkers, "registry", "out",
                Path.GetTempPath(), null, false);

            RunSeal issued = RunSeal.Issue(
                NewSealInput(sealedRequest, left, thresholdMarkers, sealedGate, sealedEnv));
            if (issued == null) return 48;
            if (!issued.EnvironmentSaysFrozen) return 48;
            if (issued.Classification != "THRESHOLDS_FROZEN") return 48;
            if (issued.Value("IFQ_PANEL") != "LEFT") return 48;

            // N1, reconstructed: the record the gate produced BEFORE the N1 fix
            // (3/3 frozen, KRT5=fixed_predeclared(500)) beside the environment
            // the Advanced overlay produced (IFQ_KRT5_THRESHOLD=0). The gate fix
            // is not what catches this here -- the gate is handed its original,
            // unsuspecting verdict and the seal refuses anyway.
            string krt5Variable = left.AnalysisChannels[0].ThresholdEnvName;
            Dictionary<string, string> zeroed = new Dictionary<string, string>(
                sealedEnv, StringComparer.OrdinalIgnoreCase);
            zeroed[krt5Variable] = "0";
            if (!SealRefuses(
                    NewSealInput(sealedRequest, left, thresholdMarkers, sealedGate, zeroed)))
                return 48;

            // The subtler half of N1: an override that is still a perfectly
            // legal cutoff, just not the one the record states. Classification
            // agrees on both sides; only the per-channel comparison catches it.
            Dictionary<string, string> shifted = new Dictionary<string, string>(
                sealedEnv, StringComparer.OrdinalIgnoreCase);
            shifted[krt5Variable] = "300";
            if (!SealRefuses(
                    NewSealInput(sealedRequest, left, thresholdMarkers, sealedGate, shifted)))
                return 48;

            // A cutoff for a marker that is not a channel of this panel: the
            // engine ignores it, so the record's [environment] block would show
            // a cutoff that changed nothing.
            Dictionary<string, string> foreignMarker = new Dictionary<string, string>(
                sealedEnv, StringComparer.OrdinalIgnoreCase);
            foreignMarker["IFQ_PDPN_THRESHOLD"] = "400";
            if (!SealRefuses(NewSealInput(
                    sealedRequest, left, thresholdMarkers, sealedGate, foreignMarker)))
                return 48;

            // A panel swap between the record and the environment.
            Dictionary<string, string> swapped = new Dictionary<string, string>(
                sealedEnv, StringComparer.OrdinalIgnoreCase);
            swapped["IFQ_PANEL"] = "RIGHT";
            if (!SealRefuses(
                    NewSealInput(sealedRequest, left, thresholdMarkers, sealedGate, swapped)))
                return 48;

            // A blocked gate that reached the run path anyway.
            RunRequest blockedRequest = NewLeftRequest(ImageRoute.IfConfocal);
            blockedRequest.Tier = RunTier.Confirmatory;
            GateResult blockedGate =
                FailClosedGate.Evaluate(blockedRequest, left, thresholdMarkers, null);
            if (!blockedGate.Blocked) return 48;
            if (!SealRefuses(NewSealInput(
                    blockedRequest, left, thresholdMarkers, blockedGate, sealedEnv)))
                return 48;

            // Stage 3 reads no IFQ_* at all.
            RunRequest slideRequest = NewLeftRequest(ImageRoute.IfSlideScanner);
            slideRequest.WsiInput = "in";
            slideRequest.WsiOutput = "out";
            SealInput stage3 = NewSealInput(
                slideRequest, left, thresholdMarkers, sealedGate,
                new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase));
            stage3.Stage = LaunchStage.Stage3Python;
            if (RunSeal.Issue(stage3) == null) return 48;
            stage3.Environment = new Dictionary<string, string>(
                StringComparer.OrdinalIgnoreCase);
            stage3.Environment["IFQ_PANEL"] = "LEFT";
            if (!SealRefuses(stage3)) return 48;

            // Stage 1 must tile under the panel the run is recorded as.
            SealInput stage1 = NewSealInput(
                slideRequest, left, thresholdMarkers, sealedGate,
                RunEnvironment.BuildStage1(slideRequest, "LEFT"));
            stage1.Stage = LaunchStage.Stage1QuPath;
            if (RunSeal.Issue(stage1) == null) return 48;
            Dictionary<string, string> stage1Wrong = RunEnvironment.BuildStage1(
                slideRequest, "LEFT");
            stage1Wrong["IFQ_WSI_PANEL"] = "RIGHT";
            SealInput stage1Bad = NewSealInput(
                slideRequest, left, thresholdMarkers, sealedGate, stage1Wrong);
            stage1Bad.Stage = LaunchStage.Stage1QuPath;
            if (!SealRefuses(stage1Bad)) return 48;

            // ---------------------------------------------------------------
            // 49 N1 AT THE GATE: the Advanced box may not overwrite a cutoff
            //    the threshold grid owns. Routes 1 and 2 were LESS strict than
            //    legacy mode on identical input, because the grid path rejects
            //    <=0 as H2_THRESHOLD_INVALID and the Advanced path for the SAME
            //    variable was unchecked.
            // ---------------------------------------------------------------
            RunRequest advOverride = NewLeftRequest(ImageRoute.IfConfocal);
            foreach (ChannelDef channel in left.AnalysisChannels)
                advOverride.Thresholds[channel.Token] = "500";
            advOverride.Tier = RunTier.Confirmatory;
            advOverride.AdvancedText = krt5Variable + "=0";
            GateResult advOverrideGate =
                FailClosedGate.Evaluate(advOverride, left, thresholdMarkers, null);
            if (!advOverrideGate.Blocked) return 49;
            if (!HasCode(advOverrideGate, "ADV_THRESHOLD_OVERRIDE")) return 49;
            // Also when the value is perfectly valid: one value, one control.
            advOverride.AdvancedText = krt5Variable + "=300";
            if (!FailClosedGate.Evaluate(advOverride, left, thresholdMarkers, null).Blocked)
                return 49;
            // ...and route 4 still accepts it, because v1.7.2's Advanced box was
            // the only place a cutoff could be set at all.
            RunRequest legacyOverride = NewLeftRequest(ImageRoute.LegacyFiji172);
            legacyOverride.AdvancedText = krt5Variable + "=300";
            if (FailClosedGate.Evaluate(legacyOverride, left, thresholdMarkers, null).Blocked)
                return 49;

            // ---------------------------------------------------------------
            // 50 N3 A PANEL WITH NO ANALYSIS CHANNEL IS NOT A FROZEN RUN.
            // ---------------------------------------------------------------
            CustomPanelParse nuclearOnlyParse = CustomPanelRegistry.Parse(
                "{\"panels\":{\"NUCONLY\":{\"label\":\"nuclear only\",\"channels\":[" +
                "{\"idx\":1,\"marker\":\"DAPI\",\"role\":\"nuclear\"}]}}}",
                defaultRoles, new List<string>(panels.Keys), left.ChannelsAreThresholdable);
            if (!nuclearOnlyParse.Ok) return 50;
            PanelDef nuclearOnly;
            if (!nuclearOnlyParse.Panels.TryGetValue("NUCONLY", out nuclearOnly)) return 50;
            if (nuclearOnly.AnalysisChannels.Count != 0) return 50;

            RunRequest nuclearRequest = NewLeftRequest(ImageRoute.IfConfocal);
            nuclearRequest.PanelKey = "NUCONLY";
            nuclearRequest.PanelConfigJson = @"C:\fixture\custom_panel.json";
            GateResult nuclearGate =
                FailClosedGate.Evaluate(nuclearRequest, nuclearOnly, thresholdMarkers, null);
            if (nuclearGate.Blocked) return 50;
            if (!nuclearGate.Exploratory) return 50;
            if (!nuclearGate.FolderStamps().Contains(FailClosedGate.ExploratoryStamp)) return 50;
            if (!HasCode(nuclearGate, "H2_NO_ANALYSIS_CHANNELS")) return 50;
            Dictionary<string, string> nuclearEnv = RunEnvironment.BuildStage2(
                nuclearRequest, nuclearOnly, thresholdMarkers, "registry", "out",
                Path.GetTempPath(), null, false);
            if (RunRecord.Build(nuclearRequest, nuclearGate, nuclearEnv, "1.8.0.0", "X64",
                                "f", "i", 0, "complete", "a", "b", null)
                    .IndexOf("run_classification=EXPLORATORY_DO_NOT_AGGREGATE",
                             StringComparison.Ordinal) < 0) return 50;

            nuclearRequest.Tier = RunTier.Confirmatory;
            if (!FailClosedGate.Evaluate(
                    nuclearRequest, nuclearOnly, thresholdMarkers, null).Blocked) return 50;
            nuclearRequest.Tier = RunTier.Exploratory;

            // ...and the seal catches it independently, handed the verdict the
            // UNFIXED gate produced: Exploratory=false with an empty policy.
            GateResult pretendFrozen = new GateResult();
            pretendFrozen.Exploratory = false;
            if (!SealRefuses(NewSealInput(
                    nuclearRequest, nuclearOnly, thresholdMarkers, pretendFrozen, nuclearEnv)))
                return 50;
            // The same environment with the honest verdict is allowed through.
            if (RunSeal.Issue(NewSealInput(
                    nuclearRequest, nuclearOnly, thresholdMarkers, nuclearGate,
                    nuclearEnv)) == null) return 50;

            // ---------------------------------------------------------------
            // 51 N4 AUTO IS USABLE, AND ITS REFUSALS ARE TRUE.
            //    request.PanelKey holds the RESOLVED panel by the time the run
            //    path evaluates the gate, so H1_PANEL_UNKNOWN fired on a key
            //    AUTO had just detected from the embedded pipeline.
            // ---------------------------------------------------------------
            RunRequest autoRequest = NewLeftRequest(ImageRoute.IfConfocal);
            autoRequest.PanelWasAuto = true;
            GateResult autoGate =
                FailClosedGate.Evaluate(autoRequest, null, thresholdMarkers, null);
            if (autoGate.Blocked) return 51;
            if (HasCode(autoGate, "H1_PANEL_UNKNOWN")) return 51;
            if (!autoGate.Exploratory) return 51;
            if (!HasCode(autoGate, "H2_AUTO_ADAPTIVE")) return 51;
            if (!autoGate.RequiredPhrases.Contains(FailClosedGate.ExploratoryPhrase)) return 51;

            // The same, with the key already resolved to a real panel: still
            // AUTO, still exploratory, still not "unknown panel".
            autoRequest.PanelKey = "LEFT";
            GateResult autoResolved =
                FailClosedGate.Evaluate(autoRequest, null, thresholdMarkers, null);
            if (autoResolved.Blocked) return 51;
            if (HasCode(autoResolved, "H1_PANEL_UNKNOWN")) return 51;

            // AUTO plus a custom panel JSON is refused by the GATE now, with the
            // same reason ReadAndValidateConfiguration throws -- it used to go
            // amber with Run enabled and throw only after the review dialog.
            autoRequest.PanelConfigJson = @"C:\fixture\custom_panel.json";
            GateResult autoCustom =
                FailClosedGate.Evaluate(autoRequest, null, thresholdMarkers, null);
            if (!autoCustom.Blocked) return 51;
            if (!HasCode(autoCustom, "H1_AUTO_WITH_CUSTOM_PANEL")) return 51;
            autoRequest.PanelConfigJson = null;

            // Confirmatory tier and the whole-slide route both still refuse it.
            autoRequest.Tier = RunTier.Confirmatory;
            if (!FailClosedGate.Evaluate(autoRequest, null, thresholdMarkers, null).Blocked)
                return 51;
            autoRequest.Tier = RunTier.Exploratory;
            autoRequest.Route = ImageRoute.IfSlideScanner;
            autoRequest.WsiInput = "in";
            autoRequest.WsiOutput = "out";
            if (!FailClosedGate.Evaluate(autoRequest, null, thresholdMarkers, null).Blocked)
                return 51;

            // A key that really is unknown must STILL be refused, and by name.
            RunRequest bogus = NewLeftRequest(ImageRoute.IfConfocal);
            bogus.PanelKey = "NOT_A_PANEL";
            GateResult bogusGate =
                FailClosedGate.Evaluate(bogus, null, thresholdMarkers, null);
            if (!bogusGate.Blocked || !HasCode(bogusGate, "H1_PANEL_UNKNOWN")) return 51;

            // Modern ARM64 routes must refuse the Fiji launcher executable and
            // accept the bundled-JVM path; the frozen legacy route is unchanged.
            ToolInventory armTools = new ToolInventory();
            armTools.FijiExecutable = "fiji-windows-arm64.exe";
            armTools.JavaExecutable = "java.exe";
            armTools.Ij1PatcherJar = "ij1-patcher.jar";
            armTools.WindowsArchitecture = "ARM64";
            RunRequest armRequest = NewLeftRequest(ImageRoute.IfConfocal);
            armRequest.Invocation = FijiInvocation.LauncherExe;
            GateResult armLauncherGate =
                FailClosedGate.Evaluate(armRequest, left, thresholdMarkers, armTools);
            if (!armLauncherGate.Blocked ||
                !HasCode(armLauncherGate, "FIJI_ARM64_LAUNCHER_UNRELIABLE")) return 52;
            armRequest.Invocation = FijiInvocation.BundledJvm;
            GateResult armJvmGate =
                FailClosedGate.Evaluate(armRequest, left, thresholdMarkers, armTools);
            if (armJvmGate.Blocked ||
                HasCode(armJvmGate, "FIJI_ARM64_LAUNCHER_UNRELIABLE")) return 52;

            // A whole-field ROI has no anatomical name of its own. Required
            // gating with an unassigned override used to launch and fail every
            // image; optional mode and an explicit reviewed type remain valid.
            RunRequest impossibleAnatomy = NewLeftRequest(ImageRoute.IfConfocal);
            impossibleAnatomy.TissueMode = "whole_field";
            impossibleAnatomy.CompartmentMode = "required";
            impossibleAnatomy.WholeFieldCompartment = "unassigned";
            GateResult impossibleAnatomyGate =
                FailClosedGate.Evaluate(impossibleAnatomy, left, thresholdMarkers, null);
            if (!impossibleAnatomyGate.Blocked ||
                !HasCode(impossibleAnatomyGate, "ANATOMY_REQUIRED_BUT_UNASSIGNED")) return 53;
            impossibleAnatomy.CompartmentMode = "optional";
            if (HasCode(FailClosedGate.Evaluate(
                    impossibleAnatomy, left, thresholdMarkers, null),
                    "ANATOMY_REQUIRED_BUT_UNASSIGNED")) return 53;
            impossibleAnatomy.CompartmentMode = "required";
            impossibleAnatomy.WholeFieldCompartment = "alveolar";
            if (HasCode(FailClosedGate.Evaluate(
                    impossibleAnatomy, left, thresholdMarkers, null),
                    "ANATOMY_REQUIRED_BUT_UNASSIGNED")) return 53;

            RunRequest legacyArm = NewLeftRequest(ImageRoute.LegacyFiji172);
            legacyArm.Invocation = FijiInvocation.LauncherExe;
            GateResult legacyArmGate =
                FailClosedGate.Evaluate(legacyArm, left, thresholdMarkers, armTools);
            if (HasCode(legacyArmGate, "FIJI_ARM64_LAUNCHER_UNRELIABLE")) return 52;
            return 0;
        }

        /// A stage 2 seal input with the fixture defaults the self-test uses.
        private static SealInput NewSealInput(
            RunRequest request, PanelDef panel, HashSet<string> engineThresholdMarkers,
            GateResult gate, Dictionary<string, string> env)
        {
            SealInput input = new SealInput();
            input.Stage = LaunchStage.Stage2Fiji;
            input.Request = request;
            input.Panel = panel;
            input.EngineThresholdMarkers = engineThresholdMarkers;
            input.Gate = gate;
            input.Environment = env;
            input.OutputDirectory = null;
            input.AdvancedKeys = new string[0];
            return input;
        }

        private static bool SealRefuses(SealInput input)
        {
            try { RunSeal.Issue(input); return false; }
            catch (InvalidOperationException) { return true; }
            catch (ArgumentException) { return true; }
        }

        private static bool HasCode(GateResult gate, string code)
        {
            foreach (GateFinding finding in gate.Findings)
                if (string.Equals(finding.Code, code, StringComparison.Ordinal)) return true;
            return false;
        }

        /// The fingerprint of the v1.7.2 environment fixture. Produced and
        /// re-verified by the legacy equivalence harness, which compares
        /// LegacyProfile against a verbatim transcription of v1.7.2's own
        /// assignments and against the real v1.7.2 source file.
        private const string V172FixtureFingerprint =
            "f95cecdbd22e809980979a83b41a3d610635f49a569af43cad39cad7c7e73940";

        private static RunRequest NewLeftRequest(ImageRoute route)
        {
            RunRequest request = new RunRequest();
            request.Route = route;
            request.PanelKey = "LEFT";
            request.OutputBase = "out";
            request.Tier = RunTier.Exploratory;
            return request;
        }

        private static bool TryExtractResource(string resourceName, string destination)
        {
            try
            {
                Assembly assembly = Assembly.GetExecutingAssembly();
                using (Stream input = assembly.GetManifestResourceStream(resourceName))
                {
                    if (input == null) return false;
                    using (FileStream output = new FileStream(
                               destination, FileMode.Create, FileAccess.Write, FileShare.None))
                        input.CopyTo(output);
                }
                return true;
            }
            catch { return false; }
        }

        private static string ComputeSha256(string path)
        {
            using (SHA256 algorithm = SHA256.Create())
            using (FileStream stream = File.OpenRead(path))
            {
                byte[] hash = algorithm.ComputeHash(stream);
                StringBuilder text = new StringBuilder(hash.Length * 2);
                foreach (byte value in hash)
                    text.Append(value.ToString("x2", CultureInfo.InvariantCulture));
                return text.ToString();
            }
        }

        private static void ExtractResource(string resourceName, string destination)
        {
            Assembly assembly = Assembly.GetExecutingAssembly();
            using (Stream input = assembly.GetManifestResourceStream(resourceName))
            {
                if (input == null)
                    throw new InvalidOperationException("Embedded runtime resource is missing: " + resourceName);
                using (FileStream output = new FileStream(destination, FileMode.Create, FileAccess.Write, FileShare.None))
                    input.CopyTo(output);
            }
        }

        public static int SelfTest()
        {
            try
            {
                RuntimePaths paths = EnsureExtracted();
                if (!File.Exists(paths.ScriptPath) || new FileInfo(paths.ScriptPath).Length < 1000)
                    return 11;
                string pipelineText = File.ReadAllText(paths.ScriptPath, Encoding.UTF8);
                if (pipelineText.IndexOf("[IFQ_PROGRESS]", StringComparison.Ordinal) < 0)
                    return 16;
                if (pipelineText.IndexOf("non_analytical_map_acquisition", StringComparison.Ordinal) < 0 ||
                    pipelineText.IndexOf("run_summary.xlsx", StringComparison.Ordinal) < 0)
                    return 17;
                if (pipelineText.IndexOf("LEFT_KRT5_AGER_T1A", StringComparison.Ordinal) < 0 ||
                    pipelineText.IndexOf("RIGHT_ProSPC_AGER_KRT8", StringComparison.Ordinal) < 0)
                    return 18;
                if (pipelineText.IndexOf("layer_aware_2_5d", StringComparison.Ordinal) < 0 ||
                    pipelineText.IndexOf("__z_plane_profile.csv", StringComparison.Ordinal) < 0)
                    return 19;
                if (pipelineText.IndexOf("ALI1_SCGB3A2_tdTOM_p63", StringComparison.Ordinal) < 0 ||
                    pipelineText.IndexOf("ALI2_KRT5_tdTOM_AcTub", StringComparison.Ordinal) < 0 ||
                    pipelineText.IndexOf("ALI3_KRT5_tdTOM_MUC5AC", StringComparison.Ordinal) < 0 ||
                    pipelineText.IndexOf("ALI1_MAP_SCGB3A2_tdTOM", StringComparison.Ordinal) < 0 ||
                    pipelineText.IndexOf("ALI23_MAP_KRT5_tdTOM", StringComparison.Ordinal) < 0)
                    return 20;
                if (pipelineText.IndexOf("DISPLAY ONLY - NOT QUANTIFIED", StringComparison.Ordinal) < 0 ||
                    pipelineText.IndexOf("__VISUAL_MERGE_PANEL__merged_enhanced.png", StringComparison.Ordinal) < 0 ||
                    pipelineText.IndexOf("visualization_only_not_quantification", StringComparison.Ordinal) < 0)
                    return 21;
                if (!string.Equals(
                        MainForm.InferBuiltInPanelFromText(
                            @"C:\images\Basal ALI krt5_488 tdTOM acetyl_647\field.oir"),
                        "ALI2",
                        StringComparison.OrdinalIgnoreCase) ||
                    !string.Equals(
                        MainForm.InferBuiltInPanelFromText(
                            @"C:\images\DAPI Pro-SPC_488 AGER_555 KRT8_647\field.czi"),
                        "RIGHT",
                        StringComparison.OrdinalIgnoreCase) ||
                    !string.Equals(
                        MainForm.InferBuiltInPanelFromText(
                            @"C:\images\IFNg ko(het) 260325 M4-1 PR8 infection proSPC_488 mRAGE_555 krt8_647 20x 2k_A01_G001_0001.oir"),
                        "RIGHT",
                        StringComparison.OrdinalIgnoreCase) ||
                    !string.Equals(
                        MainForm.InferBuiltInPanelFromText(
                            @"C:\images\IFNg ko(het) 260325 M4-2 PR8 no infection krt5_488 mRAGE_555 T1a_647 20x 2k_A01_G006_0001.oir"),
                        "LEFT",
                        StringComparison.OrdinalIgnoreCase) ||
                    !string.Equals(
                        MainForm.InferBuiltInPanelFromText(
                            @"C:\images\krt5-creERT2 tdTOM CC10_488 acetyl_647\field.oir"),
                        "E",
                        StringComparison.OrdinalIgnoreCase) ||
                    !string.Equals(
                        MainForm.InferBuiltInPanelFromText(
                            @"C:\images\krt5_488 tdTOM acetyl_647 4x mapping\field.oir"),
                        "ALI23_MAP",
                        StringComparison.OrdinalIgnoreCase) ||
                    MainForm.InferBuiltInPanelFromText(@"C:\images\unnamed\field.oir") != null)
                    return 22;
                if (pipelineText.IndexOf("IFQ_DISPLAY_PREVIEW_ONLY", StringComparison.Ordinal) < 0 ||
                    pipelineText.IndexOf("[IFQ_PREVIEW]", StringComparison.Ordinal) < 0 ||
                    pipelineText.IndexOf("VISUAL MERGE PANELS COMPLETE", StringComparison.Ordinal) < 0)
                    return 23;
                if (pipelineText.IndexOf("IFQ_PANEL_MAP_PATH", StringComparison.Ordinal) < 0 ||
                    pipelineText.IndexOf("auto_panel_assignments.csv", StringComparison.Ordinal) < 0 ||
                    pipelineText.IndexOf("per_image_panel_routing", StringComparison.Ordinal) < 0)
                    return 24;
                if (!string.Equals(
                        MainForm.DisplayChannelExportSetting(false), "true",
                        StringComparison.OrdinalIgnoreCase) ||
                    !string.Equals(
                        MainForm.DisplayChannelExportSetting(true), "true",
                        StringComparison.OrdinalIgnoreCase))
                    return 26;
                if (pipelineText.IndexOf(
                        "if (MAX_IMAGES > 0) files = files.take(MAX_IMAGES)",
                        StringComparison.Ordinal) < 0 ||
                    pipelineText.IndexOf(
                        "if (DISPLAY_PREVIEW_ONLY) files = files.take(5)",
                        StringComparison.Ordinal) >= 0)
                    return 27;
                if (pipelineText.IndexOf(
                        "high_intensity_local_density_bounded_apical_tuft",
                        StringComparison.Ordinal) < 0 ||
                    pipelineText.IndexOf(
                        "ACTUB_CILIA_SEED_PERCENTILE",
                        StringComparison.Ordinal) < 0 ||
                    pipelineText.IndexOf(
                        "thresholdSensitivity:0.60d",
                        StringComparison.Ordinal) < 0 ||
                    pipelineText.IndexOf(
                        "primaryEndpoint:true",
                        StringComparison.Ordinal) < 0)
                    return 28;
                if (!string.Equals(
                        MainForm.ClassifyWindowsArchitecture("ARM64", "AMD64", "x86"),
                        "ARM64", StringComparison.Ordinal) ||
                    !string.Equals(
                        MainForm.ClassifyWindowsArchitecture(null, "ARM64", "AMD64"),
                        "ARM64", StringComparison.Ordinal) ||
                    !string.Equals(
                        MainForm.ClassifyWindowsArchitecture(null, null, "AMD64"),
                        "X64", StringComparison.Ordinal))
                    return 29;
                string mixedRoot = Path.Combine(
                    Path.GetTempPath(),
                    "IFQuantLauncher-mixed-panel-" + Guid.NewGuid().ToString("N"));
                bool mixedDetectionOk = false;
                try
                {
                    string left = Path.Combine(
                        mixedRoot, "DAPI KRT5_488 mRAGE_555 T1alpha_647");
                    string right = Path.Combine(
                        mixedRoot, "DAPI Pro-SPC_488 mRAGE_555 KRT8_647");
                    Directory.CreateDirectory(left);
                    Directory.CreateDirectory(right);
                    File.WriteAllBytes(Path.Combine(left, "left_field.oir"), new byte[0]);
                    File.WriteAllBytes(Path.Combine(right, "right_field.oir"), new byte[0]);
                    PanelDetectionResult mixed =
                        MainForm.DetectBuiltInPanels(mixedRoot, ".*", true);
                    string generatedMap =
                        MainForm.WriteAutoPanelMap(mixed, mixedRoot);
                    mixedDetectionOk =
                        mixed.AnalyticalImageCount == 2 &&
                        mixed.PanelCounts.Count == 2 &&
                        mixed.PanelCounts.ContainsKey("LEFT") &&
                        mixed.PanelCounts.ContainsKey("RIGHT") &&
                        mixed.PanelByRelativePath.Count == 2 &&
                        File.Exists(generatedMap) &&
                        File.ReadAllLines(generatedMap, Encoding.UTF8).Length == 3;
                }
                finally
                {
                    if (Directory.Exists(mixedRoot))
                        Directory.Delete(mixedRoot, true);
                }
                if (!mixedDetectionOk)
                    return 25;

                // ---- v1.8.0 route-model invariants --------------------------
                int routeCode = RouteSelfTest(pipelineText, paths);
                if (routeCode != 0)
                    return routeCode;
                if (!File.Exists(paths.RegistryPath) || new FileInfo(paths.RegistryPath).Length < 100)
                    return 12;
                JavaScriptSerializer json = new JavaScriptSerializer();
                Dictionary<string, object> registry =
                    json.Deserialize<Dictionary<string, object>>(File.ReadAllText(paths.RegistryPath, Encoding.UTF8));
                if (registry == null || !registry.ContainsKey("markers"))
                    return 13;
                string knownFiji = @"X:\Fiji";
                if (Directory.Exists(knownFiji))
                {
                    string resolved = MainForm.ResolveFijiExecutable(knownFiji);
                    if (resolved == null || !File.Exists(resolved))
                        return 14;
                    string architecture = MainForm.GetWindowsArchitecture();
                    string expectedName = architecture == "ARM64"
                        ? "fiji-windows-arm64.exe"
                        : "fiji-windows-x64.exe";
                    string expected = Path.Combine(knownFiji, expectedName);
                    if (File.Exists(expected) &&
                        !string.Equals(Path.GetFullPath(expected), Path.GetFullPath(resolved), StringComparison.OrdinalIgnoreCase))
                        return 15;
                }
                return 0;
            }
            catch (Exception ex)
            {
                try
                {
                    File.WriteAllText(
                        Path.Combine(Path.GetTempPath(), "IFQuantLauncher-self-test-error.txt"),
                        ex.ToString(),
                        Encoding.UTF8);
                }
                catch
                {
                    // The numeric exit code remains authoritative when a
                    // restricted system cannot write the diagnostic file.
                }
                return 10;
            }
        }
    }
}
