# BAY logo assets

## Files

| File | Use |
|---|---|
| `full-light.svg` | Full wordmark — ivory + gold for dark backgrounds |
| `full-dark.svg` | Full wordmark — black + gold for light backgrounds |
| `mark-light.svg` | Compact mark (just "BAY" + Double Dot) for dark backgrounds |
| `mark-dark.svg` | Compact mark (just "BAY" + Double Dot) for light backgrounds |
| `BAY-Brand-Identity-System.pdf` | Source — full brand guidelines reference |

## Construction

The logos are hand-built SVGs per the BI PDF spec rather than
PDF-extracted, because:
1. PyMuPDF found zero embedded raster images in the BI PDF — the
   wordmark is drawn as PDF vector ops, which extract messily.
2. Hand-built SVGs using the spec are smaller, cleaner, and easier
   to maintain than path-extracted blobs.

The SVGs use Google Fonts (`Bebas Neue` for the wordmark, `Archivo`
for the subline) loaded via `@import` inside the SVG `<defs>`. This
means:
- The SVGs render correctly when viewed in any web browser (which
  fetches the fonts on demand)
- For print / static-image rendering (PNG export, slide embed),
  the rendering engine should have these fonts available locally OR
  network access at render time

If absolute portability matters (e.g. for a sponsor deliverable),
convert the `<text>` elements to outlined `<path>` glyphs using
Inkscape's "Object to Path" or Illustrator's "Create Outlines".

## brand.yml mapping

```yaml
logo:
  full_light: "content/logos/full-light.svg"
  full_dark:  "content/logos/full-dark.svg"
  mark_light: "content/logos/mark-light.svg"
  mark_dark:  "content/logos/mark-dark.svg"
```

## Brand-system specifics (per the BI PDF)

- **Wordmark**: "BAY" in Bebas Neue, geometric, wide-tracked
- **Subline**: "BLACK AND YELLOW" in Archivo wide-tracked uppercase,
  thin weight
- **Double Dot device**: two gold dots (#D4A843) — the brand's
  punctuation element, signaling the "Double Yellow Dot" of pro
  squash
- **Color rule**: 85% deep black (#0A0A0A), 10% gold (#D4A843), 5%
  warm ivory (#F5F2ED). Gold is canonical, not bright yellow.
