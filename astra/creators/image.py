"""
Generate hero-image prompts (and optionally images) for slides / artifacts.

Why prompt-first: image-generation APIs change weekly (Gemini 2.0 Flash
Image, fal.ai, Replicate, Recraft, etc.) and the "right" choice depends
on style + budget + IP terms. We always need a *good prompt* —
specific, brand-consistent, voice-aligned — and we don't always need
the actual rendered image right now.

This module's primary export is `generate_hero_image_prompt` — given
an artifact + a slide reference + the kit, it produces a detailed,
brand-aware image prompt that any downstream gen-image API will
render well.

If the GEMINI_API_KEY env var is set, we ALSO call Gemini's
imagen-equivalent and store the resulting image. If not, we only
return the prompt — the founder can paste it into whatever generator
they prefer.
"""

from __future__ import annotations

import base64
import io
import logging
import os
from typing import Any

from astra.creators._shared import (
    IMAGE_PROMPT_MODEL,
    generate_json,
    join_text_fields,
)
from astra.creators.kits import load_kit
from astra.creators.store import create_artifact, get_artifact

logger = logging.getLogger(__name__)


_IMAGE_PROMPT_SYSTEM = """You are Astra's creator sub-agent — image-prompt enhancer.

Given a kit (with brand colors, imagery style, voice) and an
image_hint from a draft artifact, produce a detailed image-generation
prompt that any modern image model (Gemini, Imagen, fal, Replicate,
Recraft) will render well.

A great prompt has:
- Subject (concrete, not abstract)
- Composition (framing, angle, focal length feel)
- Lighting + color palette (anchored to the kit's brand colors)
- Style descriptors (photographic, illustrative, geometric, etc.)
- Negative cues (what to avoid — usually the visual clichés the kit forbids)

Output STRICT JSON:

{
  "prompt": "<the full prompt — single string, comma-separated descriptors. 30-60 words.>",
  "negative_prompt": "<comma-separated list of things to avoid — e.g. 'glowing brain illustrations, AI cliches, stock photography people, lens flares, Comic Sans'>",
  "aspect_ratio": "16:9" | "1:1" | "4:5" | "3:2" | "9:16",
  "style_notes": "<one short sentence — what mood/feel this should land>"
}

Rules:

1. Anchor color descriptors to the kit's actual hex codes when relevant —
   "deep matte black (#111111) and emerald accent (#2ECC71)". The model
   reads these and biases the palette.

2. Negative prompt MUST include the kit's stated imagery anti-patterns.
   Look at the kit's `imagery` description for what to avoid (e.g.
   "no glowing brain abstractions, no stock-photo people, no AI visual
   clichés"). Include those literally.

3. Aspect ratio: default 16:9 (slide hero); 1:1 if it's a section icon;
   4:5 for portrait/figure; 3:2 for editorial; 9:16 for vertical/story.
   The artifact context tells you which.

4. Style: be specific. "Architectural minimalism with one focal subject
   on a soft-sand surface, documentary-photography lighting" — concrete
   imagery.

5. Don't include text in the image prompt — generated text from image
   models is unreliable and often produces wrong glyphs. The artifact
   layout will overlay text via the renderer.

Return ONLY the JSON."""


def _img_text_blob(d: dict[str, Any]) -> str:
    return join_text_fields(d, ("prompt", "negative_prompt", "style_notes"))


