"""
Tests for the dead_air_detection preprocessor.

The preprocessor uses ``audio_bytes_from_url_or_base64`` (the canonical
audio loader in tfc/utils/storage.py) to resolve the input, then decodes
with librosa and injects ``_dead_air_*`` kwargs for the sandbox body.
The sandbox body itself is trivial threshold logic and is verified by
inspection.
"""

from __future__ import annotations

import io
import math
from unittest.mock import patch

import pytest

from evaluations.engine.preprocessing import PREPROCESSORS, preprocess_inputs


def test_dead_air_preprocessor_registered():
    assert "dead_air_detection" in PREPROCESSORS


def test_missing_audio_returns_error():
    out = preprocess_inputs("dead_air_detection", {})
    assert out["_dead_air_error"] == "Missing input_audio"


def test_loader_failure_returns_error():
    with patch(
        "tfc.utils.storage.audio_bytes_from_url_or_base64",
        side_effect=ValueError("not a valid audio source"),
    ):
        out = preprocess_inputs(
            "dead_air_detection",
            {"input_audio": "not-a-url"},
        )
    assert "_dead_air_error" in out
    assert "not a valid audio source" in out["_dead_air_error"]


def _synth_wav_bytes(duration_sec=2.0, sr=8000, silence_segments=None):
    try:
        import numpy as np
        import soundfile as sf
    except ImportError:
        pytest.skip("numpy/soundfile not installed")
    n = int(duration_sec * sr)
    t = np.linspace(0, duration_sec, n, endpoint=False)
    y = 0.5 * np.sin(2 * math.pi * 440 * t).astype("float32")
    for (s, e) in silence_segments or []:
        y[int(s * sr):int(e * sr)] = 0.0
    buf = io.BytesIO()
    sf.write(buf, y, sr, format="WAV")
    return buf.getvalue()


def test_clean_audio_has_low_dead_air():
    body = _synth_wav_bytes(duration_sec=1.0, silence_segments=None)
    with patch(
        "tfc.utils.storage.audio_bytes_from_url_or_base64",
        return_value=body,
    ):
        out = preprocess_inputs(
            "dead_air_detection",
            {"input_audio": "https://example.com/clean.wav"},
        )
    assert "_dead_air_error" not in out
    assert out["_dead_air_percentage"] < 5.0
    assert out["_dead_air_max_gap_ms"] < 200.0


def test_silent_audio_is_mostly_dead_air():
    body = _synth_wav_bytes(
        duration_sec=2.0,
        silence_segments=[(0.0, 1.6)],
    )
    with patch(
        "tfc.utils.storage.audio_bytes_from_url_or_base64",
        return_value=body,
    ):
        out = preprocess_inputs(
            "dead_air_detection",
            {"input_audio": "https://example.com/silent.wav"},
        )
    assert "_dead_air_error" not in out
    assert out["_dead_air_percentage"] > 50.0
    assert out["_dead_air_max_gap_ms"] > 1000.0


def test_loader_called_with_no_silence_padding():
    """Padding short audio with synthetic silence would inflate the metric."""
    body = _synth_wav_bytes(duration_sec=0.5)
    with patch(
        "tfc.utils.storage.audio_bytes_from_url_or_base64",
        return_value=body,
    ) as mock_loader:
        preprocess_inputs(
            "dead_air_detection",
            {"input_audio": "https://example.com/short.wav"},
        )
    _, kwargs = mock_loader.call_args
    assert kwargs.get("pad_silence") is False
    assert kwargs.get("min_duration_seconds") is None


def test_silence_before_the_first_word_is_still_counted():
    """Only the tail is trimmed. On an outbound call, silence before the first word is the agent
    failing to speak, which is the kind of defect this eval exists to catch."""
    body = _synth_wav_bytes(duration_sec=2.0, silence_segments=[(0.0, 1.6)])
    with patch(
        "tfc.utils.storage.audio_bytes_from_url_or_base64",
        return_value=body,
    ):
        out = preprocess_inputs(
            "dead_air_detection",
            {"input_audio": "https://example.com/slow-to-speak.wav"},
        )

    assert "_dead_air_error" not in out
    assert out["_dead_air_percentage"] > 50.0
    assert out["_dead_air_max_gap_ms"] > 1000.0


def test_silence_after_the_last_word_is_not_counted_as_dead_air():
    """A recording runs on after the conversation ends while the call is torn down. On a measured
    call that tail was 34.8s of an 80.0s file: it scored 40.4% dead air with a single 30750ms gap,
    all of it our own hangup delay rather than anything the agent did."""
    # Two seconds of speech, then eight seconds of the call being torn down.
    body = _synth_wav_bytes(duration_sec=10.0, silence_segments=[(2.0, 10.0)])
    with patch(
        "tfc.utils.storage.audio_bytes_from_url_or_base64",
        return_value=body,
    ):
        out = preprocess_inputs(
            "dead_air_detection",
            {"input_audio": "https://example.com/hangup-tail.wav"},
        )

    assert "_dead_air_error" not in out
    assert out["_dead_air_recording_sec"] > 9.0, "the whole file is still reported"
    assert out["_dead_air_duration_sec"] < 3.0, "the conversation is what gets measured"
    assert out["_dead_air_percentage"] < 5.0, (
        "the hangup tail was charged as dead air: "
        f"{out['_dead_air_percentage']:.1f}%"
    )
    assert out["_dead_air_max_gap_ms"] < 500.0


def test_silence_inside_the_conversation_is_still_counted():
    """The trim must not hide the thing this eval exists to find."""
    # Speech, a four second gap in the middle, speech again, then a short tail.
    body = _synth_wav_bytes(
        duration_sec=10.0, silence_segments=[(2.0, 6.0), (9.5, 10.0)]
    )
    with patch(
        "tfc.utils.storage.audio_bytes_from_url_or_base64",
        return_value=body,
    ):
        out = preprocess_inputs(
            "dead_air_detection",
            {"input_audio": "https://example.com/mid-call-gap.wav"},
        )

    assert "_dead_air_error" not in out
    assert out["_dead_air_max_gap_ms"] > 3000.0, "a real mid-call gap must survive the trim"
    assert out["_dead_air_percentage"] > 20.0


def test_a_recording_with_no_speech_reports_that_rather_than_zero_dead_air():
    body = _synth_wav_bytes(duration_sec=3.0, silence_segments=[(0.0, 3.0)])
    with patch(
        "tfc.utils.storage.audio_bytes_from_url_or_base64",
        return_value=body,
    ):
        out = preprocess_inputs(
            "dead_air_detection",
            {"input_audio": "https://example.com/silent.wav"},
        )

    assert out["_dead_air_error"] == "No speech found in the recording"
