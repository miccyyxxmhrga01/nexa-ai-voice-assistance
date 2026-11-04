import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import numpy as np

import server
from owner_voice import MODEL_NAME, OwnerVoice


class OwnerVoiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.owner = OwnerVoice(root, root / "models")
        self.owner._speech = Mock(side_effect=lambda audio, minimum: audio)
        self.owner._embedding = Mock(return_value=np.array([1, 0], np.float32))
        self.audio = np.ones(16000 * 4, np.float32)

    def enroll(self):
        self.owner.enroll([self.audio])

    def test_unenrolled_stays_locked(self):
        self.assertEqual(self.owner.verify(self.audio), (False, "not_enrolled"))
        self.owner._embedding.assert_not_called()

    def test_same_voice_is_accepted_after_reload(self):
        self.enroll()
        self.assertEqual(self.owner.verify(self.audio), (True, "matched"))
        profile = json.loads(self.owner.profile_path.read_text())
        self.assertEqual(set(profile), {"version", "model", "embedding"})
        self.assertEqual(profile["model"], MODEL_NAME)
        other = OwnerVoice(self.owner.profile_path.parent, self.owner.model_path.parent)
        other._speech, other._embedding = self.owner._speech, self.owner._embedding
        self.assertEqual(other.verify(self.audio), (True, "matched"))

    def test_other_speaker_is_rejected(self):
        self.enroll()
        self.owner._embedding.return_value = np.array([0, 1], np.float32)
        self.assertEqual(self.owner.verify(self.audio), (False, "not_owner"))

    def test_matching_greeting_cannot_admit_other_speakers_command(self):
        self.enroll()
        self.owner._embedding.side_effect = [np.array([1, 0]), np.array([1, 0]), np.array([0, 1])]
        self.assertEqual(self.owner.verify(np.ones(16000 * 8, np.float32)), (False, "not_owner"))

    def test_mixed_enrollment_keeps_original_profile(self):
        self.enroll()
        original = self.owner.profile_path.read_bytes()
        self.owner._embedding.side_effect = lambda speech: np.array([1, 0]) if speech.mean() > 0 else np.array([0, 1])
        with self.assertRaises(ValueError):
            self.owner.enroll([self.audio, -self.audio, self.audio])
        self.assertEqual(original, self.owner.profile_path.read_bytes())

    def test_mixed_voices_in_one_recording_keep_original_profile(self):
        self.enroll()
        original = self.owner.profile_path.read_bytes()
        self.owner._embedding.side_effect = lambda speech: np.array([1, 0]) if speech.mean() > 0 else np.array([0, 1])
        mixed = np.concatenate((self.audio, -self.audio))
        with self.assertRaises(ValueError):
            self.owner.enroll([mixed])
        self.assertEqual(original, self.owner.profile_path.read_bytes())

    def test_invalid_recordings_cannot_replace_profile(self):
        self.enroll()
        original = self.owner.profile_path.read_bytes()
        self.owner._speech.side_effect = ValueError("Not enough speech")
        with self.assertRaises(ValueError):
            self.enroll()
        self.assertEqual(original, self.owner.profile_path.read_bytes())

    def test_inference_failure_stays_locked(self):
        self.enroll()
        self.owner._embedding.side_effect = RuntimeError("model failed")
        self.assertEqual(self.owner.verify(self.audio), (False, "unavailable"))

    def test_vad_failure_stays_locked(self):
        self.enroll()
        self.owner._speech.side_effect = RuntimeError("VAD failed")
        self.assertEqual(self.owner.verify(self.audio), (False, "unavailable"))

    def test_short_speech_requests_longer_phrase(self):
        self.enroll()
        self.owner._speech.side_effect = ValueError("too short")
        self.assertEqual(self.owner.verify(self.audio), (False, "too_short"))

    def test_damaged_profile_stays_locked(self):
        self.enroll()
        self.owner.profile_path.write_text("broken")
        self.assertEqual(self.owner.verify(self.audio), (False, "unavailable"))

    def test_remove_disables_voice(self):
        self.enroll()
        self.owner.remove()
        self.assertEqual(self.owner.verify(self.audio), (False, "not_enrolled"))

    def test_bad_embedding_is_rejected(self):
        for vector in ([0, 0], [float("nan"), 1], [[1, 0]]):
            with self.assertRaises(ValueError):
                self.owner._normalize(vector)


