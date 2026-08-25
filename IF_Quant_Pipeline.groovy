/*
 * ============================================================================
 *  IF_Quant_Pipeline.groovy
 *  Morphology-first lung immunofluorescence quantification. The built-in
 *  panels preserve the IFN-gamma KO / PR8 influenza-injury KRT5-pod workflow;
 *  optional JSON panel maps support other lung research contexts.
 * ----------------------------------------------------------------------------
 *  Built-in antibody set: KRT5, Pro-SPC, AGER, PDPN, CD4, CD8, Sox2
 *  (+ p63, YAP, Aqp5, CC10, tdTomato, AcTub, T1A, mRAGE).
 *
 *  Built-in panels. PANEL keys are single tokens so they survive filename
 *  parsing; select per image via samplesheet. New panels are loaded through
 *  IFQ_PANEL_CONFIG and may map any available acquisition channels:
 *  LEFT : DAPI | KRT5-488 | AGER-555 | T1alpha-647  [priority project panel]
 *  RIGHT: DAPI | Pro-SPC-488 | AGER-555 | KRT8-647 [priority project panel]
 *     A : DAPI | KRT5 | AGER      -> pod + AT1 boundary   (KRT5+/AGER-)   [Scheme1 x3]
 *     B : DAPI | KRT5 | Pro-SPC   -> regeneration readout (AT2)          [Scheme1 x2]
 *     C : DAPI | KRT5 | CD8       -> cytotoxic T infiltrate              [Scheme1 x1]
 *     D : DAPI | KRT5 | CD4       -> helper T infiltrate
 *     P : DAPI | KRT5 | PDPN      -> AT1 alt (T1-alpha)    (KRT5+/PDPN-)
 *     S : DAPI | KRT5 | Sox2      -> airway/epithelial (optional)
 *     S2: DAPI | KRT5 | p63 | YAP -> FUTURE Scheme 2 (mechanistic; see notes)
 *
 *  Feature coverage:
 *   [x] Bio-Formats import (metadata + calibration preserved)
 *   [x] Channel split, Z handling (projection configurable), calibration kept
 *   [x] Tissue / lesion ROIs (manual RoiSet.zip if present, else auto-tissue)
 *   [x] Consistent nucleus segmentation (StarDist, classic watershed fallback)
 *   [x] Perinuclear "cell" ring for cytoplasmic/membrane marker readout
 *   [x] KRT5+ pod AREA (independent threshold) + pod count/size distribution
 *   [x] KRT5+ cell counts, AGER/Pro-SPC/CD8 populations
 *   [x] Double +/- classification (e.g. KRT5+/AGER-)
 *   [x] Exports: per-cell CSV, per-image summary, masks, QC overlays
 *   [x] Full provenance: versions, every threshold/filter/parameter -> JSON
 * ----------------------------------------------------------------------------
 *  REQUIREMENTS
 *   - Fiji (Bio-Formats is bundled).
 *   - Optional but recommended update sites: CSBDeep + StarDist
 *     (Help > Update > Manage update sites). If absent, set
 *     SEGMENTER = "classic" below and the pipeline still runs.
 *   - Run from Fiji: File > New > Script..., language = Groovy, Run.
 *
 *  HOW TO USE
 *   1. Set INPUT_DIR / OUTPUT_DIR and PANEL below (or use a samplesheet).
 *   2. Set the channel ORDER for your acquisition in PANELS (1-based).
 *   3. (Optional) Put a RoiSet.zip / <image>.roi next to each image to use
 *      manually drawn lesion ROIs. Name ROIs "CTRL"/"KO" to split a slide
 *      that carries control and KO tissue side by side.
 *   4. Run once, open a QC overlay, then TUNE thresholds (see CAVEATS).
 * ----------------------------------------------------------------------------
 *  CAVEATS (read before trusting any number)
 *   * n IS COUNTED BY MICE, NOT SECTIONS. mouse_id/section_id travel through
 *     every export so you can aggregate to biological n downstream. Three
 *     technical sections from one animal are still n = 1.
 *   * DEFAULT THRESHOLDS AND MORPHOLOGY GATES ARE PILOT PLACEHOLDERS. Auto-Otsu
 *     adapts per image and therefore produces exploratory calls only. Derive
 *     fixed cutoffs from controls, validate the spatial gates, freeze all
 *     parameters once, then batch the study cohort.
 *   * AGER is a thin AT1 membrane signal; per-cell mean is weak. KRT5+/AGER-
 *     is most robust as an AREA relationship (pod area vs AT1 area). The
 *     per-object AGER call is provided but interpret it conservatively.
 *   * PROJECTION: default is MAX intensity (fine for pod AREA, standard).
 *     For Scheme 2 YAP the nuclear:cytoplasmic ratio is CORRUPTED by MIP —
 *     use a SINGLE representative plane (projection="single") for YAP.
 *   * Global IFN-g LIGAND KO changes injury severity differently from the
 *     epithelial RECEPTOR KO. This script does not correct for that; keep a
 *     viral-clearance control (NP stain / qPCR) outside the image analysis.
 * ============================================================================
 */

import ij.IJ
import ij.ImagePlus
import ij.Prefs
import ij.gui.Roi
import ij.gui.Overlay
import ij.gui.PolygonRoi
import ij.gui.ShapeRoi
import ij.measure.Calibration
import ij.measure.Measurements
import ij.measure.ResultsTable
import ij.process.ImageProcessor
import ij.process.ImageStatistics
import ij.process.AutoThresholder
import ij.process.ByteProcessor
import ij.process.ColorProcessor
import ij.process.ShortProcessor
import ij.plugin.ZProjector
import ij.plugin.ChannelSplitter
import ij.plugin.Duplicator
import ij.plugin.RoiEnlarger
import ij.plugin.filter.BackgroundSubtracter
import ij.plugin.filter.GaussianBlur
import ij.plugin.filter.ParticleAnalyzer
import ij.plugin.filter.RankFilters
import ij.plugin.filter.ThresholdToSelection
import ij.io.RoiDecoder
import loci.plugins.BF
import loci.plugins.in.ImporterOptions
import loci.formats.FormatTools
import groovy.json.JsonOutput
import groovy.json.JsonSlurper
import java.awt.Color
import java.awt.Font
import java.awt.Rectangle
import java.util.zip.ZipEntry
import java.util.zip.ZipInputStream
import java.util.zip.ZipOutputStream

// ============================================================================
//  1. USER CONFIG
// ============================================================================

// Environment variables let unattended/test runs override these defaults
// without rewriting the analysis script (see README). Forward slashes are
// accepted by Java on Windows and avoid escaping drive paths.
def envOr = { String name, String fallback ->
  def value = System.getenv(name)
  return (value == null || value.trim().isEmpty()) ? fallback : value.trim()
}
def failRun = { String message, Throwable cause = null ->
  IJ.log("FATAL: " + message)
  System.err.println("FATAL: " + message)
  if (java.awt.GraphicsEnvironment.isHeadless()) System.exit(1)
  if (cause != null) throw new IllegalStateException(message, cause)
  throw new IllegalArgumentException(message)
}
def envBool = { String name, boolean fallback ->
  String raw = envOr(name, fallback.toString()).toLowerCase()
  if (!(raw in ["true", "false"])) {
    failRun(name + " must be true or false; found '" + raw + "'")
  }
  return raw == "true"
}
def parseIntSetting = { String name, String raw ->
  try {
    return Integer.parseInt(raw)
  } catch (NumberFormatException t) {
    failRun(name + " must be an integer; found '" + raw + "'", t)
  }
}
def parseDoubleSetting = { String name, String raw ->
  try {
    return Double.parseDouble(raw)
  } catch (NumberFormatException t) {
    failRun(name + " must be numeric; found '" + raw + "'", t)
  }
}
def envInt = { String name, int fallback ->
  parseIntSetting(name, envOr(name, fallback.toString()))
}
def envDouble = { String name, double fallback ->
  parseDoubleSetting(name, envOr(name, fallback.toString()))
}
def INPUT_DIR   = envOr("IFQ_INPUT_DIR", new File("ref_images").getAbsolutePath())
def OUTPUT_DIR  = envOr("IFQ_OUTPUT_DIR", new File("analysis_output").getAbsolutePath())
def PANEL       = envOr("IFQ_PANEL", "T")
// The registry is descriptive evidence and supplies safe role defaults. A
// separate panel JSON maps the actual acquisition channels for a study. This
// keeps biological identity, analytical geometry, and image layout independent.
def MARKER_REGISTRY_PATH = envOr("IFQ_MARKER_REGISTRY", new File("config/lung_marker_registry.json").getAbsolutePath())
def PANEL_CONFIG_PATH = envOr("IFQ_PANEL_CONFIG", "")
// Optional launcher-generated two-column CSV: relative_path,panel. This allows
// one batch to route each image to a different validated panel/channel map.
// It changes panel allocation only; biological metadata still comes from the
// study samplesheet or filename parser.
def PANEL_MAP_PATH = envOr("IFQ_PANEL_MAP_PATH", "").trim()
def CANONICAL_MANIFEST_PATH = envOr("IFQ_CANONICAL_MANIFEST_PATH", "").trim()
def FILE_GLOB   = ~/(?i).*\.(czi|lif|nd2|oir|oib|oif|ics|tif|tiff)$/
// Microscope navigation maps such as Map_A01.oir are acquisition metadata/
// overview files, not analytical fields. They are retained in the manifest as
// deliberate skips and never allowed to turn an otherwise complete batch into
// a failed run.
def NON_ANALYTICAL_MAP_FILE = ~/(?i)^Map_A\d+\.(oir|oib|oif)$/
def RECURSIVE   = envBool("IFQ_RECURSIVE", false)
def INCLUDE_REGEX = envOr("IFQ_INCLUDE_REGEX", ".*")
def MAX_IMAGES  = envInt("IFQ_MAX_IMAGES", 0) // 0 = all
def ALLOW_NONEMPTY_OUTPUT = envBool("IFQ_ALLOW_NONEMPTY_OUTPUT", false)
def TISSUE_MODE = envOr("IFQ_TISSUE_MODE", "auto").toLowerCase() // auto | whole_field
def COMPARTMENT_MODE = envOr("IFQ_COMPARTMENT_MODE", "optional").toLowerCase() // optional | required
// Explicit override for a visually reviewed, anatomically homogeneous field.
// Never use this to force a mixed airway/alveolar image into one compartment.
def WHOLE_FIELD_COMPARTMENT = envOr("IFQ_WHOLE_FIELD_COMPARTMENT", "unassigned").toLowerCase()
def ALLOWED_COMPARTMENTS = ["unassigned", "airway", "alveolar", "tumor", "fibrotic",
                            "stromal", "vascular", "immune", "ambiguous"] as Set
if (!ALLOWED_COMPARTMENTS.contains(WHOLE_FIELD_COMPARTMENT)) {
  failRun("IFQ_WHOLE_FIELD_COMPARTMENT must be one of " + ALLOWED_COMPARTMENTS)
}
def FIXED_POS_THRESHOLDS = [:]
// Any marker can use a control-derived fixed cutoff via IFQ_<MARKER>_THRESHOLD.
// Adaptive Otsu remains available for pilots but is explicitly reported as
// exploratory. Examples: IFQ_CC10_THRESHOLD, IFQ_TDTOM_THRESHOLD.
def thresholdMarkers = ["KRT5", "AGER", "PDPN", "ProSPC", "CD8", "CD4",
                        "Sox2", "Aqp5", "p63", "YAP", "CC10", "tdTOM",
                        "AcTub", "T1A", "mRAGE", "KRT8", "ITGA2",
                        "PDGFRB", "SOX9", "KRAS", "RED2_KRAS_G12D_RFP", "MKI67"]
thresholdMarkers.each { marker ->
  String token = marker.toUpperCase().replaceAll(/[^A-Z0-9]+/, "")
  def rawValue = System.getenv("IFQ_" + token + "_THRESHOLD")
  if (rawValue != null && !rawValue.trim().isEmpty()) {
    FIXED_POS_THRESHOLDS[marker] = parseDoubleSetting("IFQ_" + token + "_THRESHOLD", rawValue.trim())
  }
}
def MIN_RING_POS_FRACTION = [
  "T1A": envDouble("IFQ_T1A_MIN_RING_FRACTION", 0.30d),
  "mRAGE": envDouble("IFQ_MRAGE_MIN_RING_FRACTION", 0.30d)
]
// Acetylated alpha-tubulin is concentrated in apical motile cilia rather than
// the perinuclear cytoplasm. At 20x, individual axonemes are not reliably
// resolvable, so associate thresholded ciliary signal with a nucleus using a
// wider proximity support zone and report ciliary patches as the primary
// regional readout. These pilot defaults must still be frozen against controls.
def ACTUB_SUPPORT_EXPAND_UM = envDouble("IFQ_ACTUB_SUPPORT_EXPAND_UM", 6.0d)
// The support fraction is evaluated on the cilia-specific tuft mask below,
// not on all AcTub-positive pixels. A compact tuft can occupy only a small
// fraction of the 6-um nucleus association zone.
def ACTUB_MIN_SUPPORT_FRACTION = envDouble("IFQ_ACTUB_MIN_SUPPORT_FRACTION", 0.02d)
def ACTUB_MIN_PATCH_AREA_UM2 = envDouble("IFQ_ACTUB_MIN_PATCH_AREA_UM2", 2.0d)
// Acetylated tubulin also labels stable cytoplasmic microtubules. Ciliary
// candidates must therefore form a locally dense, high-intensity apical tuft;
// diffuse/interconnected cell-body filaments are excluded from both the
// ciliary decision mask and the visualization-only white layer.
def ACTUB_CILIA_SEED_PERCENTILE =
  envDouble("IFQ_ACTUB_CILIA_SEED_PERCENTILE", 99.0d)
def ACTUB_CILIA_DENSITY_RADIUS_UM =
  envDouble("IFQ_ACTUB_CILIA_DENSITY_RADIUS_UM", 1.5d)
def ACTUB_CILIA_MIN_LOCAL_DENSITY =
  envDouble("IFQ_ACTUB_CILIA_MIN_LOCAL_DENSITY", 0.10d)
def ACTUB_MAX_PATCH_AREA_UM2 =
  envDouble("IFQ_ACTUB_MAX_PATCH_AREA_UM2", 150.0d)
// A regional ciliary component is assigned to exactly one nearest nucleus.
// This preserves cell context in dense airway epithelium without rejecting
// every 6-um support zone merely because another nucleus overlaps it.
def ACTUB_MAX_COMPONENT_DISTANCE_UM = envDouble("IFQ_ACTUB_MAX_COMPONENT_DISTANCE_UM", 12.0d)
// Require the component centroid to sit outside an equivalent-radius nuclear
// boundary. This rejects intranuclear/central puncta while retaining a
// conservative apical shell for one-plane 20x sections.
def ACTUB_MIN_COMPONENT_BOUNDARY_DISTANCE_UM =
  envDouble("IFQ_ACTUB_MIN_COMPONENT_BOUNDARY_DISTANCE_UM", 1.0d)

// Morphology is the authoritative marker call. Intensity thresholds define
// candidate pixels; a final positive additionally requires role-appropriate
// coverage, connected spatial support, compartment, and object ownership.
// Defaults are conservative PILOT values and must be calibrated from blinded
// positive/negative controls before a final cohort run.
def MORPHOLOGY_PRIMARY = envBool("IFQ_MORPHOLOGY_PRIMARY", true)
// These are geometry-class pilot defaults, not biological truth. Marker-level
// validated values below take precedence, followed by explicit channel-level
// overrides from IFQ_PANEL_CONFIG. Unknown markers therefore never need to be
// added to source code merely to participate in the same measurement model.
def ROLE_MORPHOLOGY_DEFAULTS = [
  "cyto"        : [minFraction:0.20d, minLargestShare:0.40d, requireOwnership:true],
  "membrane"    : [minFraction:0.25d, minLargestShare:0.40d, requireOwnership:true],
  "nuc_marker"  : [minFraction:0.40d, minLargestShare:0.60d, requireOwnership:false, minNuclearEnrichment:1.25d],
  "nuc_ratio"   : [minFraction:0.30d, minLargestShare:0.60d, requireOwnership:false, minNucCytoRatio:1.50d],
  "apical_cilia": [minFraction:ACTUB_MIN_SUPPORT_FRACTION, minLargestShare:0.30d, requireOwnership:true]
]
def MORPHOLOGY_RULES = [
  "KRT5" : [minFraction:0.20d, minLargestShare:0.50d, requireOwnership:true],
  "KRT8" : [minFraction:0.20d, minLargestShare:0.40d, requireOwnership:true],
  "ITGA2":[minFraction:0.25d, minLargestShare:0.40d, requireOwnership:true],
  "PDGFRB":[minFraction:0.25d, minLargestShare:0.40d, requireOwnership:true],
  "KRAS" : [minFraction:0.20d, minLargestShare:0.40d, requireOwnership:true],
  "RED2_KRAS_G12D_RFP":[minFraction:0.20d, minLargestShare:0.40d, requireOwnership:true],
  "AGER" : [minFraction:0.25d, minLargestShare:0.40d, requireOwnership:true],
  "PDPN" : [minFraction:0.25d, minLargestShare:0.40d, requireOwnership:true],
  "ProSPC":[minFraction:0.15d, minLargestShare:0.40d, requireOwnership:true],
  "CD8"  : [minFraction:0.20d, minLargestShare:0.40d, requireOwnership:true],
  "CD4"  : [minFraction:0.20d, minLargestShare:0.40d, requireOwnership:true],
  "Sox2" : [minFraction:0.40d, minLargestShare:0.60d, requireOwnership:false, minNuclearEnrichment:1.25d],
  "SOX9" : [minFraction:0.40d, minLargestShare:0.60d, requireOwnership:false, minNuclearEnrichment:1.25d],
  "MKI67": [minFraction:0.10d, minLargestShare:0.30d, requireOwnership:false, minNuclearEnrichment:1.25d],
  "Aqp5" : [minFraction:0.20d, minLargestShare:0.40d, requireOwnership:true],
  "p63"  : [minFraction:0.40d, minLargestShare:0.60d, requireOwnership:false, minNuclearEnrichment:1.25d],
  "YAP"  : [minFraction:0.30d, minLargestShare:0.60d, requireOwnership:false, minNucCytoRatio:1.50d],
  "CC10" : [minFraction:0.20d, minLargestShare:0.40d, requireOwnership:true],
  "tdTOM":[minFraction:0.20d, minLargestShare:0.40d, requireOwnership:true],
  "AcTub":[minFraction:ACTUB_MIN_SUPPORT_FRACTION, minLargestShare:0.30d, requireOwnership:true],
  "T1A"  : [minFraction:MIN_RING_POS_FRACTION["T1A"], minLargestShare:0.40d, requireOwnership:true],
  "mRAGE":[minFraction:MIN_RING_POS_FRACTION["mRAGE"], minLargestShare:0.40d, requireOwnership:true]
]
MORPHOLOGY_RULES.each { marker, rule ->
  String token = marker.toUpperCase().replaceAll(/[^A-Z0-9]+/, "")
  rule.minFraction = envDouble("IFQ_" + token + "_MIN_POSITIVE_FRACTION", rule.minFraction as double)
  rule.minLargestShare = envDouble("IFQ_" + token + "_MIN_LARGEST_COMPONENT_SHARE", rule.minLargestShare as double)
  if (rule.containsKey("minNuclearEnrichment")) {
    rule.minNuclearEnrichment = envDouble("IFQ_" + token + "_MIN_NUCLEAR_ENRICHMENT", rule.minNuclearEnrichment as double)
  }
  if (rule.containsKey("minNucCytoRatio")) {
    rule.minNucCytoRatio = envDouble("IFQ_" + token + "_MIN_NUC_CYTO_RATIO", rule.minNucCytoRatio as double)
  }
}
def DAPI_METHOD = envOr("IFQ_DAPI_METHOD", "local_phansalkar").toLowerCase() // local_phansalkar | global_otsu
def DAPI_METHOD_EXPLICIT = System.getenv("IFQ_DAPI_METHOD") != null &&
                           !System.getenv("IFQ_DAPI_METHOD").trim().isEmpty()
def DAPI_BACKGROUND_RADIUS_UM = envDouble("IFQ_DAPI_BACKGROUND_RADIUS_UM", 15.0d)
def DAPI_LOCAL_RADIUS_UM = envDouble("IFQ_DAPI_LOCAL_RADIUS_UM", 4.0d)
def DAPI_BLUR_SIGMA_PX = envDouble("IFQ_DAPI_BLUR_SIGMA_PX", 1.0d)
def DAPI_CONTRAST_SATURATION = envDouble("IFQ_DAPI_CONTRAST_SATURATION", 0.35d)

// If present, a samplesheet.csv in INPUT_DIR overrides per-file metadata.
// Columns: filename,mouse_id,section_id,genotype,condition,panel
def USE_SAMPLESHEET = true

// --- Segmentation ---
// PILOT: forced to "classic". StarDist/CSBDeep are not installed in this Fiji,
// and TensorFlow has no windows-arm64 native build, so "stardist" cannot run on
// this machine. Set back to "stardist" on an x86_64 Fiji with the update sites on.
def SEGMENTER   = envOr("IFQ_SEGMENTER", "classic").toLowerCase() // "stardist" | "classic"
def STARDIST_PROB = 0.50
def STARDIST_NMS  = 0.40
def STARDIST_TILES = 1          // raise (e.g. 4/9) for large images / low RAM

// --- Z handling ---
// "layer_aware" is an additive 2.5D workflow. It preserves the legacy global
// projection modes while allowing each marker to use a nuclear, cell-body,
// apical, full-stack, or single-plane Z policy. Automatic slab discovery is
// exploratory and every resolved range is written to the per-image provenance.
def PROJECTION  = envOr("IFQ_PROJECTION", "max").toLowerCase() // max | sum | avg | single | layer_aware
def SINGLE_PLANE = envInt("IFQ_SINGLE_PLANE", -1) // single only; -1 = middle
def Z_NUCLEAR_RANGE = envOr("IFQ_Z_NUCLEAR_RANGE", "full").toLowerCase()
def Z_CELL_BODY_RANGE = envOr("IFQ_Z_CELL_BODY_RANGE", "auto").toLowerCase()
def Z_APICAL_RANGE = envOr("IFQ_Z_APICAL_RANGE", "auto").toLowerCase()
def Z_CELL_BODY_PLANES = envInt("IFQ_Z_CELL_BODY_PLANES", 5)
def Z_APICAL_PLANES = envInt("IFQ_Z_APICAL_PLANES", 3)
// The dense, bright full-stack DAPI projections typical of layer-aware ALI
// analysis were empirically rejected by the local Phansalkar path in validation
// (zero included nuclei). Use global Otsu only as the layer-aware default while
// preserving the historical local default for every legacy projection mode.
// An explicit IFQ_DAPI_METHOD always wins.
def EFFECTIVE_DAPI_METHOD = (!DAPI_METHOD_EXPLICIT && PROJECTION == "layer_aware") ?
                            "global_otsu" : DAPI_METHOD
def MIN_INCLUDED_NUCLEI = envInt("IFQ_MIN_INCLUDED_NUCLEI", 1)

// --- Visualization-only channel enhancement ---
// These values are applied only to exported display PNGs and QC composites.
// Quantification always uses markerImg at its original calibrated intensity.
// Visual-merge-only mode stops immediately after these PNGs. Its image scope
// follows IFQ_MAX_IMAGES (0 = all matching analytical images). It writes no
// cells, masks, summaries, workbook, params, Z-profile CSV, or manifest.
def DISPLAY_PREVIEW_ONLY = envBool("IFQ_DISPLAY_PREVIEW_ONLY", false)
def EXPORT_DISPLAY_CHANNELS = DISPLAY_PREVIEW_ONLY ? true :
                              envBool("IFQ_EXPORT_DISPLAY_CHANNELS", false)
def DISPLAY_LOW_PERCENTILE = envDouble("IFQ_DISPLAY_LOW_PERCENTILE", 1.0d)
def DISPLAY_HIGH_PERCENTILE = envDouble("IFQ_DISPLAY_HIGH_PERCENTILE", 99.8d)
def DISPLAY_GAMMA = envDouble("IFQ_DISPLAY_GAMMA", 1.0d)
// Burn a calibrated scale bar into the primary visual-merge PNG. A 6 px bar
// at the native 2048 px acquisition size remains visible in a slide without
// the heavy appearance of a PowerPoint overlay. Set the length to 0 only when
// an external figure tool will add its own calibrated bar.
def DISPLAY_SCALE_BAR_UM = envDouble("IFQ_DISPLAY_SCALE_BAR_UM", 100.0d)
def DISPLAY_SCALE_BAR_THICKNESS_PX = envInt("IFQ_DISPLAY_SCALE_BAR_THICKNESS_PX", 6)

// --- Geometry (calibrated, micrometres) ---
def RING_EXPAND_UM      = envDouble("IFQ_RING_EXPAND_UM", 2.0d)
def MIN_NUCLEUS_AREA_UM2 = envDouble("IFQ_MIN_NUCLEUS_AREA_UM2", 8.0d)
def POD_MIN_AREA_UM2    = 50.0  // a "pod" particle must exceed this
def POD_BLUR_SIGMA_PX   = 2.0
def POD_THRESH_METHOD   = "Otsu" // Otsu|Triangle|Li|Huang|MaxEntropy...

// --- Candidate-pixel intensity cutoffs ---
// Otsu(inTissue) * sensitivity supplies a pilot threshold for spatial-support
// measurements. The morphology gates above, not the object mean, authorize the
// final call. Fixed control-derived thresholds should replace Otsu for final use.
def POS_SENSITIVITY = [ "KRT5":1.00, "AGER":1.00, "PDPN":1.00, "ProSPC":1.00,
                        "CD8":1.00, "CD4":1.00, "Sox2":1.00, "Aqp5":1.00,
                        "p63":1.00, "YAP":1.00, "CC10":1.00, "tdTOM":1.00,
                        "AcTub":1.00, "T1A":1.00, "mRAGE":1.00,
                        "KRT8":1.00, "ITGA2":1.00, "PDGFRB":1.00, "SOX9":1.00,
                        "KRAS":1.00, "RED2_KRAS_G12D_RFP":1.00, "MKI67":1.00 ]

// --- Tissue auto-detection (used only if no manual ROI is supplied) ---
def TISSUE_BLUR_SIGMA_PX = 4.0
def TISSUE_THRESH_METHOD  = "Triangle" // permissive, keeps sparse tissue
def TISSUE_MIN_AREA_UM2   = 2000.0     // drop debris specks

def requireFiniteNonnegative = { String name, double value, boolean allowZero = true ->
  if (!Double.isFinite(value) || value < 0.0d || (!allowZero && value == 0.0d)) {
    failRun(name + " must be a finite " +
      (allowZero ? "non-negative" : "positive") + " number; found " + value)
  }
}
if (!(SEGMENTER in ["classic", "stardist"])) {
  failRun("IFQ_SEGMENTER must be classic or stardist; found '" + SEGMENTER + "'")
}
if (!(PROJECTION in ["max", "sum", "avg", "single", "layer_aware"])) {
  failRun("IFQ_PROJECTION must be max, sum, avg, single, or layer_aware; found '" + PROJECTION + "'")
}
if (!(EFFECTIVE_DAPI_METHOD in ["local_phansalkar", "global_otsu"])) {
  failRun("IFQ_DAPI_METHOD must be local_phansalkar or global_otsu; found '" + EFFECTIVE_DAPI_METHOD + "'")
}
if (!(TISSUE_MODE in ["auto", "whole_field"])) {
  failRun("IFQ_TISSUE_MODE must be auto or whole_field; found '" + TISSUE_MODE + "'")
}
if (!(COMPARTMENT_MODE in ["optional", "required"])) {
  failRun("IFQ_COMPARTMENT_MODE must be optional or required; found '" + COMPARTMENT_MODE + "'")
}
if (SINGLE_PLANE == 0 || SINGLE_PLANE < -1) {
  failRun("IFQ_SINGLE_PLANE must be -1 (middle) or a positive 1-based Z index")
}
if (Z_CELL_BODY_PLANES < 1 || Z_APICAL_PLANES < 1) {
  failRun("IFQ_Z_CELL_BODY_PLANES and IFQ_Z_APICAL_PLANES must be positive integers")
}
if (MIN_INCLUDED_NUCLEI < 0) {
  failRun("IFQ_MIN_INCLUDED_NUCLEI must be zero or a positive integer")
}
if (!Double.isFinite(DISPLAY_LOW_PERCENTILE) ||
    !Double.isFinite(DISPLAY_HIGH_PERCENTILE) ||
    DISPLAY_LOW_PERCENTILE < 0.0d || DISPLAY_HIGH_PERCENTILE > 100.0d ||
    DISPLAY_LOW_PERCENTILE >= DISPLAY_HIGH_PERCENTILE) {
  failRun("IFQ display percentiles must satisfy 0 <= low < high <= 100")
}
requireFiniteNonnegative("IFQ_DISPLAY_GAMMA", DISPLAY_GAMMA, false)
if (MAX_IMAGES < 0) {
  failRun("IFQ_MAX_IMAGES must be 0 (all) or a positive integer")
}
if (!MORPHOLOGY_PRIMARY) {
  failRun("IFQ_MORPHOLOGY_PRIMARY=false is unsupported: intensity defines candidate pixels, but morphology must authorize the final call")
}
requireFiniteNonnegative("IFQ_RING_EXPAND_UM", RING_EXPAND_UM, false)
requireFiniteNonnegative("IFQ_MIN_NUCLEUS_AREA_UM2", MIN_NUCLEUS_AREA_UM2, false)
requireFiniteNonnegative("IFQ_ACTUB_SUPPORT_EXPAND_UM", ACTUB_SUPPORT_EXPAND_UM, false)
requireFiniteNonnegative("IFQ_ACTUB_MIN_PATCH_AREA_UM2", ACTUB_MIN_PATCH_AREA_UM2, false)
requireFiniteNonnegative("IFQ_ACTUB_CILIA_DENSITY_RADIUS_UM",
                         ACTUB_CILIA_DENSITY_RADIUS_UM, false)
