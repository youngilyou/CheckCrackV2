# datasets/

Raw drone capture data. Not tracked in git — these are large binary photo sets
(GBs per capture), not source code (see `.gitignore`).

## Expected layout

```
datasets/
  <capture_name>/
    .../rgb_images/extracted/rgb/*.JPG   # original DJI stills, EXIF+XMP intact
```

Currently used locally:

```
datasets/UAVID3D/Blume_raw_003/Blume_drone_data_capture_may2021/rgb_images/extracted/rgb/
```

## Getting the data

This is not a public dataset with a known download URL — it's a private drone
capture. To reproduce a run, copy your own DJI JPEGs (untouched — no
re-compression, which strips the EXIF/XMP metadata the pipeline depends on,
see CLAUDE.local.md #5) into a folder under here, or point the pipeline
directly at wherever your captures already live via `--images-dir`.
