# HelmTech logo assets

## Files

| File | Use |
|---|---|
| `logo.svg` | Canonical wordmark — neutral surface |
| `logo-light.svg` | Light variant — for dark backgrounds (matte black, navy) |
| `logo-dark.svg` | Dark variant — for light backgrounds (soft sand, white) |
| `logo-512.png` | 512×512 raster export — for slide thumbnails, social cards |
| `logo-1024.png` | 1024×1024 raster export — for print, large-format slides |

## Source

All files copied from
`helmtech-outreach-agent/dashboard/public/`, which is the canonical
HelmTech logo directory used by the production HelmTech outreach
dashboard. These are kept in sync with helmtech.in.

## brand.yml mapping

The kit's `brand.yml` references:
- `full_light: "content/logos/logo-light.svg"` (logo for dark backgrounds)
- `full_dark: "content/logos/logo-dark.svg"` (logo for light backgrounds)
- `mark_light: "content/logos/logo.svg"`
- `mark_dark: "content/logos/logo.svg"`

## Voice/visual conflict note

The brand identity PDF specifies Matte Black + Emerald Green
(#2ECC71). The live site at helmtech.in uses an acid-green variant
(#22C55E). The logo files here are color-neutral SVGs that work in
either palette — the surrounding brand color is what signals which
identity. Until Kunal locks the final palette, treat the BI PDF as
canonical.
