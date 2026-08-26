// ============================================================================
// qupath_wsi_tile_export.groovy  --  STAGE 1 of the whole-slide (WSI) route
// ============================================================================
// QuPath is the whole-slide FRONT END only. It never measures anything.
// It opens a slide-scanner container, picks the true high-resolution series,
// detects tissue once GLOBALLY, and cuts the tissue into small calibrated
// OME-TIFF tiles that the UNMODIFIED IF_Quant_Pipeline.groovy can analyse as
// ordinary images. ALL measurement stays in the validated Fiji engine.
//
//   Stage 1  (this script)  .vsi  ->  tiles/*.ome.tif + *_RoiSet.zip + samplesheet.csv
//   Stage 2  (unchanged)    IF_Quant_Pipeline.groovy on the tiles folder
//   Stage 3  aggregate_tiles_to_slide.py -> aggregate_to_mouse.py
//
// WHY THE _RoiSet.zip MATTERS (this is the whole trick):
//   Tiles overlap by a halo so that objects at a core boundary are fully
//   imaged. Overlap would double-count at seams. IF_Quant_Pipeline.groovy's
//   resolveTissueRois() already reads a "<stem>_RoiSet.zip" beside each image
//   and restricts EVERY measurement to it. So we write, per tile, one ROI =
//   (tile CORE rectangle) INTERSECT (global tissue mask). The validated engine
//   then clips pod area to the core and reports region_area_um2 for the core
//   only. Summing tiles is exact, with zero changes to the engine.
//
// VERIFIED BEHAVIOUR (QuPath 0.7.0, Olympus VS200 .vsi, 2026-08-06):
//   * exported tiles are BIT-IDENTICAL to the source region (all 4 channels)
//   * pixel calibration and channel names survive the round trip exactly
//   * output is a single flat series (IF_Quant_Pipeline rejects multi-series)
//   * sum of per-tile core ROI areas == whole-slide tissue geometry area
//
// USAGE (headless):
//   IFQ_WSI_INPUT=D:\Confocal_Images\...\slide.vsi  (file or folder of .vsi)
//   IFQ_WSI_OUTPUT=D:\wsi_stage1
//   "X:\QuPath\QuPath-0.7.0 (console).exe" script qupath_wsi_tile_export.groovy
//
// AREA endpoints are exact across seams. CELL COUNTS are NOT: the engine
// clips nuclei at the ROI edge rather than excluding them, so a nucleus
// straddling a core boundary can be counted in both neighbours. Stage 3
// de-duplicates counts using centroid_x_um/centroid_y_um plus the tile origin
// recorded in tile_manifest.csv. See docs/WSI_TILING_WORKFLOW.md.
// ============================================================================

import qupath.lib.images.servers.ImageServer
import qupath.lib.images.servers.ImageServers
import qupath.lib.images.writers.ome.OMEPyramidWriter
import qupath.lib.images.writers.ome.OMEPyramidWriter.CompressionType
import qupath.lib.analysis.images.ContourTracing
import qupath.lib.analysis.images.SimpleImage
import qupath.lib.analysis.images.SimpleImages
import qupath.lib.regions.ImagePlane
import qupath.lib.regions.RegionRequest
import qupath.lib.roi.GeometryTools
import qupath.lib.roi.interfaces.ROI
import qupath.imagej.tools.IJTools

import loci.formats.ImageReader
import loci.formats.MetadataTools
import loci.formats.FormatTools
import loci.formats.meta.IMetadata
import ome.units.UNITS

import ij.io.RoiEncoder
import ij.io.FileSaver
import ij.IJ
import ij.ImagePlus
import ij.process.ByteProcessor
import ij.process.FloatProcessor
import ij.process.ImageProcessor
import ij.process.AutoThresholder
import ij.plugin.filter.ThresholdToSelection
import ij.plugin.filter.RankFilters
import ij.plugin.filter.GaussianBlur

import org.locationtech.jts.geom.Geometry
import org.locationtech.jts.geom.prep.PreparedGeometryFactory
import org.locationtech.jts.geom.util.AffineTransformation

import qupath.lib.io.GsonTools          // QuPath 0.7 does not bundle groovy-json
import java.awt.image.BandedSampleModel
import java.awt.image.DataBufferUShort
import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.security.MessageDigest
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

// ---------------------------------------------------------------------------
// Settings / fail-closed helpers  (same idiom as IF_Quant_Pipeline.groovy)
// ---------------------------------------------------------------------------
def LOG_TAG = "[IFQ_WSI]"
def logMsg  = { String m -> println LOG_TAG + " " + m }

def failRun = { String message, Throwable cause = null ->
  System.err.println("FATAL: " + message)
  println LOG_TAG + " FATAL: " + message
  if (cause != null) cause.printStackTrace()
  System.exit(1)
}

def envOr = { String name, String fallback ->
  def v = System.getenv(name)
  return (v == null || v.trim().isEmpty()) ? fallback : v.trim()
}
def envInt = { String name, int fallback ->
  String raw = envOr(name, fallback.toString())
  try { return Integer.parseInt(raw) }
  catch (Exception e) { failRun(name + " must be an integer; found '" + raw + "'"); return fallback }
}
def envDouble = { String name, double fallback ->
  String raw = envOr(name, fallback.toString())
  try { return Double.parseDouble(raw) }
  catch (Exception e) { failRun(name + " must be a number; found '" + raw + "'"); return fallback }
}
def envBool = { String name, boolean fallback ->
  String raw = envOr(name, fallback.toString()).toLowerCase()
  if (!(raw in ["true", "false"])) failRun(name + " must be true or false; found '" + raw + "'")
  return raw == "true"
}

@groovy.transform.CompileStatic
class ContentHash {
  static String sha256File(File file) {
    MessageDigest digest = MessageDigest.getInstance("SHA-256")
    byte[] buffer = new byte[1024 * 1024]
    InputStream input = new BufferedInputStream(new FileInputStream(file), buffer.length)
    try {
      int count
      while ((count = input.read(buffer)) >= 0) {
        if (count > 0) digest.update(buffer, 0, count)
      }
    } finally {
      input.close()
    }
    byte[] bytes = digest.digest()
    StringBuilder encoded = new StringBuilder(bytes.length * 2)
    for (byte value : bytes) encoded.append(String.format("%02x", value & 0xff))
    return encoded.toString()
  }

  static String sha256Utf8(String text) {
    MessageDigest digest = MessageDigest.getInstance("SHA-256")
    byte[] bytes = digest.digest(text.getBytes(StandardCharsets.UTF_8))
    StringBuilder encoded = new StringBuilder(bytes.length * 2)
    for (byte value : bytes) encoded.append(String.format("%02x", value & 0xff))
    return encoded.toString()
  }
}

// Clipping a traced mask by a tile rectangle frequently yields a
// GeometryCollection (a polygon plus stray lines/points where the tile edge
// grazes the mask). JTS overlay operations REJECT GeometryCollection inputs
// with "Operation does not support GeometryCollection arguments", so every
// geometry must be reduced to its polygonal part before any boolean op.
// Dropping the lines/points loses no area -- they are zero-area artifacts.
def polygonal = { Geometry gm ->
  if (gm == null || gm.isEmpty()) return gm
  if (!(gm instanceof org.locationtech.jts.geom.GeometryCollection) ||
      gm instanceof org.locationtech.jts.geom.MultiPolygon) return gm
  def polys = []
  for (int i = 0; i < gm.getNumGeometries(); i++) {
    def part = gm.getGeometryN(i)
    if (part instanceof org.locationtech.jts.geom.Polygonal && !part.isEmpty()) polys << part
  }
  if (polys.isEmpty()) return gm.getFactory().createPolygon()
  return gm.getFactory().buildGeometry(polys)
}

// ---- inputs -----------------------------------------------------------------
def INPUT          = envOr("IFQ_WSI_INPUT", "")
def OUTPUT         = envOr("IFQ_WSI_OUTPUT", "")
def SLIDE_META_CSV = envOr("IFQ_WSI_SLIDE_METADATA", "")
def STAGE1_SCRIPT_PATH = envOr("IFQ_WSI_STAGE1_SCRIPT_PATH", "")
def REFERENCE_MASK_PROFILE_PATH = envOr("IFQ_WSI_REFERENCE_MASK_PROFILE", "")

// ---- tiling -----------------------------------------------------------------
def CORE_PX        = envInt("IFQ_WSI_CORE_PX", 2048)
def HALO_PX        = envInt("IFQ_WSI_HALO_PX", 128)
def MIN_TISSUE_UM2 = envDouble("IFQ_WSI_MIN_TILE_TISSUE_UM2", 0.0d)

// ---- series selection (refuse to quantify the macro/label/overview) ---------
def MAX_PIXEL_UM   = envDouble("IFQ_WSI_MAX_PIXEL_UM", 0.5d)
def EXPECT_NCH     = envInt("IFQ_WSI_EXPECT_CHANNELS", 4)
// This validates acquisition ORDER only. A wavelength/filter label such as
// FITC does not prove that the biological marker is KRT5; that identity remains
// the responsibility of the frozen acquisition protocol and panel mapping.
// Semicolon is the delimiter because it is not used by the supported patterns.
def DEFAULT_ORDERED_CHANNEL_PATTERNS = [
    '^\\s*(?:dapi|hoechst|405).*$',
    '^\\s*(?:fitc|488|krt.?5|pro.?spc|alexa.?488).*$',
    '^\\s*(?:cy3|555|ager|m.?rage|tritc|alexa.?555).*$',
    '^\\s*(?:cy5|647|pdpn|t1a|t1alpha|podoplanin|krt8|alexa.?647).*$'
]
def ORDERED_CHANNEL_PATTERNS_RAW = envOr(
    "IFQ_WSI_ORDERED_CHANNEL_PATTERNS",
    DEFAULT_ORDERED_CHANNEL_PATTERNS.join(";"))
def ORDERED_CHANNEL_PATTERN_TEXTS = ORDERED_CHANNEL_PATTERNS_RAW
    .split(";", -1).collect { it.trim() }
if (EXPECT_NCH < 1) {
  failRun("IFQ_WSI_EXPECT_CHANNELS must be a positive integer; found " + EXPECT_NCH)
}
if (ORDERED_CHANNEL_PATTERN_TEXTS.size() != EXPECT_NCH ||
    ORDERED_CHANNEL_PATTERN_TEXTS.any { it.isEmpty() }) {
  failRun("IFQ_WSI_ORDERED_CHANNEL_PATTERNS must contain exactly " + EXPECT_NCH +
          " non-empty semicolon-delimited regex patterns in acquisition order; found " +
          ORDERED_CHANNEL_PATTERN_TEXTS.size())
}
def ORDERED_CHANNEL_PATTERNS = []
ORDERED_CHANNEL_PATTERN_TEXTS.eachWithIndex { patternText, index ->
  try {
    ORDERED_CHANNEL_PATTERNS << java.util.regex.Pattern.compile(
        patternText, java.util.regex.Pattern.CASE_INSENSITIVE)
  } catch (java.util.regex.PatternSyntaxException e) {
    failRun("Invalid ordered channel regex at 1-based position " + (index + 1) +
            " in IFQ_WSI_ORDERED_CHANNEL_PATTERNS: " + e.getMessage())
  }
}

