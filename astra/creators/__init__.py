"""Astra's creator capability — drafts decks, docs, one-pagers,
brand kits per company, with persona-driven critique passes and
multi-format rendering (PDF + PPTX).

Public surface:
  load_kit(slug) → BusinessKit
  list_kits() → list[BusinessKit summary]
  draft_deck(...) → CreatorArtifact
  draft_doc(...) → CreatorArtifact          (Phase B2)
  draft_one_pager(...) → CreatorArtifact    (Phase B2)
  draft_brand_kit(...) → kit dir path        (Phase B2)
  critique_artifact(...) → CreatorArtifact   (Phase B2)
  render_pdf(artifact_id) → r2 url
  render_pptx(artifact_id) → r2 url          (Phase B2)
"""

from astra.creators.kits import BusinessKit, list_kits, load_kit
from astra.creators.store import (
    create_artifact,
    get_artifact,
    list_artifacts,
)

__all__ = [
    "BusinessKit",
    "list_kits",
    "load_kit",
    "create_artifact",
    "get_artifact",
    "list_artifacts",
]
