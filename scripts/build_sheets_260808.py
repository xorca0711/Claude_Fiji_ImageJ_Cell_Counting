"""Build samplesheet.csv and panel_map.csv for the 260808-CW confocal run.

The folder names encode everything, so parse them rather than hand-typing:
  IFNg ko(het) 260325 M4-1 PR8 infection krt5_488 mRAGE_555 T1a_647 20x 2k_Cycle_03
   ^genotype        ^mouse ^condition    ^panel markers            ^mag

Only 20x fields are analysed. The 4x mapping fields are navigation images and
the engine's own discovery already skips Map_A*.oir; we exclude the whole 4x
folder so a mapping field can never be mistaken for an analysis field.
"""
import csv, os, re, sys

ROOT = r"D:\Microscopy_Images\260808-CW_Confocal\260808-CW"
OUT = r"D:\IFQ_Runs\confocal_260808"
os.makedirs(OUT, exist_ok=True)

rows, panel_rows = [], []
skipped_4x = skipped_map = 0

for folder in sorted(os.listdir(ROOT)):
    fp = os.path.join(ROOT, folder)
    if not os.path.isdir(fp):
        continue

    if "4x mapping" in folder:
        skipped_4x += 1
        continue
    if "20x" not in folder:
        continue

    # genotype: ko(het) / ko(hom)
    m = re.search(r"ko\((het|hom)\)", folder)
    genotype = "IFNg_KO_" + m.group(1) if m else "NA"

    # mouse id: M4-1, M4-2, M2, M6
    m = re.search(r"\b(M\d+(?:-\d+)?)\b", folder)
    mouse = m.group(1) if m else "NA"

    # condition: "PR8 no infection" must be matched BEFORE "PR8 infection"
    if "no infection" in folder:
        condition = "uninfected"
    elif "infection" in folder:
        condition = "PR8"
    else:
        condition = "NA"

    # panel from the marker triple in the folder name
    low = folder.lower()
    if "krt5_488" in low and "t1a_647" in low:
        panel = "LEFT"
    elif "propsc_488" in low and "krt8_647" in low:
        panel = "RIGHT"
    else:
        print("UNRECOGNISED PANEL, skipping:", folder)
        continue

    for fn in sorted(os.listdir(fp)):
        if not fn.lower().endswith(".oir"):
            continue
        if fn.startswith("Map_A"):
            skipped_map += 1
            continue
        rel = os.path.join(folder, fn).replace("\\", "/")
        # section_id must be UNIQUE per image: outputKey is
        # <mouse>_<condition>_<panel>_<section_id>, and aggregate_to_mouse
        # rejects duplicate (image, region, section_id, panel) rows.
        stem = os.path.splitext(fn)[0]
        tail = stem.split("_")[-2:] if "_" in stem else [stem]
        cyc = folder.split("_Cycle")[-1] or "00"
        section = (cyc.strip("_") or "00") + "_" + "_".join(tail)
        rows.append(dict(relative_path=rel, mouse_id=mouse, section_id=section,
                         genotype=genotype, condition=condition, panel=panel))
        panel_rows.append(dict(relative_path=rel, panel=panel))

with open(os.path.join(OUT, "samplesheet.csv"), "w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=["relative_path", "mouse_id", "section_id",
                                       "genotype", "condition", "panel"])
    w.writeheader(); w.writerows(rows)

with open(os.path.join(OUT, "panel_map.csv"), "w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=["relative_path", "panel"])
    w.writeheader(); w.writerows(panel_rows)

print("analysis fields :", len(rows))
print("4x folders skipped:", skipped_4x, " Map_A skipped:", skipped_map)
seen = {}
for r in rows:
    seen[(r["mouse_id"], r["condition"], r["genotype"], r["panel"])] = \
        seen.get((r["mouse_id"], r["condition"], r["genotype"], r["panel"]), 0) + 1
print()
print(f"{'mouse':6} {'genotype':14} {'condition':11} {'panel':6} fields")
for k in sorted(seen):
    print(f"{k[0]:6} {k[2]:14} {k[1]:11} {k[3]:6} {seen[k]}")
ids = [(r["mouse_id"], r["section_id"], r["panel"]) for r in rows]
print()
print("duplicate (mouse, section_id, panel):", len(ids) - len(set(ids)))