// ---- tissue detection (the DENOMINATOR of the primary endpoint) ------------
def TISSUE_DS      = envDouble("IFQ_WSI_TISSUE_DOWNSAMPLE", 16.0d)
def TISSUE_BLUR    = envDouble("IFQ_WSI_TISSUE_BLUR_SIGMA", 2.0d)
def TISSUE_CLOSE_R = envDouble("IFQ_WSI_TISSUE_CLOSE_RADIUS", 4.0d)
def TISSUE_OPEN_R  = envDouble("IFQ_WSI_TISSUE_OPEN_RADIUS", 2.0d)
def MIN_FRAG_MM2   = envDouble("IFQ_WSI_MIN_FRAGMENT_MM2", 0.05d)
// PROTOCOL DECISION, default OFF. Filling interior rings fills ALVEOLAR
// AIRSPACE and inflated the tissue denominator by 12.5% on the pilot slide
// (75.06 -> 84.47 mm2). Whether airspace counts as "tissue" is a scientific
// decision, not cleanup. Leave false unless the protocol says otherwise.
def FILL_HOLES     = envBool("IFQ_WSI_FILL_INTERIOR_RINGS", false)

// ---- export -----------------------------------------------------------------
def COMPRESSION    = envOr("IFQ_WSI_COMPRESSION", "ZLIB")
def WRITE_TILE_PX  = envInt("IFQ_WSI_WRITE_TILE_PX", 256)
def PARALLEL       = envInt("IFQ_WSI_PARALLEL", 4)
def RESUME         = envBool("IFQ_WSI_RESUME", false)
def DRY_RUN        = envBool("IFQ_WSI_DRY_RUN", false)
// Smoke-test aid: stop after N tiles per slide. 0 = no cap. A capped run is
// NOT a valid analysis -- the manifest records the cap so Stage 3 refuses it.
def MAX_TILES      = envInt("IFQ_WSI_MAX_TILES_PER_SLIDE", 0)
if (RESUME) {
  failRun("IFQ_WSI_RESUME=true is unavailable: Stage 1 has no content-addressed " +
          "checkpoint binding the source slide, script, tissue mask, configuration, " +
          "tile, and ROI bytes. Use a new Stage 1 output root.")
}

// ---- damaged-area partition (the endpoint DENOMINATOR) ----------------------
// Endpoint: KRT5+ area / DAMAGED ALVEOLAR area (Lin et al. 2024, JCI
// 134(19):e176828 -- they drew it by hand). Damaged = parenchyma lacking AT1
// coverage. "Pixels below the AGER threshold" is NOT that: healthy alveolus is
// mostly airspace with thin AT1 membranes. It must be measured as a LOCAL AREA
// FRACTION over an alveolus-sized neighbourhood.
//
// Off by default because it REQUIRES a control-derived fixed AGER threshold.
// With per-slide adaptive thresholds the comparison inverts -- measured on the
// pilot: adaptive Otsu made UNINFECTED lung read MORE damaged than infected at
// every parameter setting. See docs/ECTOPIC_POD_ENDPOINT.md.
def PARTITION      = envBool("IFQ_WSI_PARTITION_DAMAGE", false)
def AGER_CH        = envInt("IFQ_WSI_AGER_CHANNEL", 2)
def AGER_THR_RAW   = envOr("IFQ_WSI_AGER_THRESHOLD", "")
// Defaults are the CONTROL-DERIVED operating point (alpha = 1% false positive
// on uninfected lung): AGER threshold 150, sigma 40 um, cutoff 0.14. Chosen
// from the two uninfected slides ONLY -- the infected slides were not opened by
// the calibration -- so the operating point is not tuned on the outcome.
// sigma 40 um is about one alveolar diameter, the natural neighbourhood for
// "is this alveolus lined by AT1". See docs/ECTOPIC_POD_ENDPOINT.md section 4c.
def DAMAGE_SIGMA   = envDouble("IFQ_WSI_DAMAGE_SIGMA_UM", 40.0d)
def DAMAGE_CUTOFF  = envDouble("IFQ_WSI_DAMAGE_CUTOFF", 0.14d)

// ---- downstream metadata ----------------------------------------------------
def PANEL          = envOr("IFQ_WSI_PANEL", "LEFT")
// The ROI name becomes the "region" column AND supplies the compartment token
// the engine matches on ("alveol" -> alveolar, "airway"/"bronch" -> airway...).
//
// The default is deliberately NEUTRAL. Naming a tile "alveolar_*" asserts that
// it contains no conducting airway, and nothing here establishes that -- airway
// basal cells are KRT5+ in every animal. Until airway annotations are supplied,
// claiming "alveolar" would mislabel pure-airway tiles.
// Consequence of the neutral name: AGER/T1A declare expectedCompartment
// "alveolar", so their calls degrade to context_unresolved / indeterminate.
// KRT5 pod area -- the primary endpoint -- is unaffected either way.
// To assert the compartment anyway, set IFQ_WSI_ROI_COMPARTMENT=alveolar.
def ROI_COMPARTMENT = envOr("IFQ_WSI_ROI_COMPARTMENT", "")
def ROI_NAME       = envOr("IFQ_WSI_ROI_NAME", "parenchyma_core")
def ROI_DAMAGED    = envOr("IFQ_WSI_ROI_NAME_DAMAGED", "parenchyma_damaged")
def ROI_INTACT     = envOr("IFQ_WSI_ROI_NAME_INTACT",  "parenchyma_intact")
if (!ROI_COMPARTMENT.isEmpty()) {
  ROI_NAME    = ROI_COMPARTMENT + "_" + ROI_NAME
  ROI_DAMAGED = ROI_COMPARTMENT + "_" + ROI_DAMAGED
  ROI_INTACT  = ROI_COMPARTMENT + "_" + ROI_INTACT
}

if (INPUT.isEmpty())  failRun("IFQ_WSI_INPUT is required (a .vsi file or a folder containing .vsi files)")
if (OUTPUT.isEmpty()) failRun("IFQ_WSI_OUTPUT is required")
if (STAGE1_SCRIPT_PATH.isEmpty())
  failRun("IFQ_WSI_STAGE1_SCRIPT_PATH is required so the executed Stage 1 script bytes " +
          "can be content-bound in stage1_manifest.json")
def stage1ScriptFile = new File(STAGE1_SCRIPT_PATH)
if (!stage1ScriptFile.isFile())
  failRun("IFQ_WSI_STAGE1_SCRIPT_PATH is not a file: " + STAGE1_SCRIPT_PATH)
if (Files.isSymbolicLink(stage1ScriptFile.toPath()))
  failRun("IFQ_WSI_STAGE1_SCRIPT_PATH must not be a symbolic link: " + STAGE1_SCRIPT_PATH)
stage1ScriptFile = stage1ScriptFile.getCanonicalFile()
def stage1ScriptRecord = [
  name: stage1ScriptFile.name,
  size_bytes: stage1ScriptFile.length(),
  sha256: ContentHash.sha256File(stage1ScriptFile)
]
if (HALO_PX < 0)      failRun("IFQ_WSI_HALO_PX must be >= 0")
if (CORE_PX <= 0)     failRun("IFQ_WSI_CORE_PX must be > 0")
if (MIN_TISSUE_UM2 < 0 || !Double.isFinite(MIN_TISSUE_UM2))
  failRun("IFQ_WSI_MIN_TILE_TISSUE_UM2 must be finite and >= 0; found " + MIN_TISSUE_UM2)
if (ROI_NAME.toLowerCase().contains("alveol")) {
  logMsg("NOTE: ROI names assert compartment 'alveolar'. This is only true if " +
         "conducting airways have been excluded; airway basal cells are KRT5+ " +
         "in every animal, including uninfected controls.")
} else {
  logMsg("NOTE: ROI names carry no compartment token, so AGER/T1A will report " +
         "context_unresolved / indeterminate (they declare expectedCompartment " +
         "'alveolar'). The KRT5 pod endpoint is unaffected. Set " +
         "IFQ_WSI_ROI_COMPARTMENT=alveolar once airways are excluded.")
}
double AGER_THRESHOLD = -1.0d
if (PARTITION) {
  if (AGER_THR_RAW.isEmpty())
    failRun("IFQ_WSI_PARTITION_DAMAGE=true requires an explicit IFQ_WSI_AGER_THRESHOLD. " +
            "A per-slide adaptive threshold INVERTS the endpoint: on the pilot it made " +
            "uninfected lung read more damaged than infected at every setting. " +
            "Derive it from blinded controls first (see docs/ECTOPIC_POD_ENDPOINT.md).")
  try { AGER_THRESHOLD = Double.parseDouble(AGER_THR_RAW.trim()) }
  catch (Exception e) { failRun("IFQ_WSI_AGER_THRESHOLD must be a number; found '" + AGER_THR_RAW + "'") }
  if (!Double.isFinite(AGER_THRESHOLD) || !(AGER_THRESHOLD > 0))
    failRun("IFQ_WSI_AGER_THRESHOLD must be finite and > 0")
  if (!(DAMAGE_CUTOFF > 0 && DAMAGE_CUTOFF < 1)) failRun("IFQ_WSI_DAMAGE_CUTOFF must be in (0,1)")
  if (!(DAMAGE_SIGMA > 0)) failRun("IFQ_WSI_DAMAGE_SIGMA_UM must be > 0")
}
def compType
try { compType = CompressionType.valueOf(COMPRESSION) }
catch (Exception e) { failRun("IFQ_WSI_COMPRESSION must be one of UNCOMPRESSED, LZW, ZLIB, J2K; found '" + COMPRESSION + "'") }
if (OMEPyramidWriter.isLossyCompressionType(COMPRESSION.toLowerCase())
    || COMPRESSION in ["J2K_LOSSY", "JPEG"]) {
  failRun("IFQ_WSI_COMPRESSION='" + COMPRESSION + "' is LOSSY. This is quantitative data; refusing.")
}

// ---------------------------------------------------------------------------
// Hot pixel loops. Kept @CompileStatic -- dynamic Groovy per-pixel loops over
// ~9.4 Mpx dominated the runtime (~40 s/slide) in profiling.
// ---------------------------------------------------------------------------
@groovy.transform.CompileStatic
class Px {
  /** Unsigned short[] -> FloatProcessor. */
  static FloatProcessor toFloat(short[] pix, int w, int h) {
    FloatProcessor fp = new FloatProcessor(w, h)
    float[] out = (float[]) fp.getPixels()
    for (int i = 0; i < pix.length; i++) out[i] = (float) (pix[i] & 0xFFFF)
    return fp
  }
  /** 256-bin histogram over [lo,hi]; returns the Otsu threshold in RAW units. */
  static double otsuThreshold(float[] f) {
    float lo = Float.MAX_VALUE, hi = -Float.MAX_VALUE
    for (int i = 0; i < f.length; i++) { float v = f[i]; if (v < lo) lo = v; if (v > hi) hi = v }
    if (hi <= lo) return (double) lo
    int[] hist = new int[256]
    double sc = 255.0d / (hi - lo)
    for (int i = 0; i < f.length; i++) hist[(int) Math.round((f[i] - lo) * sc)]++
    int bin = new AutoThresholder().getThreshold(AutoThresholder.Method.Otsu, hist)
    return lo + bin / sc
  }
  static ByteProcessor threshold(float[] f, int w, int h, double t) {
    ByteProcessor bp = new ByteProcessor(w, h)
    byte[] out = (byte[]) bp.getPixels()
    for (int i = 0; i < f.length; i++) if (f[i] >= t) out[i] = (byte) 255
    return bp
  }
  static long countForeground(ByteProcessor bp) {
    byte[] m = (byte[]) bp.getPixels()
    long n = 0
    for (int i = 0; i < m.length; i++) if ((m[i] & 0xFF) > 127) n++
    return n
  }
  static SimpleImage toSimpleImage(ByteProcessor bp, int w, int h) {
    byte[] m = (byte[]) bp.getPixels()
    SimpleImage si = SimpleImages.createFloatImage(w, h)
    for (int y = 0; y < h; y++)
      for (int x = 0; x < w; x++)
        si.setValue(x, y, ((m[y * w + x] & 0xFF) > 127) ? 1f : 0f)
    return si
  }
}