requireFiniteNonnegative("IFQ_ACTUB_MAX_PATCH_AREA_UM2",
                         ACTUB_MAX_PATCH_AREA_UM2, false)
requireFiniteNonnegative("IFQ_ACTUB_MAX_COMPONENT_DISTANCE_UM", ACTUB_MAX_COMPONENT_DISTANCE_UM, false)
requireFiniteNonnegative("IFQ_ACTUB_MIN_COMPONENT_BOUNDARY_DISTANCE_UM",
                         ACTUB_MIN_COMPONENT_BOUNDARY_DISTANCE_UM, true)
requireFiniteNonnegative("IFQ_DAPI_BACKGROUND_RADIUS_UM", DAPI_BACKGROUND_RADIUS_UM, false)
requireFiniteNonnegative("IFQ_DAPI_LOCAL_RADIUS_UM", DAPI_LOCAL_RADIUS_UM, false)
requireFiniteNonnegative("IFQ_DAPI_BLUR_SIGMA_PX", DAPI_BLUR_SIGMA_PX, true)
if (!Double.isFinite(ACTUB_CILIA_SEED_PERCENTILE) ||
    ACTUB_CILIA_SEED_PERCENTILE <= 0.0d ||
    ACTUB_CILIA_SEED_PERCENTILE >= 100.0d) {
  failRun("IFQ_ACTUB_CILIA_SEED_PERCENTILE must satisfy 0 < value < 100")
}
if (!Double.isFinite(ACTUB_CILIA_MIN_LOCAL_DENSITY) ||
    ACTUB_CILIA_MIN_LOCAL_DENSITY <= 0.0d ||
    ACTUB_CILIA_MIN_LOCAL_DENSITY > 1.0d) {
  failRun("IFQ_ACTUB_CILIA_MIN_LOCAL_DENSITY must satisfy 0 < value <= 1")
}
if (ACTUB_MAX_PATCH_AREA_UM2 < ACTUB_MIN_PATCH_AREA_UM2) {
  failRun("IFQ_ACTUB_MAX_PATCH_AREA_UM2 must be >= IFQ_ACTUB_MIN_PATCH_AREA_UM2")
}
if (!Double.isFinite(DAPI_CONTRAST_SATURATION) ||
    DAPI_CONTRAST_SATURATION < 0.0d || DAPI_CONTRAST_SATURATION > 100.0d) {
  failRun("IFQ_DAPI_CONTRAST_SATURATION must be between 0 and 100; found " + DAPI_CONTRAST_SATURATION)
}

// ============================================================================
//  2. PANEL DEFINITIONS  (channel idx is 1-based ACQUISITION order)
//     role: "nuclear"   -> segmentation channel (DAPI)
//           "cyto"      -> measured in perinuclear ring (KRT5, Pro-SPC, CC10)
//           "membrane"  -> measured in ring (AGER/PDPN/CD4/CD8)
//           "nuc_marker"-> measured in the nucleus (p63)
//           "nuc_ratio" -> nucleus vs ring separately (YAP)  [needs single plane]
//     areaMarker: also run independent threshold AREA quantification (KRT5 pod)
// ============================================================================

def PANELS = [
  // Priority real-project panels. These are additional presets inside the
  // universal engine; all registry markers, custom panels, and legacy panels
  // remain available. Channel idx values are acquisition order.
  "LEFT": [ label:"LEFT_KRT5_AGER_T1A",
    channels:[ [idx:1, marker:"DAPI", role:"nuclear", qcColor:"blue", fileLabel:"DAPI",
                 displayWeight:1.0d],
               [idx:2, marker:"KRT5", role:"cyto", measurement:"perinuclear_cytoplasmic_keratin",
                 qcColor:"green", fileLabel:"KRT5-488", areaMarker:true,
                 displayWeight:0.55d],
               [idx:3, marker:"AGER", role:"membrane", measurement:"thin_membrane_support",
                 expectedCompartment:"alveolar", qcColor:"red", fileLabel:"AGER-555",
                 areaMarker:true, areaMode:"membrane", areaMinAreaUm2:2.0d, areaBlurSigmaPx:0.7d,
                 displayWeight:0.45d],
               [idx:4, marker:"T1A", role:"membrane", measurement:"thin_membrane_support",
                 expectedCompartment:"alveolar", qcColor:"white", fileLabel:"T1alpha-647",
                 areaMarker:true, areaMode:"membrane", areaMinAreaUm2:2.0d, areaBlurSigmaPx:0.7d,
                 displayWeight:0.50d] ],
    classify:[ ["AGER":true,"T1A":true],
               ["KRT5":true,"AGER":false],
               ["KRT5":true,"T1A":false],
               ["KRT5":true,"AGER":false,"T1A":false] ] ],

  "RIGHT": [ label:"RIGHT_ProSPC_AGER_KRT8",
    channels:[ [idx:1, marker:"DAPI", role:"nuclear", qcColor:"blue", fileLabel:"DAPI",
                 displayWeight:1.0d],
               [idx:2, marker:"ProSPC", role:"cyto", measurement:"perinuclear_granular_cytoplasm",
                 expectedCompartment:"alveolar", qcColor:"green", fileLabel:"Pro-SPC-488",
                 displayWeight:0.55d],
               [idx:3, marker:"AGER", role:"membrane", measurement:"thin_membrane_support",
                 expectedCompartment:"alveolar", qcColor:"red", fileLabel:"AGER-555",
                 areaMarker:true, areaMode:"membrane", areaMinAreaUm2:2.0d, areaBlurSigmaPx:0.7d,
                 displayWeight:0.45d],
               [idx:4, marker:"KRT8", role:"cyto", measurement:"perinuclear_cytoplasmic_keratin",
                 expectedCompartment:"alveolar", qcColor:"white", fileLabel:"KRT8-647",
                 displayWeight:0.50d] ],
    classify:[ ["KRT8":true,"ProSPC":true],
               ["KRT8":true,"AGER":true],
               ["KRT8":true,"ProSPC":false,"AGER":false] ] ],

  // 260730-CW ALI Z-stack panels. These retain the universal decision engine
  // while declaring marker-specific depth policies for layer-aware mode.
  "ALI1": [ label:"ALI1_SCGB3A2_tdTOM_p63",
    channels:[ [idx:1, marker:"DAPI", role:"nuclear", zPolicy:"full_stack", qcColor:"blue"],
               [idx:2, marker:"SCGB3A2", role:"cyto", measurement:"perinuclear_secretory_cytoplasm",
                zPolicy:"cell_body_slab", expectedCompartment:"airway", qcColor:"green", fileLabel:"SCGB3A2-488"],
               [idx:3, marker:"tdTOM", role:"cyto", measurement:"perinuclear_lineage_reporter",
                zPolicy:"cell_body_slab", qcColor:"red", thresholdSensitivity:0.60d,
                displayLowPercentile:0.2d, displayHighPercentile:99.8d, displayGamma:0.85d,
                areaMarker:true, areaMode:"reporter", areaMinAreaUm2:8.0d],
               [idx:4, marker:"p63", role:"nuc_marker", measurement:"nuclear_transcription_factor",
                zPolicy:"nuclear_stack", expectedCompartment:"airway", qcColor:"white",
                fileLabel:"p63-647", primaryEndpoint:true] ],
    classify:[ ["tdTOM":true], ["SCGB3A2":true,"tdTOM":true],
               ["p63":true,"tdTOM":true], ["SCGB3A2":true,"p63":true] ] ],

  "ALI2": [ label:"ALI2_KRT5_tdTOM_AcTub",
    channels:[ [idx:1, marker:"DAPI", role:"nuclear", zPolicy:"full_stack", qcColor:"blue"],
               [idx:2, marker:"KRT5", role:"cyto", measurement:"perinuclear_cytoplasmic_keratin",
                zPolicy:"cell_body_slab", qcColor:"green", fileLabel:"KRT5-488", areaMarker:true],
               [idx:3, marker:"tdTOM", role:"cyto", measurement:"perinuclear_lineage_reporter",
                zPolicy:"cell_body_slab", qcColor:"red", thresholdSensitivity:0.60d,
                displayLowPercentile:0.2d, displayHighPercentile:99.8d, displayGamma:0.85d,
                areaMarker:true, areaMode:"reporter", areaMinAreaUm2:8.0d],
               [idx:4, marker:"AcTub", role:"apical_cilia", measurement:"apical_cilia_proximity",
                zPolicy:"apical_slab", expectedCompartment:"airway", qcColor:"white", fileLabel:"AcTub-647",
                primaryEndpoint:true, areaMarker:true, areaMode:"ciliary",
                areaMinAreaUm2:ACTUB_MIN_PATCH_AREA_UM2] ],
    classify:[ ["tdTOM":true], ["KRT5":true,"tdTOM":true],
               ["AcTub":true,"tdTOM":true] ] ],

  "ALI3": [ label:"ALI3_KRT5_tdTOM_MUC5AC",
    channels:[ [idx:1, marker:"DAPI", role:"nuclear", zPolicy:"full_stack", qcColor:"blue"],
               [idx:2, marker:"KRT5", role:"cyto", measurement:"perinuclear_cytoplasmic_keratin",
                zPolicy:"cell_body_slab", qcColor:"green", fileLabel:"KRT5-488", areaMarker:true],
               [idx:3, marker:"tdTOM", role:"cyto", measurement:"perinuclear_lineage_reporter",
                zPolicy:"cell_body_slab", qcColor:"red", thresholdSensitivity:0.60d,
                displayLowPercentile:0.2d, displayHighPercentile:99.8d, displayGamma:0.85d,
                areaMarker:true, areaMode:"reporter", areaMinAreaUm2:8.0d],
               [idx:4, marker:"MUC5AC", role:"regional_area", measurement:"secreted_mucin_positive_area",
                zPolicy:"apical_slab", expectedCompartment:"airway", qcColor:"white", fileLabel:"MUC5AC-647",
                primaryEndpoint:true, cellCall:false, areaMarker:true, areaMode:"generic",
                areaMinAreaUm2:8.0d, areaBlurSigmaPx:0.7d] ],
    classify:[ ["tdTOM":true], ["KRT5":true,"tdTOM":true] ] ],

  // Olympus 4x navigation/mapping fields contain only the first three
  // acquisition channels even when the folder name retains the omitted 647
  // marker. These explicit subsets prevent a missing C4 from being treated as
  // an image failure and prevent the absent marker from receiving a false
  // negative call.
  "ALI1_MAP": [ label:"ALI1_MAP_SCGB3A2_tdTOM",
    channels:[ [idx:1, marker:"DAPI", role:"nuclear", zPolicy:"full_stack", qcColor:"blue"],
               [idx:2, marker:"SCGB3A2", role:"cyto", measurement:"perinuclear_secretory_cytoplasm",
                zPolicy:"cell_body_slab", expectedCompartment:"airway", qcColor:"green", fileLabel:"SCGB3A2-488"],
               [idx:3, marker:"tdTOM", role:"cyto", measurement:"perinuclear_lineage_reporter",
                zPolicy:"cell_body_slab", qcColor:"red", thresholdSensitivity:0.60d,
                displayLowPercentile:0.2d, displayHighPercentile:99.8d, displayGamma:0.85d,
                areaMarker:true, areaMode:"reporter", areaMinAreaUm2:8.0d] ],
    classify:[ ["tdTOM":true], ["SCGB3A2":true,"tdTOM":true] ] ],

  "ALI23_MAP": [ label:"ALI23_MAP_KRT5_tdTOM",
    channels:[ [idx:1, marker:"DAPI", role:"nuclear", zPolicy:"full_stack", qcColor:"blue"],
               [idx:2, marker:"KRT5", role:"cyto", measurement:"perinuclear_cytoplasmic_keratin",
                zPolicy:"cell_body_slab", qcColor:"green", fileLabel:"KRT5-488", areaMarker:true],
               [idx:3, marker:"tdTOM", role:"cyto", measurement:"perinuclear_lineage_reporter",
                zPolicy:"cell_body_slab", qcColor:"red", thresholdSensitivity:0.60d,
                displayLowPercentile:0.2d, displayHighPercentile:99.8d, displayGamma:0.85d,
                areaMarker:true, areaMode:"reporter", areaMinAreaUm2:8.0d] ],
    classify:[ ["tdTOM":true], ["KRT5":true,"tdTOM":true] ] ],

  "A": [ label:"A_KRT5_AGER",
    channels:[ [idx:1, marker:"DAPI",  role:"nuclear"],
               [idx:2, marker:"KRT5",  role:"cyto",     areaMarker:true],
               [idx:3, marker:"AGER",  role:"membrane", expectedCompartment:"alveolar",
                areaMarker:true, areaMode:"membrane", areaMinAreaUm2:2.0d, areaBlurSigmaPx:0.7d] ],
    classify:[ ["KRT5":true,"AGER":false], ["KRT5":true,"AGER":true] ] ],

  "B": [ label:"B_KRT5_ProSPC",
    channels:[ [idx:1, marker:"DAPI",   role:"nuclear"],
               [idx:2, marker:"KRT5",   role:"cyto",    areaMarker:true],
               [idx:3, marker:"ProSPC", role:"cyto", expectedCompartment:"alveolar"] ],
    classify:[ ["KRT5":true,"ProSPC":false], ["KRT5":false,"ProSPC":true] ] ],

  "C": [ label:"C_KRT5_CD8",
    channels:[ [idx:1, marker:"DAPI", role:"nuclear"],
               [idx:2, marker:"KRT5", role:"cyto",     areaMarker:true],
               [idx:3, marker:"CD8",  role:"membrane"] ],
    classify:[ ["CD8":true], ["KRT5":true,"CD8":true] ] ],

  "D": [ label:"D_KRT5_CD4",
    channels:[ [idx:1, marker:"DAPI", role:"nuclear"],
               [idx:2, marker:"KRT5", role:"cyto",     areaMarker:true],
               [idx:3, marker:"CD4",  role:"membrane"] ],
    classify:[ ["CD4":true], ["KRT5":true,"CD4":true] ] ],

  // AT1 alternative via podoplanin (T1-alpha). Enables the KRT5+/PDPN- readout.
  "P": [ label:"P_KRT5_PDPN",
    channels:[ [idx:1, marker:"DAPI", role:"nuclear"],
               [idx:2, marker:"KRT5", role:"cyto",     areaMarker:true],
               [idx:3, marker:"PDPN", role:"membrane", expectedCompartment:"alveolar",
                areaMarker:true, areaMode:"membrane", areaMinAreaUm2:2.0d, areaBlurSigmaPx:0.7d] ],
    classify:[ ["KRT5":true,"PDPN":false], ["KRT5":true,"PDPN":true] ] ],

  // Optional airway/epithelial marker.
  "S": [ label:"S_KRT5_Sox2",
    channels:[ [idx:1, marker:"DAPI", role:"nuclear"],
               [idx:2, marker:"KRT5", role:"cyto",       areaMarker:true],
               [idx:3, marker:"Sox2", role:"nuc_marker"] ],
    classify:[ ["Sox2":true], ["KRT5":true,"Sox2":true], ["KRT5":true,"Sox2":false] ] ],

  // 260719-CW Olympus OIR panels. Bio-Formats metadata confirms channel order
  // by emission bands: 470 (DAPI), 540 (488), 620 (tdTOM), 750 nm (647).
  // The 4x stitched mapping file contains only the first three channels.
  "M": [ label:"M_4x_CC10_tdTOM_mapping",
    channels:[ [idx:1, marker:"DAPI",  role:"nuclear", qcColor:"blue"],
               [idx:2, marker:"CC10",  role:"cyto",    measurement:"perinuclear_secretory_cytoplasm", qcColor:"green", fileLabel:"CC10-488"],
               [idx:3, marker:"tdTOM", role:"cyto",    measurement:"perinuclear_lineage_reporter", qcColor:"red",
                areaMarker:true, areaMode:"reporter", areaMinAreaUm2:8.0d] ],
    classify:[ ["tdTOM":true], ["CC10":true,"tdTOM":true] ] ],

  "E": [ label:"E_CC10_tdTOM_AcTub",
    channels:[ [idx:1, marker:"DAPI",  role:"nuclear", qcColor:"blue"],
               [idx:2, marker:"CC10",  role:"cyto",    measurement:"perinuclear_secretory_cytoplasm", qcColor:"green", fileLabel:"CC10-488"],
               [idx:3, marker:"tdTOM", role:"cyto",    measurement:"perinuclear_lineage_reporter", qcColor:"red",
                areaMarker:true, areaMode:"reporter", areaMinAreaUm2:8.0d],
               [idx:4, marker:"AcTub", role:"apical_cilia", measurement:"apical_cilia_proximity", expectedCompartment:"airway", qcColor:"white", fileLabel:"AcTub-647",
                areaMarker:true, areaMode:"ciliary", areaMinAreaUm2:ACTUB_MIN_PATCH_AREA_UM2, areaBlurSigmaPx:0.7d] ],
    classify:[ ["tdTOM":true], ["CC10":true,"tdTOM":true],
               ["AcTub":true,"tdTOM":true] ] ],

  "R": [ label:"R_T1A_tdTOM_mRAGE",
    channels:[ [idx:1, marker:"DAPI",  role:"nuclear",  qcColor:"blue"],
               [idx:2, marker:"T1A",   role:"membrane", expectedCompartment:"alveolar", qcColor:"green", fileLabel:"T1alpha-488",
                areaMarker:true, areaMode:"membrane", areaMinAreaUm2:2.0d, areaBlurSigmaPx:0.7d],
               [idx:3, marker:"tdTOM", role:"cyto", measurement:"perinuclear_lineage_reporter", qcColor:"red",
                areaMarker:true, areaMode:"reporter", areaMinAreaUm2:8.0d],
               [idx:4, marker:"mRAGE", role:"membrane", expectedCompartment:"alveolar", qcColor:"white", fileLabel:"mRAGE-647",
                areaMarker:true, areaMode:"membrane", areaMinAreaUm2:2.0d, areaBlurSigmaPx:0.7d] ],
    classify:[ ["tdTOM":true], ["T1A":true,"tdTOM":true],
               ["mRAGE":true,"tdTOM":true] ] ],

  // ---------------------------------------------------------------------
  // PILOT / PLUMBING TEST ONLY -- NOT a study panel. Delete when done.
  // Maps the OME sample file ND2/karl/sample_image.nd2 (Nikon CSU, 20x,
  // 5 channels: 1=far-red 2=red 3=green 4=blue/DAPI 5=brightfield) onto the
  // panel-A *shape* (nuclear + cyto/areaMarker + membrane).
  // The DAPI-equivalent counterstain is channel 4, NOT channel 1 -- this is
  // the only panel here whose nuclear idx is not 1.
  // The marker names below are structural placeholders: the green/red
  // channels are smFISH RNA probes from a skin sample, so KRT5/AGER
  // positivity numbers this produces are MEANINGLESS. See ref_images/README.md.
  "T": [ label:"T_PILOT_ome_nd2",
    channels:[ [idx:4, marker:"DAPI", role:"nuclear"],
               [idx:3, marker:"KRT5", role:"cyto",     areaMarker:true],
               [idx:2, marker:"AGER", role:"membrane"] ],
    classify:[ ["KRT5":true,"AGER":false], ["KRT5":true,"AGER":true] ] ],

  // FUTURE: 4 channels exceeds the 3-marker slide limit; use single plane for YAP
  // (set PROJECTION="single") so the nuclear:cytoplasmic ratio is not MIP-corrupted.
  "S2": [ label:"S2_KRT5_p63_YAP",
    channels:[ [idx:1, marker:"DAPI", role:"nuclear"],
               [idx:2, marker:"KRT5", role:"cyto",       areaMarker:true],
               [idx:3, marker:"p63",  role:"nuc_marker"],
               [idx:4, marker:"YAP",  role:"nuc_ratio"] ],
    classify:[ ["KRT5":true,"p63":true], ["KRT5":true,"YAP":true] ] ]
]

// ---------------------------------------------------------------------------
// Optional universal marker registry + study-specific panel configuration
// ---------------------------------------------------------------------------
// The registry never diagnoses a disease and never assigns a cell identity by
// itself. It records aliases, localization, modalities, and research contexts,
// and can supply a default analytical role when a custom panel omits one.
def normalizeMarkerToken = { value ->
  value == null ? "" : value.toString().toUpperCase().replaceAll(/[^A-Z0-9]+/, "")
}
def markerRegistryFile = new File(MARKER_REGISTRY_PATH)
def MARKER_REGISTRY = [schema_version:"unavailable", markers:[:], research_profiles:[:]]
if (markerRegistryFile.isFile()) {
  try {
    def parsed = new JsonSlurper().parse(markerRegistryFile)
    if (!(parsed instanceof Map) || !(parsed.markers instanceof Map)) {
      throw new IllegalArgumentException("registry root must contain a 'markers' object")
    }
    MARKER_REGISTRY = parsed
  } catch (Throwable t) {
    failRun("Cannot parse IFQ_MARKER_REGISTRY '" + markerRegistryFile + "': " + t.message, t)
  }
} else {
  IJ.log("Marker registry not found; explicit channel roles remain fully supported: " + markerRegistryFile)
}

def markerProfileIndex = [:]
(MARKER_REGISTRY.markers ?: [:]).each { canonical, profile ->
  ([canonical] + (profile.aliases ?: [])).each { alias ->
    String token = normalizeMarkerToken(alias)
    if (!token.isEmpty()) markerProfileIndex[token] = [canonical:canonical, profile:profile]
  }
}
def markerProfileFor = { marker -> markerProfileIndex[normalizeMarkerToken(marker)] }

// A custom panel file is opt-in. It can add panels but cannot silently replace
// the validated built-ins. See config/custom_panels.example.json.
def CUSTOM_PANEL_KEYS = []
if (PANEL_CONFIG_PATH != null && !PANEL_CONFIG_PATH.trim().isEmpty()) {
  def panelConfigFile = new File(PANEL_CONFIG_PATH)
  if (!panelConfigFile.isFile()) {
    failRun("IFQ_PANEL_CONFIG is not a file: " + panelConfigFile)
  }
  def customDoc
  try {
    customDoc = new JsonSlurper().parse(panelConfigFile)
  } catch (Throwable t) {
    failRun("Cannot parse IFQ_PANEL_CONFIG '" + panelConfigFile + "': " + t.message, t)
  }
  if (!(customDoc instanceof Map) || !(customDoc.panels instanceof Map) || customDoc.panels.isEmpty()) {
    failRun("IFQ_PANEL_CONFIG must contain a non-empty 'panels' object")
  }
  customDoc.panels.each { rawKey, rawPanel ->
    String panelKey = rawKey.toString()
    if (!(panelKey ==~ /[A-Za-z0-9][A-Za-z0-9_.-]*/)) {
      failRun("Invalid custom panel key: " + panelKey)
    }
    if (PANELS.containsKey(panelKey)) {
      failRun("Custom panel key would replace a built-in panel: " + panelKey)
    }
    if (!(rawPanel instanceof Map)) {
      failRun("Custom panel '" + panelKey + "' must be an object")
    }
    if (!(rawPanel.channels instanceof Collection) || rawPanel.channels.isEmpty()) {
      failRun("Custom panel '" + panelKey + "' needs a non-empty channels array")
    }
    def channels = rawPanel.channels.collect { rawChannel ->
      if (!(rawChannel instanceof Map)) {
        failRun("Every channel in panel '" + panelKey + "' must be an object")
      }
      def c = [:]
      c.putAll(rawChannel)
      def matchedProfile = markerProfileFor(c.marker)
      if ((c.role == null || c.role.toString().trim().isEmpty()) && matchedProfile != null) {
        c.role = matchedProfile.profile.default_role
      }
      if (c.measurement == null && matchedProfile?.profile?.default_measurement != null) {
        c.measurement = matchedProfile.profile.default_measurement
      }
      if (c.zPolicy == null && matchedProfile?.profile?.default_z_policy != null) {
        c.zPolicy = matchedProfile.profile.default_z_policy
      }
      if (matchedProfile != null) c.registryKey = matchedProfile.canonical
      if (c.role == "regional_area") {
        c.cellCall = false
        c.areaMarker = true
        if (c.areaMode == null) c.areaMode = "generic"
      }
      return c
    }
    PANELS[panelKey] = [label:(rawPanel.label ?: panelKey).toString(),
                        channels:channels, classify:(rawPanel.classify ?: [])]
    CUSTOM_PANEL_KEYS << panelKey
  }
}

def ALLOWED_CHANNEL_ROLES = ["nuclear", "cyto", "membrane", "nuc_marker",
                             "nuc_ratio", "apical_cilia", "regional_area"] as Set
def ALLOWED_Z_POLICIES = ["full_stack", "nuclear_stack", "cell_body_slab",
                          "apical_slab", "single_plane", "explicit_range"] as Set
