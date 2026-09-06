"""fs.read pages are BYTE-addressed, so a page edge lands wherever the
offset says — including the middle of a multibyte character.

The first version of `_render_result` decoded strictly and called any
such page 'binary, not shown'. A 256 KiB page of a plain-text WhatsApp
export with an emoji straddling the boundary was therefore presented to
the model as "not a text file", and the continuation page from that
offset read the same way: the bytes were text the whole time, and the
model would tell Kunal his export was binary.

These pin the three cases apart: a cut edge is text with the cut
announced, real binary is still refused, and clean text is untouched.
"""

from astra.broker import client
from astra.runtime.tools import physical

EMOJI = "🏸"          # U+1F3F8, four UTF-8 bytes
TEXT = f"hello {EMOJI} world " * 40


def _rendered(page: bytes) -> str:
    return physical._render_result(client.IntentResult(
        intent_id=7, status="succeeded", verb="fs.read", result_bytes=page))


def test_a_page_cut_inside_a_multibyte_character_is_still_text():
    raw = TEXT.encode()
    start = raw.index(EMOJI.encode()) + 1          # cuts the leading emoji
    page = raw[start:-1]                            # and the trailing one
    out = _rendered(page)
    assert "binary" not in out.lower(), out[:200]
    assert "world" in out and EMOJI in out


def test_both_cut_edges_are_announced_so_the_model_can_page_on():
    raw = TEXT.encode()
    out = _rendered(raw[raw.index(EMOJI.encode()) + 1:-1]).lower()
    # The model must learn the edge was trimmed, or it will believe it
    # has the whole page and stop paging.
    assert "multibyte" in out or "trimmed" in out or "continue from" in out, out[:300]


def test_real_binary_is_still_refused_rather_than_mangled():
    out = _rendered(b"\x00\x01\x02\xff\xfe\x00PK\x03\x04").lower()
    assert "binary" in out


def test_clean_text_is_unchanged():
    out = _rendered(b"plain ascii, no cut edges")
    assert "plain ascii, no cut edges" in out
    assert "binary" not in out.lower()