// ---------------------------------------------------------------------------
// Retry wrapper. The external volume holding the slides dropped out mid-session
// during development; a whole-slide run is tens of minutes long.
// ---------------------------------------------------------------------------
/**
 * RFC4180 field escaping. The Olympus series name is literally
 * "20x_DAPI, FITC, Cy3, Cy5(Gray)_01" -- it contains commas, so an unquoted
 * manifest silently shifts every later column.
 */
def csvField = { v ->
  String s = (v == null) ? "" : v.toString()
  if (s.contains(",") || s.contains("\"") || s.contains("\n") || s.contains("\r"))
    return "\"" + s.replace("\"", "\"\"") + "\""
  return s
}
def csvRow = { List vals -> vals.collect { csvField(it) }.join(",") }

def withRetry = { String what, int attempts, Closure body ->
  Throwable last = null
  for (int i = 1; i <= attempts; i++) {
    try { return body() }
    catch (Throwable t) {
      last = t
      logMsg("  retry " + i + "/" + attempts + " after failure in " + what + ": " + t.getMessage())
      Thread.sleep(2000L * i)
    }
  }
  throw new IllegalStateException("Gave up on " + what + " after " + attempts + " attempts", last)
}

def contentRecord = { File input, String label ->
  if (!input.isFile()) failRun(label + " is not a regular file: " + input.absolutePath)
  if (Files.isSymbolicLink(input.toPath()))
    failRun(label + " must not be a symbolic link: " + input.absolutePath)
  File canonical = input.getCanonicalFile()
  long sizeBefore = canonical.length()
  if (sizeBefore <= 0) failRun(label + " must not be empty: " + canonical.absolutePath)
  String digest = ContentHash.sha256File(canonical)
  if (canonical.length() != sizeBefore)
    failRun(label + " changed size while it was being hashed: " + canonical.absolutePath)
  return [name: canonical.name, size_bytes: sizeBefore, sha256: digest]
}

def publishFileAtomically = { File source, File destination, String label ->
  if (destination.exists()) failRun(label + " destination already exists: " + destination.absolutePath)
  File parent = destination.parentFile
  if (!parent.isDirectory() && !parent.mkdirs())
    failRun("Could not create " + label + " directory: " + parent.absolutePath)
  def temp = Files.createTempFile(parent.toPath(), ".ifq-copy-", ".tmp")
  try {
    Files.copy(source.toPath(), temp, StandardCopyOption.REPLACE_EXISTING)
    try {
      Files.move(temp, destination.toPath(), StandardCopyOption.ATOMIC_MOVE)
    } catch (java.nio.file.AtomicMoveNotSupportedException unsupported) {
      Files.move(temp, destination.toPath())
    }
  } finally {
    Files.deleteIfExists(temp)
  }
  def sourceRecord = contentRecord(source, label + " source")
  def publishedRecord = contentRecord(destination, label + " published copy")
  if (sourceRecord.size_bytes != publishedRecord.size_bytes ||
      sourceRecord.sha256 != publishedRecord.sha256)
    failRun(label + " published bytes do not match the declared source artifact")
  return publishedRecord
}

def loadStrictBinaryMask = { File file, int expectedWidth, int expectedHeight, String label ->
  ImagePlus image = IJ.openImage(file.absolutePath)
  if (image == null) failRun("Could not open " + label + ": " + file.absolutePath)
  try {
    if (image.getStackSize() != 1 || image.getNChannels() != 1 || image.getNFrames() != 1)
      failRun(label + " must be a single-plane, single-channel image")
    if (image.getBitDepth() != 8)
      failRun(label + " must be 8-bit binary (0/255); found bit depth " + image.getBitDepth())
    if (image.getWidth() != expectedWidth || image.getHeight() != expectedHeight)
      failRun(label + " dimensions " + image.getWidth() + "x" + image.getHeight() +
              " do not match the selected-series downsample grid " +
              expectedWidth + "x" + expectedHeight)
    byte[] sourcePixels = (byte[]) image.getProcessor().getPixels()
    byte[] pixels = sourcePixels.clone()
    for (int i = 0; i < pixels.length; i++) {
      int value = pixels[i] & 0xFF
      if (value != 0 && value != 255)
        failRun(label + " must contain only 0 and 255; found " + value +
                " at linear pixel index " + i)
    }
    return new ByteProcessor(expectedWidth, expectedHeight, pixels, null)
  } finally {
    image.close()
  }
}

def publishBinaryMaskTiff = { ByteProcessor mask, File destination, String label ->
  if (destination.exists()) failRun(label + " destination already exists: " + destination.absolutePath)
  File parent = destination.parentFile
  if (!parent.isDirectory() && !parent.mkdirs())
    failRun("Could not create " + label + " directory: " + parent.absolutePath)
  def temp = Files.createTempFile(parent.toPath(), ".ifq-mask-", ".tif")
  try {
    ImagePlus image = new ImagePlus(label, mask.duplicate())
    try {
      if (!new FileSaver(image).saveAsTiff(temp.toFile().absolutePath))
        failRun("Could not serialize " + label + " as TIFF")
    } finally {
      image.close()
    }
    try {
      Files.move(temp, destination.toPath(), StandardCopyOption.ATOMIC_MOVE)
    } catch (java.nio.file.AtomicMoveNotSupportedException unsupported) {
      Files.move(temp, destination.toPath())
    }
  } finally {
    Files.deleteIfExists(temp)
  }
  return contentRecord(destination, label)
}

// Bio-Formats is the only authority for the Olympus package members actually
// used to open a VSI.  A filename/stem glob can both miss nested ETS payloads
// and absorb unrelated files.  Hash the exact getUsedFiles() set before and
// after tile export; Stage 1 publishes no manifest if membership or bytes move.
def buildSourcePackage = { File slideFile ->
  File source = slideFile.getCanonicalFile()
  File packageRoot = source.parentFile.getCanonicalFile()
  def reader = new ImageReader()
  reader.setFlattenedResolutions(false)
  List<String> usedPaths
  try {
    reader.setId(source.absolutePath)
    usedPaths = (reader.getUsedFiles() ?: new String[0]).toList()
  } finally {
    try { reader.close() } catch (Exception ignore) {}
  }
  if (usedPaths.isEmpty())
    failRun("Bio-Formats reported no used files for " + source.absolutePath)

  def members = []
  def seen = new HashSet<String>()
  boolean containsSource = false
  usedPaths.each { String reportedPath ->
    File reported = new File(reportedPath)
    if (Files.isSymbolicLink(reported.toPath()))
      failRun("Bio-Formats source-package member must not be a symbolic link: " + reportedPath)
    File member = reported.getCanonicalFile()
    if (!member.isFile())
      failRun("Bio-Formats reported a missing source-package member: " + member.absolutePath)
    String rootPrefix = packageRoot.absolutePath + File.separator
    if (!(member.absolutePath == packageRoot.absolutePath ||
          member.absolutePath.startsWith(rootPrefix)))
      failRun("Bio-Formats source-package member escapes the VSI parent directory: " +
              member.absolutePath)
    String relative = packageRoot.toPath().relativize(member.toPath()).toString()
        .replace(File.separatorChar, '/' as char)
    if (relative.isEmpty() || relative == "." || relative.startsWith("../") ||
        relative.contains("/../") || relative.contains("\\") ||
        relative.contains("\t") || relative.contains("\r") || relative.contains("\n"))
      failRun("Unsafe Bio-Formats source-package relative path: " + relative)
    if (!seen.add(relative))
      failRun("Bio-Formats reported a duplicate source-package member: " + relative)
    if (member.absolutePath == source.absolutePath) containsSource = true
    members << [
      relative_path: relative,
      size_bytes: member.length(),
      sha256: ContentHash.sha256File(member)
    ]
  }
  if (!containsSource)
    failRun("Bio-Formats used-file list does not include the source VSI: " + source.absolutePath)
  members.sort { a, b -> a.relative_path <=> b.relative_path }
  StringBuilder packageLines = new StringBuilder()
  members.each { member ->
    packageLines.append(member.relative_path).append('\t')
        .append(member.size_bytes).append('\t')
        .append(member.sha256).append('\n')
  }
  return [
    format: "olympus_vsi",
    source_vsi: source.name,
    discovery_authority: "bioformats_ImageReader_getUsedFiles",
    package_hash_algorithm: "sha256_utf8_path_tab_size_tab_sha256_lf",
    members: members,
    package_sha256: ContentHash.sha256Utf8(packageLines.toString())
  ]
}

// ---------------------------------------------------------------------------
// Series enumeration. checkImageSupport() silently DROPS thumbnail series and
// is not a series count. loci getSeriesCount() sees all of them.
// ---------------------------------------------------------------------------
def enumerateSeries = { String filePath ->
  def out = []
  def reader = new ImageReader()
  reader.setFlattenedResolutions(false)   // must match QuPath's own setting
  IMetadata meta = MetadataTools.createOMEXMLMetadata()
  reader.setMetadataStore(meta)
  try {
    reader.setId(filePath)
    int n = reader.getSeriesCount()
    for (int s = 0; s < n; s++) {
      reader.setSeries(s); reader.setResolution(0)
      def pxW = meta.getPixelsPhysicalSizeX(s)
      def pxH = meta.getPixelsPhysicalSizeY(s)
      int nCh = reader.getEffectiveSizeC()
      def chNames = (0..<nCh).collect { c -> try { meta.getChannelName(s, c) } catch (Exception e) { null } }
      out << [ series: s, name: meta.getImageName(s),
               width: reader.getSizeX(), height: reader.getSizeY(),
               nChannels: nCh, nZSlices: reader.getSizeZ(), nTimepoints: reader.getSizeT(),
               pixelType: FormatTools.getPixelTypeString(reader.getPixelType()),
               pixelWidthMicrons : pxW == null ? Double.NaN : pxW.value(UNITS.MICROMETER).doubleValue(),
               pixelHeightMicrons: pxH == null ? Double.NaN : pxH.value(UNITS.MICROMETER).doubleValue(),
               nResolutions: reader.getResolutionCount(),
               isThumbnail: reader.isThumbnailSeries(), channelNames: chNames ]
    }
  } finally { try { reader.close() } catch (Exception ignore) {} }
  return out
}

/**
 * Pick the one true scan series. Deliberately strict: this is the single
 * decision that separates a real result from a plausible wrong one.
 * QuPath opens series 0 by default, which on VS200 .vsi is the LABEL image;
 * and the "overview" series is a genuine calibrated DAPI fluorescence image at
 * 1.7 um/px, so a naive "reject > 2 um/px" rule would let it through.
 */