def ROLE_Z_POLICY_DEFAULTS = [
  "nuclear"     : "full_stack",
  "nuc_marker"  : "nuclear_stack",
  "nuc_ratio"   : "single_plane",
  "cyto"        : "cell_body_slab",
  "membrane"    : "cell_body_slab",
  "apical_cilia": "apical_slab",
  "regional_area": "full_stack"
]
PANELS.each { panelKey, panelDef ->
  if (!(panelDef.channels instanceof Collection) || panelDef.channels.isEmpty()) {
    failRun("Panel '" + panelKey + "' has no channels")
  }
  panelDef.channels.each { c ->
    if (c.idx == null || (c.idx as int) < 1) {
      failRun("Panel '" + panelKey + "' has an invalid channel idx")
    }
    if (c.marker == null || c.marker.toString().trim().isEmpty()) {
      failRun("Panel '" + panelKey + "' has a channel without a marker")
    }
    if (c.role == null || !ALLOWED_CHANNEL_ROLES.contains(c.role.toString())) {
      failRun("Panel '" + panelKey + "', marker '" + c.marker +
              "' needs one of roles " + ALLOWED_CHANNEL_ROLES)
    }
    def matchedProfile = markerProfileFor(c.marker)
    if (c.measurement == null && matchedProfile?.profile?.default_measurement != null) {
      c.measurement = matchedProfile.profile.default_measurement
    }
    if (c.zPolicy == null && matchedProfile?.profile?.default_z_policy != null) {
      c.zPolicy = matchedProfile.profile.default_z_policy
    }
    if (c.zPolicy == null) c.zPolicy = ROLE_Z_POLICY_DEFAULTS[c.role]
    c.zPolicy = c.zPolicy.toString().toLowerCase()
    if (!ALLOWED_Z_POLICIES.contains(c.zPolicy)) {
      failRun("Panel '" + panelKey + "' marker '" + c.marker +
        "' has unsupported zPolicy '" + c.zPolicy + "'; use " + ALLOWED_Z_POLICIES)
    }
    if (c.zPolicy == "explicit_range" &&
        (c.zStart == null || c.zEnd == null)) {
      failRun("Panel '" + panelKey + "' marker '" + c.marker +
        "' uses zPolicy=explicit_range but does not define zStart and zEnd")
    }
    if (c.zStart != null && (c.zStart as int) < 1) {
      failRun("Panel '" + panelKey + "' marker '" + c.marker + "' has zStart < 1")
    }
    if (c.zEnd != null && (c.zEnd as int) < 1) {
      failRun("Panel '" + panelKey + "' marker '" + c.marker + "' has zEnd < 1")
    }
    if (c.role == "regional_area") {
      c.cellCall = false
      c.areaMarker = true
      if (c.areaMode == null) c.areaMode = "generic"
      if (c.areaMinAreaUm2 == null) {
        failRun("Area-only marker '" + c.marker + "' in panel '" + panelKey +
                "' needs areaMinAreaUm2; no universal biological size cutoff exists")
      }
    }
    ["minPositiveFraction", "minLargestComponentShare"].each { field ->
      if (c[field] != null && (((double)c[field]) < 0.0d || ((double)c[field]) > 1.0d)) {
        failRun(field + " must be between 0 and 1 for marker '" + c.marker + "'")
      }
    }
    double displayLow = c.displayLowPercentile != null ?
                        c.displayLowPercentile as double : DISPLAY_LOW_PERCENTILE
    double displayHigh = c.displayHighPercentile != null ?
                         c.displayHighPercentile as double : DISPLAY_HIGH_PERCENTILE
    double displayGamma = c.displayGamma != null ?
                          c.displayGamma as double : DISPLAY_GAMMA
    if (!Double.isFinite(displayLow) || !Double.isFinite(displayHigh) ||
        displayLow < 0.0d || displayHigh > 100.0d || displayLow >= displayHigh) {
      failRun("Invalid display percentiles for marker '" + c.marker + "'")
    }
    if (!Double.isFinite(displayGamma) || displayGamma <= 0.0d) {
      failRun("displayGamma must be positive for marker '" + c.marker + "'")
    }
    boolean hasDisplayLowIntensity = c.displayLowIntensity != null
    boolean hasDisplayHighIntensity = c.displayHighIntensity != null
    if (hasDisplayLowIntensity != hasDisplayHighIntensity) {
      failRun("displayLowIntensity and displayHighIntensity must be provided together for marker '" +
              c.marker + "'")
    }
    if (hasDisplayLowIntensity) {
      double displayLowIntensity = c.displayLowIntensity as double
      double displayHighIntensity = c.displayHighIntensity as double
      if (!Double.isFinite(displayLowIntensity) || !Double.isFinite(displayHighIntensity) ||
          displayLowIntensity < 0.0d || displayLowIntensity >= displayHighIntensity) {
        failRun("Invalid absolute display window for marker '" + c.marker + "'")
      }
    }
    double displayWeight = c.displayWeight != null ? c.displayWeight as double : 1.0d
    if (!Double.isFinite(displayWeight) || displayWeight <= 0.0d || displayWeight > 1.0d) {
      failRun("displayWeight must be in (0, 1] for marker '" + c.marker + "'")
    }
    double displayBackgroundRadiusPx = c.displayBackgroundRadiusPx != null ?
                                       c.displayBackgroundRadiusPx as double : 0.0d
    if (!Double.isFinite(displayBackgroundRadiusPx) || displayBackgroundRadiusPx < 0.0d) {
      failRun("displayBackgroundRadiusPx must be non-negative for marker '" + c.marker + "'")
    }
    double displayBlackPoint = c.displayBlackPoint != null ?
                               c.displayBlackPoint as double : 0.0d
    if (!Double.isFinite(displayBlackPoint) ||
        displayBlackPoint < 0.0d || displayBlackPoint >= 1.0d) {
      failRun("displayBlackPoint must be in [0, 1) for marker '" + c.marker + "'")
    }
    double displayMedianRadiusPx = c.displayMedianRadiusPx != null ?
                                   c.displayMedianRadiusPx as double : 0.0d
    if (!Double.isFinite(displayMedianRadiusPx) ||
        displayMedianRadiusPx < 0.0d || displayMedianRadiusPx > 10.0d) {
      failRun("displayMedianRadiusPx must be in [0, 10] for marker '" + c.marker + "'")
    }
    if (c.thresholdSensitivity != null &&
        (!Double.isFinite(c.thresholdSensitivity as double) ||
         (c.thresholdSensitivity as double) <= 0.0d)) {
      failRun("thresholdSensitivity must be positive for marker '" + c.marker + "'")
    }
    if (c.primaryEndpoint != null && !(c.primaryEndpoint instanceof Boolean)) {
      failRun("primaryEndpoint must be true/false for marker '" + c.marker + "'")
    }
    if (c.allowPositiveWithoutCompartment != null &&
        !(c.allowPositiveWithoutCompartment instanceof Boolean)) {
      failRun("allowPositiveWithoutCompartment must be true/false for marker '" +
              c.marker + "'")
    }
  }
  def indexes = panelDef.channels.collect { it.idx as int }
  if (indexes.unique().size() != indexes.size()) {
    failRun("Panel '" + panelKey + "' repeats a channel idx")
  }
  def markerNames = panelDef.channels.collect { it.marker.toString() }
  if (markerNames.unique().size() != markerNames.size()) {
    failRun("Panel '" + panelKey + "' repeats a marker name")
  }
  if (panelDef.channels.count { it.role == "nuclear" } != 1) {
    failRun("Panel '" + panelKey + "' must contain exactly one nuclear segmentation channel")
  }
  def callableMarkers = panelDef.channels.findAll { it.role != "nuclear" && it.cellCall != false }
                                        .collect { it.marker.toString() } as Set
  (panelDef.classify ?: []).each { classRule ->
    if (!(classRule instanceof Map) || classRule.isEmpty()) {
      failRun("Panel '" + panelKey + "' contains an empty classification rule")
    }
    classRule.each { marker, wanted ->
      if (!callableMarkers.contains(marker.toString())) {
        failRun("Panel '" + panelKey + "' classifies unavailable/area-only marker '" + marker + "'")
      }
      if (!(wanted instanceof Boolean)) {
        failRun("Classification values must be true/false in panel '" + panelKey + "'")
      }
    }
  }
}
if (!PANELS.containsKey(PANEL)) {
  failRun("IFQ_PANEL '" + PANEL + "' is unknown. Available panels: " + PANELS.keySet().sort())
}

// Extend thresholds and morphology rules to every marker that appears in any
// panel. Explicit marker rules remain intact; new markers inherit only their
// geometry-class defaults until a study validates channel-specific settings.
def allAnalysisChannels = PANELS.values().collectMany { it.channels }
                               .findAll { it.role != "nuclear" }
allAnalysisChannels.each { c ->
  String marker = c.marker.toString()
  String token = normalizeMarkerToken(marker)
  def rawThreshold = System.getenv("IFQ_" + token + "_THRESHOLD")
  if (rawThreshold != null && !rawThreshold.trim().isEmpty()) {
    FIXED_POS_THRESHOLDS[marker] = parseDoubleSetting("IFQ_" + token + "_THRESHOLD", rawThreshold.trim())
  } else if (c.registryKey != null && FIXED_POS_THRESHOLDS.containsKey(c.registryKey.toString())) {
    // A canonical threshold (for example IFQ_MKI67_THRESHOLD) also applies
    // when the panel uses a registry alias such as Ki-67. An alias-specific
    // environment value above remains the highest-precedence override.
    FIXED_POS_THRESHOLDS[marker] = FIXED_POS_THRESHOLDS[c.registryKey.toString()]
  }
  if (!POS_SENSITIVITY.containsKey(marker)) POS_SENSITIVITY[marker] = 1.0d
  if (c.cellCall != false && !MORPHOLOGY_RULES.containsKey(marker)) {
    // Registry aliases inherit the canonical marker's validated geometry rule
    // before falling back to the broader role template. This keeps Ki-67,
    // Kras, IGTA2, PDGFR-beta, and other aliases behaviorally equivalent to
    // their canonical registry keys rather than merely descriptively matched.
    def canonicalRule = c.registryKey != null ? MORPHOLOGY_RULES[c.registryKey.toString()] : null
    def roleRule = canonicalRule ?: ROLE_MORPHOLOGY_DEFAULTS[c.role]
    if (roleRule == null) {
      failRun("No cell-call morphology defaults for role '" + c.role + "'")
    }
    MORPHOLOGY_RULES[marker] = new LinkedHashMap(roleRule)
  }
}
// Apply environment overrides after custom markers have been registered.
MORPHOLOGY_RULES.each { marker, rule ->
  String token = normalizeMarkerToken(marker)
  rule.minFraction = envDouble("IFQ_" + token + "_MIN_POSITIVE_FRACTION", rule.minFraction as double)
  rule.minLargestShare = envDouble("IFQ_" + token + "_MIN_LARGEST_COMPONENT_SHARE", rule.minLargestShare as double)
  if (rule.containsKey("minNuclearEnrichment")) {
    rule.minNuclearEnrichment = envDouble("IFQ_" + token + "_MIN_NUCLEAR_ENRICHMENT", rule.minNuclearEnrichment as double)
  }
  if (rule.containsKey("minNucCytoRatio")) {
    rule.minNucCytoRatio = envDouble("IFQ_" + token + "_MIN_NUC_CYTO_RATIO", rule.minNucCytoRatio as double)
  }
  [minFraction:rule.minFraction, minLargestShare:rule.minLargestShare].each { field, value ->
    double v = value as double
    if (!Double.isFinite(v) || v < 0.0d || v > 1.0d) {
      failRun(marker + " " + field + " must be between 0 and 1; found " + value)
    }
  }
  [minNuclearEnrichment:rule.minNuclearEnrichment, minNucCytoRatio:rule.minNucCytoRatio]
    .findAll { field, value -> value != null }.each { field, value ->
      double v = value as double
      if (!Double.isFinite(v) || v <= 0.0d) {
        failRun(marker + " " + field + " must be finite and positive; found " + value)
      }
    }
}
FIXED_POS_THRESHOLDS.each { marker, value ->
  double v = value as double
  if (!Double.isFinite(v) || v < 0.0d) {
    failRun("Fixed threshold for " + marker + " must be finite and non-negative; found " + value)
  }
}

// ============================================================================
//  3. PROVENANCE
// ============================================================================

def captureVersions() {
  def v = [:]
  v.imagej_version = IJ.getFullVersion()
  try { v.bioformats_version = FormatTools.VERSION } catch (e) { v.bioformats_version = "unknown" }
  v.java_version = System.getProperty("java.version")
  v.os = System.getProperty("os.name") + " " + System.getProperty("os.arch")
  // StarDist / CSBDeep versions are not reliably queryable; record from the
  // Updater if you need exact pins. Model + params below fully define the run.
  v.stardist_note = "record CSBDeep+StarDist versions from Help>Update if needed"
  v.timestamp = new Date().format("yyyy-MM-dd'T'HH:mm:ss")
  return v
}

// ============================================================================
//  4. HELPERS
// ============================================================================

def ensureDir(String p) {
  def d = new File(p)
  if (d.exists() && !d.isDirectory()) {
    throw new IOException("Output path exists but is not a directory: " + d)
  }
  if (!d.exists() && !d.mkdirs()) {
    throw new IOException("Cannot create output directory: " + d)
  }
  return d
}

// Headless-safe replacement for Analyze Particles + ROI Manager. When an ROI
// is present, clear pixels outside it first: ParticleAnalyzer only honors the
// ROI bounds reliably and can otherwise count particles from excluded parts of
// a non-rectangular/composite ROI.
def particlesToRois(ImagePlus imp, double minAreaCal, boolean excludeEdges,
                    double maxAreaCal = Double.POSITIVE_INFINITY) {
  ImagePlus work = imp
  Roi restriction = imp.getRoi()
  if (restriction != null) {
    // ImagePlus.duplicate() may crop to the active ROI. Duplicate the processor
    // directly so the restriction remains in the original image coordinates.
    work = new ImagePlus(imp.getTitle() + "_roi_restricted",
                         imp.getProcessor().duplicate())
    work.setCalibration(imp.getCalibration())
    ImageProcessor wp = work.getProcessor()
    Rectangle rb = restriction.getBounds()
    ImageProcessor rm = restriction.getMask()
    int rx = (int)rb.x, ry = (int)rb.y
    int rw = (int)rb.width, rh = (int)rb.height
    // Clear outside explicitly. ImageProcessor.fillOutside(ShapeRoi) changes
    // processor ROI/mask state and is unreliable with these composite ROIs.
    for (int y = 0; y < wp.getHeight(); y++) {
      for (int x = 0; x < wp.getWidth(); x++) {
        boolean inBounds = x >= rx && x < rx + rw && y >= ry && y < ry + rh
        if (!inBounds || (rm != null && rm.get(x - rx, y - ry) == 0)) wp.set(x, y, 0)
      }
    }
  }

  int opts = ParticleAnalyzer.SHOW_ROI_MASKS
  if (excludeEdges) opts |= ParticleAnalyzer.EXCLUDE_EDGE_PARTICLES
  def rt = new ResultsTable()
  // ParticleAnalyzer's Java constructor expects pixel counts (the interactive
  // dialog performs this conversion before constructing it). Convert the
  // public calibrated µm² setting explicitly.
  def workCal = work.getCalibration()
  double pixelArea = workCal.pixelWidth * workCal.pixelHeight
  double minAreaPixels = pixelArea > 0 ? minAreaCal / pixelArea : minAreaCal
  double maxAreaPixels = Double.isFinite(maxAreaCal) ?
    (pixelArea > 0 ? maxAreaCal / pixelArea : maxAreaCal) : Double.MAX_VALUE
  def pa = new ParticleAnalyzer(opts, Measurements.AREA, rt,
                                minAreaPixels, maxAreaPixels)
  pa.setHideOutputImage(true)
  ImageProcessor src = work.getProcessor()
  // Every caller supplies a 0/255 binary mask. Ignore any threshold state left
  // behind by Convert to Mask/Watershed and always select non-zero foreground.
  src.setThreshold(128, 255, ImageProcessor.NO_LUT_UPDATE)
  if (!pa.analyze(work)) {
    if (!work.is(imp)) work.close()
    return []
  }
  ImagePlus labels = pa.getOutputImage()
  if (!work.is(imp)) work.close()
  if (labels == null) return []

  // SHOW_ROI_MASKS assigns consecutive integer labels. The former conversion
  // thresholded the complete 2048x2048 label image once per object, making ROI
  // extraction O(objects * image pixels). Find all label bounds in one pass,
  // then trace only each small cropped mask. This preserves the exact particle
  // shapes and coordinates while reducing a multi-minute hotspot to seconds.
  ImageProcessor lp = labels.getProcessor()
  int nLabels = rt.size()
  int[] minX = new int[nLabels + 1]
  int[] minY = new int[nLabels + 1]
  int[] maxX = new int[nLabels + 1]
  int[] maxY = new int[nLabels + 1]
  java.util.Arrays.fill(minX, lp.getWidth())
  java.util.Arrays.fill(minY, lp.getHeight())
  java.util.Arrays.fill(maxX, -1)
  java.util.Arrays.fill(maxY, -1)
  for (int y = 0; y < lp.getHeight(); y++) {
    for (int x = 0; x < lp.getWidth(); x++) {
      int label = lp.get(x, y)
      if (label < 1 || label > nLabels) continue
      if (x < minX[label]) minX[label] = x
      if (x > maxX[label]) maxX[label] = x
      if (y < minY[label]) minY[label] = y
      if (y > maxY[label]) maxY[label] = y
    }
  }
  def out = []
  for (int i = 1; i <= nLabels; i++) {
    if (maxX[i] < minX[i] || maxY[i] < minY[i]) continue
    int w = maxX[i] - minX[i] + 1
    int h = maxY[i] - minY[i] + 1
    def bp = new ByteProcessor(w, h)
    for (int yy = 0; yy < h; yy++) {
      for (int xx = 0; xx < w; xx++) {
        if (lp.get(minX[i] + xx, minY[i] + yy) == i) bp.set(xx, yy, 255)
      }
    }
    bp.setThreshold(128, 255, ImageProcessor.NO_LUT_UPDATE)
    def particle = new ImagePlus("particle_label_" + i, bp)
    def r = ThresholdToSelection.run(particle)
    if (r != null) {
      def rb = r.getBounds()
      r.setLocation(minX[i] + rb.x, minY[i] + rb.y)
      out << r
    }
    particle.close()
  }
  labels.close()
  return out
}

// Headless-safe replacement for RoiManager.runCommand("Open", ...).
def readRoiFile(File f) {
  def rois = []
  if (f.getName().toLowerCase().endsWith(".roi")) {
    def r = new RoiDecoder(f.getAbsolutePath()).getRoi()
    if (r != null) {
      if (r.getName() == null) r.setName(f.getName() - ".roi")
      rois << r
    }
    return rois
  }
  def zis = new ZipInputStream(new BufferedInputStream(new FileInputStream(f)))
  try {
    def entry
    while ((entry = zis.getNextEntry()) != null) {
      if (!entry.getName().toLowerCase().endsWith(".roi")) continue
      def baos = new ByteArrayOutputStream()
      byte[] buf = new byte[8192]
      int len
      while ((len = zis.read(buf)) > 0) baos.write(buf, 0, len)
      def r = new RoiDecoder(baos.toByteArray(), entry.getName()).getRoi()
      if (r != null) {
        r.setName(entry.getName() - ".roi")
        rois << r
      }
    }
  } finally {
    zis.close()
  }
  return rois
}

// Import via Bio-Formats keeping metadata + calibration; grayscale, no split yet.
def bfOpen(String path) {
  def opts = new ImporterOptions()
  opts.setId(path)
  opts.setSplitChannels(false)
  opts.setColorMode(ImporterOptions.COLOR_MODE_GRAYSCALE)
  opts.setVirtual(false)
  opts.setAutoscale(false)
  def imps
  try {
    imps = BF.openImagePlus(opts)
  } catch (IllegalArgumentException t) {
    // Bio-Formats/OIRReader uses OME PositiveInteger for SizeX/SizeY/SizeZ.
    // An interrupted Olympus acquisition can retain a substantial file while
    // declaring one of those dimensions as zero. Make that source-data defect
    // actionable instead of exposing the opaque PositiveInteger exception.
    if ((t.getMessage() ?: "").contains("must be non-null and strictly positive")) {
      throw new IOException(
        "Bio-Formats cannot read '" + new File(path).name +
        "' because its microscope metadata declares a zero pixel dimension. " +
        "The acquisition is incomplete or corrupt; re-export it from Olympus software " +
        "or exclude it with a reviewed canonical field manifest.", t)
    }
    throw t
  }
  if (imps == null || imps.length == 0) {
    throw new IOException("Bio-Formats returned no image series for: " + path)
  }
  if (imps.length != 1) {
    imps.each { if (it != null) { it.changes = false; it.close() } }
    throw new IllegalArgumentException("Bio-Formats found " + imps.length +
      " series in '" + new File(path).name + "'. This pipeline requires one series per file; " +
      "split the acquisition or add an explicit series-selection policy before quantification.")
  }
  return imps[0]
}

// Project one single-channel stack to 2D per PROJECTION setting.
def projectChannel(ImagePlus ch, String projection, int singlePlane) {
  if (ch.getNSlices() <= 1) return ch
  if (projection == "single") {
    int z = (singlePlane >= 1) ? singlePlane : (int)Math.ceil(ch.getNSlices()/2.0)
    if (z < 1 || z > ch.getNSlices()) {
      throw new IllegalArgumentException("Requested IFQ_SINGLE_PLANE=" + z +
        " but channel '" + ch.getTitle() + "' has " + ch.getNSlices() + " Z planes")
    }
    ch.setSlice(z)
    def ip = ch.getProcessor().duplicate()
    def out = new ImagePlus(ch.getTitle(), ip)
    out.setCalibration(ch.getCalibration())
    return out
  }
  def method = (projection == "sum") ? "sum" : (projection == "avg") ? "avg" : "max"
  def out = ZProjector.run(ch, method)
  out.setCalibration(ch.getCalibration())
  return out
}

// Find the contiguous Z window with the greatest integrated mean signal. This
// is intentionally simple, deterministic, and auditable. It is used only when
// layer-aware mode receives an "auto" slab; fixed study ranges remain preferred
// for confirmatory analysis.
def brightestZWindow(ImagePlus reference, int requestedPlanes) {
  int n = reference.getNSlices()
  int width = Math.max(1, Math.min(requestedPlanes, n))
  def means = []
  for (int z = 1; z <= n; z++) {
    reference.setSlice(z)
    means << (reference.getProcessor().getStatistics().mean as double)
  }
  int bestStart = 1
  double bestScore = Double.NEGATIVE_INFINITY
  for (int start = 1; start <= n - width + 1; start++) {
    double score = 0.0d
    for (int offset = 0; offset < width; offset++) score += means[start - 1 + offset]
    if (score > bestScore) {
      bestScore = score
      bestStart = start
    }
  }
  return [start:bestStart, end:(bestStart + width - 1),
          source:"auto_brightest_contiguous_window", score:bestScore]
}

def resolveConfiguredZRange(String rawSetting, ImagePlus reference, int autoPlanes,
                            String settingName) {
  int n = reference.getNSlices()
  String raw = (rawSetting ?: "auto").trim().toLowerCase()
  if (n <= 1 || raw == "full") {
    return [start:1, end:n, source:(n <= 1 ? "single_slice_input" : "configured_full")]
  }
  if (raw == "auto") return brightestZWindow(reference, autoPlanes)
  def match = (raw =~ /^\s*(\d+)\s*[:\-]\s*(\d+)\s*$/)
  if (!match.matches()) {
    throw new IllegalArgumentException(settingName +
      " must be 'auto', 'full', or a 1-based inclusive range such as 3:7; found '" +
      rawSetting + "'")
  }
  int start = Integer.parseInt(match.group(1))
  int end = Integer.parseInt(match.group(2))
  if (start < 1 || end < start || end > n) {
    throw new IllegalArgumentException(settingName + "=" + rawSetting +
      " is outside the available 1:" + n + " Z planes")
  }
  return [start:start, end:end, source:"configured_range"]
}

def resolveMarkerZSelection(ImagePlus markerChannel, ImagePlus nuclearChannel,
                            Map channelDef, Map cfg) {
  int n = markerChannel.getNSlices()
  if (cfg.projection != "layer_aware" || n <= 1) {
    int single = cfg.projection == "single" ?
      ((cfg.singlePlane >= 1) ? cfg.singlePlane : (int)Math.ceil(n / 2.0d)) : -1
    return [policy:"global", start:(single >= 1 ? single : 1),
            end:(single >= 1 ? single : n), projection:cfg.projection,
            range_source:(n <= 1 ? "single_slice_input" : "global_projection")]
  }

  String policy = (channelDef.zPolicy ?: "full_stack").toString().toLowerCase()
  def range
  String projection = (channelDef.zProjection ?: "max").toString().toLowerCase()
  if (!(projection in ["max", "sum", "avg", "single"])) {
    throw new IllegalArgumentException("Marker '" + channelDef.marker +
      "' has unsupported zProjection '" + projection + "'")
  }
  switch (policy) {
    case "full_stack":
      range = [start:1, end:n, source:"policy_full_stack"]
      break
    case "nuclear_stack":
      range = resolveConfiguredZRange(cfg.zNuclearRange, nuclearChannel,
                                      cfg.zCellBodyPlanes, "IFQ_Z_NUCLEAR_RANGE")
      break
    case "cell_body_slab":
      range = resolveConfiguredZRange(cfg.zCellBodyRange, nuclearChannel,
                                      cfg.zCellBodyPlanes, "IFQ_Z_CELL_BODY_RANGE")
      break
    case "apical_slab":
      range = resolveConfiguredZRange(cfg.zApicalRange, markerChannel,
                                      cfg.zApicalPlanes, "IFQ_Z_APICAL_RANGE")
      break
    case "single_plane":
      int plane
      if (channelDef.zStart != null) {
        plane = channelDef.zStart as int
      } else if (cfg.singlePlane >= 1) {
        plane = cfg.singlePlane
      } else {
        def auto = brightestZWindow(nuclearChannel, 1)
        plane = auto.start as int
      }
      if (plane < 1 || plane > n) {
        throw new IllegalArgumentException("Marker '" + channelDef.marker +
          "' requested Z plane " + plane + " but the image has " + n + " planes")
      }
      range = [start:plane, end:plane, source:"single_plane_policy"]
      projection = "single"
      break
    case "explicit_range":
      int start = channelDef.zStart as int
      int end = channelDef.zEnd as int
      if (start < 1 || end < start || end > n) {
        throw new IllegalArgumentException("Marker '" + channelDef.marker +
          "' explicit Z range " + start + ":" + end +
          " is outside the available 1:" + n + " planes")
      }
      range = [start:start, end:end, source:"panel_explicit_range"]
      break
    default:
      throw new IllegalArgumentException("Unsupported Z policy '" + policy +
        "' for marker '" + channelDef.marker + "'")
  }
  return [policy:policy, start:range.start, end:range.end,
          projection:projection, range_source:range.source,
          auto_score:(range.score != null ? range.score : "")]
}

def projectChannelRange(ImagePlus ch, Map selection) {
  int start = selection.start as int
  int end = selection.end as int
  String projection = selection.projection.toString()
  if (start == end || projection == "single") {
    ch.setSlice(start)
    def out = new ImagePlus(ch.getTitle(), ch.getProcessor().duplicate())
    out.setCalibration(ch.getCalibration())
    return out
  }
  ImagePlus slab = null
  try {
    slab = new Duplicator().run(ch, 1, 1, start, end, 1, 1)
    String method = (projection == "sum") ? "sum" :
                    (projection == "avg") ? "avg" : "max"
    def out = ZProjector.run(slab, method)
    out.setCalibration(ch.getCalibration())
    return out
  } finally {
    if (slab != null) {
      slab.changes = false
      slab.close()
    }
  }
}

def channelZProfile(ImagePlus ch, String marker, Map selection, Calibration cal) {
  def rows = []
  double weightedSignal = 0.0d
  double weightedZUm = 0.0d
  for (int z = 1; z <= ch.getNSlices(); z++) {
    ch.setSlice(z)
    def stats = ch.getProcessor().getStatistics()
    double mean = stats.mean as double
    double zUm = (z - 1) * (Double.isFinite(cal.pixelDepth) && cal.pixelDepth > 0.0d ?
                            cal.pixelDepth : 1.0d)
    weightedSignal += Math.max(0.0d, mean)
    weightedZUm += Math.max(0.0d, mean) * zUm
    rows << [marker:marker, z_plane:z, z_um:zUm, mean_intensity:mean,
             maximum_intensity:(stats.max as double),
             selected:(z >= (selection.start as int) &&
                       z <= (selection.end as int) ? 1 : 0),
             z_policy:selection.policy, z_projection:selection.projection,
             range_source:selection.range_source]
  }
  double centroid = weightedSignal > 0.0d ? weightedZUm / weightedSignal : Double.NaN
  return [rows:rows, intensity_weighted_z_centroid_um:
          (Double.isFinite(centroid) ? centroid : "")]
}

// Iterate ROI pixels safely (respects non-rectangular mask).
def eachRoiPixel(Roi roi, Closure body) {
  Rectangle b = roi.getBounds()
  ImageProcessor mask = roi.getMask()
  int bx = (int)b.x, by = (int)b.y
  int bw = (int)b.width, bh = (int)b.height
  for (int y = 0; y < bh; y++) {
    for (int x = 0; x < bw; x++) {
      if (mask == null || mask.get(x, y) != 0) body((int)(bx + x), (int)(by + y))
    }
  }
}

// Bit-depth-robust Otsu (or named method) on raw intensities within an ROI.
def autoThresholdInRoi(ImagePlus imp, Roi roi, String method) {
  ImageProcessor ip = imp.getProcessor()
  ip.setRoi(roi)
  ImageStatistics st = ImageStatistics.getStatistics(ip, Measurements.MIN_MAX, imp.getCalibration())
  double mn = st.min, mx = st.max
  if (mx <= mn) return mx
  int[] hist = new int[256]
  eachRoiPixel(roi) { x, y ->
    double v = ip.getPixelValue(x, y)
    int bin = (int)Math.round((v - mn) / (mx - mn) * 255.0)
    if (bin < 0) bin = 0; if (bin > 255) bin = 255
    hist[bin]++
  }
  def m = AutoThresholder.Method.valueOf(method)
  int tbin = new AutoThresholder().getThreshold(m, hist)
  return mn + (tbin / 255.0) * (mx - mn)
}