class SpeechTests(unittest.TestCase):
    def test_cuda_probe_runs_lazy_decoder_and_falls_back_on_failure(self):
        devices = []
        cpu_model = Mock()

        def failed_segments():
            raise RuntimeError("CUDA inference failed")
            yield

        def factory(name, **options):
            devices.append(options["device"])
            if options["device"] == "cpu":
                return cpu_model
            return SimpleNamespace(transcribe=lambda *a, **kw: (failed_segments(), None))

        modules = {
            "faster_whisper": SimpleNamespace(WhisperModel=factory),
            "ctranslate2": SimpleNamespace(get_cuda_device_count=lambda: 1),
        }
        with patch.dict(sys.modules, modules), patch.dict(server.os.environ, {"NEXA_STT_DEVICE": "auto"}), patch.object(server, "_use_pip_cuda_libraries"), patch.object(server, "log"), patch.dict(server._stt_device):
            self.assertIs(server._load_whisper("test.en"), cpu_model)
        self.assertEqual(devices, ["cuda", "cpu"])

    def test_doubtful_partial_command_is_not_executed(self):
        good = SimpleNamespace(text="open", avg_logprob=-0.2, compression_ratio=1)
        bad = SimpleNamespace(text="calculator", avg_logprob=-1.4, compression_ratio=1)
        self.assertEqual(server._speech_text([good, bad]), "")
        self.assertEqual(server._speech_text([good]), "open")

    def test_repetitive_hallucination_is_rejected(self):
        bad = SimpleNamespace(text="thank you " * 30, avg_logprob=-0.2, compression_ratio=3)
        self.assertEqual(server._speech_text([bad]), "")

    def test_guessed_word_rejects_the_whole_request(self):
        words = [SimpleNamespace(word=w, probability=p) for w, p in
                 (("open", 0.99), ("my", 0.99), ("bank", 0.05))]
        segment = SimpleNamespace(text="open my bank", avg_logprob=-0.2, compression_ratio=1,
                                  no_speech_prob=0.01, words=words)
        self.assertEqual(server._speech_text([segment]), "")
        words[-1].probability = 0.9
        self.assertEqual(server._speech_text([segment]), "open my bank")

    def test_uncertain_sentence_is_rejected_even_without_one_extremely_bad_word(self):
        segment = SimpleNamespace(text="close the browser", avg_logprob=-0.5, compression_ratio=1,
                                  words=[SimpleNamespace(word=w, probability=0.4)
                                         for w in ("close", "the", "browser")])
        self.assertEqual(server._speech_text([segment]), "")

    def test_likely_non_speech_and_invalid_confidence_are_rejected(self):
        segment = SimpleNamespace(text="thank you", avg_logprob=-0.7, compression_ratio=1,
                                  no_speech_prob=0.95)
        self.assertEqual(server._speech_text([segment]), "")
        segment.avg_logprob = float("nan")
        self.assertEqual(server._speech_text([segment]), "")
        segment.avg_logprob = -0.2
        segment.words = [SimpleNamespace(word="thank you", probability=float("nan"))]
        self.assertEqual(server._speech_text([segment]), "")

    def test_accurate_decode_retries_once_without_a_wake_hint(self):
        good = SimpleNamespace(text="turn the volume down", avg_logprob=-0.2, compression_ratio=1)
        bad = SimpleNamespace(text="turn the volume up", avg_logprob=-1.5, compression_ratio=1)
        info = SimpleNamespace(duration_after_vad=2)
        model = Mock()
        model.transcribe.side_effect = [(iter([bad]), info), (iter([good]), info)]
        name = server.WHISPER_ACCURATE_MODEL
        with patch.object(server, "get_whisper", return_value=model), patch.dict(server._stt_device, {name: "cpu"}):
            text, _, _ = server._transcribe(np.zeros(32000, np.float32), True, "Nexa")
        self.assertEqual(text, good.text)
        self.assertEqual(model.transcribe.call_count, 2)
        first, retry = model.transcribe.call_args_list
        self.assertEqual(first.kwargs["hotwords"], "Nexa")
        self.assertIsNone(retry.kwargs["hotwords"])
        for call in (first, retry):
            self.assertEqual(call.kwargs["temperature"], 0)
            self.assertTrue(call.kwargs["word_timestamps"])
            self.assertFalse(call.kwargs["condition_on_previous_text"])

    def test_rejected_retry_never_returns_an_uncertain_draft(self):
        bad = SimpleNamespace(text="delete the file", avg_logprob=-1.5, compression_ratio=1)
        model = Mock()
        model.transcribe.side_effect = [(iter([bad]), SimpleNamespace(duration_after_vad=2)) for _ in range(2)]
        with patch.object(server, "get_whisper", return_value=model), patch.dict(server._stt_device, {server.WHISPER_ACCURATE_MODEL: "cpu"}):
            self.assertEqual(server._transcribe(np.zeros(32000, np.float32), True)[0], "")
        self.assertEqual(model.transcribe.call_count, 2)

    def test_silence_does_not_trigger_a_second_decode(self):
        model = Mock()
        model.transcribe.return_value = (iter([]), SimpleNamespace(duration_after_vad=0))
        with patch.object(server, "get_whisper", return_value=model), patch.dict(server._stt_device, {server.WHISPER_ACCURATE_MODEL: "cpu"}):
            self.assertEqual(server._transcribe(np.zeros(32000, np.float32), True)[0], "")
        model.transcribe.assert_called_once()

    def test_quiet_audio_amplification_is_bounded(self):
        audio = np.array([0.01, -0.02], np.float32)
        np.testing.assert_allclose(server.louder(audio), audio * 10)
        np.testing.assert_array_equal(server.louder(np.zeros(4)), np.zeros(4))


class VoiceApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.owner = Mock()
        self.owner.status.return_value = {"enrolled": True, "ready": True}
        self.owner.verify.return_value = (True, "matched")
        self.owner_patch = patch.object(server, "owner_voice", self.owner)
        self.owner_patch.start()
        self.addCleanup(self.owner_patch.stop)
        transport = httpx.ASGITransport(app=server.app, client=("127.0.0.1", 1234))
        self.client = httpx.AsyncClient(transport=transport, base_url="http://localhost")
        self.addAsyncCleanup(self.client.aclose)
        self.pcm = (np.sin(np.arange(16000 * 4) * 0.1) * 8000).astype("<i2").tobytes()

    async def test_rejected_voice_never_reaches_transcription(self):
        for reason in ("not_enrolled", "not_owner", "unavailable", "too_short"):
            self.owner.verify.return_value = (False, reason)
            with patch.object(server, "_transcribe") as decoder:
                r = await self.client.post("/api/transcribe?for=mic&q=accurate", content=self.pcm)
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["text"], "")
                self.assertEqual(r.json()["owner"], reason)
                decoder.assert_not_called()

    async def test_all_listeners_require_owner(self):
        self.owner.verify.return_value = (False, "not_owner")
        for purpose in ("wake", "mic", "talk", "request", ""):
            r = await self.client.post(f"/api/transcribe?for={purpose}", content=self.pcm)
            self.assertEqual(r.json()["owner"], "not_owner")

    async def test_verified_mic_does_not_bias_toward_wake_word(self):
        with patch.object(server, "_transcribe", return_value=("what time is it", 0, "test/cpu")) as decoder, patch.object(server, "log"):
            r = await self.client.post("/api/transcribe?for=mic&q=accurate", content=self.pcm)
            self.assertEqual(r.json()["text"], "what time is it")
            self.assertIsNone(decoder.call_args.args[2])
            self.assertTrue(decoder.call_args.args[1])

    async def test_cross_origin_enrollment_is_rejected(self):
        for route in ("/api/owner/prepare", "/api/owner/enroll"):
            r = await self.client.post(route, headers={"Origin": "https://example.com"}, json={})
            self.assertEqual(r.status_code, 403)
        r = await self.client.delete("/api/owner", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status_code, 403)
        self.owner.enroll.assert_not_called()
        self.owner.remove.assert_not_called()

    async def test_enrollment_accepts_one_valid_sample_and_older_three_samples(self):
        sample = base64.b64encode(self.pcm).decode()
        short = base64.b64encode(self.pcm[:16000 * 2]).decode()
        for payload in ({"samples": []}, {"samples": [sample] * 4}, {"samples": ["bad"]}, {"samples": [short]}, []):
            r = await self.client.post("/api/owner/enroll", json=payload)
            self.assertEqual(r.status_code, 400)
        self.owner.enroll.assert_not_called()
        for count in (1, 3):
            r = await self.client.post("/api/owner/enroll", json={"samples": [sample] * count})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(len(self.owner.enroll.call_args.args[0]), count)

    async def test_wake_calls_never_skip_voice_verification(self):
        for reason in ("not_enrolled", "not_owner", "unavailable", "too_short"):
            self.owner.verify.return_value = (False, reason)
            with patch.object(server, "_transcribe") as decoder:
                r = await self.client.post("/api/transcribe?for=wake&q=accurate", content=self.pcm)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["text"], "")
            self.assertEqual(r.json()["owner"], reason)
            decoder.assert_not_called()

    async def test_oversized_recording_is_rejected_before_speaker_check(self):
        r = await self.client.post("/api/transcribe", content=bytes(16000 * 2 * 41))
        self.assertEqual(r.status_code, 413)
        self.owner.verify.assert_not_called()

    async def test_truncated_recording_cannot_become_a_partial_command(self):
        with patch.object(server, "_transcribe") as decoder:
            r = await self.client.post("/api/transcribe?for=request&q=accurate&truncated=1", content=self.pcm)
        self.assertEqual(r.json()["text"], "")
        self.assertEqual(r.json()["reason"], "too_long")
        self.assertTrue(r.json()["retry"])
        decoder.assert_not_called()
        self.owner.verify.assert_not_called()

    async def test_incomplete_pcm_sample_is_rejected(self):
        r = await self.client.post("/api/transcribe?for=mic", content=self.pcm + b"\x01")
        self.assertEqual(r.status_code, 400)
        self.owner.verify.assert_not_called()