def selectSeries = { List seriesList, double maxPxUm, int nChReq, List orderedPatterns ->
  def rejected = []
  def cands = seriesList.findAll { s ->
    if (s.isThumbnail)                     { rejected << "${s.series}: thumbnail series";              return false }
    if (s.nChannels != nChReq)             { rejected << "${s.series}: nChannels=${s.nChannels} != ${nChReq}"; return false }
    if (s.nZSlices != 1)                   { rejected << "${s.series}: nZSlices=${s.nZSlices} != 1";   return false }
    // NaN handling: under Groovy operator semantics NaN compares GREATER than
    // every finite value, so do this explicitly rather than relying on it.
    if (Double.isNaN(s.pixelWidthMicrons) || !(s.pixelWidthMicrons > 0.0d)) {
      rejected << "${s.series}: uncalibrated (pixelWidthMicrons=${s.pixelWidthMicrons})"; return false }
    if (s.pixelWidthMicrons > maxPxUm)     { rejected << "${s.series}: ${s.pixelWidthMicrons} um/px > ${maxPxUm}"; return false }
    def mismatches = []
    for (int channelIndex = 0; channelIndex < nChReq; channelIndex++) {
      def name = s.channelNames[channelIndex]
      if (name == null || !orderedPatterns[channelIndex].matcher(name.toString()).matches()) {
        mismatches << "C${channelIndex + 1}='${name}'"
      }
    }
    if (!mismatches.isEmpty()) {
      rejected << "${s.series}: ordered acquisition-channel mismatch " + mismatches.join(", ")
      return false
    }
    return true
  }
  return [candidates: cands, rejected: rejected]
}

// ---------------------------------------------------------------------------
// Slide-level metadata. mouse_id must be real: aggregate_to_mouse.py rejects
// "NA"/"UNKNOWN", so guessing here would only surface as a confusing failure
// three stages later. Fail loudly instead.
// ---------------------------------------------------------------------------
def SLIDE_PATTERN = ~/(?i)^IFNg\s+KO\((het|hom)\)\s+([\d.]+)\s+(m[\w.-]+)\s+(pr8)\s+(no\s+infection|infection)\s*$/

def parseSlideName = { String stem ->
  def m = SLIDE_PATTERN.matcher(stem)
  if (!m.matches()) return null
  String geno = m.group(1).toLowerCase()
  String mouse = m.group(3)
  String cond  = m.group(5).toLowerCase().replaceAll(/\s+/, "_")
  return [ mouse_id : mouse,
           genotype : (geno == "het" ? "IFNg_KO_het" : "IFNg_KO_hom"),
           condition: (cond == "infection" ? "PR8" : "naive"),
           harvest  : m.group(2) ]
}

def readSlideMetadataCsv = { String p ->
  def out = [:]
  if (p.isEmpty()) return out
  def f = new File(p)
  if (!f.isFile()) failRun("IFQ_WSI_SLIDE_METADATA not found: " + p)
  def lines = f.readLines().findAll { !it.trim().isEmpty() && !it.trim().startsWith("#") }
  if (lines.isEmpty()) failRun("IFQ_WSI_SLIDE_METADATA is empty: " + p)
  def hdr = lines[0].split(",").collect { it.trim().toLowerCase() }
  ["vsi_filename", "mouse_id", "genotype", "condition"].each { req ->
    if (!hdr.contains(req)) failRun("IFQ_WSI_SLIDE_METADATA must have a '" + req + "' column; found " + hdr)
  }
  lines.drop(1).each { line ->
    def parts = line.split(",", -1).collect { it.trim() }
    def row = [:]; hdr.eachWithIndex { hname, i -> row[hname] = i < parts.size() ? parts[i] : "" }
    out[row.vsi_filename] = [ mouse_id: row.mouse_id, genotype: row.genotype,
                              condition: row.condition, harvest: (row.harvest ?: "") ]
  }
  return out
}

def loadReferenceMaskProfile = { String profilePath, List<File> inputSlides ->
  if (profilePath.isEmpty()) return null
  File profileFile = new File(profilePath)
  def profileContent = contentRecord(profileFile, "IFQ_WSI_REFERENCE_MASK_PROFILE")
  profileFile = profileFile.getCanonicalFile()
  Map root
  try {
    root = (Map) GsonTools.getInstance().fromJson(profileFile.getText("UTF-8"), Map.class)
  } catch (Exception e) {
    failRun("Cannot parse IFQ_WSI_REFERENCE_MASK_PROFILE as JSON: " + profileFile.absolutePath, e)
    return null
  }
  if (root == null) failRun("IFQ_WSI_REFERENCE_MASK_PROFILE must contain a JSON object")
  def rootKeys = ['$schema', "schema_version", "profile_id", "review_state",
                  "review_protocol_id", "coordinate_space", "mask_logic", "slides"] as Set
  if ((root.keySet() as Set) != rootKeys)
    failRun("Reference-mask profile fields must be exactly " + rootKeys + "; found " + root.keySet())
  if (root['$schema'] != "https://ifquant-lung.invalid/schemas/wsi-reference-mask-profile.schema.json")
    failRun('Reference-mask profile has an unsupported $schema')
  if (root.schema_version != "1.0.0") failRun("Reference-mask profile schema_version must be 1.0.0")
  if (!(root.profile_id instanceof String) || root.profile_id.trim().isEmpty() ||
      root.profile_id != root.profile_id.trim() ||
      root.profile_id.contains("\t") || root.profile_id.contains("\r") ||
      root.profile_id.contains("\n"))
    failRun("Reference-mask profile_id must be a non-empty string without surrounding/control whitespace")
  if (!(root.review_state in ["engineering_unreviewed", "expert_reviewed"]))
    failRun("Reference-mask review_state must be engineering_unreviewed or expert_reviewed")
  if (!(root.review_protocol_id instanceof String) || root.review_protocol_id.trim().isEmpty() ||
      root.review_protocol_id != root.review_protocol_id.trim() ||
      root.review_protocol_id.contains("\t") || root.review_protocol_id.contains("\r") ||
      root.review_protocol_id.contains("\n"))
    failRun("Reference-mask review_protocol_id must be a non-empty string without surrounding/control whitespace")
  if (root.coordinate_space != "selected_series_downsample_grid")
    failRun("Reference-mask coordinate_space must be selected_series_downsample_grid")
  if (root.mask_logic != "tissue_foreground_minus_airway_foreground")
    failRun("Reference-mask mask_logic must be tissue_foreground_minus_airway_foreground")
  if (!(root.slides instanceof List) || root.slides.isEmpty())
    failRun("Reference-mask profile must contain at least one slide")

  File profileRoot = profileFile.parentFile.getCanonicalFile()
  String profileRootPrefix = profileRoot.absolutePath + File.separator
  def safeMaskArtifact = { Object raw, String label ->
    if (!(raw instanceof Map)) failRun(label + " must be an object")
    def expected = ["relative_path", "size_bytes", "sha256"] as Set
    if ((raw.keySet() as Set) != expected)
      failRun(label + " fields must be exactly " + expected + "; found " + raw.keySet())
    String token = raw.relative_path == null ? "" : raw.relative_path.toString()
    def parts = token.split("/", -1) as List
    if (token.isEmpty() || token != token.trim() || token.startsWith("/") || token.contains("\\") ||
        token ==~ /^[A-Za-z]:.*/ || parts.any { it.isEmpty() || it == "." || it == ".." } ||
        token.contains("\t") || token.contains("\r") || token.contains("\n"))
      failRun(label + ".relative_path is not a safe portable relative path: " + token)
    if (!(raw.size_bytes instanceof Number) ||
        raw.size_bytes.doubleValue() != Math.rint(raw.size_bytes.doubleValue()) ||
        raw.size_bytes.longValue() <= 0)
      failRun(label + ".size_bytes must be a positive integer")
    String declaredSha = raw.sha256 == null ? "" : raw.sha256.toString()
    if (!(declaredSha ==~ /^[0-9a-f]{64}$/)) failRun(label + ".sha256 must be lowercase SHA-256")
    File artifact = new File(profileRoot, token)
    if (Files.isSymbolicLink(artifact.toPath())) failRun(label + " must not be a symbolic link: " + token)
    artifact = artifact.getCanonicalFile()
    if (!artifact.absolutePath.startsWith(profileRootPrefix))
      failRun(label + " escapes the reference-mask profile directory: " + token)
    def actual = contentRecord(artifact, label)
    if (actual.size_bytes != raw.size_bytes.longValue() || actual.sha256 != declaredSha)
      failRun(label + " content does not match its declared size/SHA-256: " + token)
    return [relative_path: token, size_bytes: actual.size_bytes, sha256: actual.sha256,
            _file: artifact]
  }

  def integerField = { Map row, String key, String label, int minimum ->
    def value = row[key]
    if (!(value instanceof Number) || value.doubleValue() != Math.rint(value.doubleValue()) ||
        value.longValue() < minimum)
      failRun(label + "." + key + " must be an integer >= " + minimum)
    return value.intValue()
  }
  def normalizedSlides = [:]
  def slideKeys = ["source_vsi", "source_package_sha256", "series_index",
                   "full_resolution_width", "full_resolution_height", "downsample",
                   "mask_width", "mask_height", "tissue_mask", "airway_mask"] as Set
  root.slides.eachWithIndex { raw, index ->
    String label = "Reference-mask slides[" + index + "]"
    if (!(raw instanceof Map)) failRun(label + " must be an object")
    if ((raw.keySet() as Set) != slideKeys)
      failRun(label + " fields must be exactly " + slideKeys + "; found " + raw.keySet())
    String sourceVsi = raw.source_vsi == null ? "" : raw.source_vsi.toString()
    if (sourceVsi.isEmpty() || sourceVsi != sourceVsi.trim() || sourceVsi.contains(":") ||
        sourceVsi.contains("/") || sourceVsi.contains("\\") ||
        sourceVsi.contains("\t") || sourceVsi.contains("\r") || sourceVsi.contains("\n") ||
        !sourceVsi.toLowerCase().endsWith(".vsi"))
      failRun(label + ".source_vsi must be a plain .vsi filename")
    if (normalizedSlides.containsKey(sourceVsi))
      failRun("Reference-mask profile contains duplicate source_vsi: " + sourceVsi)
    String packageSha = raw.source_package_sha256 == null ? "" : raw.source_package_sha256.toString()
    if (!(packageSha ==~ /^[0-9a-f]{64}$/))
      failRun(label + ".source_package_sha256 must be lowercase SHA-256")
    if (!(raw.downsample instanceof Number) || !Double.isFinite(raw.downsample.doubleValue()) ||
        raw.downsample.doubleValue() <= 0)
      failRun(label + ".downsample must be finite and > 0")
    normalizedSlides[sourceVsi] = [
      source_vsi: sourceVsi,
      source_package_sha256: packageSha,
      series_index: integerField(raw, "series_index", label, 0),
      full_resolution_width: integerField(raw, "full_resolution_width", label, 1),
      full_resolution_height: integerField(raw, "full_resolution_height", label, 1),
      downsample: raw.downsample.doubleValue(),
      mask_width: integerField(raw, "mask_width", label, 1),
      mask_height: integerField(raw, "mask_height", label, 1),
      tissue_mask: safeMaskArtifact(raw.tissue_mask, label + ".tissue_mask"),
      airway_mask: safeMaskArtifact(raw.airway_mask, label + ".airway_mask")
    ]
  }
  def inputNames = inputSlides.collect { it.name }
  if ((normalizedSlides.keySet() as Set) != (inputNames as Set))
    failRun("Reference-mask profile slide set must exactly equal IFQ_WSI_INPUT. profile=" +
            normalizedSlides.keySet().sort() + " input=" + inputNames.sort())
  return [file: profileFile, content: profileContent,
          profile_id: root.profile_id.trim(), review_state: root.review_state,
          review_protocol_id: root.review_protocol_id.trim(),
          coordinate_space: root.coordinate_space, mask_logic: root.mask_logic,
          slides: normalizedSlides]
}