// Mean raw intensity + area of one ROI on one channel.
def measureRoi(ImagePlus imp, Roi roi) {
  ImageProcessor ip = imp.getProcessor()
  ip.setRoi(roi)
  ImageStatistics st = ImageStatistics.getStatistics(
      ip, Measurements.MEAN | Measurements.AREA | Measurements.CENTROID,
      imp.getCalibration())
  return [mean: st.mean, area: st.area, cx: st.xCentroid, cy: st.yCentroid]
}

// Assign every accepted ciliary component to at most one nearby nucleus. A
// spatial grid avoids an O(components x nuclei) scan in dense 2k fields.
// Distances and centroids are calibrated because measureRoi uses image
// calibration. This is a nucleus-associated ciliary-component endpoint, not
// reconstruction of a complete cell boundary or an individual axoneme count.
def assignCiliaryComponentsToNuclei(componentStats, nucleusStats,
                                    double maxCentroidDistanceUm,
                                    double minBoundaryDistanceUm,
                                    double maxBoundaryDistanceUm) {
  def byNucleus = [:].withDefault { [] }
  if (componentStats == null || componentStats.isEmpty() ||
      nucleusStats == null || nucleusStats.isEmpty()) {
    return [by_nucleus:byNucleus, assigned_component_count:0,
            unassigned_component_count:(componentStats == null ? 0 : componentStats.size())]
  }

  double cellSize = Math.max(maxCentroidDistanceUm, 0.001d)
  def grid = [:].withDefault { [] }
  nucleusStats.eachWithIndex { ns, ni ->
    int gx = (int)Math.floor(ns.cx / cellSize)
    int gy = (int)Math.floor(ns.cy / cellSize)
    double equivalentRadius = Math.sqrt(Math.max(0.0d, ns.area as double) / Math.PI)
    grid[gx + ":" + gy] << [index:ni, cx:ns.cx as double, cy:ns.cy as double,
                             equivalent_radius_um:equivalentRadius]
  }

  int assigned = 0
  componentStats.eachWithIndex { cs, ci ->
    int gx = (int)Math.floor(cs.cx / cellSize)
    int gy = (int)Math.floor(cs.cy / cellSize)
    def nearest = null
    double nearestDistance = Double.POSITIVE_INFINITY
    for (int dx = -1; dx <= 1; dx++) {
      for (int dy = -1; dy <= 1; dy++) {
        grid[(gx + dx) + ":" + (gy + dy)].each { candidate ->
          double xdiff = cs.cx - (double)candidate.cx
          double ydiff = cs.cy - (double)candidate.cy
          double distance = Math.sqrt(xdiff * xdiff + ydiff * ydiff)
          if (distance < nearestDistance ||
              (distance == nearestDistance && nearest != null &&
               (int)candidate.index < (int)nearest.index)) {
            nearest = candidate
            nearestDistance = distance
          }
        }
      }
    }
    double boundaryDistance = nearest == null ? Double.POSITIVE_INFINITY :
      nearestDistance - (double)nearest.equivalent_radius_um
    if (nearest != null && nearestDistance <= maxCentroidDistanceUm &&
        boundaryDistance >= minBoundaryDistanceUm &&
        boundaryDistance <= maxBoundaryDistanceUm) {
      byNucleus[(int)nearest.index] << [
        component_index:ci + 1,
        area_um2:cs.area as double,
        centroid_x_um:cs.cx as double,
        centroid_y_um:cs.cy as double,
        nucleus_centroid_distance_um:nearestDistance,
        nucleus_boundary_distance_um:boundaryDistance
      ]
      assigned++
    }
  }
  return [by_nucleus:byNucleus, assigned_component_count:assigned,
          unassigned_component_count:componentStats.size() - assigned]
}

// Count positive (>0) pixels of a mask inside an ROI -> calibrated area.
def positiveAreaInRoi(ImagePlus maskImp, Roi roi) {
  ImageProcessor mp = maskImp.getProcessor()
  Calibration cal = maskImp.getCalibration()
  Rectangle b = roi.getBounds()
  ImageProcessor roiMask = roi.getMask()
  int bx = (int)b.x, by = (int)b.y
  int x0 = Math.max(0, bx), y0 = Math.max(0, by)
  int x1 = Math.min(mp.getWidth(), bx + (int)b.width)
  int y1 = Math.min(mp.getHeight(), by + (int)b.height)
  long pos = 0L
  for (int y = y0; y < y1; y++) {
    for (int x = x0; x < x1; x++) {
      if ((roiMask == null || roiMask.get(x - bx, y - by) != 0) &&
          mp.get(x, y) != 0) pos++
    }
  }
  return pos * cal.pixelWidth * cal.pixelHeight
}

// Fraction of pixels in an object/ring whose raw intensity reaches threshold.
// This separates a spatially supported membrane pattern from a bright speck
// that happens to raise the object's mean.
def fractionAboveThreshold(ImagePlus imp, Roi roi, double threshold) {
  ImageProcessor ip = imp.getProcessor()
  long total = 0L, positive = 0L
  eachRoiPixel(roi) { x, y ->
    if (x >= 0 && y >= 0 && x < ip.getWidth() && y < ip.getHeight()) {
      total++
      if (ip.getPixelValue(x, y) >= threshold) positive++
    }
  }
  return total > 0 ? positive / (double)total : 0.0d
}

// Morphology support inside an object ROI. In addition to total positive
// coverage, report how much of the positive signal belongs to the largest
// 8-connected component. A high largest-component share distinguishes a
// coherent nuclear/cytoplasmic/membrane pattern from scattered bright specks.
def spatialSupportStats(ImagePlus imp, Roi roi, double threshold) {
  if (roi == null) return [total:0L, positive:0L, fraction:0.0d,
                           components:0, largest:0L, largestShare:0.0d]
  ImageProcessor ip = imp.getProcessor()
  Rectangle b = roi.getBounds()
  ImageProcessor rm = roi.getMask()
  int bw = (int)b.width, bh = (int)b.height
  if (bw <= 0 || bh <= 0) return [total:0L, positive:0L, fraction:0.0d,
                                  components:0, largest:0L, largestShare:0.0d]
  boolean[] positiveMask = new boolean[bw * bh]
  long total = 0L, positive = 0L
  for (int yy = 0; yy < bh; yy++) {
    for (int xx = 0; xx < bw; xx++) {
      if (rm != null && rm.get(xx, yy) == 0) continue
      int x = (int)b.x + xx, y = (int)b.y + yy
      if (x < 0 || y < 0 || x >= ip.getWidth() || y >= ip.getHeight()) continue
      total++
      if (ip.getPixelValue(x, y) >= threshold) {
        positiveMask[yy * bw + xx] = true
        positive++
      }
    }
  }

  boolean[] visited = new boolean[bw * bh]
  int[] queue = new int[bw * bh]
  int components = 0
  long largest = 0L
  int[] dx = [-1, 0, 1, -1, 1, -1, 0, 1] as int[]
  int[] dy = [-1, -1, -1, 0, 0, 1, 1, 1] as int[]
  for (int start = 0; start < positiveMask.length; start++) {
    if (!positiveMask[start] || visited[start]) continue
    components++
    int head = 0, tail = 0
    queue[tail++] = start
    visited[start] = true
    long size = 0L
    while (head < tail) {
      int idx = queue[head++]
      size++
      int x0 = idx % bw, y0 = (int)(idx / bw)
      for (int k = 0; k < 8; k++) {
        int nx = x0 + dx[k], ny = y0 + dy[k]
        if (nx < 0 || ny < 0 || nx >= bw || ny >= bh) continue
        int ni = ny * bw + nx
        if (positiveMask[ni] && !visited[ni]) {
          visited[ni] = true
          queue[tail++] = ni
        }
      }
    }
    if (size > largest) largest = size
  }
  return [total:total, positive:positive,
          fraction:(total > 0 ? positive / (double)total : 0.0d),
          components:components, largest:largest,
          largestShare:(positive > 0 ? largest / (double)positive : 0.0d)]
}

// Spatial index for the strict ownership screen below. Every nucleus centroid
// belongs to one fixed-size pixel cell, so a local support ROI checks only
// neighboring grid cells rather than scanning every nucleus in the image.
def buildNucleusCentroidGrid(nuclei, int cellSizePx = 32) {
  int size = Math.max(4, cellSizePx)
  def cells = [:].withDefault { [] }
  nuclei.eachWithIndex { nucleus, ni ->
    Rectangle nb = nucleus.getBounds()
    int cx = (int)Math.round(nb.getCenterX())
    int cy = (int)Math.round(nb.getCenterY())
    cells[((int)Math.floor(cx / (double)size)) + ":" +
          ((int)Math.floor(cy / (double)size))] << [index:ni, x:cx, y:cy]
  }
  return [cell_size_px:size, cells:cells]
}

// Strict ownership screen for nucleus-associated measurements. If a support
// territory encloses another nucleus centroid, pixels cannot be assigned to one
// cell unambiguously without a full membrane/cell segmentation; leave the final
// call indeterminate instead of double-counting shared signal.
def supportHasOtherNucleus(Roi support, int currentIndex, nuclei, spatialGrid = null) {
  if (support == null) return true
  if (spatialGrid != null) {
    Rectangle sb = support.getBounds()
    int size = spatialGrid.cell_size_px as int
    int minGX = (int)Math.floor(sb.x / (double)size)
    int maxGX = (int)Math.floor((sb.x + Math.max(0, sb.width - 1)) / (double)size)
    int minGY = (int)Math.floor(sb.y / (double)size)
    int maxGY = (int)Math.floor((sb.y + Math.max(0, sb.height - 1)) / (double)size)
    for (int gx = minGX; gx <= maxGX; gx++) {
      for (int gy = minGY; gy <= maxGY; gy++) {
        def candidates = spatialGrid.cells[gx + ":" + gy]
        for (int k = 0; k < candidates.size(); k++) {
          def candidate = candidates[k]
          if ((int)candidate.index != currentIndex &&
              support.contains((int)candidate.x, (int)candidate.y)) return true
        }
      }
    }
    return false
  }
  for (int j = 0; j < nuclei.size(); j++) {
    if (j == currentIndex) continue
    Rectangle nb = nuclei[j].getBounds()
    int cx = (int)Math.round(nb.getCenterX())
    int cy = (int)Math.round(nb.getCenterY())
    if (support.contains(cx, cy)) return true
  }
  return false
}

def buildMaskAtThreshold(ImagePlus ch, double threshold) {
  ImagePlus dup = ch.duplicate()
  double upper = ch.getBitDepth() == 8 ? 255.0d : (ch.getBitDepth() == 16 ? 65535.0d : Double.MAX_VALUE)
  dup.getProcessor().setThreshold(threshold, upper, ImageProcessor.NO_LUT_UPDATE)
  IJ.run(dup, "Convert to Mask", "")
  dup.setCalibration(ch.getCalibration())
  dup.setProperty("thresholdValue", threshold)
  return dup
}

// Build a binary mask ImagePlus (255 = signal) from a channel by threshold.
// The numeric lower threshold is stashed as a property for provenance export.
// Requires Prefs.blackBackground=true (set in main) so foreground = 255.
def buildThresholdMask(ImagePlus ch, double blurSigma, String method, Double fixedThreshold = null) {
  ImagePlus dup = ch.duplicate()
  if (blurSigma > 0) new GaussianBlur().blurGaussian(dup.getProcessor(), blurSigma)
  double upper = dup.getBitDepth() == 8 ? 255.0d : (dup.getBitDepth() == 16 ? 65535.0d : Double.MAX_VALUE)
  double thr
  if (fixedThreshold != null) {
    thr = fixedThreshold
    dup.getProcessor().setThreshold(thr, upper, ImageProcessor.NO_LUT_UPDATE)
  } else {
    IJ.setAutoThreshold(dup, method + " dark")
    thr = dup.getProcessor().getMinThreshold()   // raw intensity, -1 if unset
  }
  if (!Double.isFinite(thr) || thr < 0.0d) {
    dup.close()
    throw new IllegalStateException("Could not resolve a valid " + method +
      " threshold for channel '" + ch.getTitle() + "'")
  }
  IJ.run(dup, "Convert to Mask", "")
  dup.setCalibration(ch.getCalibration())
  dup.setProperty("thresholdValue", thr)
  return dup
}

// Remove connected foreground components below the declared physical area.
// Applying this before both area measurement and mask export keeps the numeric
// endpoint, component table, and saved QC mask internally consistent.
def filterBinaryMaskByArea(ImagePlus mask, double minAreaUm2,
                           double maxAreaUm2 = Double.POSITIVE_INFINITY) {
  def work = mask.duplicate()
  work.setCalibration(mask.getCalibration())
  def accepted = particlesToRois(work, minAreaUm2, false, maxAreaUm2)
  work.close()
  def bp = new ByteProcessor(mask.getWidth(), mask.getHeight())
  bp.setValue(255)
  accepted.each { bp.fill(it) }
  def out = new ImagePlus(mask.getTitle() + "_area_filtered", bp)
  out.setCalibration(mask.getCalibration())
  out.setProperty("thresholdValue", mask.getProperty("thresholdValue"))
  out.setProperty("minimumComponentAreaUm2", minAreaUm2)
  out.setProperty("maximumComponentAreaUm2",
    Double.isFinite(maxAreaUm2) ? maxAreaUm2 : "unbounded")
  return out
}

// Build a cilia-specific AcTub mask. Acetylated tubulin is not cilium-specific:
// it can label stable cytoplasmic microtubules, centrosomes, spindles, and
// midbodies. Motile-cilia candidates must therefore satisfy three independent
// gates: high intensity, dense local clustering at the apical scale, and a
// bounded physical patch size. The returned binary mask is the only AcTub
// image used for ciliary morphology/area decisions.
def buildCiliaryTuftMask(ImagePlus ch, cfg, Double fixedThreshold = null) {
  def bounds = displayPercentileBounds(
    ch, cfg.actubCiliaSeedPercentile as double, 100.0d)
  double percentileThreshold = bounds.low as double
  double seedThreshold = fixedThreshold != null ?
    Math.max(fixedThreshold as double, percentileThreshold) :
    percentileThreshold

  ImagePlus seed = buildMaskAtThreshold(ch, seedThreshold)
  ImagePlus density = null
  ImagePlus filtered = null
  try {
    ImageProcessor densityIp = seed.getProcessor().duplicate().convertToFloat()
    double sigmaPx = Math.max(
      0.5d,
      (cfg.actubCiliaDensityRadiusUm as double) /
      Math.max(ch.getCalibration().pixelWidth, 1.0e-9d))
    new GaussianBlur().blurGaussian(densityIp, sigmaPx)
    density = new ImagePlus(ch.getTitle() + "_ciliary_local_density", densityIp)
    density.setCalibration(ch.getCalibration())
    double densityThreshold =
      255.0d * (cfg.actubCiliaMinLocalDensity as double)
    density.getProcessor().setThreshold(
      densityThreshold, 255.0d, ImageProcessor.NO_LUT_UPDATE)
    IJ.run(density, "Convert to Mask", "")
    density.setProperty("thresholdValue", seedThreshold)
    density.setProperty("seedPercentile", cfg.actubCiliaSeedPercentile)
    density.setProperty("densityRadiusUm", cfg.actubCiliaDensityRadiusUm)
    density.setProperty("minimumLocalDensity",
                        cfg.actubCiliaMinLocalDensity)
    filtered = filterBinaryMaskByArea(
      density, cfg.actubMinPatchAreaUm2 as double,
      cfg.actubMaxPatchAreaUm2 as double)
    filtered.setProperty("thresholdValue", seedThreshold)
    filtered.setProperty("seedPercentile", cfg.actubCiliaSeedPercentile)
    filtered.setProperty("densityRadiusUm", cfg.actubCiliaDensityRadiusUm)
    filtered.setProperty("minimumLocalDensity",
                         cfg.actubCiliaMinLocalDensity)
    filtered.setProperty("maskModel",
      "high_intensity_local_density_bounded_apical_tuft")
    return filtered
  } finally {
    seed.close()
    if (density != null) density.close()
  }
}

// Visualization helper: suppress every pixel not accepted by the cilia mask.
// This operates only on the disposable 8-bit display copy.
def applyBinaryDisplayMask(ImagePlus display, ImagePlus binaryMask,
                           String suffix) {
  ImageProcessor src = display.getProcessor()
  ImageProcessor mask = binaryMask.getProcessor()
  ImageProcessor out = src.duplicate()
  int width = out.getWidth(), height = out.getHeight()
  for (int y = 0; y < height; y++) {
    for (int x = 0; x < width; x++) {
      if (mask.get(x, y) == 0) out.set(x, y, 0)
    }
  }
  def result = new ImagePlus(display.getTitle() + suffix, out)
  result.setCalibration(display.getCalibration())
  return result
}

// Perinuclear cytoplasm only = enlarged cell ROI minus the nucleus ROI.
// Used for a true nuclear:cytoplasmic ratio (e.g. YAP). null if degenerate.
def ringOnly(Roi nuc, Roi cell) {
  try {
    ShapeRoi s = new ShapeRoi(cell).not(new ShapeRoi(nuc))
    Rectangle b = s.getBounds()
    if (b == null || b.width <= 0 || b.height <= 0) return null
    return s
  } catch (Throwable e) { return null }
}

// Tissue ROI: manual if RoiSet/.roi present, else auto from DAPI.
def resolveTissueRois(String imgPath, ImagePlus dapi, cfg) {
  def base = new File(imgPath)
  def stem = base.name.replaceFirst(/\.[^.]+$/, "")
  def parent = base.getParent()
  def candidates = [ new File(parent, stem + "_RoiSet.zip"),
                     new File(parent, stem + ".zip"),
                     new File(parent, stem + ".roi"),
                     new File(parent, "RoiSet.zip") ]
  def hit = candidates.find { it.exists() }
  if (hit != null) {
    def rois = readRoiFile(hit)
    if (rois.isEmpty()) {
      throw new IllegalArgumentException("Manual ROI file contains no readable .roi entries: " + hit)
    }
    def named = []
    rois.eachWithIndex { r, i ->
      def bounds = r.getBounds()
      if (bounds == null || bounds.width <= 0 || bounds.height <= 0 ||
          bounds.x < 0 || bounds.y < 0 || bounds.x + bounds.width > dapi.getWidth() ||
          bounds.y + bounds.height > dapi.getHeight()) {
        throw new IllegalArgumentException("ROI '" + (r.getName() ?: (i + 1)) +
          "' is empty or extends outside image bounds in " + hit.name)
      }
      named << [name: (r.getName() ?: ("region" + (i+1))), roi: r]
    }
    def duplicateNames = named.groupBy { it.name }.findAll { name, entries -> entries.size() > 1 }.keySet()
    if (!duplicateNames.isEmpty()) {
      throw new IllegalArgumentException("ROI names must be unique within " + hit.name +
        "; duplicates: " + duplicateNames.sort())
    }
    for (int i = 0; i < named.size(); i++) {
      for (int j = i + 1; j < named.size(); j++) {
        def overlap = new ShapeRoi(named[i].roi).and(new ShapeRoi(named[j].roi))
        def ob = overlap.getBounds()
        boolean hasArea = false
        for (int y = ob.y; !hasArea && y < ob.y + ob.height; y++) {
          for (int x = ob.x; x < ob.x + ob.width; x++) {
            if (overlap.contains(x, y)) { hasArea = true; break }
          }
        }
        if (hasArea) {
          throw new IllegalArgumentException("Manual ROIs overlap and would double-count cells: '" +
            named[i].name + "' and '" + named[j].name + "' in " + hit.name +
            ". Encode multiple context tags in one ROI name instead.")
        }
      }
    }
    return [source: hit.name, regions: named]
  }
  if (cfg.tissueMode == "whole_field") {
    return [source: "whole_field", regions: [[name: "whole_field",
             roi: new Roi(0, 0, dapi.getWidth(), dapi.getHeight())]]]
  }
  // Auto tissue from DAPI
  def mask = buildThresholdMask(dapi, cfg.tissueBlur, cfg.tissueMethod)
  // The `black` token is LOAD-BEARING and must never be removed. ImageJ's Binary
  // Options dialog is a GenericDialog: in macro mode an ABSENT checkbox keyword
  // reads as UNCHECKED, so an option string without `black` makes this call
  // write Prefs.blackBackground = false GLOBALLY, silently overriding the true
  // set in main(). This runs inside resolveTissueRois, i.e. BEFORE segmentNuclei,
  // for every image -- so the flipped pref then inverted the polarity of
  // `Fill Holes` during nucleus segmentation, which erased every nucleus that did
  // not touch the image frame and left only the border-connected rim.
  //
  // Measured cost of the missing token on the 260808-CW confocal batch, POOLED
  // over all 79 fields (total nuclei / total tissue area -- the defensible
  // batch-level estimator): nucleus density 152.5/mm^2 recorded against
  // 15393.3/mm^2 corrected, a ~101x undercount. 100% of candidate components were
  // border-touching in all 79 fields, and a replay of the buggy path reproduced
  // the shipped mask at IoU = 1.0000.
  //
  // ON THE ESTIMATOR, because an earlier version of this comment quoted
  // 185 -> 16422 (89x): those were SINGLE-FIELD numbers from the diagnostic probe
  // presented as if they were the batch. Mean-of-per-field over the batch gives
  // 141.0 -> 15341.4 (109x). All three pairs are internally consistent; only the
  // pooled pair is quoted now, and the estimator is named wherever it appears.
  // Derivation: sum(n_nuclei) / sum(region_area_um2) over run_summary.csv of
  // D:\IFQ_Runs\confocal_260808 and confocal_260808_fixed.
  //
  // Area masks were NOT affected: they are read via setThreshold(128,255,
  // NO_LUT_UPDATE) on pixel values, and Convert to Mask inverts the LUT rather
  // than the data.
  IJ.run(mask, "Options...", "iterations=2 count=1 black do=Close")
  def rois = particlesToRois(mask, cfg.tissueMinArea, false)
  mask.close()
  if (rois.isEmpty()) {
    throw new IllegalStateException("Automatic DAPI tissue detection found no region. " +
      "Review the DAPI channel/settings or explicitly set IFQ_TISSUE_MODE=whole_field; " +
      "the pipeline will not silently analyze the complete background field.")
  }
  // merge all particles into one "tissue" region
  ShapeRoi merged = null
  rois.each { r -> def s = new ShapeRoi(r); merged = (merged == null) ? s : merged.or(s) }
  return [source: "auto_dapi", regions: [[name: "tissue", roi: merged]]]
}

// Nucleus ROIs within a given tissue region.
def segmentNuclei(ImagePlus dapi, Roi region, cfg) {
  // Keep full-image coordinates. ImagePlus.duplicate() crops when an ROI is
  // active; inheriting that cropped coordinate system made non-whole-field ROI
  // masks and returned nuclei spatially inconsistent with the source image.
  ImagePlus crop = new ImagePlus(dapi.getTitle() + "_segmentation_work",
                                 dapi.getProcessor().duplicate())
  crop.setCalibration(dapi.getCalibration())
  if (cfg.segmenter == "stardist") {
    // StarDist's ROI Manager output is interactive-only. Keep it isolated from
    // the classic path so classic segmentation remains fully headless-safe.
    def rm = ij.plugin.frame.RoiManager.getInstance() ?: new ij.plugin.frame.RoiManager()
    rm.reset()
    crop.setTitle("DAPI_seg")
    crop.show()   // StarDist is happiest with a shown image (interactive mode)
    IJ.run(crop, "Command From Macro",
      "command=[de.csbdresden.stardist.StarDist2D], " +
      "args=['input':'DAPI_seg', 'modelChoice':'Versatile (fluorescent nuclei)', " +
      "'normalizeInput':'true', 'percentileBottom':'1.0', 'percentileTop':'99.8', " +
      "'probThresh':'" + cfg.prob + "', 'nmsThresh':'" + cfg.nms + "', " +
      "'outputType':'ROI Manager', 'nTiles':'" + cfg.tiles + "', " +
      "'excludeBoundary':'2', 'roiPosition':'Automatic', 'verbose':'false', " +
      "'showCsbdeepProgress':'false', 'showProbAndDist':'false'], process=[false]")
    def rois = rm.getRoisAsArray().collect { it }
    crop.changes = false; crop.close()
    // keep only nuclei whose centroid lies inside the region
    def included = rois.findAll { region.contains((int)it.getBounds().getCenterX(), (int)it.getBounds().getCenterY()) }
    return [included: included, rejected: []]
  } else {
    // Classic/local-threshold watershed fallback. The local mode is intended
    // for uneven DAPI illumination and is fully recorded in params.json.
    def m = new ImagePlus(crop.getTitle() + "_binary_work",
                          crop.getProcessor().duplicate())
    m.setCalibration(dapi.getCalibration())
    if (cfg.dapiMethod == "local_phansalkar") {
      double px = Math.max(dapi.getCalibration().pixelWidth, 1.0e-9d)
      int backgroundRadiusPx = Math.max(3, (int)Math.round(cfg.dapiBackgroundRadiusUm / px))
      int localRadiusPx = Math.max(3, (int)Math.round(cfg.dapiLocalRadiusUm / px))
      IJ.run(m, "Subtract Background...", "rolling=" + backgroundRadiusPx + " sliding")
      if (cfg.dapiBlurSigmaPx > 0) new GaussianBlur().blurGaussian(m.getProcessor(), cfg.dapiBlurSigmaPx)
      IJ.run(m, "Enhance Contrast...", "saturated=" + cfg.dapiContrastSaturation + " normalize")
      if (m.getBitDepth() != 8) IJ.run(m, "8-bit", "")
      IJ.run(m, "Auto Local Threshold",
             "method=Phansalkar radius=" + localRadiusPx + " parameter_1=0 parameter_2=0 white")
    } else {
      new GaussianBlur().blurGaussian(m.getProcessor(), 2.0)
      IJ.setAutoThreshold(m, "Otsu dark")
      IJ.run(m, "Convert to Mask", "")
    }
    IJ.run(m, "Fill Holes", "")
    IJ.run(m, "Watershed", "")
    m.setCalibration(dapi.getCalibration())
    m.setRoi(region)
    // Keep rejected candidates for QC instead of silently dropping them.
    // A candidate is the same Otsu/watershed particle used for counting; the
    // only post-segmentation rejection rules are calibrated area and image edge.
    def candidates = particlesToRois(m, 0.0d, false)
    // Preserve ParticleAnalyzer's calibrated size/edge decision as the
    // authoritative count. Diagnostics are derived afterward and cannot alter it.
    def rois = particlesToRois(m, cfg.minNucArea, true)
    def roiKey = { r ->
      def b = r.getBounds()
      return b.x + ":" + b.y + ":" + b.width + ":" + b.height
    }
    def includedKeys = rois.collect { roiKey(it) } as Set
    def rejected = candidates.findAll { !includedKeys.contains(roiKey(it)) }.collect { r ->
      def b = r.getBounds()
      double area = measureRoi(dapi, r).area
      boolean edge = b.x <= 0 || b.y <= 0 || b.x + b.width >= dapi.getWidth() || b.y + b.height >= dapi.getHeight()
      return [roi: r,
              reason: (edge ? "image_edge" : (area < cfg.minNucArea ? "area_below_minimum" : "particle_filter")),
              area_um2: area]
    }
    def candidateMask = m.duplicate(); candidateMask.setCalibration(dapi.getCalibration())
    m.close()
    crop.close()
    return [included: rois, rejected: rejected, candidateMask: candidateMask]
  }
}

// ============================================================================
//  5. PER-IMAGE PROCESSING
// ============================================================================

