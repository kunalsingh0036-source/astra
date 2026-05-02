# Business kits — the data layer for Astra's creator capability

Each kit is a self-contained directory describing one company's brand,
voice, audiences, and content library. Astra's `creator` sub-agent
loads the kit when generating any artifact (deck, doc, one-pager,
email, social post) for that company.

## What's here

```
business-kits/
├── _schema/                # canonical templates — start here when adding a new kit
├── helmtech/               # Kunal's 4 active companies
├── apex/
├── bay/
└── top-studios/
```

Future client kits (drafted by Top Studios via the `draft_brand_kit`
tool) follow the same shape and live alongside these.

## How a kit is structured

```
<company>/
├── brand.yml              # structured: colors, fonts, logo paths, taglines
├── voice.md               # narrative: tone rules, do/don't phrases, voice samples
├── thesis.md              # the core IP that never changes (3-layer thesis, etc.)
├── audiences/             # one .md per persona — investor, customer, partner, regulator
│   └── <persona>.md
└── content/               # company-owned materials
    ├── proof-points.md    # numbers, customers, press, testimonials
    ├── logos/             # SVG primary, PNG fallbacks, dark + light variants
    ├── screenshots/       # product screenshots (for software companies)
    └── reference-decks/   # PDFs of past materials Astra learns voice + structure from
```

## Why this shape

**Mixed format on purpose:**

- **YAML for structured fields** (colors, font names, logo paths) →
  the renderer can read these directly without LLM interpretation,
  so colors are exactly right every time.
- **Markdown for narrative** (voice rules, thesis, persona) → the LLM
  reads these to internalize how the company talks. Markdown beats
  rigid schemas for things that humans need to write naturally.
- **Files for binary assets** (logos, screenshots) → referenced by
  path from brand.yml. Renderer embeds them directly.

**Per-company directory isolation:**

A kit is portable. To onboard a Top Studios client, Astra runs
`draft_brand_kit` and produces a new directory in this exact shape
— no special-casing, no "client mode" vs "internal mode". Same
machinery generates artifacts for HelmTech as for a paying customer.

## Adding a new kit

1. Copy `_schema/` into a new directory named after the company
   (lowercase-kebab-case): `business-kits/<company-slug>/`
2. Fill in `brand.yml`, `voice.md`, `thesis.md`
3. Add at least one persona under `audiences/`
4. Add `content/proof-points.md` and any logos/screenshots
5. Reload the kit cache (the creator sub-agent picks up new kits on
   its next invocation; no restart needed since kits are read at tool
   call time, not at boot)

Or — for clients — run the `draft_brand_kit` tool with the source
materials and Astra produces a v0 kit you review and edit.

## Schema versioning

The kit schema is defined by `_schema/`. If we change the schema
(e.g. add a new required field), update `_schema/` first, then
migrate each company kit to match. The creator tools validate kits
against `_schema/brand.yml.template` at load time and warn on missing
fields rather than crashing.