// ---------------------------------------------------------------------------
// Resolve the input slide list
// ---------------------------------------------------------------------------
def inFile = new File(INPUT)
def slides = []
if (inFile.isDirectory()) {
  // .vsi ONLY. Never open .ets directly -- those are the internal pyramid tiles
  // inside the hidden _<name>_ sidecar folder and give a broken partial read.
  slides = inFile.listFiles().findAll { it.isFile() && it.name.toLowerCase().endsWith(".vsi") }.sort { it.name }
} else if (inFile.isFile()) {
  if (!inFile.name.toLowerCase().endsWith(".vsi"))
    failRun("IFQ_WSI_INPUT must be a .vsi file (never .ets): " + INPUT)
  slides = [inFile]
} else {
  failRun("IFQ_WSI_INPUT does not exist: " + INPUT)
}
if (slides.isEmpty()) failRun("No .vsi files found under " + INPUT)

def slideMetaCsv = readSlideMetadataCsv(SLIDE_META_CSV)
def referenceMaskProfile = loadReferenceMaskProfile(REFERENCE_MASK_PROFILE_PATH, slides)

// Stage 1 has no content-addressed resume/checkpoint contract. Refuse every
// pre-existing entry -- including hidden or unrelated files -- before writing
// any analytical artifact, so stale tiles can never survive into this run.
def outRoot = new File(OUTPUT)
if (outRoot.exists()) {
  if (!outRoot.isDirectory())
    failRun("IFQ_WSI_OUTPUT exists but is not a directory: " + outRoot.getAbsolutePath())
  def existingOutputEntries = outRoot.listFiles()
  if (existingOutputEntries == null)
    failRun("Cannot inspect IFQ_WSI_OUTPUT before launch: " + outRoot.getAbsolutePath())
  if (existingOutputEntries.length > 0)
    failRun("IFQ_WSI_OUTPUT must be empty because Stage 1 resume is unavailable; found " +
            existingOutputEntries.length + " pre-existing entr" +
            (existingOutputEntries.length == 1 ? "y" : "ies") + ". Use a new output root.")
} else if (!outRoot.mkdirs()) {
  failRun("Could not create IFQ_WSI_OUTPUT: " + outRoot.getAbsolutePath())
}

def publishedReferenceProfile = null
if (referenceMaskProfile != null) {
  File publishedProfileFile = new File(outRoot, "reference_mask_profile.json")
  def publishedContent = publishFileAtomically(
      referenceMaskProfile.file, publishedProfileFile, "reference-mask profile")
  if (publishedContent.size_bytes != referenceMaskProfile.content.size_bytes ||
      publishedContent.sha256 != referenceMaskProfile.content.sha256)
    failRun("Reference-mask profile changed between validation and publication")
  publishedReferenceProfile = [
    profile_id: referenceMaskProfile.profile_id,
    review_state: referenceMaskProfile.review_state,
    review_protocol_id: referenceMaskProfile.review_protocol_id,
    coordinate_space: referenceMaskProfile.coordinate_space,
    mask_logic: referenceMaskProfile.mask_logic,
    published_relative_path: publishedProfileFile.name,
    content: publishedContent,
    content_verified_before_and_after: true
  ]
}

logMsg("slides            : " + slides.size())
logMsg("core/halo px      : " + CORE_PX + " / " + HALO_PX + "  (export " + (CORE_PX + 2*HALO_PX) + " px)")
logMsg("tissue downsample : " + TISSUE_DS + "   fillInteriorRings=" + FILL_HOLES)
logMsg("reference space   : " + (referenceMaskProfile == null ?
       "automatic DAPI/Otsu engineering mask (airways not excluded)" :
       ("external tissue-minus-airway profile " + referenceMaskProfile.profile_id +
        " [" + referenceMaskProfile.review_state + "]")))
logMsg("compression       : " + COMPRESSION + " (lossless)   panel=" + PANEL + "   roiName=" + ROI_NAME)
logMsg("output            : " + outRoot.getAbsolutePath())

def runRecord = [ schema_version: "1.2", stage: "wsi_tile_export",
                  generated_utc : java.time.Instant.now().toString(),
                  stage1_script: stage1ScriptRecord,
                  qupath_series_selection: [
                    max_pixel_um: MAX_PIXEL_UM,
                    expect_channels: EXPECT_NCH,
                    ordered_channel_patterns: ORDERED_CHANNEL_PATTERN_TEXTS,
                    channel_order_authority: "acquisition_order_pattern_only_not_biological_identity"
                  ],
                  tiling: [ core_px: CORE_PX, halo_px: HALO_PX, min_tile_tissue_um2: MIN_TISSUE_UM2 ],
                  tissue: [
                    mode: (referenceMaskProfile == null ?
                           "automatic_dapi_otsu_engineering" : "external_binary_reference_masks"),
                    downsample: TISSUE_DS, blur_sigma_px: TISSUE_BLUR,
                    close_radius_px: TISSUE_CLOSE_R, open_radius_px: TISSUE_OPEN_R,
                    min_fragment_mm2: MIN_FRAG_MM2, fill_interior_rings: FILL_HOLES,
                    threshold_method: (referenceMaskProfile == null ? "Otsu" : "external_binary_mask"),
                    channel: (referenceMaskProfile == null ? "nuclear/DAPI (index 0)" : null),
                    airway_exclusion: (referenceMaskProfile == null ?
                                       "not_available" : "explicit_binary_mask_subtracted"),
                    reference_mask_profile: publishedReferenceProfile
                  ],
                  export: [ compression: COMPRESSION, write_tile_px: WRITE_TILE_PX,
                            format: "OME-TIFF, single series, single resolution" ],
                  downstream: [
                    panel: PANEL,
                    partition_damage: PARTITION,
                    ager_channel: AGER_CH,
                    ager_threshold: (PARTITION ? AGER_THRESHOLD : null),
                    damage_sigma_um: DAMAGE_SIGMA,
                    damage_cutoff: DAMAGE_CUTOFF,
                    roi_compartment: ROI_COMPARTMENT,
                    roi_name: ROI_NAME,
                    roi_name_damaged: ROI_DAMAGED,
                    roi_name_intact: ROI_INTACT
                  ],
                  slides: [] ]

def mouseIndex = [:]   // mouse_id -> [genotype, condition]  (collision guard)
int totalTiles = 0