def processImage(String imgPath, String outputKey, panelKey, panelDef, meta, cfg, outDir) {
  IJ.log("---- " + new File(imgPath).name + "  [panel " + panelKey + "] ----")
  def sourceStem = new File(imgPath).name.replaceFirst(/\.[^.]+$/, "")
  def imgOut = ensureDir(outDir + "/" + outputKey)
  def fileSafe = { value ->
    def s = (value == null || value.toString().trim().isEmpty()) ? "NA" : value.toString().trim()
    def cleaned = s.replaceAll(/[^A-Za-z0-9._-]+/, "-").replaceAll(/^-+|-+$/, "")
    return cleaned.isEmpty() ? "NA" : cleaned
  }
  // Use the marker map as the per-image filename prefix. The containing
  // directory already identifies the specimen, so avoiding that duplication
  // keeps Windows paths short enough to open reliably.
  def channelSignature = panelDef.channels.sort { it.idx }.collect { c ->
    "C" + c.idx + "-" + fileSafe(c.fileLabel ?: c.marker)
  }.join("_")
  def fileKey = channelSignature

  ImagePlus raw = null
  def channels = []
  def markerImg = [:]
  def markerZInfo = [:]
  def zProfileRows = []
  def zProfileSummary = [:]
  def displayImages = [:]
  def displaySettings = [:]
  def areaMasks = [:]
  def transientImages = []
  try {
  raw = bfOpen(imgPath)
  Calibration cal = raw.getCalibration()
  String calibrationUnit = (cal.getUnit() ?: "").toLowerCase()
  boolean micrometreUnit = calibrationUnit in ["um", "µm", "μm", "micron", "microns", "micrometer", "micrometers"]
  if (!Double.isFinite(cal.pixelWidth) || !Double.isFinite(cal.pixelHeight) ||
      cal.pixelWidth <= 0.0d || cal.pixelHeight <= 0.0d || !micrometreUnit) {
    throw new IllegalArgumentException("Image calibration must provide positive micrometre pixel dimensions; found " +
      cal.pixelWidth + " x " + cal.pixelHeight + " " + cal.getUnit() + " in " + new File(imgPath).name)
  }
  double pixelAspectDifference = Math.abs(cal.pixelWidth - cal.pixelHeight) /
                                 Math.max(cal.pixelWidth, cal.pixelHeight)
  if (pixelAspectDifference > 0.01d) {
    throw new IllegalArgumentException("Perinuclear ROI enlargement assumes square pixels; found " +
      cal.pixelWidth + " x " + cal.pixelHeight + " um in " + new File(imgPath).name)
  }
  if (cfg.projection == "layer_aware" && raw.getNSlices() > 1 &&
      (!Double.isFinite(cal.pixelDepth) || cal.pixelDepth <= 0.0d)) {
    throw new IllegalArgumentException("Layer-aware Z analysis requires a positive Z calibration; found " +
      cal.pixelDepth + " " + cal.getUnit() + " in " + new File(imgPath).name)
  }
  // Channel maps may intentionally skip an unused acquisition channel. The
  // highest referenced index, not the number of mapped channels, defines the
  // minimum acquisition size.
  def nChExpected = panelDef.channels.collect { it.idx as int }.max()
  channels = ChannelSplitter.split(raw)
  if (channels.length < nChExpected) {
    throw new IllegalArgumentException("Found " + channels.length +
      " channels but panel '" + panelKey + "' references channel " + nChExpected)
  }

  // Map marker -> projected 2D channel image. In layer-aware mode, each marker
  // resolves its own slab before projection; legacy modes continue to use the
  // same range and method for every channel.
  markerImg = [:]
  def nuclearDef = panelDef.channels.find { it.role == "nuclear" }
  def nuclearMarker = nuclearDef?.marker
  def nuclearChannel = nuclearDef != null ? channels[(nuclearDef.idx as int) - 1] : null
  if (nuclearChannel == null) {
    throw new IllegalArgumentException("Panel '" + panelKey + "' has no nuclear channel")
  }
  nuclearChannel.setCalibration(cal)
  panelDef.channels.each { c ->
    def ch = channels[c.idx - 1]
    ch.setCalibration(cal)
    def zSelection = resolveMarkerZSelection(ch, nuclearChannel, c, cfg)
    def proj = projectChannelRange(ch, zSelection)
    proj.setCalibration(cal)
    markerImg[c.marker] = proj
    markerZInfo[c.marker] = zSelection
    def profile = channelZProfile(ch, c.marker.toString(), zSelection, cal)
    zProfileRows.addAll(profile.rows)
    zProfileSummary[c.marker] = [policy:zSelection.policy,
                                 start_plane:zSelection.start,
                                 end_plane:zSelection.end,
                                 projection:zSelection.projection,
                                 range_source:zSelection.range_source,
                                 auto_score:zSelection.auto_score,
                                 intensity_weighted_z_centroid_um:
                                   profile.intensity_weighted_z_centroid_um]
    IJ.log("[IFQ_Z] " + c.marker + " policy=" + zSelection.policy +
           " range=" + zSelection.start + ":" + zSelection.end +
           " projection=" + zSelection.projection +
           " source=" + zSelection.range_source)
  }

  // Build a separate, disposable display branch. None of these 8-bit images
  // are ever assigned back to markerImg, so percentile stretching and gamma
  // cannot alter thresholds, masks, morphology features, or final calls.
  if (cfg.exportDisplayChannels) {
    panelDef.channels.each { c ->
      double displayLow = c.displayLowPercentile != null ?
                          c.displayLowPercentile as double : cfg.displayLowPercentile
      double displayHigh = c.displayHighPercentile != null ?
                           c.displayHighPercentile as double : cfg.displayHighPercentile
      double displayGamma = c.displayGamma != null ?
                            c.displayGamma as double : cfg.displayGamma
      Double displayLowIntensity = c.displayLowIntensity != null ?
                                   c.displayLowIntensity as double : null
      Double displayHighIntensity = c.displayHighIntensity != null ?
                                    c.displayHighIntensity as double : null
      double displayBackgroundRadiusPx = c.displayBackgroundRadiusPx != null ?
                                          c.displayBackgroundRadiusPx as double : 0.0d
      double displayBlackPoint = c.displayBlackPoint != null ?
                                 c.displayBlackPoint as double : 0.0d
      double displayMedianRadiusPx = c.displayMedianRadiusPx != null ?
                                     c.displayMedianRadiusPx as double : 0.0d
      def enhanced = buildDisplayChannel(markerImg[c.marker], c.marker.toString(),
                                         displayLow, displayHigh, displayGamma,
                                         displayLowIntensity, displayHighIntensity,
                                         displayBackgroundRadiusPx,
                                         displayBlackPoint,
                                         displayMedianRadiusPx)
      def displayImage = enhanced.image
      boolean ciliaFocused = c.role == "apical_cilia"
      if (ciliaFocused) {
        boolean fixedCiliaThreshold = cfg.fixedThresholds.containsKey(c.marker)
        Double fixedThreshold = fixedCiliaThreshold ?
          cfg.fixedThresholds[c.marker] as double : null
        def ciliaMask = buildCiliaryTuftMask(
          markerImg[c.marker], cfg, fixedThreshold)
        def maskedDisplay = applyBinaryDisplayMask(
          displayImage, ciliaMask, "_CILIA_FOCUSED")
        displayImage.close()
        displayImage = maskedDisplay
        ciliaMask.close()
      }
      displayImages[c.marker] = displayImage
      def zInfo = markerZInfo[c.marker]
      displaySettings[c.marker] = [
        channel_index: c.idx,
        role: c.role,
        primary_endpoint: c.primaryEndpoint == true,
        color: c.qcColor ?: "white",
        low_percentile: enhanced.low_percentile,
        high_percentile: enhanced.high_percentile,
        resolved_low_intensity: enhanced.low_intensity,
        resolved_high_intensity: enhanced.high_intensity,
        gamma: enhanced.gamma,
        weight: (c.displayWeight != null ? c.displayWeight as double : 1.0d),
        window_source: enhanced.window_source,
        display_background_radius_px: enhanced.background_radius_px,
        display_black_point: enhanced.black_point,
        display_median_radius_px: enhanced.median_radius_px,
        background_suppression:
          (ciliaFocused ?
            "high_intensity_local_density_bounded_apical_tuft" :
            ((enhanced.background_radius_px > 0.0d ||
              enhanced.median_radius_px > 0.0d) ?
              ([enhanced.background_radius_px > 0.0d ? "rolling_ball" : null,
                enhanced.median_radius_px > 0.0d ? "median_despeckle" : null]
                .findAll { it != null }.join("+") + "_display_only") : "none")),
        z_start_plane: zInfo.start,
        z_end_plane: zInfo.end,
        z_projection: zInfo.projection
      ]
      def labeled = labelDisplayOnlyExport(
        displayImage,
        (c.primaryEndpoint == true ? "PRIMARY ENDPOINT | " : "") +
        "C" + c.idx + " " + c.marker +
        (ciliaFocused ? " | cilia-focused cytoskeleton suppressed" : "") +
        " | " + (enhanced.window_source == "absolute" ?
          ("raw[" + enhanced.low_intensity + "-" + enhanced.high_intensity + "]") :
          ("p" + displayLow + "-" + displayHigh)) +
        " gamma " + displayGamma +
        (enhanced.black_point > 0.0d ? " black " + enhanced.black_point : "") +
        (enhanced.median_radius_px > 0.0d ?
          " median " + enhanced.median_radius_px + "px" : ""))
      transientImages << labeled
      IJ.saveAs(labeled, "PNG", imgOut.getAbsolutePath() + "/" + fileKey +
                "__DISPLAY_ONLY__C" + c.idx + "-" + fileSafe(c.marker) +
                "_enhanced.png")
      labeled.close()
    }
    def mergedDisplay = buildQcComposite(markerImg, panelDef, displayImages)
    transientImages << mergedDisplay
    def labeledMerge = labelDisplayOnlyExport(
      mergedDisplay,
      ((panelDef.channels.find { it.primaryEndpoint == true }) != null ?
        ("PRIMARY C" +
         panelDef.channels.find { it.primaryEndpoint == true }.idx + "=" +
         panelDef.channels.find { it.primaryEndpoint == true }.marker + " | ") :
        "") +
      panelDef.channels.collect { c ->
        c.marker + "=" + (c.qcColor ?: "white") + "@" +
          (c.displayWeight != null ? c.displayWeight : 1.0d)
      }.join(", "), true, cfg.displayScaleBarUm as double,
      cfg.displayScaleBarThicknessPx as int)
    transientImages << labeledMerge
    IJ.saveAs(labeledMerge, "PNG", imgOut.getAbsolutePath() + "/" + fileKey +
              "__VISUAL_MERGE_PANEL__merged_enhanced.png")
    labeledMerge.close()
    mergedDisplay.close()
  }
  if (cfg.displayPreviewOnly) {
    IJ.log("[IFQ_PREVIEW] Display exports complete; segmentation and quantification deliberately not run.")
    return [summary: [], cells: 0, tissue_source: "not_run_preview_only",
            channel_signature: channelSignature, preview_only: true]
  }
  def dapi = markerImg[nuclearMarker]
  def nonNuclearChannels = panelDef.channels.findAll { it.role != "nuclear" }
  def cellChannels = nonNuclearChannels.findAll { it.cellCall != false }
  def expectedCompartmentsFor = { c ->
    def expected = []
    if (c.expectedCompartments instanceof Collection) {
      expected.addAll(c.expectedCompartments.collect { it.toString().toLowerCase() })
    } else if (c.expectedCompartment != null) {
      expected << c.expectedCompartment.toString().toLowerCase()
    }
    return expected.unique()
  }

  // Precompute per-marker positivity thresholds (Otsu within whole field first;
  // refined per tissue region below).
  def tissue = resolveTissueRois(imgPath, dapi, cfg)

  // Pre-build marker-specific area masks. The minimum component area is applied
  // to the saved mask itself so reported area and component counts agree.
  areaMasks = [:]
  def areaThresholdSources = [:]
  panelDef.channels.findAll { it.areaMarker }.each { c ->
    double areaBlur = c.containsKey("areaBlurSigmaPx") ? (double)c.areaBlurSigmaPx : cfg.podBlur
    String areaMethod = c.containsKey("areaThresholdMethod") ? c.areaThresholdMethod : cfg.podMethod
    double minArea = c.containsKey("areaMinAreaUm2") ? (double)c.areaMinAreaUm2 : cfg.podMinArea
    boolean fixedAreaThreshold = cfg.fixedThresholds.containsKey(c.marker)
    Double resolvedAreaThreshold = fixedAreaThreshold ? (double)cfg.fixedThresholds[c.marker] : null
    if (c.role == "apical_cilia" || c.areaMode == "ciliary") {
      areaMasks[c.marker] = buildCiliaryTuftMask(
        markerImg[c.marker], cfg, resolvedAreaThreshold)
      areaThresholdSources[c.marker] = fixedAreaThreshold ?
        "fixed_plus_ciliary_local_density_morphology" :
        "adaptive_high_percentile_ciliary_local_density_exploratory"
    } else {
      def rawAreaMask = buildThresholdMask(
        markerImg[c.marker], areaBlur, areaMethod, resolvedAreaThreshold)
      transientImages << rawAreaMask
      areaMasks[c.marker] = filterBinaryMaskByArea(rawAreaMask, minArea)
      areaThresholdSources[c.marker] = fixedAreaThreshold ? "fixed_predeclared" :
        ("adaptive_" + areaMethod.toLowerCase().replaceAll(/[^a-z0-9]+/, "_") + "_exploratory")
      rawAreaMask.close()
    }
  }

  def cellRows = []      // per-object records (all regions)
  def summaryRows = []   // per-region summary
  def qcRegionOverlays = [:]

  tissue.regions.eachWithIndex { reg, ri ->
    long stageStartedNs = System.nanoTime()
    def logStage = { String stage ->
      long nowNs = System.nanoTime()
      IJ.log("[IFQ_STAGE] " + stage + " " +
             String.format(java.util.Locale.US, "%.3f",
                           (nowNs - stageStartedNs) / 1.0e9d) + "s")
      stageStartedNs = nowNs
    }
    def region = reg.roi
    def regName = reg.name
    def regFileToken = fileSafe(regName)
    def regionLower = regName.toLowerCase()
    def regionTags = []
    if (regionLower.contains("alveol")) regionTags << "alveolar"
    if (regionLower.contains("airway") || regionLower.contains("bronch")) regionTags << "airway"
    if (regionLower.contains("tumor") || regionLower.contains("tumour") || regionLower.contains("luad")) regionTags << "tumor"
    if (regionLower.contains("fibrot") || regionLower.contains("honeycomb") || regionLower.contains("uip")) regionTags << "fibrotic"
    if (regionLower.contains("strom") || regionLower.contains("mesench")) regionTags << "stromal"
    if (regionLower.contains("vascul") || regionLower.contains("vessel") || regionLower.contains("capillar")) regionTags << "vascular"
    if (regionLower.contains("immune") || regionLower.contains("inflamm") || regionLower.contains("lymph")) regionTags << "immune"
    if (regionLower.contains("ambig")) regionTags = ["ambiguous"]
    regionTags = regionTags.unique()
    if (regionTags.isEmpty() && cfg.wholeFieldCompartment != "unassigned") {
      regionTags << cfg.wholeFieldCompartment
    }
    def compartment = regionTags.contains("ambiguous") ? "ambiguous" :
                      (regionTags.contains("alveolar") ? "alveolar" :
                       (regionTags.contains("airway") ? "airway" :
                        (regionTags.isEmpty() ? "unassigned" : regionTags[0])))
    if (cfg.compartmentMode == "required" && regionTags.isEmpty()) {
      throw new IllegalArgumentException("Morphology classification required: use a recognizable anatomical/context ROI name; found '" + regName + "'")
    }
    def regStat = measureRoi(dapi, region)   // area of region
    double regionAreaUm2 = regStat.area
    if (!Double.isFinite(regionAreaUm2) || regionAreaUm2 <= 0.0d) {
      throw new IllegalArgumentException("Analysis ROI '" + regName + "' has zero or invalid calibrated area")
    }

    // per-marker channel thresholds inside THIS region (adaptive)
    def chThresh = [:]
    def chThreshSource = [:]
    def chThreshSensitivity = [:]
    nonNuclearChannels.each { c ->
      boolean fixed = cfg.fixedThresholds.containsKey(c.marker)
      double t = fixed ? (double)cfg.fixedThresholds[c.marker] : autoThresholdInRoi(markerImg[c.marker], region, "Otsu")
      double sens = fixed ? 1.0d :
        (c.thresholdSensitivity != null ?
          c.thresholdSensitivity as double :
          (cfg.sensitivity[c.marker] ?: 1.0d))
      chThresh[c.marker] = t * sens
      chThreshSensitivity[c.marker] = sens
      chThreshSource[c.marker] = fixed ? "fixed_predeclared" : "adaptive_otsu_exploratory"
    }

    // Region boundaries for each fluorescence channel; these replace the
    // former per-cell marker-positive circles in the QC overlay.
    def qcMasks = [:]
    nonNuclearChannels.each { c ->
      qcMasks[c.marker] = buildMaskAtThreshold(markerImg[c.marker], chThresh[c.marker])
      transientImages << qcMasks[c.marker]
    }

    // ---- Marker-positive area/components (pods, reporter fields, ciliary patches) ----
    def areaStats = [:]
    panelDef.channels.findAll { it.areaMarker }.each { c ->
      def mask = areaMasks[c.marker]
      String areaMode = c.areaMode ?: "pod"
      double minComponentArea = c.containsKey("areaMinAreaUm2") ? (double)c.areaMinAreaUm2 : cfg.podMinArea
      double positiveArea = positiveAreaInRoi(mask, region)
      // Particle analysis is intentionally mode-specific: large KRT5 pods,
      // reporter-positive cell/clusters, or subcellular ciliary patches.
      def maskReg = mask.duplicate(); maskReg.setCalibration(cal); maskReg.setRoi(region)
      transientImages << maskReg
      def componentRois = particlesToRois(maskReg, minComponentArea, false)
      def componentStats = componentRois.collect { component ->
        def measured = measureRoi(mask, component)
        [roi:component, area:measured.area as double,
         cx:measured.cx as double, cy:measured.cy as double]
      }
      def componentAreas = componentStats.collect { it.area as double }
      def areaThr = mask.getProperty("thresholdValue")
      areaStats[c.marker] = [ mode: areaMode, area_um2: positiveArea,
                              frac_of_region: (regionAreaUm2 > 0 ? positiveArea/regionAreaUm2 : 0),
                              n_components: componentRois.size(),
                              mean_component_area_um2: (componentAreas.isEmpty()? 0 : componentAreas.sum()/componentAreas.size()),
                              min_component_area_um2: minComponentArea,
                              max_component_area_um2:
                                (areaMode == "ciliary" ?
                                  cfg.actubMaxPatchAreaUm2 : ""),
                              threshold: (areaThr != null ? areaThr : -1),
                              threshold_source: areaThresholdSources[c.marker],
                              mask_model:
                                (mask.getProperty("maskModel") ?: "threshold_area_filter"),
                              component_stats: componentStats ]
      maskReg.close()
    }
    logStage("area_components")

    // ---- Nuclei -> cells ----
    def segmentation = segmentNuclei(dapi, region, cfg)
    if (segmentation.candidateMask != null) transientImages << segmentation.candidateMask
    def nuclei = segmentation.included
    if (nuclei.size() < cfg.minIncludedNuclei) {
      int rejectedCount = (segmentation.rejected ?: []).size()
      int candidateCount = nuclei.size() + rejectedCount
      throw new IllegalStateException("Nucleus QC failed in region '" + regName +
        "': included " + nuclei.size() + " of " + candidateCount +
        " candidates, below IFQ_MIN_INCLUDED_NUCLEI=" + cfg.minIncludedNuclei +
        ". Review DAPI projection, IFQ_DAPI_METHOD, tissue ROI, minimum area, and the DAPI QC mask; " +
        "this field cannot produce valid cell fractions.")
    }
    def nucleusStats = nuclei.collect { measureRoi(dapi, it) }
    def nucleusCentroidGrid = buildNucleusCentroidGrid(nuclei)
    logStage("nucleus_segmentation_and_stats")
    def rejectedNuclei = segmentation.rejected ?: []
    def posCount = [:].withDefault { 0 }
    def finalPosCount = [:].withDefault { 0 }
    def finalNegCount = [:].withDefault { 0 }
    def indeterminateCount = [:].withDefault { 0 }
    // Audit how often legacy mean intensity and the morphology-authoritative
    // final call disagree. These are sensitivity/QC counters, not ground-truth
    // false-positive or false-negative labels.
    def rawPosFinalNegCount = [:].withDefault { 0 }
    def rawNegFinalPosCount = [:].withDefault { 0 }
    // Strict marker evidence is counted separately from endpoint context. This
    // prevents localization-correct signal from disappearing when anatomy is
    // unresolved, while still preventing unsupported negatives and compound
    // cell identities.
    def markerEvidencePosCount = [:].withDefault { 0 }
    def contextUnresolvedPosCount = [:].withDefault { 0 }
    def contextExcludedEvidencePosCount = [:].withDefault { 0 }
    def classCount = [:].withDefault { 0 }
    def classEvaluableCount = [:].withDefault { 0 }
    def allNucRois = []
    def finalPositiveRois = [:].withDefault { [] }
    def indeterminateRois = [:].withDefault { [] }
    def apicalComponentAssignments = [:]
    cellChannels.findAll { it.role == "apical_cilia" }.each { c ->
      def stats = areaStats[c.marker]
      apicalComponentAssignments[c.marker] = assignCiliaryComponentsToNuclei(
        stats != null ? stats.component_stats : [], nucleusStats,
        cfg.actubMaxComponentDistanceUm,
        cfg.actubMinComponentBoundaryDistanceUm,
        cfg.actubSupportExpandUm)
    }
    logStage("apical_component_ownership")

    nuclei.eachWithIndex { nuc, ni ->
      allNucRois << nuc
      def cellRoi = RoiEnlarger.enlarge(nuc, (int)Math.round(cfg.ringExpandUm / cal.pixelWidth))
      def row = [ image: sourceStem, output_key: outputKey, panel: panelKey, region: regName,
                  compartment: compartment, region_tags: regionTags.join("|"), cell_id: (ni + 1) ]
      row.mouse_id = meta.mouse_id; row.section_id = meta.section_id
      row.genotype = meta.genotype; row.condition = meta.condition
      def cs = nucleusStats[ni]
      row.centroid_x_um = cs.cx; row.centroid_y_um = cs.cy; row.nucleus_area_um2 = cs.area

      def calls = [:]       // morphology-authoritative three-state calls: 1, 0, or ""
      def rawCalls = [:]    // legacy mean-intensity calls retained for audit only
      def callContextResolved = [:] // compound classes require resolved context
      cellChannels.each { c ->
        def m = c.marker
        def img = markerImg[m]
        def rule = new LinkedHashMap(cfg.roleMorphologyDefaults[c.role] ?:
                                     [minFraction:0.20d, minLargestShare:0.40d, requireOwnership:true])
        if (cfg.morphologyRules[m] != null) rule.putAll(cfg.morphologyRules[m])
        // Per-channel overrides are the final authority because the same
        // antigen can require different geometry in different assays.
        if (c.minPositiveFraction != null) rule.minFraction = c.minPositiveFraction as double
        if (c.minLargestComponentShare != null) rule.minLargestShare = c.minLargestComponentShare as double
        if (c.requireOwnership != null) rule.requireOwnership = c.requireOwnership as boolean
        if (c.minNuclearEnrichment != null) rule.minNuclearEnrichment = c.minNuclearEnrichment as double
        if (c.minNucCytoRatio != null) rule.minNucCytoRatio = c.minNucCytoRatio as double
        def spatialRoi = nuc
        def ownershipSupport = null
        boolean componentContextPass = false
        boolean componentOwnershipPass = false
        def ownedCiliaryComponents = []
        double val, nucVal = 0.0d, cytoVal = 0.0d, enrichmentRatio = 0.0d
        boolean projectionValid = true
        if (c.role == "nuc_marker") {
          val = measureRoi(img, nuc).mean
          nucVal = val
          def ring = ringOnly(nuc, cellRoi)
          cytoVal = ring != null ? measureRoi(img, ring).mean : 0.0d
          enrichmentRatio = cytoVal > 0 ? nucVal / cytoVal : (nucVal > 0 ? Double.POSITIVE_INFINITY : 0.0d)
          row[m + "_nuc_mean"] = nucVal
          row[m + "_reference_ring_mean"] = cytoVal
          row[m + "_nuclear_enrichment_ratio"] = enrichmentRatio
        } else if (c.role == "nuc_ratio") {
          nucVal = measureRoi(img, nuc).mean
          // true cytoplasmic ring = (enlarged cell) MINUS (nucleus)
          def ring = ringOnly(nuc, cellRoi)
          cytoVal = (ring != null) ? measureRoi(img, ring).mean : measureRoi(img, cellRoi).mean
          spatialRoi = (ring != null) ? ring : cellRoi
          // Morphology support is measured in the nucleus; the cytoplasmic
          // ring supplies the localization ratio, not the positive pixels.
          spatialRoi = nuc
          val = nucVal
          enrichmentRatio = cytoVal > 0 ? nucVal / cytoVal : (nucVal > 0 ? Double.POSITIVE_INFINITY : 0.0d)
          row[m + "_nuc_mean"] = nucVal
          row[m + "_cyto_mean"] = cytoVal
          row[m + "_nuc_cyto_ratio"] = enrichmentRatio
          projectionValid = raw.getNSlices() <= 1 ||
                            markerZInfo[m]?.projection == "single"
        } else if (c.role == "apical_cilia") {
          // Ciliary axonemes sit on the luminal/apical surface and commonly lie
          // beyond a 2-um cytoplasmic ring in tissue sections. Use a wider
          // support zone, and call proximity by the spatial fraction above the
          // image/region threshold rather than by a diluted support-zone mean.
          double supportExpandUm = c.supportExpandUm != null ? (double)c.supportExpandUm : cfg.actubSupportExpandUm
          def support = RoiEnlarger.enlarge(nuc, (int)Math.round(supportExpandUm / cal.pixelWidth))
          spatialRoi = support ?: cellRoi
          ownershipSupport = spatialRoi
          val = measureRoi(img, spatialRoi).mean
          def assignment = apicalComponentAssignments[m]
          ownedCiliaryComponents = assignment != null ? (assignment.by_nucleus[ni] ?: []) : []
          componentOwnershipPass = !ownedCiliaryComponents.isEmpty()
          double ownedArea = ownedCiliaryComponents.isEmpty() ? 0.0d :
                             ownedCiliaryComponents.collect { it.area_um2 as double }.sum() as double
          double nearestCentroidDistance = ownedCiliaryComponents.isEmpty() ? Double.NaN :
            ownedCiliaryComponents.collect { it.nucleus_centroid_distance_um as double }.min() as double
          double nearestBoundaryDistance = ownedCiliaryComponents.isEmpty() ? Double.NaN :
            ownedCiliaryComponents.collect { it.nucleus_boundary_distance_um as double }.min() as double
          row[m + "_support_expand_um"] = supportExpandUm
          row[m + "_measurement_model"] = c.measurement ?: "apical_cilia_proximity"
          row[m + "_cellular_context_model"] = "unique_nearest_nucleus_ciliary_component"
          row[m + "_owned_ciliary_component_count"] = ownedCiliaryComponents.size()
          row[m + "_owned_ciliary_component_area_um2"] = ownedArea
          row[m + "_nearest_ciliary_component_centroid_distance_um"] =
            Double.isFinite(nearestCentroidDistance) ? nearestCentroidDistance : ""
          row[m + "_nearest_ciliary_component_boundary_distance_um"] =
            Double.isFinite(nearestBoundaryDistance) ? nearestBoundaryDistance : ""
          row[m + "_maximum_component_distance_um"] = cfg.actubMaxComponentDistanceUm
          row[m + "_minimum_component_boundary_distance_um"] =
            cfg.actubMinComponentBoundaryDistanceUm
          row[m + "_component_ownership_pass"] = componentOwnershipPass ? 1 : 0
        } else { // cyto / membrane: measure the perinuclear ring, not nucleus + ring
          def ring = ringOnly(nuc, cellRoi)
          spatialRoi = (ring != null) ? ring : cellRoi
          ownershipSupport = cellRoi
          val = measureRoi(img, spatialRoi).mean
          row[m + "_measurement_model"] = c.measurement ?: "perinuclear_ring"
        }

        if (c.requiresSinglePlane == true) {
          projectionValid = raw.getNSlices() <= 1 ||
                            markerZInfo[m]?.projection == "single"
        }
        if (!row.containsKey(m + "_measurement_model")) {
          row[m + "_measurement_model"] = c.measurement ?: c.role
        }

        // AcTub morphology is evaluated only on the cilia-specific tuft mask.
        // Raw AcTub intensity remains an audit field but cytoplasmic stable
        // microtubules cannot satisfy the final spatial-pattern gate.
        def morphologySupportImage =
          (c.role == "apical_cilia" && areaMasks[m] != null) ?
            areaMasks[m] : img
        double morphologySupportThreshold =
          c.role == "apical_cilia" ? 128.0d : chThresh[m]
        def supportStats = spatialSupportStats(
          morphologySupportImage, spatialRoi, morphologySupportThreshold)
        double minFraction = (double)rule.minFraction
        double minLargestShare = (double)rule.minLargestShare
        boolean fractionPass = supportStats.fraction >= minFraction
        boolean connectedPass = supportStats.largestShare >= minLargestShare
        if (c.role == "apical_cilia") {
          componentContextPass = componentOwnershipPass && fractionPass && connectedPass
          row[m + "_component_context_pass"] = componentContextPass ? 1 : 0
        }
        boolean ownershipClear = c.role == "apical_cilia" ? true :
                                 (!(rule.requireOwnership ?: false) ||
                                  !supportHasOtherNucleus(ownershipSupport, ni, nuclei,
                                                          nucleusCentroidGrid))
        boolean enrichmentPass = true
        if (c.role == "nuc_marker") {
          enrichmentPass = enrichmentRatio >= (double)(rule.minNuclearEnrichment ?: 1.0d)
        } else if (c.role == "nuc_ratio") {
          enrichmentPass = enrichmentRatio >= (double)(rule.minNucCytoRatio ?: 1.0d)
        }

        def expectedCompartments = expectedCompartmentsFor(c)
        boolean hasCompartmentRequirement = !expectedCompartments.isEmpty()
        boolean compartmentAssigned = !regionTags.isEmpty() && !regionTags.contains("ambiguous")
        boolean compartmentPass = expectedCompartments.isEmpty() ||
                                  (compartmentAssigned && expectedCompartments.any { regionTags.contains(it) })
        boolean knownWrongCompartment = hasCompartmentRequirement &&
                                        compartmentAssigned && !compartmentPass
        boolean allowPositiveWithoutCompartment =
          c.allowPositiveWithoutCompartment != null ?
            (c.allowPositiveWithoutCompartment as boolean) : true
        boolean localizationPatternPass = c.role == "apical_cilia" ?
                                          componentContextPass :
                                          (fractionPass && connectedPass && enrichmentPass)
        boolean markerEvidencePass = localizationPatternPass &&
                                     ownershipClear && projectionValid &&
                                     supportStats.total > 0

        row[m + "_mean"] = val
        boolean intensityPos = val >= chThresh[m]
        rawCalls[m] = intensityPos ? 1 : 0
        row[m + "_pos"] = intensityPos ? 1 : 0  // legacy/raw audit field

        // Compartment-dependent endpoints use an asymmetric context policy.
        // Strong marker evidence can establish an exploratory marker-positive
        // call when anatomy is unresolved, but absence cannot establish a
        // negative. A known incompatible compartment is never overridden.
        boolean authorityPositiveEvidence = cfg.morphologyPrimary ?
                                            markerEvidencePass : intensityPos
        boolean contextUnresolvedPositive = hasCompartmentRequirement &&
                                            !compartmentAssigned &&
                                            allowPositiveWithoutCompartment &&
                                            authorityPositiveEvidence
        boolean evaluable = supportStats.total > 0 && projectionValid
        def indeterminateReasons = []
        if (hasCompartmentRequirement && !compartmentPass &&
            !contextUnresolvedPositive) {
          evaluable = false
          indeterminateReasons << (compartmentAssigned ? "wrong_compartment" : "compartment_unassigned")
        }
        if (!ownershipClear) {
          evaluable = false
          indeterminateReasons << "shared_perinuclear_support"
        }
        if (!projectionValid) {
          indeterminateReasons << (c.role == "nuc_ratio" ?
                                    "projection_invalid_for_nuclear_ratio" :
                                    "projection_invalid_for_marker")
        }
        if (supportStats.total <= 0) {
          indeterminateReasons << "empty_spatial_support"
        }

        row[m + "_threshold_source"] = chThreshSource[m]
        row[m + "_candidate_threshold_sensitivity"] =
          chThreshSensitivity[m]
        row[m + "_support_fraction_above_threshold"] = supportStats.fraction
        row[m + "_minimum_support_fraction"] = minFraction
        row[m + "_positive_component_count"] = supportStats.components
        row[m + "_largest_positive_component_share"] = supportStats.largestShare
        row[m + "_minimum_largest_component_share"] = minLargestShare
        row[m + "_fraction_pass"] = fractionPass ? 1 : 0
        row[m + "_connected_pattern_pass"] = connectedPass ? 1 : 0
        row[m + "_ownership_clear"] = ownershipClear ? 1 : 0
        row[m + "_projection_valid"] = projectionValid ? 1 : 0
        row[m + "_expected_compartment"] = expectedCompartments.isEmpty() ? "none" : expectedCompartments.join("|")
        row[m + "_compartment_pass"] = compartmentPass ? 1 : 0
        row[m + "_context_resolved"] =
          (!hasCompartmentRequirement || compartmentPass) ? 1 : 0
        row[m + "_context_policy"] = hasCompartmentRequirement ?
          "asymmetric_positive_evidence_negative_requires_compartment" : "not_required"
        row[m + "_context_state"] = !hasCompartmentRequirement ? "not_required" :
          (compartmentPass ? "compatible" :
           (knownWrongCompartment ? "known_incompatible" :
            (contextUnresolvedPositive ? "unresolved_positive_evidence" : "unresolved")))
        row[m + "_allow_positive_without_compartment"] =
          allowPositiveWithoutCompartment ? 1 : 0
        row[m + "_marker_evidence_pass"] = markerEvidencePass ? 1 : 0
        row[m + "_enrichment_pass"] = enrichmentPass ? 1 : 0
        if (c.role == "membrane" || c.role == "cyto") {
          row[m + "_ring_fraction_above_threshold"] = supportStats.fraction
          row[m + "_minimum_ring_fraction"] = minFraction
        }
        row[m + "_pattern_pos"] = c.role == "apical_cilia" ?
                                   (componentContextPass ? 1 : 0) :
                                   ((fractionPass && connectedPass) ? 1 : 0)
        row[m + "_compartment_consistent"] = expectedCompartments.isEmpty() ? 1 :
                                              (compartmentAssigned ? (compartmentPass ? 1 : 0) : "")

        if (intensityPos) {
          posCount[m] = posCount[m] + 1
        }

        boolean morphologyPass = markerEvidencePass &&
                                 (compartmentPass || contextUnresolvedPositive)
        def finalCall = ""
        String callStatus
        def failureReasons = []
        if (c.role == "apical_cilia") {
          if (!componentOwnershipPass) failureReasons << "no_unique_apical_ciliary_component"
          if (!fractionPass) failureReasons << "insufficient_spatial_coverage"
          if (!connectedPass) failureReasons << "fragmented_spatial_pattern"
        } else {
          if (!fractionPass) failureReasons << "insufficient_spatial_coverage"
          if (!connectedPass) failureReasons << "fragmented_spatial_pattern"
        }
        if (!enrichmentPass) failureReasons << (c.role == "nuc_ratio" ? "nuc_cyto_ratio_below_minimum" : "nuclear_enrichment_below_minimum")

        // Evaluability always precedes the selected decision authority. The
        // former ordering allowed legacy mean intensity to turn unresolved
        // anatomy, invalid projection, or shared support into false negatives.
        if (!evaluable) {
          finalCall = ""
          callStatus = "indeterminate"
          indeterminateCount[m] = indeterminateCount[m] + 1
        } else if (!cfg.morphologyPrimary) {
          finalCall = intensityPos ? 1 : 0
          callStatus = intensityPos ?
            (contextUnresolvedPositive ?
              "legacy_intensity_positive_context_unresolved" :
              "legacy_intensity_positive") :
            "legacy_intensity_negative"
        } else {
          finalCall = morphologyPass ? 1 : 0
          boolean fixedThreshold = chThreshSource[m] == "fixed_predeclared"
          callStatus = morphologyPass ?
                       (contextUnresolvedPositive ?
                        (c.role == "apical_cilia" ?
                         "exploratory_positive_cellular_context" :
                         "exploratory_positive_context_unresolved") :
                        (fixedThreshold ? "positive" : "exploratory_positive")) :
                       (fixedThreshold ? "negative" : "exploratory_negative")
        }
        if (markerEvidencePass) {
          markerEvidencePosCount[m] = markerEvidencePosCount[m] + 1
          if (knownWrongCompartment) {
            contextExcludedEvidencePosCount[m] =
              contextExcludedEvidencePosCount[m] + 1
          }
        }
        if (contextUnresolvedPositive && finalCall == 1) {
          contextUnresolvedPosCount[m] = contextUnresolvedPosCount[m] + 1
        }
        if (finalCall == 1) {
          finalPosCount[m] = finalPosCount[m] + 1
          finalPositiveRois[m] << nuc
          if (!intensityPos) rawNegFinalPosCount[m] = rawNegFinalPosCount[m] + 1
        } else if (finalCall == 0) {
          finalNegCount[m] = finalNegCount[m] + 1
          if (intensityPos) rawPosFinalNegCount[m] = rawPosFinalNegCount[m] + 1
        } else {
          indeterminateRois[m] << nuc
        }
        calls[m] = finalCall
        callContextResolved[m] = !hasCompartmentRequirement || compartmentPass
        row[m + "_morphology_pass"] = (evaluable && morphologyPass) ? 1 : 0
        row[m + "_negative_eligible"] =
          (evaluable && !authorityPositiveEvidence &&
           (!hasCompartmentRequirement || compartmentPass)) ? 1 : 0
        row[m + "_final_call"] = finalCall
        row[m + "_true_pos"] = finalCall       // compatibility alias
        row[m + "_call_status"] = callStatus
        def callReasons = (indeterminateReasons + failureReasons).unique()
        if (contextUnresolvedPositive && finalCall == 1) {
          callReasons << (c.role == "apical_cilia" ?
            "unique_apical_ciliary_component_context_without_airway_roi" :
            "strict_marker_evidence_with_anatomical_context_unresolved")
        }
        row[m + "_call_reason"] = callReasons.unique().join(";")
      }
      // classifications
      panelDef.classify.each { rule ->
        def key = rule.collect { mk, want -> mk + (want ? "+" : "-") }.join("_")
        // A marker-positive call may be retained when its anatomical context is
        // unresolved, but it cannot authorize a compound lineage/state class.
        boolean classEvaluable = rule.every { mk, want ->
          (calls[mk] == 0 || calls[mk] == 1) && callContextResolved[mk] != false
        }
        if (!classEvaluable) {
          row["class_" + key] = ""
          row["class_" + key + "_status"] = "indeterminate"
        } else {
          boolean ok = rule.every { mk, want -> calls[mk] == (want ? 1 : 0) }
          row["class_" + key] = ok ? 1 : 0
          row["class_" + key + "_status"] = ok ? "positive" : "negative"
          classEvaluableCount[key] = classEvaluableCount[key] + 1
          if (ok) classCount[key] = classCount[key] + 1
        }
      }
      cellRows << row
    }
    logStage("per_cell_decisions")

    // ---- QC overlay for this region ----
    def qc = buildQcOverlay(markerImg, panelDef, region, allNucRois, qcMasks,
                            displayImages.isEmpty() ? null : displayImages)
    transientImages << qc
    def qcPath = imgOut.getAbsolutePath() + "/" + fileKey + "__" + regFileToken + "__QC.png"
    IJ.saveAs(qc, "PNG", qcPath); qc.close()
    def dapiQc = buildDapiQc(dapi, region, allNucRois, rejectedNuclei, cfg)
    transientImages << dapiQc
    IJ.saveAs(dapiQc, "PNG", imgOut.getAbsolutePath() + "/" + fileKey + "__" + regFileToken + "__DAPI_QC.png")
    dapiQc.close()
    cellChannels.each { c ->
      def callQc = buildCallDecisionQc(dapi, region, allNucRois,
                                        finalPositiveRois[c.marker], indeterminateRois[c.marker],
                                        c.marker, chThreshSource[c.marker], cfg)
      transientImages << callQc
      IJ.saveAs(callQc, "PNG", imgOut.getAbsolutePath() + "/" + fileKey + "__" + regFileToken + "__" + c.marker + "_CALL_QC.png")
      callQc.close()
    }
    if (segmentation.candidateMask != null) {
      IJ.saveAs(segmentation.candidateMask, "Tiff",
                imgOut.getAbsolutePath() + "/" + fileKey + "__" + regFileToken + "__DAPI_candidate_mask.tif")
      segmentation.candidateMask.close()
    }

    // ---- region summary row ----
    def nucleusCandidateTotal = nuclei.size() + rejectedNuclei.size()
    def rejectedBelowMin = rejectedNuclei.count { it.reason == "area_below_minimum" }
    def rejectedAtEdge = rejectedNuclei.count { it.reason == "image_edge" }
    def rejectedByParticle = rejectedNuclei.count { it.reason == "particle_filter" }
    def primaryEndpointChannel =
      panelDef.channels.find { it.primaryEndpoint == true }
    def srow = [ image: sourceStem, output_key: outputKey, panel: panelKey, region: regName,
                 mouse_id: meta.mouse_id, section_id: meta.section_id,
                 genotype: meta.genotype, condition: meta.condition, compartment: compartment,
                 region_tags: regionTags.join("|"),
                 primary_endpoint_marker:
                   (primaryEndpointChannel != null ?
                     primaryEndpointChannel.marker : ""),
                 primary_endpoint_channel:
                   (primaryEndpointChannel != null ?
                     primaryEndpointChannel.idx : ""),
                 region_area_um2: regionAreaUm2,
                  dapi_segmentation_method: cfg.dapiMethod,
                  n_nuclei: nuclei.size(),
                  n_rejected_nucleus_candidates: rejectedNuclei.size(),
                  n_rejected_below_min_area: rejectedBelowMin,
                  n_rejected_at_image_edge: rejectedAtEdge,
                  n_rejected_by_particle_filter: rejectedByParticle,
                  n_nucleus_candidates_total: nucleusCandidateTotal,
                  nucleus_candidate_acceptance_fraction: (nucleusCandidateTotal > 0 ? nuclei.size() / (double)nucleusCandidateTotal : 0),
                  nucleus_candidate_rejection_fraction: (nucleusCandidateTotal > 0 ? rejectedNuclei.size() / (double)nucleusCandidateTotal : 0),
                  rejected_below_min_fraction_of_rejected: (!rejectedNuclei.isEmpty() ? rejectedBelowMin / (double)rejectedNuclei.size() : 0),
                  rejected_edge_fraction_of_rejected: (!rejectedNuclei.isEmpty() ? rejectedAtEdge / (double)rejectedNuclei.size() : 0),
                  rejected_particle_filter_fraction_of_rejected: (!rejectedNuclei.isEmpty() ? rejectedByParticle / (double)rejectedNuclei.size() : 0) ]
    cellChannels.each { c ->
      // The conventional summary fields follow the authoritative final call.
      // Raw object-mean decisions remain available under explicit audit names.
      srow[c.marker + "_pos_count"] = finalPosCount[c.marker]
      srow[c.marker + "_density_per_mm2"] = (regionAreaUm2 > 0 ? finalPosCount[c.marker] / (regionAreaUm2/1e6) : 0)
      srow[c.marker + "_raw_mean_pos_count"] = posCount[c.marker]
      srow[c.marker + "_raw_mean_density_per_mm2"] = (regionAreaUm2 > 0 ? posCount[c.marker] / (regionAreaUm2/1e6) : 0)
      srow[c.marker + "_pos_threshold"] = chThresh[c.marker]   // resolved raw-intensity cutoff
      srow[c.marker + "_threshold_source"] = chThreshSource[c.marker]
      srow[c.marker + "_candidate_threshold_sensitivity"] =
        chThreshSensitivity[c.marker]
      srow[c.marker + "_is_primary_endpoint"] =
        c.primaryEndpoint == true
      srow[c.marker + "_measurement_model"] = c.measurement ?: c.role
      srow[c.marker + "_call_authority"] = cfg.morphologyPrimary ? "morphology_primary" : "legacy_mean_intensity"
      srow[c.marker + "_morphology_pos_count"] = finalPosCount[c.marker]
      srow[c.marker + "_morphology_negative_count"] = finalNegCount[c.marker]
      srow[c.marker + "_indeterminate_count"] = indeterminateCount[c.marker]
      // Explicit, human-readable final quantification columns. These repeat
      // the authoritative three-state counts with an unambiguous denominator
      // so the CSV and Excel workbook can be read without reconstructing the
      // endpoint from audit fields.
      srow[c.marker + "_final_positive_cell_count"] = finalPosCount[c.marker]
      srow[c.marker + "_final_positive_fraction_of_total_cells"] =
        (!nuclei.isEmpty() ? finalPosCount[c.marker] / (double)nuclei.size() : 0)
      srow[c.marker + "_final_negative_cell_count"] = finalNegCount[c.marker]
      srow[c.marker + "_final_negative_fraction_of_total_cells"] =
        (!nuclei.isEmpty() ? finalNegCount[c.marker] / (double)nuclei.size() : 0)
      srow[c.marker + "_final_indeterminate_cell_count"] = indeterminateCount[c.marker]
      srow[c.marker + "_final_indeterminate_fraction_of_total_cells"] =
        (!nuclei.isEmpty() ? indeterminateCount[c.marker] / (double)nuclei.size() : 0)
      srow[c.marker + "_marker_evidence_pos_count"] =
        markerEvidencePosCount[c.marker]
      srow[c.marker + "_context_unresolved_positive_count"] =
        contextUnresolvedPosCount[c.marker]
      srow[c.marker + "_context_excluded_evidence_positive_count"] =
        contextExcludedEvidencePosCount[c.marker]
      def evaluableCount = finalPosCount[c.marker] + finalNegCount[c.marker]
      def contextResolvedPosCount =
        finalPosCount[c.marker] - contextUnresolvedPosCount[c.marker]
      def contextResolvedEvaluableCount =
        contextResolvedPosCount + finalNegCount[c.marker]
      def discordantCount = rawPosFinalNegCount[c.marker] + rawNegFinalPosCount[c.marker]
      def reviewBurdenCount = indeterminateCount[c.marker] + discordantCount
      srow[c.marker + "_morphology_evaluable_count"] = evaluableCount
      srow[c.marker + "_morphology_positive_fraction_of_evaluable"] =
        (evaluableCount > 0 ? finalPosCount[c.marker] / (double)evaluableCount : 0)
      srow[c.marker + "_morphology_negative_fraction_of_evaluable"] =
        (evaluableCount > 0 ? finalNegCount[c.marker] / (double)evaluableCount : 0)
      srow[c.marker + "_context_resolved_positive_count"] =
        contextResolvedPosCount
      srow[c.marker + "_context_resolved_evaluable_count"] =
        contextResolvedEvaluableCount
      srow[c.marker + "_context_resolved_positive_fraction"] =
        (contextResolvedEvaluableCount > 0 ?
          contextResolvedPosCount / (double)contextResolvedEvaluableCount : 0)
      srow[c.marker + "_context_resolved_positive_fraction_of_total_cells"] =
        (!nuclei.isEmpty() ?
          contextResolvedPosCount / (double)nuclei.size() : 0)
      srow[c.marker + "_context_unresolved_positive_fraction_of_included"] =
        (!nuclei.isEmpty() ?
          contextUnresolvedPosCount[c.marker] / (double)nuclei.size() : 0)
      srow[c.marker + "_indeterminate_fraction_of_included"] =
        (!nuclei.isEmpty() ? indeterminateCount[c.marker] / (double)nuclei.size() : 0)
      srow[c.marker + "_raw_positive_final_negative_count"] = rawPosFinalNegCount[c.marker]
      srow[c.marker + "_raw_negative_final_positive_count"] = rawNegFinalPosCount[c.marker]
      srow[c.marker + "_intensity_morphology_discordant_count"] = discordantCount
      srow[c.marker + "_intensity_morphology_discordant_fraction_of_evaluable"] =
        (evaluableCount > 0 ? discordantCount / (double)evaluableCount : 0)
      srow[c.marker + "_review_burden_proxy_count"] = reviewBurdenCount
      srow[c.marker + "_review_burden_proxy_fraction_of_included"] =
        (!nuclei.isEmpty() ? reviewBurdenCount / (double)nuclei.size() : 0)
      srow[c.marker + "_morphology_density_per_mm2"] =
        (regionAreaUm2 > 0 ? finalPosCount[c.marker] / (regionAreaUm2/1e6) : 0)
      srow[c.marker + "_true_pos_count"] = finalPosCount[c.marker] // compatibility alias
      def expectedForSummary = expectedCompartmentsFor(c)
      srow[c.marker + "_expected_compartment"] = expectedForSummary.isEmpty() ? "none" : expectedForSummary.join("|")
      srow[c.marker + "_context_policy"] = expectedForSummary.isEmpty() ?
        "not_required" :
        "asymmetric_positive_evidence_negative_requires_compartment"
      if (c.role == "apical_cilia") {
        def assignment = apicalComponentAssignments[c.marker]
        def ownedNucleusCount = assignment == null ? 0 :
          assignment.by_nucleus.values().count { it != null && !it.isEmpty() }
        srow[c.marker + "_nuclei_with_owned_ciliary_component"] = ownedNucleusCount
        srow[c.marker + "_assigned_ciliary_component_count"] =
          assignment == null ? 0 : assignment.assigned_component_count
        srow[c.marker + "_unassigned_ciliary_component_count"] =
          assignment == null ? 0 : assignment.unassigned_component_count
        srow[c.marker + "_maximum_component_distance_um"] = cfg.actubMaxComponentDistanceUm
        srow[c.marker + "_minimum_component_boundary_distance_um"] =
          cfg.actubMinComponentBoundaryDistanceUm
        srow[c.marker + "_cellular_context_model"] =
          "unique_apical_component_plus_local_coverage_asymmetric_context"
      }
    }
    areaStats.each { m, as ->
      srow[m + "_positive_area_um2"] = as.area_um2
      srow[m + "_positive_area_frac"] = as.frac_of_region
      srow[m + "_n_components"] = as.n_components
      srow[m + "_mean_component_area_um2"] = as.mean_component_area_um2
      srow[m + "_min_component_area_um2"] = as.min_component_area_um2
      srow[m + "_max_component_area_um2"] = as.max_component_area_um2
      srow[m + "_area_mask_model"] = as.mask_model
      srow[m + "_area_threshold"] = as.threshold
      srow[m + "_area_mode"] = as.mode
      srow[m + "_area_threshold_source"] = as.threshold_source
      def areaChannel = panelDef.channels.find { it.marker == m }
      def areaExpectedCompartments = areaChannel == null ? [] : expectedCompartmentsFor(areaChannel)
      if (!areaExpectedCompartments.isEmpty() &&
          (compartment == "unassigned" || compartment == "ambiguous")) {
        srow[m + "_area_call_status"] = as.area_um2 > 0 ?
          "positive_area_evidence_context_unresolved" :
          "indeterminate_context_unresolved"
      } else if (!areaExpectedCompartments.isEmpty() &&
                 !areaExpectedCompartments.any { regionTags.contains(it) }) {
        srow[m + "_area_call_status"] = as.area_um2 > 0 ?
          "context_excluded_positive_area_evidence" :
          "wrong_compartment_not_interpretable"
      } else {
        srow[m + "_area_call_status"] = as.threshold_source == "fixed_predeclared" ?
                                         "fixed_threshold_area" :
                                         "exploratory_adaptive_threshold"
      }
      // Keep the historical KRT5 pod fields for downstream compatibility.
      if (as.mode == "pod") {
        srow[m + "_pod_area_um2"] = as.area_um2
        srow[m + "_pod_area_frac"] = as.frac_of_region
        srow[m + "_n_pods"] = as.n_components
        srow[m + "_mean_pod_area_um2"] = as.mean_component_area_um2
        srow[m + "_pod_threshold"] = as.threshold
      }
    }
    // Emit every declared class even when no cell is context-resolved enough
    // to evaluate it. Omitting an all-indeterminate class can be misread as
    // "not tracked" or zero positive.
    panelDef.classify.each { rule ->
      def k = rule.collect { mk, want -> mk + (want ? "+" : "-") }.join("_")
      srow["class_" + k + "_count"] = classCount[k]
      srow["class_" + k + "_evaluable_count"] = classEvaluableCount[k]
      srow["class_" + k + "_indeterminate_count"] =
        nuclei.size() - classEvaluableCount[k]
    }
    summaryRows << srow
    qcMasks.each { k, v -> v.close() }

    // save nuclei mask for the region
    saveLabelMask(dapi, allNucRois, imgOut.getAbsolutePath() + "/" + fileKey + "__" + regFileToken + "__nuclei_mask.tif")
    saveLabelMask(dapi, rejectedNuclei.collect { it.roi }, imgOut.getAbsolutePath() + "/" + fileKey + "__" + regFileToken + "__rejected_nuclei_mask.tif")
    cellChannels.each { c ->
      saveLabelMask(dapi, finalPositiveRois[c.marker],
                    imgOut.getAbsolutePath() + "/" + fileKey + "__" + regFileToken + "__" + c.marker + "_morphology_positive_nuclei_mask.tif")
      saveLabelMask(dapi, indeterminateRois[c.marker],
                    imgOut.getAbsolutePath() + "/" + fileKey + "__" + regFileToken + "__" + c.marker + "_indeterminate_nuclei_mask.tif")
    }
  }

  // Save morphology-specific binary masks with names that describe the unit.
  panelDef.channels.findAll { it.areaMarker }.each { c ->
    def mask = areaMasks[c.marker]
    String areaMode = c.areaMode ?: "pod"
    String suffix = areaMode == "pod" ? "pod_mask" :
                    (areaMode == "ciliary" ? "ciliary_mask" :
                     (areaMode == "membrane" ? "membrane_positive_mask" :
                      (areaMode == "reporter" ? "reporter_positive_mask" : "positive_area_mask")))
    IJ.saveAs(mask, "Tiff", imgOut.getAbsolutePath() + "/" + fileKey + "__" + c.marker + "_" + suffix + ".tif")
  }

  // per-image params/provenance
  def params = [
    image: new File(imgPath).name, output_key: outputKey, channel_signature: channelSignature,
    panel: panelKey, panel_label: panelDef.label,
    calibration: [ pixel_width_um: cal.pixelWidth, pixel_height_um: cal.pixelHeight,
                   pixel_depth_um: cal.pixelDepth, unit: cal.getUnit(),
                   n_slices: raw.getNSlices(), n_channels: channels.length ],
    projection: cfg.projection, single_plane: cfg.singlePlane,
    z_handling: [
      mode: cfg.projection == "layer_aware" ? "layer_aware_2_5d" : "legacy_global_projection",
      nuclear_range_setting: cfg.zNuclearRange,
      cell_body_range_setting: cfg.zCellBodyRange,
      apical_range_setting: cfg.zApicalRange,
      cell_body_auto_planes: cfg.zCellBodyPlanes,
      apical_auto_planes: cfg.zApicalPlanes,
      resolved_marker_ranges: zProfileSummary,
      voxel_anisotropy_z_to_xy: (raw.getNSlices() > 1 && cal.pixelWidth > 0.0d ?
                                  cal.pixelDepth / cal.pixelWidth : 1.0d),
      limitation: "Layer-aware mode uses restricted 2D slab projections; it does not claim true 3D cell-boundary reconstruction."
    ],
    display_enhancement: [
      enabled: cfg.exportDisplayChannels,
      authority: "visualization_only_not_quantification",
      quantitative_source: "original calibrated marker projections in markerImg",
      processing: "duplicate projection, percentile display stretch, 8-bit conversion, optional gamma",
      global_low_percentile: cfg.displayLowPercentile,
      global_high_percentile: cfg.displayHighPercentile,
      global_gamma: cfg.displayGamma,
      resolved_channels: displaySettings,
      warning: "Enhanced PNGs and enhanced QC backgrounds must not be used for intensity measurement or threshold calibration."
    ],
    segmenter: cfg.segmenter, stardist_prob: cfg.prob, stardist_nms: cfg.nms, stardist_tiles: cfg.tiles,
    dapi_preprocessing: [ method: cfg.dapiMethod,
                          method_source: cfg.dapiMethodSource,
                          background_radius_um: cfg.dapiBackgroundRadiusUm,
                          local_radius_um: cfg.dapiLocalRadiusUm,
                          blur_sigma_px: cfg.dapiBlurSigmaPx,
                          contrast_saturation_percent: cfg.dapiContrastSaturation ],
    ring_expand_um: cfg.ringExpandUm, min_nucleus_area_um2: cfg.minNucArea,
    acetylated_tubulin_model: [ measurement: "apical_cilia_proximity_and_regional_patches",
                                biological_specificity_note: "acetylated tubulin also labels stable cytoplasmic microtubules; only the apical tuft mask is authoritative",
                                support_expand_um: cfg.actubSupportExpandUm,
                                minimum_support_positive_fraction: cfg.actubMinSupportFraction,
                                minimum_ciliary_patch_area_um2: cfg.actubMinPatchAreaUm2,
                                maximum_ciliary_patch_area_um2: cfg.actubMaxPatchAreaUm2,
                                ciliary_seed_percentile: cfg.actubCiliaSeedPercentile,
                                ciliary_density_radius_um: cfg.actubCiliaDensityRadiusUm,
                                minimum_ciliary_local_density: cfg.actubCiliaMinLocalDensity,
                                mask_model: "high_intensity_local_density_bounded_apical_tuft",
                                cellular_context_model: "unique_apical_component_plus_local_coverage",
                                maximum_component_centroid_distance_um: cfg.actubMaxComponentDistanceUm,
                                minimum_component_boundary_distance_um: cfg.actubMinComponentBoundaryDistanceUm,
                                maximum_component_boundary_distance_um: cfg.actubSupportExpandUm,
                                decision_asymmetry: "positive component context allowed without airway ROI; negative requires independently assigned airway ROI" ],
    pod_min_area_um2: cfg.podMinArea, pod_blur_sigma_px: cfg.podBlur, pod_thresh_method: cfg.podMethod,
    pos_sensitivity: cfg.sensitivity, black_background: cfg.blackBackground,
    fixed_pos_thresholds: cfg.fixedThresholds,
    decision_hierarchy: [ authority: cfg.morphologyPrimary ? "morphology_primary" : "legacy_mean_intensity",
                          call_states: ["positive", "negative", "indeterminate"],
                          intensity_role: "candidate-pixel threshold and audit field; not final-call authority",
                          fixed_threshold_requirement: "confirmatory calls require predeclared control-derived thresholds",
                          evaluability_precedes_authority: true,
                          compartment_policy: "strict marker evidence may be retained as context-unresolved positive; negative requires compatible compartment; known incompatible compartment remains indeterminate",
                          compound_class_policy: "context-unresolved marker positives cannot authorize compound lineage/state classes" ],
    morphology_rules: cfg.morphologyRules,
    role_morphology_defaults: cfg.roleMorphologyDefaults,
    marker_registry: [path:cfg.markerRegistryPath, schema_version:cfg.markerRegistrySchema],
    custom_panel_config: cfg.panelConfigPath,
    custom_panel_keys: cfg.customPanelKeys,
    compartment_mode: cfg.compartmentMode,
    whole_field_compartment: cfg.wholeFieldCompartment,
    minimum_ring_positive_fraction: cfg.minRingPosFraction,
    tissue_mode: cfg.tissueMode, tissue_roi_source: tissue.source,
    tissue_thresh_method: cfg.tissueMethod,
    rejected_nucleus_rules: [minimum_area_um2: cfg.minNucArea,
                             minimum_included_nuclei_per_region: cfg.minIncludedNuclei,
                             exclude_image_edge: true],
    channel_map: panelDef.channels
  ]
  new File(imgOut, fileKey + "__params.json").setText(
    JsonOutput.prettyPrint(JsonOutput.toJson(params)), "UTF-8")
  writeCsv(zProfileRows, imgOut.getAbsolutePath() + "/" +
           fileKey + "__z_plane_profile.csv")

  // write per-image cell CSV
  writeCsv(cellRows, imgOut.getAbsolutePath() + "/" + fileKey + "__cells.csv")

  return [summary: summaryRows, cells: cellRows.size(), tissue_source: tissue.source,
          channel_signature: channelSignature]
  } finally {
    // Z projection creates new images while leaving the split stacks open.
    // Close every object by identity on success, early return, or exception so
    // one bad field cannot exhaust memory and corrupt the remainder of a batch.
    def seen = java.util.Collections.newSetFromMap(new java.util.IdentityHashMap())
    def resources = []
    resources.addAll(markerImg.values())
    resources.addAll(displayImages.values())
    resources.addAll(areaMasks.values())
    resources.addAll(transientImages)
    resources.addAll(channels ?: [])
    if (raw != null) resources << raw
    resources.findAll { it != null }.each { imp ->
      if (seen.add(imp)) {
        try { imp.changes = false; imp.close() } catch (Throwable ignored) { }
      }
    }
  }
}

