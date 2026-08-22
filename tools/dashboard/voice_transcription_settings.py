"""Setting schema for live-tunable WhisperLive transcription config.

The voice pipeline's transcription knobs were hardcoded module constants
in :mod:`voice_whisperlive` (the ``WHISPERLIVE_*`` values). This set,
``dashboard.voice.transcription`` (a global singleton), turns them into
operator-tunable values read fresh each time a mic connection opens — so
the silence-gating thresholds and language/VAD behavior can be adjusted
from the Settings UI or ``graph set`` with no code change.

LIVE vs RESTART:

- ``no_speech_thresh``, ``vad_threshold``, ``language``, ``use_vad`` are
  sent in the per-connection WhisperLive init payload, so a change takes
  effect on the NEXT mic connection — no restart.
- ``model`` is baked into the long-running WhisperLive server subprocess
  (launched with ``single_model``), so changing it here only affects the
  model string the client advertises in its init payload; the actually-
  served model changes only when that subprocess is relaunched. Exposed
  for completeness / forward-compat (single_model disabled).

:func:`resolve_transcription_config` never raises: any missing or
malformed field falls back to the :mod:`voice_whisperlive` module
default, so the voice path keeps working even if the row is absent.
"""

from __future__ import annotations

from dataclasses import dataclass

from tools.graph import settings_ops
from tools.graph.schemas.registry import SettingSchema, field, singleton, publication_band
from tools.graph.schemas.registry import home

from tools.dashboard import voice_whisperlive as _vw


VOICE_TRANSCRIPTION_SET_ID = "dashboard.voice.transcription"
SCHEMA_REVISION = 1
SINGLETON_KEY = "default"


#: The operator's own store. Observed: every stored row lives there
#: and none in any organization's database. Declared so it is
#: enforced rather than agreed -- an undeclared home refuses
#: nothing, and a plain read looks in the caller's own store and
#: reports nothing for rows sitting one database over.
@publication_band(min="raw", max="curated")
@home("personal")
@singleton(key=SINGLETON_KEY)
class VoiceTranscriptionV1(SettingSchema):
    """Global WhisperLive transcription config — one row."""

    set_id = VOICE_TRANSCRIPTION_SET_ID
    schema_revision = SCHEMA_REVISION

    model: str = field(
        default=_vw.WHISPERLIVE_MODEL,
        description=(
            "Model the client advertises in the WhisperLive init payload. "
            "The running server uses single_model, so the served model is "
            "the one its subprocess was launched with; changing this only "
            "matters if single_model is disabled. Requires a voice-server "
            "relaunch to affect the actual model."
        ),
    )
    language: str = field(
        default=_vw.WHISPERLIVE_LANGUAGE,
        description="Transcription language code (e.g. 'en'). Live: applies on the next mic connection.",
    )
    use_vad: bool = field(
        default=_vw.WHISPERLIVE_USE_VAD,
        description="Enable Silero VAD gating before the model. Live: next mic connection.",
    )
    no_speech_thresh: float = field(
        default=_vw.WHISPERLIVE_NO_SPEECH_THRESH,
        description=(
            "faster-whisper no_speech_threshold (0.0-1.0). A segment is "
            "dropped as silence when its no_speech_prob EXCEEDS this value, "
            "so LOWER = MORE aggressive silence drop (the opposite of the "
            "intuitive reading). Note a high-logprob 'confident' segment is "
            "kept regardless, so this lever alone won't kill a confident "
            "'thank you' hallucination — raise vad_threshold for that. "
            "Live: next mic connection."
        ),
    )
    vad_threshold: float = field(
        default=_vw.WHISPERLIVE_VAD_THRESHOLD,
        description=(
            "Silero VAD speech-probability threshold (0.0-1.0). HIGHER = "
            "stricter speech detection, so more silence/noise is filtered "
            "BEFORE it reaches the model — the primary lever against "
            "'thank you'/'mm-hmm' silence hallucinations. Live: next mic "
            "connection."
        ),
    )


@dataclass
class TranscriptionConfig:
    """Resolved, validated transcription knobs for one mic connection."""

    model: str
    language: str
    use_vad: bool
    no_speech_thresh: float
    vad_threshold: float


def _module_defaults() -> TranscriptionConfig:
    return TranscriptionConfig(
        model=_vw.WHISPERLIVE_MODEL,
        language=_vw.WHISPERLIVE_LANGUAGE,
        use_vad=_vw.WHISPERLIVE_USE_VAD,
        no_speech_thresh=_vw.WHISPERLIVE_NO_SPEECH_THRESH,
        vad_threshold=_vw.WHISPERLIVE_VAD_THRESHOLD,
    )


def resolve_transcription_config(
    *,
    org: "str | None | settings_ops._CallerOrgSentinel" = settings_ops.CALLER_ORG,
) -> TranscriptionConfig:
    """Resolve the effective transcription config.

    Each field falls back to the :mod:`voice_whisperlive` module default
    when absent or the wrong type. Never raises on a read-shaped error —
    returns all-defaults so the voice path always has a usable config.
    """
    defaults = _module_defaults()
    try:
        members = settings_ops.read_set(
            VOICE_TRANSCRIPTION_SET_ID,
            org=org,
            peers=[],
        )
    except Exception:
        return defaults

    payload: dict = {}
    for m in members.members:
        if m.key == SINGLETON_KEY:
            payload = m.payload or {}
            break

    def _str(key: str, dflt: str) -> str:
        v = payload.get(key)
        return v if isinstance(v, str) and v else dflt

    def _bool(key: str, dflt: bool) -> bool:
        v = payload.get(key)
        return v if isinstance(v, bool) else dflt

    def _num(key: str, dflt: float) -> float:
        v = payload.get(key)
        # bool is a subclass of int — exclude it explicitly.
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        return dflt

    return TranscriptionConfig(
        model=_str("model", defaults.model),
        language=_str("language", defaults.language),
        use_vad=_bool("use_vad", defaults.use_vad),
        no_speech_thresh=_num("no_speech_thresh", defaults.no_speech_thresh),
        vad_threshold=_num("vad_threshold", defaults.vad_threshold),
    )