// ===========================================================================
// PER-SLIDE
// ===========================================================================
slides.each { slideFile ->
  String stem = slideFile.name.replaceFirst(/\.[^.]+$/, "")
  logMsg("")
  logMsg("=================================================================")
  logMsg("SLIDE: " + slideFile.name)

  logMsg("  discovering and hashing Bio-Formats source package ...")
  def sourcePackageBefore = withRetry("source package provenance", 3) {
    buildSourcePackage(slideFile)
  }
  logMsg("  source package: " + sourcePackageBefore.members.size() +
         " file(s), sha256=" + sourcePackageBefore.package_sha256)

  // ---- metadata --------------------------------------------------------
  def md = slideMetaCsv[slideFile.name] ?: slideMetaCsv[stem] ?: parseSlideName(stem)
  if (md == null || !md.mouse_id || md.mouse_id.toString().trim().isEmpty()) {
    failRun("Cannot determine mouse_id for '" + slideFile.name + "'.\n" +
            "  Either rename to the documented convention " +
            "'IFNg KO(het|hom) <date> <mouse> pr8 [no ]infection.vsi'\n" +
            "  or supply IFQ_WSI_SLIDE_METADATA=<csv with vsi_filename,mouse_id,genotype,condition>.\n" +
            "  Refusing to emit mouse_id='NA' -- aggregate_to_mouse.py would reject it later.")
  }
  def prev = mouseIndex[md.mouse_id]
  if (prev != null && (prev.genotype != md.genotype || prev.condition != md.condition)) {
    failRun("mouse_id '" + md.mouse_id + "' maps to two different (genotype, condition) pairs: " +
            prev + " and " + md + ". n = MICE, so this must be resolved before analysis.")
  }
  mouseIndex[md.mouse_id] = [genotype: md.genotype, condition: md.condition]
  logMsg("  mouse_id=" + md.mouse_id + "  genotype=" + md.genotype + "  condition=" + md.condition)

  // ---- series selection ------------------------------------------------
  def seriesList = withRetry("enumerateSeries(" + slideFile.name + ")", 3) { enumerateSeries(slideFile.getAbsolutePath()) }
  logMsg("  series found: " + seriesList.size())
  seriesList.each { s ->
    logMsg(String.format("    [%d] %-42s %6dx%-6d C=%d Z=%d %8.4f um/px %s%s",
        s.series, (s.name ?: "?"), s.width, s.height, s.nChannels, s.nZSlices,
        s.pixelWidthMicrons, s.pixelType, s.isThumbnail ? " (thumbnail)" : ""))
  }
  def sel = selectSeries(seriesList, MAX_PIXEL_UM, EXPECT_NCH, ORDERED_CHANNEL_PATTERNS)
  if (sel.candidates.size() != 1) {
    failRun("Expected exactly ONE series with <= " + MAX_PIXEL_UM + " um/px, " + EXPECT_NCH +
            " channels, Z=1 and recognisable channel names in '" + slideFile.name + "'.\n" +
            "  Found " + sel.candidates.size() + " candidate(s): " + sel.candidates.collect { it.series } + "\n" +
            "  Rejections: " + sel.rejected.join("; ") + "\n" +
            "  Refusing to guess -- picking the wrong series silently quantifies the macro/overview image.")
  }
  def chosen = sel.candidates[0]
  logMsg("  SELECTED series " + chosen.series + " '" + chosen.name + "' " +
         chosen.width + "x" + chosen.height + " @ " + chosen.pixelWidthMicrons + " um/px " +
         chosen.channelNames)

  ImageServer server = withRetry("buildServer", 3) {
    ImageServers.buildServer(slideFile.toURI(), "--series", "" + chosen.series)
  }
  def cal = server.getPixelCalibration()
  if (!cal.hasPixelSizeMicrons())
    failRun("Series " + chosen.series + " of " + slideFile.name + " has no pixel calibration.")
  double pxUm = cal.getPixelWidthMicrons()
  double pxUmH = cal.getPixelHeightMicrons()
  double aspect = Math.abs(pxUmH - pxUm) / pxUm
  if (aspect > 0.01d)
    failRun("Non-square pixels (" + pxUm + " x " + pxUmH + " um, " + (aspect*100) + "%). " +
            "IF_Quant_Pipeline.groovy rejects > 1%.")
  // This scanner reports pixelWidth != pixelHeight (0.3449973537 vs 0.3449984138).
  // ImageJ computes area as width*height, so collapsing to a single scalar and
  // squaring it makes every Stage 1 area disagree with Stage 2 by ~3e-6. Small,
  // but it is a pure bookkeeping error and it muddies the reconciliation check
  // that exists to detect real problems.
  double pxAreaUm2 = pxUm * pxUmH
  int W = server.getWidth(), H = server.getHeight()

  def slideOut = new File(outRoot, stem)
  if (!slideOut.mkdir()) failRun("Could not create slide output directory: " + slideOut.absolutePath)
  def referenceDir = new File(slideOut, "reference_space")
  if (!referenceDir.mkdir()) failRun("Could not create reference-space directory: " + referenceDir.absolutePath)
  def referenceArtifactsToVerify = []

  // ---- global tissue detection ----------------------------------------
  logMsg((referenceMaskProfile == null ? "  detecting tissue on channel 0" :
          "  loading external tissue/airway masks") + " at downsample " + TISSUE_DS + " ...")
  long t0 = System.currentTimeMillis()
  def img = withRetry("readRegion(tissue)", 3) { server.readRegion(TISSUE_DS, 0, 0, W, H) }
  def raster = img.getRaster()
  int mw = img.getWidth(), mh = img.getHeight()
  def sm = raster.getSampleModel(), db = raster.getDataBuffer()
  boolean fast = (db instanceof DataBufferUShort) && (sm instanceof BandedSampleModel) &&
                 raster.getSampleModelTranslateX() == 0 && raster.getSampleModelTranslateY() == 0 &&
                 sm.getBankIndices()[0] == 0 && sm.getBandOffsets()[0] == 0 &&
                 sm.getScanlineStride() == mw && db.getOffsets()[0] == 0
  Double thr = null
  ByteProcessor bp
  long rawTissuePx = 0L, airwayPx = 0L
  def referenceSpaceRecord
  if (referenceMaskProfile == null) {
    short[] pix
    if (fast) {
      pix = ((DataBufferUShort) db).getData(0)
    } else {
      int[] tmp = new int[mw * mh]
      raster.getSamples(0, 0, mw, mh, 0, tmp)
      pix = new short[mw * mh]
      for (int i = 0; i < tmp.length; i++) pix[i] = (short) tmp[i]
    }
    def fp = Px.toFloat(pix, mw, mh)
    new GaussianBlur().blurGaussian(fp, TISSUE_BLUR)
    float[] f = (float[]) fp.getPixels()
    thr = Px.otsuThreshold(f)
    bp = Px.threshold(f, mw, mh, thr)
    // RankFilters MAX/MIN, NOT ByteProcessor.dilate()/erode(): with
    // ij.Prefs.blackBackground=false (the headless default) those are polarity
    // inverted and silently destroy the mask.
    def rf = new RankFilters()
    rf.rank(bp, TISSUE_CLOSE_R, RankFilters.MAX); rf.rank(bp, TISSUE_CLOSE_R, RankFilters.MIN)
    rf.rank(bp, TISSUE_OPEN_R,  RankFilters.MIN); rf.rank(bp, TISSUE_OPEN_R,  RankFilters.MAX)
    rawTissuePx = Px.countForeground(bp)
    def rasterFile = new File(referenceDir, "automatic_dapi_tissue_raster.tif")
    def rasterContent = publishBinaryMaskTiff(bp, rasterFile, "automatic DAPI tissue raster")
    referenceArtifactsToVerify << [file: rasterFile, content: rasterContent,
                                  label: "automatic DAPI tissue raster"]
    referenceSpaceRecord = [
      mode: "automatic_dapi_otsu_engineering",
      authority: "generated_dapi_otsu_raster_and_content_bound_tile_rois",
      profile_id: "automatic_dapi_otsu_engineering",
      review_state: "engineering_unreviewed",
      review_protocol_id: "automatic_dapi_otsu",
      coordinate_space: "selected_series_downsample_grid",
      mask_logic: "dapi_otsu_tissue_without_airway_exclusion",
      sampling_semantics: "exhaustive_grid_over_declared_reference_space",
      airway_excluded: false,
      downsample: TISSUE_DS, mask_width: mw, mask_height: mh,
      tissue_mask: [published_relative_path: "reference_space/" + rasterFile.name,
                    content: rasterContent],
      source_tissue_mask: null, source_airway_mask: null
    ]
  } else {
    def declared = referenceMaskProfile.slides[slideFile.name]
    if (declared == null) failRun("Reference-mask profile lacks slide " + slideFile.name)
    if (declared.source_package_sha256 != sourcePackageBefore.package_sha256)
      failRun("Reference-mask profile source_package_sha256 does not match the current raw package for " +
              slideFile.name)
    if (declared.series_index != chosen.series ||
        declared.full_resolution_width != W || declared.full_resolution_height != H)
      failRun("Reference-mask profile series identity/dimensions do not match the selected series for " +
              slideFile.name)
    if (Math.abs(declared.downsample - TISSUE_DS) > 1e-12 ||
        declared.mask_width != mw || declared.mask_height != mh)
      failRun("Reference-mask profile downsample-grid declaration does not match the opened series for " +
              slideFile.name + ": declared " + declared.mask_width + "x" + declared.mask_height +
              " @ " + declared.downsample + ", actual " + mw + "x" + mh + " @ " + TISSUE_DS)

    def sourceTissue = loadStrictBinaryMask(declared.tissue_mask._file, mw, mh,
                                            "external tissue mask for " + slideFile.name)
    def sourceAirway = loadStrictBinaryMask(declared.airway_mask._file, mw, mh,
                                            "external airway mask for " + slideFile.name)
    byte[] tissuePixels = (byte[]) sourceTissue.getPixels()
    byte[] airwayPixels = (byte[]) sourceAirway.getPixels()
    byte[] finalPixels = new byte[tissuePixels.length]
    for (int i = 0; i < tissuePixels.length; i++) {
      boolean inTissue = (tissuePixels[i] & 0xFF) == 255
      boolean inAirway = (airwayPixels[i] & 0xFF) == 255
      if (inTissue) rawTissuePx++
      if (inAirway) airwayPx++
      if (inAirway && !inTissue)
        failRun("External airway mask is not a subset of the tissue mask for " +
                slideFile.name + " at linear pixel index " + i)
      if (inTissue && !inAirway) finalPixels[i] = (byte) 255
    }
    bp = new ByteProcessor(mw, mh, finalPixels, null)

    File sourceTissueCopy = new File(referenceDir, "source_tissue_mask.bin")
    File sourceAirwayCopy = new File(referenceDir, "source_airway_mask.bin")
    def sourceTissueContent = publishFileAtomically(
        declared.tissue_mask._file, sourceTissueCopy, "external tissue mask")
    def sourceAirwayContent = publishFileAtomically(
        declared.airway_mask._file, sourceAirwayCopy, "external airway mask")
    if (sourceTissueContent.sha256 != declared.tissue_mask.sha256 ||
        sourceTissueContent.size_bytes != declared.tissue_mask.size_bytes ||
        sourceAirwayContent.sha256 != declared.airway_mask.sha256 ||
        sourceAirwayContent.size_bytes != declared.airway_mask.size_bytes)
      failRun("Reference masks changed between profile validation and Stage 1 publication")
    File finalMaskFile = new File(referenceDir, "analysis_tissue_minus_airway_mask.tif")
    def finalMaskContent = publishBinaryMaskTiff(
        bp, finalMaskFile, "analysis tissue-minus-airway mask")
    referenceArtifactsToVerify.addAll([
      [file: sourceTissueCopy, content: sourceTissueContent, label: "published tissue source mask"],
      [file: sourceAirwayCopy, content: sourceAirwayContent, label: "published airway source mask"],
      [file: finalMaskFile, content: finalMaskContent, label: "analysis tissue-minus-airway mask"]
    ])
    referenceSpaceRecord = [
      mode: "external_binary_reference_masks",
      authority: "content_bound_external_profile_and_binary_masks",
      profile_id: referenceMaskProfile.profile_id,
      profile_sha256: referenceMaskProfile.content.sha256,
      review_state: referenceMaskProfile.review_state,
      review_protocol_id: referenceMaskProfile.review_protocol_id,
      coordinate_space: referenceMaskProfile.coordinate_space,
      mask_logic: referenceMaskProfile.mask_logic,
      sampling_semantics: "exhaustive_grid_over_declared_reference_space",
      airway_excluded: true,
      downsample: TISSUE_DS, mask_width: mw, mask_height: mh,
      tissue_foreground_px_before_airway_exclusion: rawTissuePx,
      airway_foreground_px: airwayPx,
      source_tissue_mask: [
        profile_relative_path: declared.tissue_mask.relative_path,
        published_relative_path: "reference_space/" + sourceTissueCopy.name,
        content: sourceTissueContent
      ],
      source_airway_mask: [
        profile_relative_path: declared.airway_mask.relative_path,
        published_relative_path: "reference_space/" + sourceAirwayCopy.name,
        content: sourceAirwayContent
      ],
      tissue_mask: [published_relative_path: "reference_space/" + finalMaskFile.name,
                    content: finalMaskContent]
    ]
  }
  long fgPx = Px.countForeground(bp)
  if (fgPx <= 0)
    failRun("The declared tissue reference space contains no analyzable foreground in " +
            slideFile.name + ". Refusing to emit an empty tiling.")
  referenceSpaceRecord.analysis_tissue_foreground_px = fgPx
  def simple = Px.toSimpleImage(bp, mw, mh)

  // Pass the SAME downsample into the RegionRequest: createTracedGeometry then
  // does the scaling AND the origin offset, giving full-resolution coordinates.
  // Mixing raw pyramid levels with a different scale factor introduces a ~0.4%
  // Y stretch on this format.
  def req = RegionRequest.createInstance(server.getPath(), TISSUE_DS, 0, 0, W, H)
  Geometry g = ContourTracing.createTracedGeometry(simple, 0.5d, Double.POSITIVE_INFINITY, req)
  double minFragPx = (MIN_FRAG_MM2 * 1e6) / pxAreaUm2
  if (referenceMaskProfile == null) {
    g = GeometryTools.removeFragments(g, minFragPx)
    if (FILL_HOLES) g = GeometryTools.removeInteriorRings(g, minFragPx)
  }
  g = GeometryTools.constrainToBounds(g, 0, 0, W, H)
  if (g == null || g.isEmpty()) failRun("Tissue geometry empty after cleanup for " + slideFile.name)
  double tissueMm2 = g.getArea() * pxAreaUm2 / 1e6
  if (referenceMaskProfile == null) {
    logMsg(String.format("  tissue: Otsu=%.2f  mask=%.2f%%  area=%.2f mm2  (%d ms)",
        thr, 100.0 * fgPx / (mw * (double) mh), tissueMm2, System.currentTimeMillis() - t0))
  } else {
    logMsg(String.format("  reference masks: tissue=%d px airway=%d px analyzable=%d px " +
        "(%.2f%%) area=%.2f mm2  (%d ms)", rawTissuePx, airwayPx, fgPx,
        100.0 * fgPx / (mw * (double) mh), tissueMm2, System.currentTimeMillis() - t0))
  }

  // ---- damaged-alveolar territory (endpoint denominator) ----------------
  // AT1-INTACT territory = where AGER+ pixels occupy at least DAMAGE_CUTOFF of
  // an alveolus-sized neighbourhood. Smoothing a 0/1 mask with a Gaussian IS
  // the local area fraction. Everything else in tissue is damaged.
  Geometry gDamaged = null
  double damagedMm2 = 0.0d
  if (PARTITION) {
    long tD = System.currentTimeMillis()
    short[] apix
    if (fast) {
      apix = ((DataBufferUShort) db).getData(AGER_CH)
    } else {
      int[] tmp2 = new int[mw * mh]
      raster.getSamples(0, 0, mw, mh, AGER_CH, tmp2)
      apix = new short[mw * mh]
      for (int i = 0; i < tmp2.length; i++) apix[i] = (short) tmp2[i]
    }
    def afp = Px.toFloat(apix, mw, mh)
    new GaussianBlur().blurGaussian(afp, 1.0d)
    float[] af = (float[]) afp.getPixels()

    byte[] tmask = (byte[]) bp.getPixels()
    def dfp = new FloatProcessor(mw, mh)
    float[] dens = (float[]) dfp.getPixels()
    long agerPos = 0
    for (int i = 0; i < dens.length; i++) {
      boolean inTissue = (tmask[i] & 0xFF) > 127
      boolean pos = inTissue && af[i] >= AGER_THRESHOLD
      dens[i] = pos ? 1f : 0f
      if (pos) agerPos++
    }
    double sigmaPx = DAMAGE_SIGMA / (pxUm * TISSUE_DS)
    new GaussianBlur().blurGaussian(dfp, sigmaPx)

    def dbp = new ByteProcessor(mw, mh)
    byte[] dmask = (byte[]) dbp.getPixels()
    long dCount = 0
    for (int i = 0; i < dens.length; i++) {
      if (((tmask[i] & 0xFF) > 127) && dens[i] < DAMAGE_CUTOFF) { dmask[i] = (byte) 255; dCount++ }
    }
    if (dCount > 0) {
      def dsimple = Px.toSimpleImage(dbp, mw, mh)
      gDamaged = ContourTracing.createTracedGeometry(dsimple, 0.5d, Double.POSITIVE_INFINITY, req)
      gDamaged = GeometryTools.constrainToBounds(gDamaged, 0, 0, W, H)
      gDamaged = polygonal(gDamaged)
      if (gDamaged != null && !gDamaged.isEmpty()) {
        gDamaged = polygonal(gDamaged.intersection(g))
        damagedMm2 = gDamaged.getArea() * pxAreaUm2 / 1e6
      }
    }
    logMsg(String.format(
        "  damaged territory: AGERthr=%.0f (FIXED) sigma=%.0fum cutoff=%.2f -> " +
        "AGER+=%.1f%% of tissue, damaged=%.2f mm2 (%.1f%% of tissue)  (%d ms)",
        AGER_THRESHOLD, DAMAGE_SIGMA, DAMAGE_CUTOFF,
        100.0 * agerPos / Math.max(1L, fgPx), damagedMm2,
        tissueMm2 > 0 ? 100.0 * damagedMm2 / tissueMm2 : 0.0d,
        System.currentTimeMillis() - tD))
  }

  // ---- tile grid --------------------------------------------------------
  def tilesDir = new File(slideOut, "tiles")
  if (!tilesDir.mkdir()) failRun("Could not create tiles directory: " + tilesDir.absolutePath)
  def prep = new PreparedGeometryFactory().create(g)

  def manifestRows = []
  def candidateRows = []
  def rasterAreaPx = [:]        // tileId -> [core:, damaged:] as RASTERISED pixels
  double coreTissueTotalPx = 0.0d
  int nWritten = 0, nResumed = 0
  int nOutsideTissue = 0, nSkippedLowTissue = 0, nSkippedEmptyRaster = 0
  long tExport = System.currentTimeMillis()

  boolean capped = false
  int nExcludedByCap = 0
  for (int cy = 0; cy < H; cy += CORE_PX) {
    for (int cx = 0; cx < W; cx += CORE_PX) {
      int cw = Math.min(CORE_PX, W - cx)
      int chh = Math.min(CORE_PX, H - cy)
      String tileId = String.format("x%06d_y%06d", cx, cy)
      def candidateBase = [
        tile_id: tileId, core_x: cx, core_y: cy, core_w: cw, core_h: chh,
        core_tissue_area_px: 0.0d, core_tissue_area_um2: 0.0d
      ]
      if (MAX_TILES > 0 && manifestRows.size() >= MAX_TILES) {
        capped = true
        nExcludedByCap++
        candidateRows << candidateBase + [
          status: "excluded_by_max_tiles_cap",
          reason: "candidate_not_evaluated_after_IFQ_WSI_MAX_TILES_PER_SLIDE"
        ]
        continue
      }
      def rect = GeometryTools.createRectangle(cx, cy, cw, chh)
      if (!prep.intersects(rect)) {
        nOutsideTissue++
        candidateRows << candidateBase + [
          status: "outside_tissue", reason: "no_intersection_with_global_tissue_mask"
        ]
        continue
      }
      Geometry gi = polygonal(g.intersection(rect))
      if (gi == null || gi.isEmpty()) {
        nOutsideTissue++
        candidateRows << candidateBase + [
          status: "outside_tissue", reason: "empty_global_tissue_intersection"
        ]
        continue
      }
      double coreTissuePx = gi.getArea()
      double coreTissueUm2 = coreTissuePx * pxAreaUm2
      candidateBase.core_tissue_area_px = coreTissuePx
      candidateBase.core_tissue_area_um2 = coreTissueUm2
      if (coreTissueUm2 < MIN_TISSUE_UM2) {
        nSkippedLowTissue++
        candidateRows << candidateBase + [
          status: "below_minimum_tissue",
          reason: "core_tissue_area_below_IFQ_WSI_MIN_TILE_TISSUE_UM2"
        ]
        continue
      }
      // Geometric damaged area for this core, used as the resume-path fallback.
      double coreDamagedPx = 0.0d
      if (PARTITION && gDamaged != null && !gDamaged.isEmpty()) {
        def gd = polygonal(gi.intersection(gDamaged))
        if (gd != null && !gd.isEmpty()) coreDamagedPx = gd.getArea()
      }

      // export window = core grown by the halo, clipped to the slide
      int ex = Math.max(0, cx - HALO_PX)
      int ey = Math.max(0, cy - HALO_PX)
      int ex2 = Math.min(W, cx + cw + HALO_PX)
      int ey2 = Math.min(H, cy + chh + HALO_PX)
      int ew = ex2 - ex, eh = ey2 - ey

      String tileBase = stem.replaceAll(/[^A-Za-z0-9._-]+/, "-") + "_" + tileId
      def tileFile    = new File(tilesDir, tileBase + ".ome.tif")
      // IF_Quant_Pipeline strips ONLY the final extension, so the companion for
      // "foo.ome.tif" must be "foo.ome_RoiSet.zip" -- NOT "foo_RoiSet.zip".
      def roiFile     = new File(tilesDir, tileBase + ".ome_RoiSet.zip")

      boolean haveTile = RESUME && tileFile.isFile() && tileFile.length() > 0 && roiFile.isFile()
      if (haveTile) {
        nResumed++
      } else if (!DRY_RUN) {
        // --- ROIs, in coordinates local to the EXPORT window (clipped at slide
        //     edges, so use ex/ey rather than cx-HALO).
        //
        // RASTERISE rather than convert geometry directly. Two reasons:
        //  1. java.awt.geom.Area.getBounds() rounds OUTWARD from getBounds2D(),
        //     so a curved boundary turns an in-bounds shape into
        //     Rectangle[x=-1, width=2305] on a 2304 px tile -- which the engine
        //     rejects, silently dropping that tile's tissue AND pod area.
        //     A mask is integer and in-bounds by construction.
        //  2. When partitioning, damaged and intact share a boundary. Converting
        //     each separately can assign a boundary pixel to BOTH, and the engine
        //     hard-fails on ROIs that overlap by even one pixel. Painting them
        //     into one label image makes them disjoint by construction.
        def lab = new ByteProcessor(ew, eh)
        def paint = { Geometry gm, int v ->
          if (gm == null || gm.isEmpty()) return
          def loc = AffineTransformation
              .translationInstance((double) -ex, (double) -ey).transform(gm)
          ROI r2 = GeometryTools.geometryToROI(loc, ImagePlane.getDefaultPlane())
          if (r2 == null || r2.isEmpty()) return
          def ir = IJTools.convertToIJRoi(r2, new ij.measure.Calibration(), 1.0d)
          lab.setValue(v); lab.fill(ir)
        }
        // Paint the whole core-tissue region first, then overwrite the intact
        // part. Order makes the shared boundary deterministic and unambiguous.
        paint(gi, 1)
        if (PARTITION) {
          Geometry giIntact = (gDamaged == null || gDamaged.isEmpty()) ? gi
                              : polygonal(gi.difference(gDamaged))
          paint(giIntact, 2)
        }

        def regions = []
        if (PARTITION) {
          regions << [name: ROI_DAMAGED, val: 1]
          regions << [name: ROI_INTACT,  val: 2]
        } else {
          regions << [name: ROI_NAME, val: 1]
        }

        def entries = []
        double coreRasterPx = 0.0d, damagedRasterPx = 0.0d
        byte[] lp = (byte[]) lab.getPixels()
        regions.each { reg ->
          long cnt = 0
          def m = new ByteProcessor(ew, eh)
          byte[] mp = (byte[]) m.getPixels()
          for (int i = 0; i < lp.length; i++) {
            if ((lp[i] & 0xFF) == reg.val) { mp[i] = (byte) 255; cnt++ }
          }
          if (cnt <= 0) return              // omit empty regions; never write an empty zip
          coreRasterPx += cnt
          if (reg.val == 1 && PARTITION) damagedRasterPx = cnt
          m.setThreshold(128, 255, ImageProcessor.NO_LUT_UPDATE)
          def ir = new ThresholdToSelection().convert(m)
          if (ir == null) return
          ir.setName(reg.name)
          def bb = ir.getBounds()
          if (bb.x < 0 || bb.y < 0 || bb.x + bb.width > ew || bb.y + bb.height > eh)
            failRun("Tile " + tileBase + " region '" + reg.name + "': bounds " + bb +
                    " outside the " + ew + "x" + eh + " tile. Stage 2 would reject it.")
          entries << [name: reg.name, roi: ir]
        }
        if (entries.isEmpty()) {
          nSkippedEmptyRaster++
          candidateRows << candidateBase + [
            status: "empty_raster",
            reason: "global_tissue_geometry_rasterized_to_no_roi_pixels"
          ]
          continue
        }

        def zos = new ZipOutputStream(new BufferedOutputStream(new FileOutputStream(roiFile)))
        try {
          entries.each { en ->
            def bos = new ByteArrayOutputStream()
            new RoiEncoder(bos).write(en.roi)   // returns void; do not test its value
            zos.putNextEntry(new ZipEntry(en.name + ".roi"))
            zos.write(bos.toByteArray())
            zos.closeEntry()
          }
        } finally { zos.close() }
        rasterAreaPx[tileId] = [core: coreRasterPx, damaged: damagedRasterPx]

        // --- tile: flat single-series OME-TIFF (multi-series is rejected by Stage 2)
        withRetry("writeTile(" + tileBase + ")", 3) {
          if (tileFile.exists()) tileFile.delete()
          def ser = new OMEPyramidWriter.Builder(server)
              .region(ex, ey, ew, eh)
              .downsamples(1.0d)
              .compression(compType)
              .channelsPlanar()
              .tileSize(WRITE_TILE_PX)
              .parallelize(PARALLEL)
              .name(tileBase)
              .build()
          OMEPyramidWriter.createWriter(ser).writeImage(tileFile.getAbsolutePath())
          return true
        }
        nWritten++
      }

      coreTissueTotalPx += coreTissuePx
      candidateRows << candidateBase + [
        status: (DRY_RUN ? "dry_run" : (haveTile ? "resumed" : "exported")),
        reason: (DRY_RUN ? "tile_export_suppressed_by_IFQ_WSI_DRY_RUN" :
                 (haveTile ? "existing_tile_and_roi_reused" : "stage1_tile_and_roi_written"))
      ]
      manifestRows << [
        tile_id: tileId, tile_file: tileFile.name, roiset_file: roiFile.name,
        slide_stem: stem, source_vsi: slideFile.name,
        series_index: chosen.series, series_name: chosen.name,
        pixel_size_um: pxUm, pixel_size_um_y: pxUmH,
        core_x: cx, core_y: cy, core_w: cw, core_h: chh,
        export_x: ex, export_y: ey, export_w: ew, export_h: eh,
        halo_left: cx - ex, halo_top: cy - ey,
        halo_right: ex2 - (cx + cw), halo_bottom: ey2 - (cy + chh),
        core_tissue_area_px: coreTissuePx,
        core_tissue_area_um2: coreTissueUm2,
        // Rasterised areas are what Stage 2 actually measures (the engine counts
        // ROI pixels), so Stage 3 reconciles against these, not the polygon area.
        // A RESUMED tile was not rasterised in this run, so fall back to the
        // geometric area rather than silently reporting zero.
        core_raster_area_um2:
            (rasterAreaPx[tileId] != null ? rasterAreaPx[tileId].core : coreTissuePx) * pxAreaUm2,
        damaged_raster_area_um2:
            (rasterAreaPx[tileId] != null ? rasterAreaPx[tileId].damaged : coreDamagedPx) * pxAreaUm2,
        partitioned: PARTITION,
        mouse_id: md.mouse_id, genotype: md.genotype, condition: md.condition,
        section_id: tileId, panel: PANEL,
        region_name: (PARTITION ? (ROI_DAMAGED + "|" + ROI_INTACT) : ROI_NAME)
      ]

      if ((manifestRows.size() % 25) == 0)
        logMsg("    ... " + manifestRows.size() + " tiles (" + nWritten + " written, " + nResumed + " resumed)")
    }
  }

  // The candidate ledger is the sampling-frame record. It includes grid cores
  // that never become Stage 2 inputs; tile_manifest.csv remains restricted to
  // physical tile/ROI pairs so downstream exact-cover checks stay meaningful.
  def candidateCols = candidateRows[0].keySet() as List
  def candidateCsv = new StringBuilder(csvRow(candidateCols)).append("\n")
  candidateRows.each { r ->
    candidateCsv.append(csvRow(candidateCols.collect { c -> r[c] })).append("\n")
  }
  def candidateManifestFile = new File(slideOut, "tile_candidate_manifest.csv")
  candidateManifestFile.setText(candidateCsv.toString(), "UTF-8")

  if (manifestRows.isEmpty())
    failRun("No tiles intersect tissue for " + slideFile.name + " -- refusing to emit an empty run.")

  // ---- reconciliation: per-tile core areas must sum to the slide tissue ----
  double sumMm2 = coreTissueTotalPx * pxAreaUm2 / 1e6
  double relDiff = Math.abs(sumMm2 - tissueMm2) / tissueMm2
  logMsg(String.format("  tiles=%d written=%d resumed=%d outside=%d below-min(<%.0f um2)=%d empty-raster=%d   (%d ms)",
      manifestRows.size(), nWritten, nResumed, nOutsideTissue,
      MIN_TISSUE_UM2, nSkippedLowTissue, nSkippedEmptyRaster,
      System.currentTimeMillis() - tExport))
  logMsg(String.format("  SEAM CHECK: sum(core tissue) = %.4f mm2 vs slide tissue %.4f mm2  (rel diff %.3e)",
      sumMm2, tissueMm2, relDiff))
  if (capped) {
    logMsg("  *** CAPPED at IFQ_WSI_MAX_TILES_PER_SLIDE=" + MAX_TILES +
           " -- SMOKE TEST ONLY, coverage is incomplete and the seam check above is expected to fail. ***")
  } else if (relDiff > 1e-6 && nSkippedLowTissue == 0 && nSkippedEmptyRaster == 0) {
    failRun("Tile/reference area reconciliation failed for " + slideFile.name +
            ": relative difference " + relDiff + " exceeds 1e-6. " +
            "Refusing to declare complete Stage 1 coverage.")
  }

  // ---- samplesheet.csv, written INTO the tiles folder (= Stage 2 INPUT_DIR) --
  def ss = new StringBuilder("filename,mouse_id,section_id,genotype,condition,panel\n")
  manifestRows.each { r ->
    ss.append(csvRow([r.tile_file, r.mouse_id, r.section_id, r.genotype, r.condition, r.panel])).append("\n")
  }
  new File(tilesDir, "samplesheet.csv").setText(ss.toString(), "UTF-8")

  // ---- tile_manifest.csv --------------------------------------------------
  def cols = manifestRows[0].keySet() as List
  def mf = new StringBuilder(csvRow(cols)).append("\n")
  manifestRows.each { r -> mf.append(csvRow(cols.collect { c -> r[c] })).append("\n") }
  new File(slideOut, "tile_manifest.csv").setText(mf.toString(), "UTF-8")

  totalTiles += manifestRows.size()
  boolean coverageComplete = !capped && !DRY_RUN &&
      nSkippedLowTissue == 0 && nSkippedEmptyRaster == 0 && relDiff <= 1e-6
  server.close()
  def sourcePackageAfter = withRetry("source package post-run verification", 3) {
    buildSourcePackage(slideFile)
  }
  if (sourcePackageAfter != sourcePackageBefore)
    failRun("Bio-Formats source-package membership or bytes changed during Stage 1 for " +
            slideFile.name + "; refusing to publish stage1_manifest.json")
  referenceArtifactsToVerify.each { artifact ->
    def after = contentRecord(artifact.file, artifact.label + " post-run verification")
    if (after.size_bytes != artifact.content.size_bytes || after.sha256 != artifact.content.sha256)
      failRun(artifact.label + " changed while Stage 1 was active; refusing to publish the manifest")
  }
  if (referenceMaskProfile != null) {
    def declared = referenceMaskProfile.slides[slideFile.name]
    [[artifact: declared.tissue_mask, label: "external tissue mask"],
     [artifact: declared.airway_mask, label: "external airway mask"]].each { item ->
      def after = contentRecord(item.artifact._file, item.label + " post-run verification")
      if (after.size_bytes != item.artifact.size_bytes || after.sha256 != item.artifact.sha256)
        failRun(item.label + " changed while Stage 1 was active; refusing to publish the manifest")
    }
  }
  referenceSpaceRecord.content_verified_before_and_after = true
  runRecord.slides << [
    source_vsi: slideFile.name, slide_stem: stem,
    source_package: sourcePackageBefore,
    series_index: chosen.series, series_name: chosen.name,
    width: W, height: H, pixel_size_um: pxUm, pixel_size_um_y: pxUmH, n_channels: chosen.nChannels,
    channel_names: chosen.channelNames,
    ordered_channel_names: chosen.channelNames,
    ordered_channel_patterns: ORDERED_CHANNEL_PATTERN_TEXTS,
    channel_order_authority: "acquisition_order_pattern_only_not_biological_identity",
    tissue_threshold_otsu: thr, tissue_area_mm2: tissueMm2,
    reference_space: referenceSpaceRecord,
    sum_core_tissue_mm2: sumMm2, seam_check_rel_diff: relDiff,
    n_tiles: manifestRows.size(), n_written: nWritten, n_resumed: nResumed,
    tile_candidate_manifest: candidateManifestFile.name,
    n_grid_cores_total: (int)(Math.ceil(W / (double)CORE_PX) * Math.ceil(H / (double)CORE_PX)),
    n_grid_cores_visited: candidateRows.size(),
    n_outside_tissue: nOutsideTissue,
    n_skipped_low_tissue: nSkippedLowTissue,
    n_skipped_empty_raster: nSkippedEmptyRaster,
    n_candidate_exported: candidateRows.count { it.status == "exported" },
    n_candidate_resumed: candidateRows.count { it.status == "resumed" },
    n_candidate_dry_run: candidateRows.count { it.status == "dry_run" },
    n_candidate_excluded_by_cap: nExcludedByCap,
    coverage_complete: coverageComplete, max_tiles_cap: MAX_TILES, dry_run: DRY_RUN,
    mouse_id: md.mouse_id, genotype: md.genotype, condition: md.condition,
    tiles_dir: tilesDir.getAbsolutePath()
  ]
}