// ============================================================================
//  6. OUTPUT HELPERS
// ============================================================================

def markerOutlineColor(String marker) {
  switch (marker) {
    case "T1A": case "CC10": return new Color(0, 255, 0)
    case "tdTOM": return new Color(255, 40, 40)
    case "mRAGE": case "AcTub": return Color.WHITE
    case "KRT5": return Color.MAGENTA
    default: return new Color(255, 180, 0)
  }
}

def displayPercentileBounds(ImagePlus imp, double lowPercentile,
                            double highPercentile) {
  ImageProcessor ip = imp.getProcessor()
  int[] histogram = ip.getHistogram()
  long total = 0L
  histogram.each { total += it as long }
  def rawStats = ip.getStatistics()
  double rawMin = rawStats.min as double
  double rawMax = rawStats.max as double
  if (total <= 0L || rawMax <= rawMin) {
    return [low:rawMin, high:(rawMax > rawMin ? rawMax : rawMin + 1.0d)]
  }
  long lowRank = (long)Math.floor((lowPercentile / 100.0d) * (total - 1L))
  long highRank = (long)Math.floor((highPercentile / 100.0d) * (total - 1L))
  def findBin = { long rank ->
    long cumulative = 0L
    for (int i = 0; i < histogram.length; i++) {
      cumulative += histogram[i] as long
      if (cumulative > rank) return i
    }
    return histogram.length - 1
  }
  int lowBin = findBin(lowRank) as int
  int highBin = findBin(highRank) as int
  def binToValue = { int bin ->
    if ((imp.getBitDepth() == 8 && histogram.length == 256) ||
        (imp.getBitDepth() == 16 && histogram.length == 65536)) {
      return bin as double
    }
    return rawMin + (bin / Math.max(1.0d, histogram.length - 1.0d)) *
                    (rawMax - rawMin)
  }
  double low = binToValue(lowBin) as double
  double high = binToValue(highBin) as double
  if (!Double.isFinite(low) || !Double.isFinite(high) || high <= low) {
    low = rawMin
    high = rawMax > rawMin ? rawMax : rawMin + 1.0d
  }
  return [low:low, high:high]
}

