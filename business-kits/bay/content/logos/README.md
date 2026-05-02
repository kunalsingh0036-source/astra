# BAY logo assets — source state

**Status (2026-05-02):** No standalone logo files have been
extracted from the brand identity. The canonical source is the
`BAY-Brand-Identity-System.pdf` in this directory.

The brand kit's `brand.yml` references logo paths under
`content/logos/full-light.svg`, `full-dark.svg`, `mark-light.svg`,
`mark-dark.svg` — **these placeholders do not yet exist**. Astra's
deck templates will fall back to text-only rendering of "BAY" in
Bebas Neue + the Double Dot device drawn in CSS until proper
SVG/PNG files are exported from the BI PDF.

## To resolve

1. Open `BAY-Brand-Identity-System.pdf` in a vector editor
   (Illustrator, Affinity Designer, Inkscape, Figma)
2. Extract the canonical wordmark in two color variants:
   - **Light variant** (gold + ivory on black) → `full-light.svg`
   - **Dark variant** (gold + black on ivory) → `full-dark.svg`
3. Extract the "BAY" mark alone (no "BLACK AND YELLOW" subline) in
   the same two variants → `mark-light.svg`, `mark-dark.svg`
4. Update `brand.yml` to remove the placeholder warning

## Brand-system specifics (per the BI PDF)

- **Wordmark**: "BAY" in Bebas Neue, geometric serif-influenced
  display, wide-tracked
- **Subline**: "BLACK AND YELLOW" wide-tracked uppercase, thin
  weight
- **Double Dot device**: two gold dots (#D4A843), used as a
  punctuation/separator element
- **Color rule**: 85% deep black (#0A0A0A), 10% gold (#D4A843), 5%
  warm ivory (#F5F2ED). The gold is *gold*, not bright yellow —
  flagged in `brand.yml` for Kunal to confirm or override.