async def generate_hero_image_prompt(
    *,
    business_slug: str,
    image_hint: str,
    aspect_ratio: str = "16:9",
    artifact_context: str = "",
) -> dict[str, Any]:
    """Generate a brand-aware image-generation prompt.

    Returns the structured prompt dict (NOT a saved artifact — image
    prompts are ephemeral; the rendered image is what gets stored).
    Pair with `generate_hero_image` if you also want to call an image
    API and save the result.
    """
    kit = load_kit(business_slug)

    # Include the kit's imagery direction explicitly. render_for_prompt
    # doesn't surface it (it's not relevant for most draft tools), but
    # for image generation it's the most important kit field — the
    # anti-patterns ("no glowing brain abstractions") drive the
    # negative prompt.
    imagery_direction = (
        (kit.brand.get("brand", {}) or {}).get("imagery", "") or ""
    )

    user_prompt = (
        f"<business-kit>\n{kit.render_for_prompt()}\n</business-kit>\n\n"
        f"<imagery-direction>\n{imagery_direction}\n</imagery-direction>\n\n"
        f"<image-hint>{image_hint}</image-hint>\n"
        f"<aspect-ratio-hint>{aspect_ratio}</aspect-ratio-hint>\n"
    )
    if artifact_context:
        user_prompt += f"<artifact-context>\n{artifact_context[:1500]}\n</artifact-context>\n"
    user_prompt += "\nProduce the image prompt now. Return JSON only."

    return await generate_json(
        system=_IMAGE_PROMPT_SYSTEM,
        user=user_prompt,
        forbidden=[],
        text_blob_fn=_img_text_blob,
        model=IMAGE_PROMPT_MODEL,
        max_tokens=1200,
    )


async def _gemini_generate_image(prompt: str, aspect_ratio: str) -> bytes | None:
    """Call Gemini's image generation if available.

    Returns PNG bytes or None if unavailable.

    Why this is gated rather than required: not every Astra deployment
    has a Gemini key, and the founder can always paste the prompt into
    a tool of their choice. We make the rendered-image path a bonus,
    not a dependency.
    """
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return None
    try:
        # Lazy import — google-genai is heavy and only needed when keyed.
        from google import genai  # type: ignore[import-untyped]
        from google.genai import types  # type: ignore[import-untyped]
    except ImportError:
        logger.warning(
            "[image] GEMINI_API_KEY set but google-genai package not installed; "
            "skipping image render. `pip install google-genai` to enable."
        )
        return None

    client = genai.Client(api_key=key)
    # imagen-4.0 / gemini-2.5-flash-image — pick the best available
    # for the aspect ratio. Defaults work for 16:9.
    try:
        resp = client.models.generate_images(
            model="imagen-4.0-generate-001",
            prompt=prompt,
            config=types.GenerateImagesConfig(
                number_of_images=1,
                aspect_ratio=aspect_ratio,
                safety_filter_level="BLOCK_LOW_AND_ABOVE",
                person_generation="ALLOW_ADULT",
            ),
        )
    except Exception as e:
        logger.error("[image] gemini call failed: %s", e)
        return None

    if not resp.generated_images:
        return None
    img = resp.generated_images[0]
    if hasattr(img, "image") and hasattr(img.image, "image_bytes"):
        return img.image.image_bytes
    return None


async def generate_hero_image(
    *,
    business_slug: str,
    image_hint: str,
    aspect_ratio: str = "16:9",
    artifact_context: str = "",
    parent_artifact_id: int | None = None,
) -> dict[str, Any]:
    """End-to-end: prompt → optional render → save artifact.

    Always saves a creator_artifacts row of kind="image_prompt" with
    the structured prompt JSON. If image generation succeeds (Gemini
    key set + package installed), the row's content also includes the
    base64-encoded PNG bytes under `image_b64`.

    Returns the saved artifact dict.
    """
    prompt_json = await generate_hero_image_prompt(
        business_slug=business_slug,
        image_hint=image_hint,
        aspect_ratio=aspect_ratio,
        artifact_context=artifact_context,
    )

    # Try to render
    img_bytes = await _gemini_generate_image(
        prompt_json.get("prompt", ""),
        prompt_json.get("aspect_ratio") or aspect_ratio,
    )
    if img_bytes:
        prompt_json["image_b64"] = base64.b64encode(img_bytes).decode("ascii")
        prompt_json["image_format"] = "png"
        prompt_json["image_byte_size"] = len(img_bytes)
    else:
        prompt_json["image_b64"] = None
        prompt_json["image_render_status"] = (
            "prompt-only — set GEMINI_API_KEY (and install google-genai) to auto-render"
        )

    title = f"Image: {(image_hint or '')[:60]}"
    artifact = await create_artifact(
        business_slug=business_slug,
        kind="image_prompt",
        title=title,
        ask=image_hint,
        content=prompt_json,
        parent_id=parent_artifact_id,
    )
    return artifact
