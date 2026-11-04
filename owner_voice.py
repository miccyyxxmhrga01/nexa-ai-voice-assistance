"""Local speaker verification. Raw enrollment recordings are never saved.

Uses sherpa-onnx's CAMPPlus English speaker embeddings, not speech pitch or a password.
This is a convenience filter, not authentication: recordings and cloned voices may pass.
"""
import importlib.util
import json
import threading
from pathlib import Path

import httpx
import numpy as np

SAMPLE_RATE = 16000
MODEL_NAME = "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"
MODEL_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/" + MODEL_NAME
MATCH_THRESHOLD = 0.6


class OwnerVoice:
    def __init__(self, data_dir: Path, model_dir: Path):
        self.profile_path = data_dir / "owner_voice.json"
        self.model_path = model_dir / MODEL_NAME
        self._extractor = None
        self._lock = threading.RLock()

    def status(self):
        return {
            "enrolled": self.profile_path.exists(),
            "ready": self.model_path.is_file() and importlib.util.find_spec("sherpa_onnx") is not None,
        }

    def prepare(self):
        """Download the public model once. Called by setup or an explicit UI action."""
        with self._lock:
            if importlib.util.find_spec("sherpa_onnx") is None:
                raise RuntimeError("Run setup.ps1 to install the speaker recognition package, then restart Nexa.")
            if not self.model_path.is_file():
                self.model_path.parent.mkdir(parents=True, exist_ok=True)
                partial = self.model_path.with_suffix(".part")
                try:
                    with httpx.stream("GET", MODEL_URL, follow_redirects=True, timeout=60) as response:
                        response.raise_for_status()
                        with partial.open("wb") as output:
                            for chunk in response.iter_bytes(1 << 20):
                                output.write(chunk)
                    partial.replace(self.model_path)
                finally:
                    partial.unlink(missing_ok=True)
            self._get_extractor()

    def _get_extractor(self):
        if self._extractor is None:
            if not self.model_path.is_file():
                raise RuntimeError("Set up owner voice in Settings > General first.")
            import sherpa_onnx

            config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(self.model_path), num_threads=2, provider="cpu", debug=False,
            )
            if not config.validate():
                raise RuntimeError("The speaker model is invalid. Run setup.ps1 again.")
            self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        return self._extractor

    @staticmethod
    def _speech(audio, minimum):
        from faster_whisper.vad import get_speech_timestamps, VadOptions

        if audio.ndim != 1 or not np.isfinite(audio).all():
            raise ValueError("Invalid voice recording.")
        spans = get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=300, speech_pad_ms=80))
        speech = np.concatenate([audio[s["start"]:s["end"]] for s in spans]) if spans else np.array([], np.float32)
        if len(speech) < SAMPLE_RATE * minimum:
            raise ValueError(f"Please speak for at least {minimum:g} seconds in a quiet room.")
        return np.ascontiguousarray(speech, dtype=np.float32)

    @staticmethod
    def _normalize(vector):
        vector = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(vector))
        if vector.ndim != 1 or not np.isfinite(vector).all() or norm < 1e-8:
            raise ValueError("Couldn't recognize a clear voice. Please record again.")
        return vector / norm

    def _embedding(self, speech):
        extractor = self._get_extractor()
        stream = extractor.create_stream()
        stream.accept_waveform(sample_rate=SAMPLE_RATE, waveform=speech)
        stream.input_finished()
        if not extractor.is_ready(stream):
            raise ValueError("Please speak a longer phrase.")
        return self._normalize(extractor.compute(stream))

    @staticmethod
    def _windows(speech):
        """Check the whole recording and shorter spans for a consistent speaker."""
        windows = [speech]
        # Two-second crops produce unreliable embeddings even for the same
        # speaker. Short requests use their full recording; longer ones also
        # check overlapping four-second spans for a speaker change.
        width, step = SAMPLE_RATE * 4, SAMPLE_RATE * 2
        if len(speech) > width:
            starts = list(range(0, len(speech) - width + 1, step))
            starts.append(len(speech) - width)
            windows.extend(speech[start:start + width] for start in sorted(set(starts)))
        return windows

    def enroll(self, recordings):
        # Older app windows can still submit their three recordings.
        if not 1 <= len(recordings) <= 3:
            raise ValueError("Record one short voice sample.")
        with self._lock:
            vectors = [self._embedding(window) for audio in recordings
                       for window in self._windows(self._speech(audio, minimum=2))]
            if any(float(a @ b) < MATCH_THRESHOLD for i, a in enumerate(vectors) for b in vectors[i + 1:]):
                raise ValueError("I heard more than one voice. Record the sentence again in a quiet room.")
            profile = {
                "version": 1, "model": MODEL_NAME,
                "embedding": self._normalize(np.mean(vectors, axis=0)).tolist(),
            }
            self.profile_path.parent.mkdir(parents=True, exist_ok=True)
            partial = self.profile_path.with_suffix(".tmp")
            try:
                partial.write_text(json.dumps(profile), encoding="utf-8")
                partial.replace(self.profile_path)
            finally:
                partial.unlink(missing_ok=True)

    def remove(self):
        with self._lock:
            self.profile_path.unlink(missing_ok=True)

    def verify(self, audio):
        with self._lock:
            if not self.profile_path.exists():
                return False, "not_enrolled"
            try:
                speech = self._speech(audio, minimum=1)
            except ValueError:
                return False, "too_short"
            except Exception:
                return False, "unavailable"
            try:
                profile = json.loads(self.profile_path.read_text(encoding="utf-8"))
                if profile["version"] != 1 or profile["model"] != MODEL_NAME:
                    return False, "unavailable"
                owner = self._normalize(profile["embedding"])
                # Check the full phrase and overlapping windows, so a matching greeting
                # doesn't automatically admit a different speaker's following command.
                for window in self._windows(speech):
                    vector = self._embedding(window)
                    if vector.shape != owner.shape:
                        return False, "unavailable"
                    if float(vector @ owner) < MATCH_THRESHOLD:
                        return False, "not_owner"
                return True, "matched"
            except Exception:
                # A missing model, damaged profile, or inference failure must stay locked.
                return False, "unavailable"
