"""
Per-call cost estimation for Gemini Live.

Source of truth is Gemini's own `usageMetadata` (token counts, broken down by
modality), surfaced to us through LiveKit's `metrics_collected` event. We never
guess token counts — we only multiply Google-reported tokens by Google's
published per-1M-token price list. The result is an ESTIMATE for live
per-call clarity; reconcile the monthly total against aistudio.google.com/spend.

Because this is an audio-to-audio model, audio tokens dominate and are priced
very differently from text (audio input is 4x text input, audio output ~2.7x
text output). So we MUST keep the per-modality split — a single blended rate
would be wrong for a voice call.
"""

# Paid-tier price per 1,000,000 tokens, USD. Keyed by model.
# gemini-3.1-flash-live-preview — from Google AI pricing.
PRICING = {
    "gemini-3.1-flash-live-preview": {
        "input":  {"text": 0.75, "audio": 3.00, "image": 1.00},
        "output": {"text": 4.50, "audio": 12.00, "image": 0.00},
    },
}

# Any model we don't have an explicit table for is priced with these rates.
_DEFAULT_MODEL = "gemini-3.1-flash-live-preview"


def _rates(model: str) -> dict:
    return PRICING.get(model or _DEFAULT_MODEL, PRICING[_DEFAULT_MODEL])


def compute_cost(
    model: str, *,
    text_in: int = 0, audio_in: int = 0, image_in: int = 0,
    text_out: int = 0, audio_out: int = 0, image_out: int = 0,
) -> float:
    """Cost in USD for one call, per-modality token counts × the price table.

        Cost = ( text_in·0.75 + audio_in·3.00 + image_in·1.00
               + text_out·4.50 + audio_out·12.00 ) / 1,000,000
    """
    r = _rates(model)
    micros = (
        text_in   * r["input"]["text"]
        + audio_in  * r["input"]["audio"]
        + image_in  * r["input"]["image"]
        + text_out  * r["output"]["text"]
        + audio_out * r["output"]["audio"]
        + image_out * r["output"]["image"]
    )
    return round(micros / 1_000_000, 6)


class CallUsage:
    """Accumulates per-modality token counts across a single call.

    Fed one realtime/LLM metrics object at a time from the LiveKit
    `metrics_collected` event (every model response emits one). At call end,
    `.cost(model)` turns the totals into a dollar figure.
    """

    def __init__(self) -> None:
        self.text_in = self.audio_in = self.image_in = 0
        self.text_out = self.audio_out = self.image_out = 0

    def add(self, m) -> None:
        """Fold one metrics object into the running totals. Defensive against
        LiveKit version differences — missing fields are treated as 0, and a
        metric that reports only totals (no modality split) is attributed to the
        dominant modality for its kind (audio for realtime, text for plain LLM)."""
        idt = getattr(m, "input_token_details", None)
        odt = getattr(m, "output_token_details", None)

        had_in_detail = had_out_detail = False
        if idt is not None:
            ti = getattr(idt, "text_tokens", 0) or 0
            ai = getattr(idt, "audio_tokens", 0) or 0
            ii = getattr(idt, "image_tokens", 0) or 0
            if ti or ai or ii:
                self.text_in += ti
                self.audio_in += ai
                self.image_in += ii
                had_in_detail = True
        if odt is not None:
            to = getattr(odt, "text_tokens", 0) or 0
            ao = getattr(odt, "audio_tokens", 0) or 0
            io = getattr(odt, "image_tokens", 0) or 0
            if to or ao or io:
                self.text_out += to
                self.audio_out += ao
                self.image_out += io
                had_out_detail = True

        # Fallback when the provider gave only aggregate token counts.
        is_realtime = "realtime" in str(getattr(m, "type", "")).lower()
        if not had_in_detail:
            tin = getattr(m, "input_tokens", 0) or 0
            if tin:
                if is_realtime:
                    self.audio_in += tin
                else:
                    self.text_in += tin
        if not had_out_detail:
            tout = getattr(m, "output_tokens", 0) or 0
            if tout:
                if is_realtime:
                    self.audio_out += tout
                else:
                    self.text_out += tout

    def total_tokens(self) -> int:
        return (self.text_in + self.audio_in + self.image_in
                + self.text_out + self.audio_out + self.image_out)

    def cost(self, model: str) -> float:
        return compute_cost(
            model,
            text_in=self.text_in, audio_in=self.audio_in, image_in=self.image_in,
            text_out=self.text_out, audio_out=self.audio_out, image_out=self.image_out,
        )
