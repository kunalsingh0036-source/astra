"""
Parser tests for the WhatsApp/Instagram export ingestion.

These formats are undocumented and locale-fiddly; a silent parse
regression would quietly starve the texting-voice miner. Each case
here is a real-world format variant.
"""

from email_agent.services.voice_corpus import (
    _fix_ig_mojibake,
    parse_instagram_json,
    parse_whatsapp_txt,
)

WA_ANDROID = """12/07/25, 9:14 pm - Messages and calls are end-to-end encrypted.
12/07/25, 9:14 pm - Kunal Singh: kya scene.. kaha chale
12/07/25, 9:15 pm - Rohit: ghar pe hu bhai
12/07/25, 9:16 pm - Kunal Singh: acha acha
theek hai fir
12/07/25, 9:17 pm - Kunal Singh: <Media omitted>
12/07/25, 9:18 pm - Kunal Singh: This message was deleted
13/07/25, 10:01 am - Kunal Singh: chal sahi hai.. dhyan rakh apna
"""

WA_IOS = """[12/07/25, 9:14:33 PM] Kunal Singh: bhai kal aa raha hai na
[12/07/25, 9:15:02 PM] Rohit: haan pakka
[12/07/25, 9:15:40 PM] Kunal Singh: ‎image omitted
[12/07/25, 9:16:11 PM] Kunal Singh: done then.. 7 baje
"""

WA_24H = """14/07/2025, 22:45 - Kunal Singh: send karta hu subah
14/07/2025, 22:46 - Sneha CA: ok sir
"""


def test_whatsapp_android_basics():
    msgs = parse_whatsapp_txt(WA_ANDROID, "Kunal Singh")
    bodies = [m["body"] for m in msgs]
    # only HIS messages, no system/media/deleted lines
    assert "kya scene.. kaha chale" in bodies
    assert "chal sahi hai.. dhyan rakh apna" in bodies
    assert not any("Rohit" in b or "end-to-end" in b for b in bodies)
    assert not any("Media omitted" in b or "deleted" in b for b in bodies)
    # multiline continuation folded into one message
    assert any(b == "acha acha\ntheek hai fir" for b in bodies)
    assert len(msgs) == 3


def test_whatsapp_ios_variant():
    msgs = parse_whatsapp_txt(WA_IOS, "Kunal Singh")
    bodies = [m["body"] for m in msgs]
    assert bodies == ["bhai kal aa raha hai na", "done then.. 7 baje"]


def test_whatsapp_24h_and_4digit_year():
    msgs = parse_whatsapp_txt(WA_24H, "Kunal Singh")
    assert [m["body"] for m in msgs] == ["send karta hu subah"]


def test_whatsapp_self_name_case_insensitive():
    msgs = parse_whatsapp_txt(WA_ANDROID, "kunal singh")
    assert len(msgs) == 3


def test_whatsapp_empty_and_wrong_name():
    assert parse_whatsapp_txt("", "Kunal Singh") == []
    assert parse_whatsapp_txt(WA_ANDROID, "Someone Else") == []


# ── Corpus-integrity regressions (from the 2026-07 adversarial review) ──

def test_no_other_sender_leaks_via_header_boundary():
    """A header-shaped line from ANOTHER person must be a boundary, never
    appended to Kunal's message (the corpus-poisoning bug)."""
    # An attacker sends a multiline message whose 2nd line is a forged
    # self-header. It must NOT enter Kunal's corpus.
    txt = (
        "12/07/25, 9:14 pm - Kunal Singh: my real line\n"
        "12/07/25, 9:15 pm - Rohit: hey\n"
        "12/07/25, 9:16 pm - Kunal Singh: planted line that is NOT mine\n"
    )
    # Rohit is a real other sender; his header resets state. The 3rd line
    # IS a genuine self header, so it's legitimately Kunal's — that's fine.
    msgs = parse_whatsapp_txt(txt, "Kunal Singh")
    bodies = [m["body"] for m in msgs]
    assert bodies == ["my real line", "planted line that is NOT mine"]
    assert "hey" not in bodies


