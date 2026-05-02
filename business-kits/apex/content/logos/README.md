# Apex logo assets

## Files

| File | Source | Use |
|---|---|---|
| `apex-logo.png` | `apex-human-website/public/products/apex-logo.png` | Canonical logo as it currently appears on theapexhumancompany.com |
| `apex-human-logo.pdf` | `apex-human-website/apex-human-logo.pdf` | Vector master — for high-res print, large-format slides |
| `wordmark-experimental-terracotta.svg` | `logo-options/experimental/option-04-terracotta-creme.svg` | Experimental wordmark in terracotta + crème palette (closest match to current brand crimson) |
| `wordmark-experimental-brown.svg` | `logo-options/experimental/option-05-terracotta-brown.svg` | Alt experimental wordmark in muted brown palette |

## Notes for Kunal

- The "experimental" SVGs in `apex-human-website/logo-options/` are
  in flux — 10 color variants exploring navy, terracotta, ochre,
  teal, charcoal. None of them exactly match the current `brand.yml`
  crimson + crème spec.
- Once a final wordmark is locked, replace the PNG with a clean
  SVG export and update `brand.yml`'s logo paths to point at the
  canonical files.

## brand.yml mapping

The kit's `brand.yml` should reference these paths once the canonical
files are locked. Until then, `apex-logo.png` is the working default
for both light and dark surfaces.
