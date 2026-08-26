# Sealed StarDist 2D runtime

StarDist is an optional 2D nucleus-segmentation route. It is no longer allowed
to use a plugin-managed built-in model or the global ROI Manager. The engine
invokes the official `de.csbdresden.stardist.StarDist2D` SciJava command without
postprocessing UI, requests its `label` Dataset output, and deterministically
converts each nonzero integer label into one ImageJ ROI.

Classic watershed remains the default and does not require StarDist files.

## Activation contract

Set all three variables for a quantitative StarDist run:

```powershell
$env:IFQ_SEGMENTER = "stardist"
$env:IFQ_STARDIST_MODEL_PATH = "D:\IFQ_Runtime\models\lung-nuclei-v1.zip"
$env:IFQ_STARDIST_RUNTIME_MANIFEST = "D:\IFQ_Runtime\stardist-runtime.json"
```

`IFQ_STARDIST_MODEL_PATH` must be an existing, non-symlink `.zip` exported for
the Fiji StarDist plugin. Every ZIP entry is streamed to validate archive
integrity; empty archives, duplicate names, and path-traversal entries fail.
`IFQ_STARDIST_RUNTIME_MANIFEST` must be strict UTF-8, have a `.json` extension,
and validate against
[`schemas/stardist-runtime-manifest.schema.json`](../schemas/stardist-runtime-manifest.schema.json).
Relative artifact paths are resolved beneath the manifest folder; absolute
paths are also accepted. Parent traversal is rejected.

The manifest is deliberately closed. Its four required roles are:

| Role | Required class evidence |
| --- | --- |
| `stardist_plugin` | `de.csbdresden.stardist.StarDist2D`, `de.csbdresden.stardist.StarDist2DNMS` |
| `csbdeep_plugin` | `de.csbdresden.csbdeep.commands.GenericNetwork` |
| `tensorflow_java` | `org.tensorflow.Graph` |
| `tensorflow_native` | none; this record binds the platform-native library bytes |

Add more nonempty artifacts when the local installation has other runtime files
that must be frozen. Roles and resolved paths must be unique. For each declared Java
class, the engine confirms that the JVM actually loaded it from the declared
artifact path; a hash-matching but unused JAR is not accepted as authority.

Example shape (replace every placeholder with the exact local file size and
lowercase SHA-256):

```json
{
  "schema_version": "1.0.0",
  "profile_id": "fiji-stardist-lung-v1",
  "artifacts": [
    {
      "role": "stardist_plugin",
      "path": "StarDist_.jar",
      "size_bytes": 123,
      "sha256": "0000000000000000000000000000000000000000000000000000000000000000",
      "expected_classes": [
        "de.csbdresden.stardist.StarDist2D",
        "de.csbdresden.stardist.StarDist2DNMS"
      ]
    },
    {
      "role": "csbdeep_plugin",
      "path": "CSBDeep_.jar",
      "size_bytes": 123,
      "sha256": "0000000000000000000000000000000000000000000000000000000000000000",
      "expected_classes": [
        "de.csbdresden.csbdeep.commands.GenericNetwork"
      ]
    },
    {
      "role": "tensorflow_java",
      "path": "tensorflow.jar",
      "size_bytes": 123,
      "sha256": "0000000000000000000000000000000000000000000000000000000000000000",
      "expected_classes": ["org.tensorflow.Graph"]
    },
    {
      "role": "tensorflow_native",
      "path": "tensorflow_jni.dll",
      "size_bytes": 123,
      "sha256": "0000000000000000000000000000000000000000000000000000000000000000",
      "expected_classes": []
    }
  ]
}
```

The model, manifest, and every listed artifact are SHA-256 verified before and
after each StarDist command and again at the end of the run. Any byte or Java
code-source mismatch fails the run. Symbolic-link leaf paths are rejected.

## Output evidence

For each analysis region the engine writes
`*__StarDist_instance_labels.tif`. The per-image `*__params.json` records:

- model and runtime-manifest content identities;
- every runtime artifact identity and class-to-code-source binding;
- the exact output TIFF identity;
- dimensions, label count, and a canonical SHA-256 over unsigned 16-bit,
  little-endian, row-major label pixels.

The published TIFF is rehashed before the params record is written. The
canonical pixel digest distinguishes computational output from TIFF container
metadata.

Label output is accepted only when every pixel is a finite integer in the
unsigned-16 range. A label above 65,535 fails before pixel hashing or TIFF
publication instead of being truncated into a different instance identifier.

## Validation boundary

This closes the software activation and provenance path. It does not establish
that a chosen model, probability threshold, NMS threshold, or tiling setting is
biologically valid for a study. A representative blinded fixture must still be
benchmarked against expert nucleus annotations before StarDist-derived counts
are reportable. The plugin is 2D; this route does not claim 3D nuclear
reconstruction.

The sealed identities and canonical output digest make runtime drift detectable;
they do not by themselves prove bit-identical TensorFlow inference across a new
CPU, JVM, or operating-system build. Promoting a runtime profile therefore also
requires a replay on a fixed technical fixture and comparison of its canonical
label digest before the biological benchmark.

## API source anchors

The command contract is taken from the upstream StarDist ImageJ implementation:

- [`StarDist2D.java`](https://github.com/stardist/stardist-imagej/blob/master/src/main/java/de/csbdresden/stardist/StarDist2D.java)
  declares the `Dataset input`, file-model parameter, and `Dataset label` output.
- [`Opt.java`](https://github.com/stardist/stardist-imagej/blob/master/src/main/java/de/csbdresden/stardist/Opt.java)
  defines the exact `Model (.zip) from File` and `Label Image` option strings.
- [StarDist ImageJ README](https://github.com/stardist/stardist-imagej)
  documents the CSBDeep and StarDist Fiji update-site dependencies and the
  plugin's 2D scope.