def buildDisplayChannel(ImagePlus source, String marker, double lowPercentile,
                        double highPercentile, double gamma,
                        Double lowIntensity = null, Double highIntensity = null,
                        double backgroundRadiusPx = 0.0d,
                        double blackPoint = 0.0d,
                        double medianRadiusPx = 0.0d) {
  ImageProcessor work = source.getProcessor().duplicate()
  if (backgroundRadiusPx > 0.0d) {
    new BackgroundSubtracter().rollingBallBackground(
      work, backgroundRadiusPx, false, false, false, true, true)
  }
  // An optional median filter suppresses isolated hot pixels without broadly
  // blurring membrane morphology. It operates only on this disposable display
  // branch and therefore cannot affect masks, thresholds, or measurements.
  if (medianRadiusPx > 0.0d) {
    new RankFilters().rank(work, medianRadiusPx, RankFilters.MEDIAN)
  }
  def windowImage = new ImagePlus(marker + "_DISPLAY_WINDOW", work)
  def bounds = (lowIntensity != null && highIntensity != null) ?
    [low:lowIntensity as double, high:highIntensity as double] :
    displayPercentileBounds(windowImage, lowPercentile, highPercentile)
  work.setMinAndMax(bounds.low as double, bounds.high as double)
  ImageProcessor byteIp = work.convertToByte(true)
  windowImage.close()
  if (Math.abs(gamma - 1.0d) > 1.0e-9d) byteIp.gamma(gamma)
  if (blackPoint > 0.0d) {
    byte[] pixels = byteIp.getPixels() as byte[]
    double floor = 255.0d * blackPoint
    double scale = 255.0d / (255.0d - floor)
    for (int i = 0; i < pixels.length; i++) {
      int v = pixels[i] & 0xff
      int adjusted = v <= floor ? 0 :
        Math.max(0, Math.min(255, Math.round((v - floor) * scale) as int))
      pixels[i] = (byte)(adjusted & 0xff)
    }
  }
  def out = new ImagePlus(marker + "_DISPLAY_ONLY", byteIp)
  out.setCalibration(source.getCalibration())
  return [image:out, low_intensity:bounds.low, high_intensity:bounds.high,
          low_percentile:lowPercentile, high_percentile:highPercentile,
          gamma:gamma,
          background_radius_px:backgroundRadiusPx,
          black_point:blackPoint,
          median_radius_px:medianRadiusPx,
          window_source:(lowIntensity != null ? "absolute" : "percentile")]
}

def labelDisplayOnlyExport(ImagePlus source, String label,
                            boolean visualMergePanel = false,
                            double scaleBarUm = 0.0d,
                            int scaleBarThicknessPx = 6) {
  ColorProcessor cp = source.getProcessor().convertToRGB() as ColorProcessor
  int bannerHeight = Math.min(30, Math.max(18, cp.getHeight()))
  cp.setColor(Color.BLACK)
  cp.setRoi(0, 0, cp.getWidth(), bannerHeight)
  cp.fill()
  cp.resetRoi()
  cp.setColor(Color.WHITE)
  cp.setFont(new Font("SansSerif", Font.BOLD, 13))
  cp.drawString((visualMergePanel ?
    "VISUAL MERGE PANEL - NOT QUANTIFIED | " :
    "DISPLAY ONLY - NOT QUANTIFIED | ") + label, 8, 19)
  if (visualMergePanel && scaleBarUm > 0.0d) {
    Calibration cal = source.getCalibration()
    double pixelWidthUm = cal == null ? Double.NaN : cal.pixelWidth
    String unit = cal == null ? "" : (cal.getUnit() ?: "").toLowerCase()
    boolean micrometreUnit = unit in ["um", "\u00b5m", "\u03bcm", "micron", "microns",
                                      "micrometer", "micrometers", "micrometre", "micrometres"]
    if (!Double.isFinite(pixelWidthUm) || pixelWidthUm <= 0.0d || !micrometreUnit) {
      throw new IllegalArgumentException(
        "Cannot draw visual-merge scale bar without positive micrometre calibration; found " +
        pixelWidthUm + " " + unit)
    }
    int barWidthPx = Math.max(1, Math.round(scaleBarUm / pixelWidthUm) as int)
    if (barWidthPx >= cp.getWidth() - 48) {
      throw new IllegalArgumentException(
        "Requested visual-merge scale bar (" + scaleBarUm + " um) is too wide for " +
        cp.getWidth() + " px at " + pixelWidthUm + " um/px")
    }
    int thickness = Math.max(2, Math.min(20, scaleBarThicknessPx))
    int margin = Math.max(18, Math.round(cp.getWidth() * 0.018d) as int)
    int barX = cp.getWidth() - margin - barWidthPx
    int barY = cp.getHeight() - margin - thickness
    int fontSize = Math.max(24, Math.min(44, Math.round(cp.getWidth() / 49.0d) as int))
    String scaleLabel = (Math.rint(scaleBarUm) == scaleBarUm ?
      Integer.toString(scaleBarUm as int) : Double.toString(scaleBarUm)) + " \u00b5m"
    cp.setAntialiasedText(true)
    cp.setFont(new Font("SansSerif", Font.BOLD, fontSize))
    int labelWidth = cp.getStringWidth(scaleLabel)
    int labelX = Math.max(margin, barX + barWidthPx - labelWidth)
    int labelY = barY - Math.max(8, thickness)

    // One-pixel black keyline and text shadow preserve visibility without the
    // thick boxed appearance of the former PowerPoint shapes.
    cp.setColor(Color.BLACK)
    cp.setRoi(barX - 1, barY - 1, barWidthPx + 2, thickness + 2)
    cp.fill(); cp.resetRoi()
    cp.drawString(scaleLabel, labelX + 1, labelY + 1)
    cp.setColor(Color.WHITE)
    cp.setRoi(barX, barY, barWidthPx, thickness)
    cp.fill(); cp.resetRoi()
    cp.drawString(scaleLabel, labelX, labelY)
  }
  def labeled = new ImagePlus(source.getTitle() + "_labeled", cp)
  labeled.setCalibration(source.getCalibration())
  return labeled
}

// Additively merge display-normalized channels into a headless-safe RGB image.
// For panel R: DAPI=blue, T1A=green, tdTOM=red, mRAGE=white.
def buildQcComposite(markerImg, panelDef, displayImages = null) {
  def sourceImages = displayImages ?: markerImg
  def first = sourceImages.values().iterator().next()
  int w = first.getWidth(), h = first.getHeight()
  def layers = panelDef.channels.collect { c ->
    def ip = sourceImages[c.marker].getProcessor().duplicate()
    if (displayImages == null) ip.resetMinAndMax()
    return [color: (c.qcColor ?: "white"),
            weight: (c.displayWeight != null ? c.displayWeight as double : 1.0d),
            ip:(displayImages == null ? ip.convertToByte(true) : ip)]
  }
  def out = new ColorProcessor(w, h)
  for (int y = 0; y < h; y++) {
    for (int x = 0; x < w; x++) {
      int rr = 0, gg = 0, bb = 0
      layers.each { layer ->
        int v = Math.max(0, Math.min(255,
          Math.round(layer.ip.get(x, y) * (layer.weight as double)) as int))
        switch (layer.color) {
          case "red": rr = Math.max(rr, v); break
          case "green": gg = Math.max(gg, v); break
          case "blue": bb = Math.max(bb, v); break
          default:
            rr = Math.max(rr, v); gg = Math.max(gg, v); bb = Math.max(bb, v)
        }
      }
      out.set(x, y, (rr << 16) | (gg << 8) | bb)
    }
  }
  def composite = new ImagePlus("four_channel_QC", out)
  composite.setCalibration(first.getCalibration())
  return composite
}

def buildQcOverlay(markerImg, panelDef, Roi region, nucRois, channelMasks,
                   displayImages = null) {
  def rgb = buildQcComposite(markerImg, panelDef, displayImages)
  ImageProcessor cp = rgb.getProcessor()

  // Continuous fluorescence-region boundaries replace the former per-cell
  // marker circles. Color follows acquisition: green/red/white.
  panelDef.channels.findAll { it.role != "nuclear" }.each { c ->
    def pm = channelMasks[c.marker].duplicate()
    pm.getProcessor().setThreshold(128, 255, ImageProcessor.NO_LUT_UPDATE)
    def signalRoi = ThresholdToSelection.run(pm)
    cp.setLineWidth(c.marker == "mRAGE" ? 2 : 1)
    cp.setColor(markerOutlineColor(c.marker))
    if (signalRoi != null) {
      def clipped = new ShapeRoi(signalRoi).and(new ShapeRoi(region))
      clipped.drawPixels(cp)
    }
    pm.close()
  }

  // Orange is analysis-region membership; it is never an exclusion symbol.
  cp.setLineWidth(2); cp.setColor(new Color(255, 150, 0)); region.drawPixels(cp)

  // Cyan is the only per-object circle: nuclei included in DAPI-based counts.
  cp.setLineWidth(2); cp.setColor(new Color(0, 220, 255))
  nucRois.each { it.drawPixels(cp) }

  // Burn a compact legend into the exported PNG so colors remain auditable.
  cp.setMask(null); cp.setColor(Color.BLACK); cp.setRoi(0, 0, Math.min(cp.getWidth(), 1190), 64); cp.fill()
  cp.resetRoi(); cp.setMask(null); cp.setFont(new Font("SansSerif", Font.BOLD, 16)); cp.setColor(Color.WHITE)
  def rawLegend = panelDef.channels.collect { c -> c.marker + " " + (c.qcColor ?: "gray") }.join(" | ")
  cp.drawString((displayImages == null ? "RAW DISPLAY: " : "DISPLAY-NORMALIZED: ") +
                rawLegend, 10, 22)
  cp.drawString("OUTLINES: counted DAPI nuclei cyan | ROI orange | thresholded marker regions", 10, 48)
  return rgb
}

// DAPI-only audit view. Contrast balancing affects this PNG only; segmentation
// uses the separately recorded preprocessing path and candidate mask.
def buildDapiQc(ImagePlus dapi, Roi region, nucRois, rejectedNuclei, cfg) {
  def display = dapi.duplicate()
  IJ.run(display, "Enhance Contrast...", "saturated=" + cfg.dapiContrastSaturation + " normalize")
  if (display.getBitDepth() != 8) IJ.run(display, "8-bit", "")
  def rgb = new ImagePlus("DAPI_segmentation_QC", display.getProcessor().convertToRGB())
  display.close()
  ImageProcessor cp = rgb.getProcessor()
  cp.setLineWidth(2); cp.setColor(new Color(255, 150, 0)); region.drawPixels(cp)
  cp.setLineWidth(2); cp.setColor(new Color(210, 0, 255))
  rejectedNuclei.each { it.roi.drawPixels(cp) }
  cp.setLineWidth(2); cp.setColor(new Color(0, 220, 255))
  nucRois.each { it.drawPixels(cp) }
  cp.setMask(null); cp.setColor(Color.BLACK); cp.setRoi(0, 0, Math.min(cp.getWidth(), 1050), 58); cp.fill()
  cp.resetRoi(); cp.setMask(null); cp.setFont(new Font("SansSerif", Font.BOLD, 16)); cp.setColor(Color.WHITE)
  cp.drawString("DAPI ONLY (display-balanced): counted cyan | rejected candidate violet | ROI orange", 10, 23)
  cp.drawString("Segmentation method: " + cfg.dapiMethod, 10, 47)
  return rgb
}

// Marker-specific decision audit: negative/evaluable nuclei are cyan, final
// positives are green, and indeterminate nuclei are magenta. This directly
// visualizes the hierarchy that downstream classifications consume.
def buildCallDecisionQc(ImagePlus dapi, Roi region, allNucRois,
                        positiveRois, indeterminateRois,
                        String marker, String thresholdSource, cfg) {
  def display = dapi.duplicate()
  IJ.run(display, "Enhance Contrast...", "saturated=" + cfg.dapiContrastSaturation + " normalize")
  if (display.getBitDepth() != 8) IJ.run(display, "8-bit", "")
  def rgb = new ImagePlus(marker + "_call_QC", display.getProcessor().convertToRGB())
  display.close()
  ImageProcessor cp = rgb.getProcessor()
  cp.setLineWidth(2); cp.setColor(new Color(255, 150, 0)); region.drawPixels(cp)
  cp.setLineWidth(2); cp.setColor(new Color(0, 210, 255)); allNucRois.each { it.drawPixels(cp) }
  cp.setLineWidth(3); cp.setColor(new Color(60, 255, 70)); positiveRois.each { it.drawPixels(cp) }
  cp.setLineWidth(3); cp.setColor(new Color(230, 0, 255)); indeterminateRois.each { it.drawPixels(cp) }
  cp.setMask(null); cp.setColor(Color.BLACK); cp.setRoi(0, 0, Math.min(cp.getWidth(), 1200), 61); cp.fill()
  cp.resetRoi(); cp.setMask(null); cp.setFont(new Font("SansSerif", Font.BOLD, 16)); cp.setColor(Color.WHITE)
  cp.drawString(marker + " FINAL CALLS: positive green | negative cyan | indeterminate magenta | ROI orange", 10, 23)
  cp.drawString("Threshold source: " + thresholdSource + " | authority: morphology", 10, 48)
  return rgb
}

def saveLabelMask(ImagePlus ref, nucRois, String path) {
  // 16-bit labels so >255 nuclei per region do not collide.
  def ip = new ShortProcessor(ref.getWidth(), ref.getHeight())
  nucRois.eachWithIndex { r, i ->
    ip.setValue(i + 1); ip.fill(r)
  }
  def lab = new ImagePlus("labels", ip); lab.setCalibration(ref.getCalibration())
  IJ.saveAs(lab, "Tiff", path); lab.close()
}

def writeCsv(rows, String path) {
  if (rows == null || rows.isEmpty()) { new File(path).setText("", "UTF-8"); return }
  def cols = [] as LinkedHashSet
  rows.each { r -> cols.addAll(r.keySet()) }
  cols = cols as List
  def sb = new StringBuilder()
  sb.append(cols.join(",")).append("\n")
  rows.each { r ->
    sb.append(cols.collect { c ->
      def v = r.containsKey(c) ? r[c] : ""
      def s = (v == null) ? "" : v.toString()
      (s.contains(",") || s.contains("\"") || s.contains("\r") || s.contains("\n")) ?
        "\"" + s.replace("\"", "\"\"") + "\"" : s
    }.join(",")).append("\n")
  }
  new File(path).setText(sb.toString(), "UTF-8")
}

def xlsxXmlEscape = { value ->
  def s = value == null ? "" : value.toString()
  return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
          .replace("\"", "&quot;").replace("'", "&apos;")
}

def xlsxColumnName = { int zeroBased ->
  int value = zeroBased + 1
  def out = new StringBuilder()
  while (value > 0) {
    int remainder = (value - 1) % 26
    out.insert(0, (char)(('A' as char) + remainder))
    value = (int)((value - 1) / 26)
  }
  return out.toString()
}

def xlsxSheetXml = { sheet ->
  def rows = sheet.rows
  def safeRows = rows == null ? [] : rows
  def columns = [] as LinkedHashSet
  safeRows.each { row -> columns.addAll(row.keySet()) }
  def cols = columns as List
  if (cols.isEmpty()) cols = ["message"]

  int lastRow = Math.max(1, safeRows.size() + 1)
  String lastColumn = xlsxColumnName(cols.size() - 1)
  def xml = new StringBuilder()
  xml.append('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>')
  xml.append('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">')
  String tabColor = sheet.name == "Image Positive Counts" ? "FF2F75B5" :
                    (sheet.name == "Skipped Inputs" ? "FFF4B183" : "FF70AD47")
  xml.append('<sheetPr><tabColor rgb="').append(tabColor).append('"/></sheetPr>')
  xml.append('<dimension ref="A1:').append(lastColumn).append(lastRow).append('"/>')
  xml.append('<sheetViews><sheetView workbookViewId="0" showGridLines="0">')
  xml.append('<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>')
  xml.append('</sheetView></sheetViews>')
  xml.append('<cols>')
  cols.eachWithIndex { col, i ->
    int contentLength = safeRows.collect { row ->
      def value = row.containsKey(col) ? row[col] : ""
      value == null ? 0 : value.toString().length()
    }.max() ?: 0
    double maximumWidth = col.toString() == "Image" ? 70.0d : 42.0d
    double width = Math.max(12.0d,
      Math.min(maximumWidth, Math.max(col.toString().length(), contentLength) + 2.0d))
    xml.append('<col min="').append(i + 1).append('" max="').append(i + 1)
       .append('" width="').append(width).append('" customWidth="1"/>')
  }
  xml.append('</cols><sheetData>')
  xml.append('<row r="1" ht="32" customHeight="1">')
  cols.eachWithIndex { col, i ->
    String ref = xlsxColumnName(i) + "1"
    xml.append('<c r="').append(ref).append('" s="1" t="inlineStr"><is><t>')
       .append(xlsxXmlEscape(col)).append('</t></is></c>')
  }
  xml.append('</row>')
  safeRows.eachWithIndex { row, rowIndex ->
    int excelRow = rowIndex + 2
    xml.append('<row r="').append(excelRow).append('">')
    cols.eachWithIndex { col, colIndex ->
      def value = row.containsKey(col) ? row[col] : ""
      String ref = xlsxColumnName(colIndex) + excelRow
      boolean percentage = col.toString().toLowerCase().contains("fraction")
      boolean alternate = rowIndex % 2 == 1
      int style = percentage ? (alternate ? 4 : 2) : (alternate ? 3 : 0)
      if (value instanceof Number) {
        xml.append('<c r="').append(ref).append('"')
        if (style > 0) xml.append(' s="').append(style).append('"')
        xml.append('><v>').append(value.toString()).append('</v></c>')
      } else if (value instanceof Boolean) {
        xml.append('<c r="').append(ref).append('"')
        if (style > 0) xml.append(' s="').append(style).append('"')
        xml.append(' t="b"><v>')
           .append(value ? "1" : "0").append('</v></c>')
      } else {
        xml.append('<c r="').append(ref).append('"')
        if (style > 0) xml.append(' s="').append(style).append('"')
        xml.append(' t="inlineStr"><is><t>')
           .append(xlsxXmlEscape(value)).append('</t></is></c>')
      }
    }
    xml.append('</row>')
  }
  xml.append('</sheetData>')
  if (!safeRows.isEmpty()) {
    xml.append('<autoFilter ref="A1:').append(lastColumn).append(lastRow).append('"/>')
  }
  xml.append('</worksheet>')
  return xml.toString()
}