if (referenceMaskProfile != null) {
  def profileAfter = contentRecord(
      referenceMaskProfile.file, "reference-mask profile post-run verification")
  def publishedProfileAfter = contentRecord(
      new File(outRoot, publishedReferenceProfile.published_relative_path),
      "published reference-mask profile post-run verification")
  if (profileAfter.size_bytes != referenceMaskProfile.content.size_bytes ||
      profileAfter.sha256 != referenceMaskProfile.content.sha256 ||
      publishedProfileAfter.size_bytes != referenceMaskProfile.content.size_bytes ||
      publishedProfileAfter.sha256 != referenceMaskProfile.content.sha256)
    failRun("Reference-mask profile changed while Stage 1 was active; refusing to publish the manifest")
}

def stage1ScriptRecordAfter = [
  name: stage1ScriptFile.name,
  size_bytes: stage1ScriptFile.length(),
  sha256: ContentHash.sha256File(stage1ScriptFile)
]
if (stage1ScriptRecordAfter != stage1ScriptRecord)
  failRun("The executed Stage 1 script changed while the run was active; refusing to " +
          "publish stage1_manifest.json")

def stage1ManifestPath = new File(outRoot, "stage1_manifest.json").toPath()
def stage1ManifestTemp = Files.createTempFile(outRoot.toPath(), ".stage1_manifest.", ".tmp")
try {
  Files.write(stage1ManifestTemp,
      GsonTools.getInstance(true).toJson(runRecord).getBytes(StandardCharsets.UTF_8))
  try {
    Files.move(stage1ManifestTemp, stage1ManifestPath, StandardCopyOption.ATOMIC_MOVE)
  } catch (java.nio.file.AtomicMoveNotSupportedException unsupported) {
    Files.move(stage1ManifestTemp, stage1ManifestPath)
  }
} finally {
  Files.deleteIfExists(stage1ManifestTemp)
}

logMsg("")
logMsg("=================================================================")
logMsg("DONE. " + slides.size() + " slide(s), " + totalTiles + " tiles -> " + outRoot.getAbsolutePath())
logMsg("Wrote stage1_manifest.json and, per slide, tile_candidate_manifest.csv + tile_manifest.csv + tiles/samplesheet.csv")
logMsg("")
logMsg("NEXT (Stage 2) -- per slide, against the UNMODIFIED engine:")
logMsg("  IFQ_INPUT_DIR=<slide>/tiles  IFQ_OUTPUT_DIR=<slide>/analysis")
logMsg("  IFQ_PANEL=" + PANEL + "                     # default is 'T' (pilot) -- MUST be set")
logMsg("  IFQ_MIN_INCLUDED_NUCLEI=0            # or sparse tiles are dropped, losing their area")
logMsg("  IFQ_KRT5_THRESHOLD / IFQ_AGER_THRESHOLD / IFQ_T1A_THRESHOLD")
logMsg("                                       # MANDATORY: adaptive Otsu on a background tile")
logMsg("                                       # reports KRT5_pod_area_frac ~0.89")
