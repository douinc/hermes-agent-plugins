"""Make local speech-to-text fast enough to keep up with a meeting.

Two measured problems, both fixed here from plugin land rather than by editing
hermes core.

**1. The GPU is not being used at all.** ``ctranslate2.get_cuda_device_count()``
can return 0 on a host with a perfectly good NVIDIA GPU, so faster-whisper's
``device="auto", compute_type="auto"`` resolves to CPU + float32 — which is
what produced the "target device does not support efficient float16" warning.
On CPU the supported types are ``{float32, int8, int8_float32}``, and int8 is
measurably faster on the same audio:

    float32   10s audio → 10.0s
    int8      10s audio →  6.2s

Real CUDA would be a different order of magnitude, but that needs a ctranslate2
build with CUDA support for the host's architecture — out of scope for a plugin.

**2. Half-second blips get transcribed.** ``MIN_SPEECH_DURATION = 0.5`` sends
sub-second fragments to Whisper, and near-silence is exactly what makes it
hallucinate ("시청해주셔서 감사합니다", "자막 제공 …"). Raising the floor stops
those from being generated at all, which beats filtering them afterwards.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_APPLIED = False


def _compute_type() -> str:
    return os.environ.get("HERMES_STT_COMPUTE_TYPE", "int8").strip()


def _device() -> str:
    return os.environ.get("HERMES_STT_DEVICE", "auto").strip()


def _min_speech() -> float:
    try:
        return float(os.environ.get("HERMES_DISCORD_MIN_SPEECH_S", "1.0"))
    except ValueError:
        return 1.0


def _patch_whisper_loader() -> bool:
    """Force a compute type instead of letting "auto" settle on float32."""
    try:
        from tools import transcription_tools as tt
        from faster_whisper import WhisperModel
    except Exception as exc:
        logger.warning("discord_meeting: STT tuning unavailable: %s", exc)
        return False

    original = tt._load_local_whisper_model

    def _load(model_name: str):
        device, ctype = _device(), _compute_type()
        try:
            model = WhisperModel(model_name, device=device, compute_type=ctype)
            logger.info(
                "discord_meeting: whisper %s loaded (device=%s compute=%s)",
                model_name, device, ctype,
            )
            return model
        except Exception as exc:
            # Never trade transcription for tuning — hermes' own loader has the
            # CUDA→CPU fallback logic, so hand back to it on any failure.
            logger.warning(
                "discord_meeting: %s/%s load failed (%s) — using hermes default loader",
                device, ctype, exc,
            )
            return original(model_name)

    tt._load_local_whisper_model = _load
    return True


def _patch_min_speech() -> bool:
    """Raise the floor on what counts as an utterance worth transcribing."""
    from .patching import DISCORD_ADAPTER_MODULES, live_attr

    patched = 0
    for _module, cls in live_attr("VoiceReceiver", DISCORD_ADAPTER_MODULES):
        before = cls.MIN_SPEECH_DURATION
        cls.MIN_SPEECH_DURATION = _min_speech()
        logger.info(
            "discord_meeting: MIN_SPEECH_DURATION %.2fs → %.2fs (%s)",
            before, cls.MIN_SPEECH_DURATION, _module.__name__,
        )
        patched += 1
    return patched > 0


def install() -> bool:
    """Apply the STT patches. Returns True only when they actually landed.

    Returning True unconditionally made this un-retryable: register() runs
    before the Discord adapter module is imported, so the VoiceReceiver patch
    silently found nothing and was never attempted again.
    """
    global _APPLIED
    if _APPLIED:
        return True
    loader = _patch_whisper_loader()
    min_speech = _patch_min_speech()
    _patch_utterance_logging()
    _APPLIED = bool(loader and min_speech)
    return _APPLIED


def _patch_utterance_logging() -> bool:
    """Log every completed utterance before it reaches speech-to-text.

    ``_process_voice_input`` returns silently on four different paths (STT
    failure, empty transcript, hallucination match, exception), and
    ``check_silence`` drops unattributable buffers without a word, so "nothing
    was transcribed" and "nothing was ever captured" look identical in the log.
    This makes the capture stage itself visible: if these lines are absent, the
    audio never arrived; if they are present but no "Voice input from user"
    follows, speech-to-text is the problem.
    """
    from .patching import DISCORD_ADAPTER_MODULES, live_attr

    patched = 0
    for _module, cls in live_attr("VoiceReceiver", DISCORD_ADAPTER_MODULES):
        if getattr(cls.check_silence, "_discord_meeting_wrapped", False):
            patched += 1
            continue

        original = cls.check_silence

        def check_silence(self, __original=original):
            completed = __original(self)
            try:
                for user_id, pcm in completed or []:
                    # 16-bit stereo at 48 kHz — the receiver's native format.
                    seconds = len(pcm) / (48000 * 2 * 2)
                    logger.info(
                        "discord_meeting: utterance captured user=%s %.1fs (%d bytes)",
                        user_id, seconds, len(pcm),
                    )
            except Exception:
                pass
            return completed

        check_silence._discord_meeting_wrapped = True
        cls.check_silence = check_silence
        patched += 1
    return patched > 0
