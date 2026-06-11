"""AI enhancement pass — polishes Zoom transcript text via Claude.

System prompt cached (ephemeral) — ~8% cost on cache hits after the first call.
Chunks under 12 words skip Claude entirely (corrections output is enough).
"""

import anthropic
import config

_SYSTEM_PROMPT = """\
You are a transcription polisher for Firestarter Worldwide Ministries.

You will receive raw speech wrapped in <transcript> tags from a live Zoom meeting. \
Output ONLY the polished text — no preamble, no explanation, no refusal, no meta-commentary. \
If the text is too fragmented or garbled to meaningfully polish, return it EXACTLY as given — \
word for word, unchanged. Never explain, never refuse.

Polish rules:
- Fix punctuation and capitalisation; break into natural paragraphs
- Remove false starts, filler words, mid-sentence restarts
- Do NOT add quotation marks around direct speech — leave it as plain prose
- Format spoken lists as proper lists
- Replace glossolalia / tongues with: [Tongues spoken]
- Preserve every substantive word — do not summarise or condense
- Omit obvious meta-commentary: volume/delivery coaching, directions to sit or stand, descriptions of laughter
- Use British English spelling (honour, realise, fulfil, behaviour, etc.)

Your entire response is the polished text only.\
"""

_MIN_WORDS_FOR_AI = 12


def enhance(corrected_text: str) -> tuple[str, float]:
    """Polish corrected_text. Returns (polished_text, cost_usd)."""
    if not corrected_text.strip():
        return corrected_text, 0.0
    if len(corrected_text.split()) < _MIN_WORDS_FOR_AI:
        return corrected_text, 0.0

    api_key = config.get("api_key", "")
    if not api_key:
        return corrected_text, 0.0

    model  = config.get("model_name", "claude-haiku-4-5")
    client = anthropic.Anthropic(api_key=api_key)

    try:
        response = client.messages.create(
            model=model,
            max_tokens=2048,
            system=[{
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": f"<transcript>{corrected_text}</transcript>"}],
            timeout=60.0,
        )
    except anthropic.APIError as exc:
        print(f"[ZoomScribe2] Anthropic API error: {exc}")
        return corrected_text, 0.0

    polished = response.content[0].text.strip()

    _REFUSAL_PHRASES = (
        # Direct refusal verbs Claude uses:
        "i'm not able to",
        "i am not able to",
        "i cannot",
        "i can't",
        "i'm unable to",
        "i am unable to",
        # Content-quality complaints that signal a refusal:
        "it appears to be incomplete",
        "incomplete or unclear",
        "doesn't form coherent",
        "does not form coherent",
        "could you provide",
        "please provide",
        "i need enough context",
        "not enough context",
        "no coherent",
        "corrupted",
        "this appears to be",
        # Meta-commentary markers:
        "actual content worth",
        "filler words and false",
    )
    if any(phrase in polished.lower() for phrase in _REFUSAL_PHRASES):
        print("[ZoomScribe2] Claude refused this chunk — using raw text instead.")
        return corrected_text, 0.0

    usage  = response.usage
    t_in   = usage.input_tokens
    t_out  = usage.output_tokens
    cached = getattr(usage, "cache_read_input_tokens", 0) or 0
    cost   = config.add_cost(tokens_in=t_in, tokens_out=t_out, cached_in=cached)
    return polished, cost
