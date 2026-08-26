// Read-only Bio-Formats provenance probe for Olympus VSI packages.
//
// Usage:
//   IFQ_VSI_INPUT=<one .vsi file or a directory of .vsi files>
//   "<QuPath console.exe>" script scripts/inspect_vsi_used_files.groovy
//
// Bio-Formats, rather than a sibling-name glob, is the authority for which
// container and companion files participate in opening each slide.  This
// probe intentionally writes nothing; its output is used to verify the package
// membership contract before the same discovery is bound into Stage 1.

import loci.formats.ImageReader

String input = System.getenv("IFQ_VSI_INPUT")
if (input == null || input.trim().isEmpty())
    throw new IllegalArgumentException("IFQ_VSI_INPUT is required")

File candidate = new File(input.trim())
List<File> slides
if (candidate.isDirectory()) {
    slides = candidate.listFiles()
        .findAll { it.isFile() && it.name.toLowerCase().endsWith(".vsi") }
        .sort { it.name }
} else if (candidate.isFile() && candidate.name.toLowerCase().endsWith(".vsi")) {
    slides = [candidate]
} else {
    throw new IllegalArgumentException(
        "IFQ_VSI_INPUT must be a .vsi file or a directory containing .vsi files")
}
if (slides.isEmpty())
    throw new IllegalStateException("No .vsi files found at " + candidate.absolutePath)

slides.each { File slide ->
    ImageReader reader = new ImageReader()
    reader.setFlattenedResolutions(false)
    try {
        reader.setId(slide.canonicalPath)
        List<String> used = (reader.getUsedFiles() ?: new String[0])
            .collect { new File(it).canonicalPath }
            .unique()
            .sort()
        if (!used.contains(slide.canonicalPath))
            throw new IllegalStateException(
                "Bio-Formats used-file list does not contain the source VSI: " + slide)
        println "SLIDE=" + slide.canonicalPath
        println "SERIES_COUNT=" + reader.seriesCount
        println "USED_FILE_COUNT=" + used.size()
        used.each { String path ->
            File file = new File(path)
            if (!file.isFile())
                throw new IllegalStateException("Bio-Formats reported a missing file: " + path)
            println "USED_FILE=" + path + "\tSIZE=" + file.length()
        }
    } finally {
        try { reader.close() } catch (Exception ignored) {}
    }
}