def writeXlsxWorkbook = { List sheets, String path ->
  def zip = new ZipOutputStream(new BufferedOutputStream(new FileOutputStream(path)))
  def putText = { String entryName, String text ->
    zip.putNextEntry(new ZipEntry(entryName))
    zip.write(text.getBytes("UTF-8"))
    zip.closeEntry()
  }
  try {
    def contentTypes = new StringBuilder(
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' +
      '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' +
      '<Default Extension="xml" ContentType="application/xml"/>' +
      '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>' +
      '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>')
    sheets.eachWithIndex { sheet, i ->
      contentTypes.append('<Override PartName="/xl/worksheets/sheet').append(i + 1)
        .append('.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
    }
    contentTypes.append('</Types>')
    putText("[Content_Types].xml", contentTypes.toString())
    putText("_rels/.rels",
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
      '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>' +
      '</Relationships>')

    def workbook = new StringBuilder(
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" ' +
      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>')
    sheets.eachWithIndex { sheet, i ->
      workbook.append('<sheet name="').append(xlsxXmlEscape(sheet.name))
        .append('" sheetId="').append(i + 1).append('" r:id="rId').append(i + 1).append('"/>')
    }
    workbook.append('</sheets></workbook>')
    putText("xl/workbook.xml", workbook.toString())

    def workbookRels = new StringBuilder(
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">')
    sheets.eachWithIndex { sheet, i ->
      workbookRels.append('<Relationship Id="rId').append(i + 1)
        .append('" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet')
        .append(i + 1).append('.xml"/>')
    }
    workbookRels.append('<Relationship Id="rId').append(sheets.size() + 1)
      .append('" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>')
    workbookRels.append('</Relationships>')
    putText("xl/_rels/workbook.xml.rels", workbookRels.toString())

    putText("xl/styles.xml",
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' +
      '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts>' +
      '<fills count="4"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>' +
      '<fill><patternFill patternType="solid"><fgColor rgb="FF2F75B5"/><bgColor indexed="64"/></patternFill></fill>' +
      '<fill><patternFill patternType="solid"><fgColor rgb="FFDDEBF7"/><bgColor indexed="64"/></patternFill></fill></fills>' +
      '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>' +
      '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>' +
      '<cellXfs count="5"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>' +
      '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>' +
      '<xf numFmtId="10" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>' +
      '<xf numFmtId="0" fontId="0" fillId="3" borderId="0" xfId="0" applyFill="1"/>' +
      '<xf numFmtId="10" fontId="0" fillId="3" borderId="0" xfId="0" applyFill="1" applyNumberFormat="1"/></cellXfs>' +
      '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>' +
      '</styleSheet>')
    sheets.eachWithIndex { sheet, i ->
      putText("xl/worksheets/sheet" + (i + 1) + ".xml", xlsxSheetXml(sheet))
    }
  } finally {
    zip.close()
  }
}

def buildPerImagePositiveQuantification = { summaryRows ->
  def output = []
  summaryRows.each { row ->
    def markerNames = [] as LinkedHashSet
    row.keySet().findAll { it.toString().endsWith("_final_positive_cell_count") }.each { name ->
      markerNames << name.toString().replaceFirst(/_final_positive_cell_count$/, "")
    }
    long total = ((row.n_nuclei ?: 0) as Number).longValue()
    def record = [
      "Image": row.image ?: "NA",
      "Region": row.region ?: "NA",
      "Mouse": row.mouse_id ?: "NA",
      "Section": row.section_id ?: "NA",
      "Genotype": row.genotype ?: "NA",
      "Condition": row.condition ?: "NA",
      "Panel": row.panel ?: "NA",
      "Total cells": total
    ]
    markerNames.each { marker ->
      long positive =
        ((row[marker + "_final_positive_cell_count"] ?: 0) as Number).longValue()
      record[marker + " positive cells"] = positive
      record[marker + " positive fraction of total cells"] =
        total > 0 ? positive / (double)total : 0
    }
    output << record
  }
  return output
}

// ============================================================================
//  7. SAMPLESHEET / METADATA
// ============================================================================

def parseCsvLine(String line) {
  def values = []
  def field = new StringBuilder()
  boolean quoted = false
  for (int i = 0; i < line.length(); i++) {
    char ch = line.charAt(i)
    if (ch == '"' as char) {
      if (quoted && i + 1 < line.length() && line.charAt(i + 1) == ('"' as char)) {
        field.append('"'); i++
      } else {
        quoted = !quoted
      }
    } else if (ch == (',' as char) && !quoted) {
      values << field.toString(); field.setLength(0)
    } else {
      field.append(ch)
    }
  }
  if (quoted) throw new IllegalArgumentException("Unclosed quoted field in samplesheet.csv line: " + line)
  values << field.toString()
  return values
}

def parseMeta(String fname, sheet, defaultPanel, String sourceContext = "", String relativePath = "") {
  // sheet: map filename -> [mouse_id, section_id, genotype, condition, panel]
  if (sheet != null) {
    String normalizedRelative = relativePath.replace('\\', '/')
    if (!normalizedRelative.isEmpty() && sheet.byRelative.containsKey(normalizedRelative)) {
      return sheet.byRelative[normalizedRelative]
    }
    if (sheet.ambiguousFilenames.contains(fname)) {
      throw new IllegalArgumentException("samplesheet.csv contains duplicate filename '" + fname +
        "'. Add a relative_path value for each duplicate and run with IFQ_RECURSIVE=true.")
    }
    if (sheet.byFilename.containsKey(fname)) return sheet.byFilename[fname]
  }
  // fallback: try token convention mouseID_condition_panel_section.ext
  def stem = fname.replaceFirst(/\.[^.]+$/, "")
  // 260719-CW convention. Use the parent path as context because stitched
  // overview names (for example Stitch_A01_G001.oir) omit mouse/stain details.
  def identityText = stem + " " + sourceContext
  def cw = (identityText =~ /(?i).*?\b(\d{6})\s+([MF]\d+)\s+(pr8_(?:bleo|PBS))\b/)
  def section = (stem =~ /(?i).*(A\d+_G\d+(?:_\d+)?)$/)
  if (cw.find() && section.matches()) {
    def lower = identityText.toLowerCase()
    def inferredPanel = lower.contains("4x mapping") && lower.contains("cc10_488") ? "M" :
                        lower.contains("cc10_488") ? "E" :
                        (lower.contains("t1a_488") ? "R" : defaultPanel)
    return [ mouse_id: cw.group(1) + "_" + cw.group(2),
             section_id: section.group(1), genotype: "krt5-creERT2;tdTOM",
             condition: cw.group(3).toLowerCase(), panel: inferredPanel ]
  }
  def toks = stem.split("_")
  // The launcher/IFQ_PANEL is authoritative when a filename does not match a
  // recognized acquisition convention. Arbitrary underscore-delimited stain
  // text must never be interpreted as a panel key. A samplesheet remains the
  // explicit per-image override mechanism.
  return [ mouse_id: (toks.length > 0 ? toks[0] : "NA"),
           condition: (toks.length > 1 ? toks[1] : "NA"),
           panel: defaultPanel,
           section_id: (toks.length > 3 ? toks[3] : "NA"),
           genotype: "NA" ]
}

def loadSamplesheet(String dir) {
  def f = new File(dir, "samplesheet.csv")
  if (!f.exists()) return null
  def lines = f.readLines()
  if (lines.isEmpty()) return null
  def header = parseCsvLine(lines[0]).collect { it.trim() }
  if (!header.isEmpty()) header[0] = header[0].replace("\uFEFF", "")
  def idx = { name -> header.indexOf(name) }
  if (idx("filename") < 0 && idx("relative_path") < 0) {
    throw new IllegalArgumentException("samplesheet.csv needs a filename or relative_path column")
  }
  def byFilename = [:]
  def byRelative = [:]
  def ambiguousFilenames = [] as Set
  lines.drop(1).each { ln ->
    if (ln.trim().isEmpty() || ln.trim().startsWith("#")) return   // skip blanks + comments
    def p = parseCsvLine(ln)
    def get = { n -> def i = idx(n); (i >= 0 && i < p.size()) ? p[i].trim() : "NA" }
    def fn = get("filename")
    def relative = get("relative_path").replace('\\', '/')
    if ((fn == null || fn.isEmpty() || fn == "NA") &&
        (relative == null || relative.isEmpty() || relative == "NA")) return
    def meta = [ mouse_id: get("mouse_id"), section_id: get("section_id"),
                 genotype: get("genotype"), condition: get("condition"), panel: get("panel") ]
    if (relative != null && !relative.isEmpty() && relative != "NA") {
      if (byRelative.containsKey(relative)) {
        throw new IllegalArgumentException("samplesheet.csv repeats relative_path '" + relative + "'")
      }
      byRelative[relative] = meta
    }
    if (fn != null && !fn.isEmpty() && fn != "NA") {
      if (byFilename.containsKey(fn)) {
        ambiguousFilenames << fn
        byFilename.remove(fn)
      } else if (!ambiguousFilenames.contains(fn)) {
        byFilename[fn] = meta
      }
    }
  }
  return [byFilename:byFilename, byRelative:byRelative, ambiguousFilenames:ambiguousFilenames]
}

def loadPanelMap(String path) {
  if (path == null || path.trim().isEmpty()) return null
  def f = new File(path)
  if (!f.isFile()) {
    throw new IllegalArgumentException("IFQ_PANEL_MAP_PATH is not a file: " + path)
  }
  def lines = f.readLines("UTF-8")
  if (lines.isEmpty()) {
    throw new IllegalArgumentException("Panel map is empty: " + path)
  }
  def header = parseCsvLine(lines[0]).collect { it.trim() }
  if (!header.isEmpty()) header[0] = header[0].replace("\uFEFF", "")
  int relativeIdx = header.indexOf("relative_path")
  int panelIdx = header.indexOf("panel")
  if (relativeIdx < 0 || panelIdx < 0) {
    throw new IllegalArgumentException(
      "Panel map needs relative_path and panel columns: " + path)
  }
  def byRelative = [:]
  lines.drop(1).eachWithIndex { ln, lineIndex ->
    if (ln.trim().isEmpty() || ln.trim().startsWith("#")) return
    def values = parseCsvLine(ln)
    String relative = relativeIdx < values.size() ?
      values[relativeIdx].trim().replace('\\', '/') : ""
    String panel = panelIdx < values.size() ? values[panelIdx].trim() : ""
    if (relative.isEmpty() || panel.isEmpty()) {
      throw new IllegalArgumentException(
        "Panel map line " + (lineIndex + 2) + " needs non-empty relative_path and panel")
    }
    if (byRelative.containsKey(relative)) {
      throw new IllegalArgumentException("Panel map repeats relative_path '" + relative + "'")
    }
    byRelative[relative] = panel
  }
  if (byRelative.isEmpty()) {
    throw new IllegalArgumentException("Panel map contains no image assignments: " + path)
  }
  return [source:f, byRelative:byRelative]
}
def loadCanonicalManifest(String path) {
  if (path == null || path.trim().isEmpty()) return null
  def f = new File(path)
  if (!f.isFile()) {
    throw new IllegalArgumentException(
      "IFQ_CANONICAL_MANIFEST_PATH is not a file: " + path)
  }
  def lines = f.readLines("UTF-8")
  if (lines.isEmpty()) {
    throw new IllegalArgumentException("Canonical manifest is empty: " + path)
  }
  def header = parseCsvLine(lines[0]).collect { it.trim() }
  if (!header.isEmpty()) header[0] = header[0].replace("\uFEFF", "")
  int relativeIdx = header.indexOf("source_relative_path")
  if (relativeIdx < 0) relativeIdx = header.indexOf("relative_path")
  if (relativeIdx < 0) {
    throw new IllegalArgumentException(
      "Canonical manifest needs source_relative_path or relative_path: " + path)
  }
  def byRelative = [] as LinkedHashSet
  lines.drop(1).eachWithIndex { ln, lineIndex ->
    if (ln.trim().isEmpty() || ln.trim().startsWith("#")) return
    def values = parseCsvLine(ln)
    String relative = relativeIdx < values.size() ?
      values[relativeIdx].trim().replace('\\', '/').replaceFirst(/^\.\//, "") : ""
    if (relative.isEmpty() || new File(relative).isAbsolute() ||
        relative.tokenize('/').contains("..")) {
      throw new IllegalArgumentException(
        "Canonical manifest line " + (lineIndex + 2) +
        " needs a safe non-empty path relative to IFQ_INPUT_DIR")
    }
    if (!byRelative.add(relative)) {
      throw new IllegalArgumentException(
        "Canonical manifest repeats source_relative_path '" + relative + "'")
    }
  }
  if (byRelative.isEmpty()) {
    throw new IllegalArgumentException(
      "Canonical manifest contains no image assignments: " + path)
  }
  return [source:f, byRelative:byRelative]
}

// ============================================================================
//  8. MAIN
// ============================================================================

// Force a consistent binary convention: thresholded objects become 255 on a 0
// background. Without this, "Convert to Mask" can invert per the user's Fiji
// prefs and silently corrupt every area/count. Recorded in provenance below.
Prefs.blackBackground = true

def outputRoot
try { outputRoot = ensureDir(OUTPUT_DIR) }
catch (Throwable t) { failRun("Cannot prepare OUTPUT_DIR='" + OUTPUT_DIR + "': " + t.message, t) }
def existingOutputEntries = outputRoot.listFiles() ?: [] as File[]
if (!ALLOW_NONEMPTY_OUTPUT && existingOutputEntries.length > 0) {
  failRun("OUTPUT_DIR is not empty: " + OUTPUT_DIR +
    ". Use a new run directory to prevent stale masks/cell tables, or explicitly set " +
    "IFQ_ALLOW_NONEMPTY_OUTPUT=true after reviewing the existing contents.")
}
def cfg = [ segmenter: SEGMENTER, prob: STARDIST_PROB, nms: STARDIST_NMS, tiles: STARDIST_TILES,
           dapiMethod: EFFECTIVE_DAPI_METHOD,
           dapiMethodSource: DAPI_METHOD_EXPLICIT ? "explicit_environment" :
                             (PROJECTION == "layer_aware" ? "layer_aware_safe_default" : "legacy_default"),
           dapiBackgroundRadiusUm: DAPI_BACKGROUND_RADIUS_UM,
           dapiLocalRadiusUm: DAPI_LOCAL_RADIUS_UM, dapiBlurSigmaPx: DAPI_BLUR_SIGMA_PX,
           dapiContrastSaturation: DAPI_CONTRAST_SATURATION,
           blackBackground: true,
           projection: PROJECTION, singlePlane: SINGLE_PLANE,
           zNuclearRange: Z_NUCLEAR_RANGE, zCellBodyRange: Z_CELL_BODY_RANGE,
           zApicalRange: Z_APICAL_RANGE, zCellBodyPlanes: Z_CELL_BODY_PLANES,
           zApicalPlanes: Z_APICAL_PLANES,
           exportDisplayChannels: EXPORT_DISPLAY_CHANNELS,
           displayPreviewOnly: DISPLAY_PREVIEW_ONLY,
           displayLowPercentile: DISPLAY_LOW_PERCENTILE,
           displayHighPercentile: DISPLAY_HIGH_PERCENTILE,
           displayGamma: DISPLAY_GAMMA,
           displayScaleBarUm: DISPLAY_SCALE_BAR_UM,
           displayScaleBarThicknessPx: DISPLAY_SCALE_BAR_THICKNESS_PX,
           ringExpandUm: RING_EXPAND_UM, minNucArea: MIN_NUCLEUS_AREA_UM2,
           minIncludedNuclei: MIN_INCLUDED_NUCLEI,
           podMinArea: POD_MIN_AREA_UM2, podBlur: POD_BLUR_SIGMA_PX, podMethod: POD_THRESH_METHOD,
           sensitivity: POS_SENSITIVITY, fixedThresholds: FIXED_POS_THRESHOLDS,
           morphologyPrimary: MORPHOLOGY_PRIMARY, morphologyRules: MORPHOLOGY_RULES,
           roleMorphologyDefaults: ROLE_MORPHOLOGY_DEFAULTS,
           markerRegistryPath: markerRegistryFile.isFile() ? markerRegistryFile.getAbsolutePath() : "unavailable",
           markerRegistrySchema: MARKER_REGISTRY.schema_version ?: "unavailable",
           panelConfigPath: PANEL_CONFIG_PATH ?: "built_in_only", customPanelKeys: CUSTOM_PANEL_KEYS,
           panelMapMode: PANEL_MAP_PATH ? "per_image_relative_path" : "single_panel_or_samplesheet",
           canonicalManifestPath: CANONICAL_MANIFEST_PATH ?: null,
           canonicalManifestMode: CANONICAL_MANIFEST_PATH ? "allowlist" : "discovery",
           minRingPosFraction: MIN_RING_POS_FRACTION, compartmentMode: COMPARTMENT_MODE,
           wholeFieldCompartment: WHOLE_FIELD_COMPARTMENT,
           actubSupportExpandUm: ACTUB_SUPPORT_EXPAND_UM,
           actubMinSupportFraction: ACTUB_MIN_SUPPORT_FRACTION,
           actubMinPatchAreaUm2: ACTUB_MIN_PATCH_AREA_UM2,
           actubMaxPatchAreaUm2: ACTUB_MAX_PATCH_AREA_UM2,
           actubCiliaSeedPercentile: ACTUB_CILIA_SEED_PERCENTILE,
           actubCiliaDensityRadiusUm: ACTUB_CILIA_DENSITY_RADIUS_UM,
           actubCiliaMinLocalDensity: ACTUB_CILIA_MIN_LOCAL_DENSITY,
           actubMaxComponentDistanceUm: ACTUB_MAX_COMPONENT_DISTANCE_UM,
           actubMinComponentBoundaryDistanceUm: ACTUB_MIN_COMPONENT_BOUNDARY_DISTANCE_UM,
           tissueMode: TISSUE_MODE, tissueBlur: TISSUE_BLUR_SIGMA_PX,
           tissueMethod: TISSUE_THRESH_METHOD, tissueMinArea: TISSUE_MIN_AREA_UM2,
           allowNonemptyOutput: ALLOW_NONEMPTY_OUTPUT ]

def versions = captureVersions()
IJ.log("ImageJ " + versions.imagej_version + " | Bio-Formats " + versions.bioformats_version)

def inDir = new File(INPUT_DIR)
if (!inDir.isDirectory()) {
  failRun("INPUT_DIR is not a folder: " + INPUT_DIR +
    ". Set IFQ_INPUT_DIR before launching Fiji or edit the documented fallback.")
}

def sheet
try { sheet = USE_SAMPLESHEET ? loadSamplesheet(INPUT_DIR) : null }
catch (Throwable t) { failRun("Cannot load samplesheet.csv: " + t.message, t) }
def panelMap
try { panelMap = loadPanelMap(PANEL_MAP_PATH) }
catch (Throwable t) { failRun("Cannot load per-image panel map: " + t.message, t) }
def canonicalManifest
try { canonicalManifest = loadCanonicalManifest(CANONICAL_MANIFEST_PATH) }
catch (Throwable t) { failRun("Cannot load canonical field manifest: " + t.message, t) }


def listed = []
if (RECURSIVE) inDir.eachFileRecurse { f -> if (f.isFile()) listed << f }
else listed = (inDir.listFiles() ?: [] as File[]).toList()
def includePattern
try { includePattern = ~/(?i)${INCLUDE_REGEX}/ }
catch (Throwable t) { failRun("Invalid IFQ_INCLUDE_REGEX='" + INCLUDE_REGEX + "': " + t.message, t) }
def matchedFiles = listed.findAll {
  it.isFile() && (it.name ==~ FILE_GLOB) && (it.getAbsolutePath() ==~ includePattern)
}.sort { it.getAbsolutePath() }
def deliberatelySkippedFiles = matchedFiles.findAll { it.name ==~ NON_ANALYTICAL_MAP_FILE }
def candidateFiles = matchedFiles.findAll { !(it.name ==~ NON_ANALYTICAL_MAP_FILE) }
def canonicalExcludedFiles = []
def files = candidateFiles
if (canonicalManifest != null) {
  def discoveredByRelative = [:]
  candidateFiles.each { f ->
    String relative = inDir.toPath().relativize(f.toPath()).toString().replace('\\', '/')
    discoveredByRelative[relative] = f
  }
  def missingCanonical = canonicalManifest.byRelative.findAll {
    !discoveredByRelative.containsKey(it)
  }
  if (!missingCanonical.isEmpty()) {
    failRun("Canonical manifest is missing " + missingCanonical.size() +
      " file(s) from the configured input scope, including: " +
      missingCanonical.take(3).join(", ") +
      ". Check IFQ_INPUT_DIR, IFQ_INCLUDE_REGEX, and IFQ_RECURSIVE.")
  }
  canonicalExcludedFiles = candidateFiles.findAll { f ->
    String relative = inDir.toPath().relativize(f.toPath()).toString().replace('\\', '/')
    !canonicalManifest.byRelative.contains(relative)
  }
  files = candidateFiles.findAll { f ->
    String relative = inDir.toPath().relativize(f.toPath()).toString().replace('\\', '/')
    canonicalManifest.byRelative.contains(relative)
  }
  IJ.log("Canonical manifest selected " + files.size() + " intended image(s); " +
         canonicalExcludedFiles.size() + " discovery candidate(s) retained as audit-only skips.")
}
if (MAX_IMAGES > 0) files = files.take(MAX_IMAGES)
IJ.log("Found " + files.size() + " analytical image(s); deliberately skipped " +
       deliberatelySkippedFiles.size() + " non-analysis acquisition(s).")
deliberatelySkippedFiles.each { f ->
  IJ.log("[IFQ_SKIP] " + f.name + " | non_analytical_map_acquisition")
}
if (files.isEmpty()) {
  failRun("No analytical images matched INPUT_DIR='" + INPUT_DIR +
    "', IFQ_INCLUDE_REGEX='" + INCLUDE_REGEX + "', and the supported image extensions. " +
    deliberatelySkippedFiles.size() + " non-analysis map acquisition(s) were deliberately skipped.")
}
if (panelMap != null) {
  def missingAssignments = files.findAll { f ->
    String relative = inDir.toPath().relativize(f.toPath()).toString().replace('\\', '/')
    !panelMap.byRelative.containsKey(relative)
  }
  if (!missingAssignments.isEmpty()) {
    failRun("Per-image AUTO panel map is missing " + missingAssignments.size() +
      " processed image(s), including: " +
      missingAssignments.take(3).collect { it.name }.join(", "))
  }
  def unknownPanelAssignments = panelMap.byRelative.findAll { relative, panel ->
    !PANELS.containsKey(panel)
  }
  if (!unknownPanelAssignments.isEmpty()) {
    failRun("Per-image AUTO panel map requests unknown panel(s): " +
      unknownPanelAssignments.values().unique().sort() +
      ". Available panels: " + PANELS.keySet().sort())
  }
  if (!DISPLAY_PREVIEW_ONLY) {
    new File(OUTPUT_DIR, "auto_panel_assignments.csv").bytes = panelMap.source.bytes
  }
}
if (canonicalManifest != null && !DISPLAY_PREVIEW_ONLY) {
  new File(OUTPUT_DIR, "canonical_field_manifest.csv").bytes = canonicalManifest.source.bytes
}

def masterSummary = []
def manifest = [ run_timestamp: versions.timestamp, versions: versions, config: cfg,
                  input_dir: INPUT_DIR, output_dir: OUTPUT_DIR, recursive: RECURSIVE,
                  include_regex: INCLUDE_REGEX, max_images: MAX_IMAGES,
                  per_image_panel_routing: panelMap != null,
                  panel_map_record: panelMap != null && !DISPLAY_PREVIEW_ONLY ?
                    "auto_panel_assignments.csv" : null,
                  matched_input_count: matchedFiles.size(),
                  analytical_input_count: files.size(),
                  canonical_manifest_path: canonicalManifest != null ?
                    canonicalManifest.source.getAbsolutePath() : null,
                  canonical_input_count: canonicalManifest != null ? files.size() : null,
                  canonical_excluded_count: canonicalExcludedFiles.size(),
                  status: "running", success_count: 0,
                  skipped_count: deliberatelySkippedFiles.size() + canonicalExcludedFiles.size(),
                  failure_count: 0, output_failure_count: 0, images: [] ]
deliberatelySkippedFiles.each { f ->
  def relativePath = inDir.toPath().relativize(f.toPath()).toString()
  manifest.images << [
    file: f.name, relative_path: relativePath, output_key: null, panel: null,
    status: "skipped", skip_reason: "non_analytical_map_acquisition",
    message: "Microscope map/overview acquisition excluded before image analysis"
  ]
}
canonicalExcludedFiles.each { f ->
  def relativePath = inDir.toPath().relativize(f.toPath()).toString()
  manifest.images << [
    file: f.name, relative_path: relativePath, output_key: null, panel: null,
    status: "skipped", skip_reason: "not_in_canonical_manifest",
    message: "Discovery candidate excluded by reviewer-approved canonical field manifest"
  ]
}

def safeToken = { value ->
  def s = (value == null || value.toString().trim().isEmpty()) ? "NA" : value.toString().trim()
  def cleaned = s.replaceAll(/[^A-Za-z0-9._-]+/, "-").replaceAll(/^-+|-+$/, "")
  return cleaned.isEmpty() ? "NA" : cleaned
}
def usedOutputKeys = [] as Set
def failures = []

files.eachWithIndex { f, fileIndex ->
  IJ.log("[IFQ_PROGRESS] " + (fileIndex + 1) + "/" + files.size() + " " + f.name)
  def relativePath = inDir.toPath().relativize(f.toPath()).toString()
  def panelKey = PANEL
  def outputKey = null
  try {
    def m = parseMeta(f.name, sheet, PANEL, f.parentFile.absolutePath, relativePath)
    String normalizedRelativePath = relativePath.replace('\\', '/')
    String requestedPanel = panelMap != null ?
      panelMap.byRelative[normalizedRelativePath].toString().trim() :
      (m.panel == null ? "" : m.panel.toString().trim())
    panelKey = (requestedPanel.isEmpty() || requestedPanel == "NA") ? PANEL : requestedPanel
    if (!PANELS.containsKey(panelKey)) {
      throw new IllegalArgumentException("Image '" + f.name + "' requests unknown panel '" + panelKey +
                                         "'. Available panels: " + PANELS.keySet().sort())
    }
    def panelDef = PANELS[panelKey]
    // Metadata, not the source filename, defines the readable output key. If
    // two distinct files resolve to the same metadata tuple, append a stable
    // relative-path hash instead of allowing the second image to overwrite the
    // first image's masks, cells, and parameters.
    def baseStem = [m.mouse_id, m.condition, panelKey, m.section_id].collect { safeToken(it) }.join("_")
    outputKey = baseStem
    if (usedOutputKeys.contains(outputKey)) {
      outputKey = baseStem + "__" + Integer.toHexString(relativePath.replace('\\', '/').hashCode())
      int collisionIndex = 2
      while (usedOutputKeys.contains(outputKey)) {
        outputKey = baseStem + "__" + Integer.toHexString(relativePath.replace('\\', '/').hashCode()) + "_" + collisionIndex++
      }
    }
    usedOutputKeys << outputKey
    def res = processImage(f.getAbsolutePath(), outputKey, panelKey, panelDef, m, cfg, OUTPUT_DIR)
    masterSummary.addAll(res.summary)
    manifest.success_count = manifest.success_count + 1
    manifest.images << [ file: f.name, relative_path: relativePath, output_key: outputKey,
                         panel: panelKey, status: "success", channel_signature: res.channel_signature,
                         tissue_source: res.tissue_source, n_cells: res.cells ]
  } catch (Throwable t) {
    IJ.log("  ERROR on " + f.name + ": " + t)
    def sw = new java.io.StringWriter(); t.printStackTrace(new java.io.PrintWriter(sw)); IJ.log(sw.toString())
    manifest.failure_count = manifest.failure_count + 1
    failures << f.name
    manifest.images << [ file: f.name, relative_path: relativePath, output_key: outputKey,
                         panel: panelKey, status: "failed", error: t.getMessage() ]
  }
}

if (DISPLAY_PREVIEW_ONLY) {
  if (!failures.isEmpty()) {
    failRun("Visual merge panel generation failed for " + failures.size() +
      " image(s): " + failures.join(", ") + ". Review the Fiji log.")
  }
  IJ.log("VISUAL MERGE PANELS COMPLETE. Wrote merged and supporting PNGs only for " +
         manifest.success_count + " image(s) to " + OUTPUT_DIR +
         "; segmentation and quantification were not run.")
  if (java.awt.GraphicsEnvironment.isHeadless()) System.exit(0)
  return
}

// master summary + manifest
writeCsv(masterSummary, OUTPUT_DIR + "/run_summary.csv")
def finalQuantification = buildPerImagePositiveQuantification(masterSummary)
def skippedInputs = manifest.images.findAll { it.status == "skipped" }.collect { record ->
  [
    file: record.file,
    relative_path: record.relative_path,
    status: record.status,
    skip_reason: record.skip_reason,
    message: record.message
  ]
}
def workbookFailure = null
try {
  writeXlsxWorkbook([
    [name: "Image Positive Counts", rows: finalQuantification],
    [name: "Run Summary", rows: masterSummary],
    [name: "Skipped Inputs", rows: skippedInputs]
  ], OUTPUT_DIR + "/run_summary.xlsx")
  manifest.summary_workbook = "run_summary.xlsx"
  manifest.summary_workbook_status = "complete"
  manifest.final_quantification_level = "image_region"
  manifest.final_quantification_sheet = "Image Positive Counts"
} catch (Throwable t) {
  workbookFailure = t
  manifest.output_failure_count = 1
  manifest.summary_workbook = "run_summary.xlsx"
  manifest.summary_workbook_status = "failed"
  manifest.summary_workbook_error = t.getMessage()
  IJ.log("ERROR writing run_summary.xlsx: " + t)
}
manifest.status = failures.isEmpty() && workbookFailure == null ?
  "complete" : (manifest.success_count > 0 ? "partial_failure" : "failed")
new File(OUTPUT_DIR, "run_manifest.json").setText(
  JsonOutput.prettyPrint(JsonOutput.toJson(manifest)), "UTF-8")

IJ.log("DONE. Wrote run_summary.csv, run_summary.xlsx, and run_manifest.json to " + OUTPUT_DIR +
       " | success=" + manifest.success_count + " skipped=" + manifest.skipped_count +
       " failure=" + manifest.failure_count)
IJ.log("Reminder: aggregate run_summary.csv to MOUSE level before stats (n = mice, not sections).")
if (!failures.isEmpty()) {
  failRun("Batch completed with " + failures.size() +
    " failed image(s): " + failures.join(", ") + ". See run_manifest.json and the Fiji log.")
}
if (workbookFailure != null) {
  failRun("Image analysis completed, but run_summary.xlsx could not be written: " +
    workbookFailure.getMessage() + ". See run_manifest.json and the Fiji log.", workbookFailure)
}

// ImageJ starts non-daemon UI/event threads even with --headless. Exit after
// synchronous exports so command-line and cluster jobs do not hang at DONE.
if (java.awt.GraphicsEnvironment.isHeadless()) System.exit(0)
