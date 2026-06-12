"""Channel-addendum logic — WhatsApp turns get a phone-optimized
system-prompt suffix; web/unknown turns are unchanged."""

import importlib.util
import pathlib


def _load_main():
    """Load services/stream/main.py far enough to grab _channel_addendum
    without importing the whole FastAPI app (which needs deploy-only
    deps). We exec only the helper + its data."""
    src = pathlib.Path("services/stream/main.py").read_text()
    # Extract the addendum block (data dict + function) and exec it
    # standalone — it has no external deps.
    start = src.index("_CHANNEL_ADDENDA")
    end = src.index("def _check_secret")
    ns: dict = {}
    exec(src[start:end], ns)
    return ns


def test_whatsapp_addendum_present_and_phone_shaped():
    ns = _load_main()
    add = ns["_channel_addendum"]("whatsapp")
    assert add, "whatsapp must get an addendum"
    low = add.lower()
    assert "whatsapp" in low
    # The behaviors we actually care about are stated:
    assert "markdown" in low or "plain text" in low
    assert "tight" in low or "short" in low


def test_web_and_unknown_get_nothing():
    ns = _load_main()
    assert ns["_channel_addendum"](None) == ""
    assert ns["_channel_addendum"]("web") == ""
    assert ns["_channel_addendum"]("") == ""
    assert ns["_channel_addendum"]("telegram") == ""  # not yet configured


def test_case_and_whitespace_insensitive():
    ns = _load_main()
    base = ns["_channel_addendum"]("whatsapp")
    assert ns["_channel_addendum"](" WhatsApp ") == base
    assert ns["_channel_addendum"]("WHATSAPP") == base