class DisplayReplyTests(unittest.IsolatedAsyncioTestCase):
    def test_separate_casual_speech_and_markdown(self):
        speech, content = server.split_display_reply('<voice>Yep, here is the comparison.</voice>\n<display># Compare\n| A | B |\n|---|---|\n| 1 | 2 |</display>')
        self.assertEqual(speech, 'Yep, here is the comparison.')
        self.assertTrue(content.startswith('# Compare'))
        self.assertIn('| 1 | 2 |', content)

    def test_unformatted_local_reply_stays_on_screen(self):
        speech, content = server.split_display_reply('## A simple answer\n- First item\n- Second item')
        self.assertNotIn('First item', speech)
        self.assertIn('First item', content)

    def test_code_or_long_spoken_section_gets_short_acknowledgement(self):
        for spoken in ('```python print(1)```', 'word ' * 100, 'https://example.com'):
            speech, content = server.split_display_reply(f'<voice>{spoken}</voice><display># Code\nprint(1)</display>')
            self.assertLess(len(speech), 100)
            self.assertNotIn('print(1)', speech)

    async def test_split_tags_across_stream_chunks_and_preserve_sources(self):
        async def events():
            for event in ({'status': 'Searching'}, {'sources': [{'url': 'https://example.com'}]},
                          {'t': '<voi'}, {'t': 'ce>Here you go.</voice><dis'},
                          {'t': 'play># Answer\nThe result.</display>'}):
                yield json.dumps(event) + '\n'
        output = [json.loads(line) async for line in server.display_reply_stream(events())]
        self.assertEqual(output[0], {'status': 'Searching'})
        self.assertIn('sources', output[1])
        self.assertEqual(output[2], {'display': {'content': '# Answer\nThe result.'}})
        self.assertEqual(output[3], {'t': 'Here you go.'})


class AutoBrainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.answers = {"groq": "Online answer.", "gemini": "Backup answer.", "claude": "Claude answer."}
        self.discovery = {}
        self.local_requests = []
        self.online = self.enterContext(patch.object(server, "check_online", AsyncMock(return_value=True)))
        self.enterContext(patch.object(server, "plan_action", return_value=None))
        self.enterContext(patch.object(server, "log"))
        self.enterContext(patch.object(server, "DEFAULT_MODEL", "local:test"))
        self.enterContext(patch.object(server, "ONLINE_BRAIN_ORDER", ("groq", "gemini", "claude")))
        self.enterContext(patch.dict(server._rest, {p: {"until": 0, "reason": ""} for p in server.BRAINS}, clear=True))
        for provider in server.BRAINS:
            self.enterContext(patch.object(server, f"{provider}_key", return_value="test-key"))
            self.enterContext(patch.object(server, f"{provider}_label", side_effect=lambda model: model))
            discovery = AsyncMock(return_value=[f"{provider}-model"])
            self.discovery[provider] = discovery
            self.enterContext(patch.object(server, f"auto_{provider}_models", discovery))

            async def stream(*args, provider=provider, **kwargs):
                self.calls.append(provider)
                answer = self.answers[provider]
                if isinstance(answer, Exception):
                    raise answer
                if answer:
                    yield answer

            self.enterContext(patch.object(server, f"stream_{provider}", stream))

        transport = httpx.ASGITransport(app=server.app, client=("127.0.0.1", 1234))
        client_type = httpx.AsyncClient
        self.client = client_type(transport=transport, base_url="http://localhost")
        self.addAsyncCleanup(self.client.aclose)

        def local_response(request):
            self.local_requests.append(json.loads(request.content))
            return httpx.Response(200, text=json.dumps({"message": {"content": "Local answer."}}) + "\n")

        self.enterContext(patch.object(server.httpx, "AsyncClient", side_effect=lambda *args, **kwargs:
            client_type(transport=httpx.MockTransport(local_response))))

    async def ask(self, display=False):
        response = await self.client.post("/api/chat", json={
            "messages": [{"role": "user", "content": "Explain the moon"}], "voice": True, "web": False, "display": display,
        })
        self.assertEqual(response.status_code, 200)
        return [json.loads(line) for line in response.text.splitlines()]

    async def test_default_automatically_uses_online_ai(self):
        events = await self.ask()
        self.assertIn({"t": "Online answer."}, events)
        self.assertEqual(self.calls, ["groq"])
        self.assertEqual(self.local_requests, [])

    async def test_requested_display_routes_summary_and_content_separately(self):
        self.answers['groq'] = '<voice>Yep, here is the moon.</voice><display># The Moon\nOur natural satellite.</display>'
        events = await self.ask(display=True)
        self.assertIn({'display': {'content': '# The Moon\nOur natural satellite.'}}, events)
        self.assertIn({'t': 'Yep, here is the moon.'}, events)
        self.assertFalse(any('<display>' in event.get('t', '') for event in events))

    async def test_code_configured_provider_order_is_used(self):
        with patch.object(server, "ONLINE_BRAIN_ORDER", ("gemini", "groq", "claude")):
            events = await self.ask()
        self.assertIn({"t": "Backup answer."}, events)
        self.assertEqual(self.calls, ["gemini"])

    async def test_offline_skips_provider_discovery_and_uses_configured_local_model(self):
        self.online.return_value = False
        events = await self.ask()
        self.assertIn({"t": "Local answer."}, events)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.local_requests[0]["model"], "local:test")
        for discovery in self.discovery.values():
            discovery.assert_not_awaited()

    async def test_failed_online_provider_automatically_uses_next(self):
        self.answers["groq"] = server.BrainError(401, "invalid key")
        events = await self.ask()
        self.assertIn({"t": "Backup answer."}, events)
        self.assertEqual(self.calls, ["groq", "gemini"])
        self.assertEqual(self.local_requests, [])

    async def test_empty_online_answer_cannot_leave_voice_silent(self):
        self.answers["groq"] = ""
        events = await self.ask()
        self.assertIn({"t": "Backup answer."}, events)
        self.assertEqual(self.calls, ["groq", "gemini"])

    async def test_all_online_failures_fall_back_to_local(self):
        self.answers = {provider: server.BrainError(503, "unavailable") for provider in server.BRAINS}
        events = await self.ask()
        self.assertIn({"t": "Local answer."}, events)
        self.assertEqual(self.local_requests[0]["model"], "local:test")


if __name__ == "__main__":
    unittest.main()