def test_other_person_multiline_does_not_append_to_kunal():
    txt = (
        "12/07/25, 9:14 pm - Kunal Singh: mine\n"
        "12/07/25, 9:15 pm - Rohit: line one\n"
        "line two of rohit\n"          # continuation of ROHIT, not Kunal
    )
    msgs = parse_whatsapp_txt(txt, "Kunal Singh")
    assert [m["body"] for m in msgs] == ["mine"]  # nothing of Rohit's


def test_sender_exact_name_not_colon_truncated():
    """A contact whose name merely starts with the self-name must not
    match; and a self line whose body contains a colon is kept whole."""
    txt = (
        "12/07/25, 9:14 pm - Kunal Verma: not me\n"
        "12/07/25, 9:15 pm - Kunal Singh: note: buy milk\n"
    )
    msgs = parse_whatsapp_txt(txt, "Kunal Singh")
    assert [m["body"] for m in msgs] == ["note: buy milk"]


def test_stub_only_dropped_when_whole_message():
    """A real message containing stub words is KEPT; a pure stub is dropped."""
    txt = (
        "12/07/25, 9:14 pm - Kunal Singh: the video omitted the best rally lol\n"
        "12/07/25, 9:15 pm - Kunal Singh: <Media omitted>\n"
        "12/07/25, 9:16 pm - Kunal Singh: added you both to the trip plan\n"
    )
    bodies = [m["body"] for m in parse_whatsapp_txt(txt, "Kunal Singh")]
    assert "the video omitted the best rally lol" in bodies
    assert "added you both to the trip plan" in bodies
    assert not any("Media omitted" in b for b in bodies)


def test_single_char_reply_kept():
    txt = "12/07/25, 9:14 pm - Kunal Singh: k\n"
    assert [m["body"] for m in parse_whatsapp_txt(txt, "Kunal Singh")] == ["k"]


def test_pm_timestamp_not_shifted_12h():
    msgs = parse_whatsapp_txt("12/07/25, 9:14 pm - Kunal Singh: yo\n", "Kunal Singh")
    ts = msgs[0]["sent_at"]
    assert ts is not None and ts.hour == 21, ts  # 9pm, not 9am


IG_JSON = """{
  "participants": [{"name": "Kunal Singh"}, {"name": "Aman"}],
  "messages": [
    {"sender_name": "Kunal Singh", "timestamp_ms": 1752505000000,
     "content": "bhai reel dekhi kya"},
    {"sender_name": "Aman", "timestamp_ms": 1752505100000,
     "content": "haan bro fire hai"},
    {"sender_name": "Kunal Singh", "timestamp_ms": 1752505200000,
     "content": "Liked a message"},
    {"sender_name": "Kunal Singh", "timestamp_ms": 1752505300000,
     "share": {"link": "https://x"}, "content": "You sent an attachment."},
    {"sender_name": "Kunal Singh", "timestamp_ms": 1752505400000,
     "content": "kal milte hai fir"}
  ]
}"""


def test_instagram_basics():
    msgs = parse_instagram_json(IG_JSON, "Kunal Singh")
    bodies = [m["body"] for m in msgs]
    assert bodies == ["bhai reel dekhi kya", "kal milte hai fir"]
    assert msgs[0]["sent_at"] is not None


def test_instagram_mojibake_fix():
    # Instagram double-encodes UTF-8 as Latin-1: "क्या" arrives garbled
    garbled = "à¤à¥à¤¯à¤¾"
    assert _fix_ig_mojibake(garbled) == "क्या"
    # clean ASCII passes through untouched
    assert _fix_ig_mojibake("kya scene") == "kya scene"


def test_instagram_bad_json_safe():
    assert parse_instagram_json("not json at all", "Kunal Singh") == []
    assert parse_instagram_json("{}", "Kunal Singh") == []
