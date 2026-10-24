import base64
import io
import json
import ssl
import tempfile
import unittest
import wave
from pathlib import Path

import websockets

from local_engine import LocalVoicevoxEngine
from server import ASSETS_DIR, MODEL_DIR, CPU_NUM_THREADS
from server import VoicevoxStreamingServer, ensure_tls_context, serve_page


EXPECTED = {
    ("四国めたん", "あまあま", 0),
    ("春日部つむぎ", "ノーマル", 8),
    ("冥鳴ひまり", "ノーマル", 14),
    ("WhiteCUL", "たのしい", 24),
}


def voice_set(speakers):
    return {
        (speaker["name"], style["name"], style["id"])
        for speaker in speakers for style in speaker["styles"]
    }


class FourVoicesTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = LocalVoicevoxEngine(ASSETS_DIR, MODEL_DIR, CPU_NUM_THREADS)

    def test_lists_exactly_requested_four_voices(self):
        speakers = self.engine.get_speakers()
        self.assertEqual(len(speakers), 4)
        self.assertEqual(voice_set(speakers), EXPECTED)

    def test_unselected_voices_are_rejected(self):
        for style_id in [1, 2, 3, 23, 25, 26, 6000, -1]:
            with self.subTest(style_id=style_id):
                with self.assertRaisesRegex(ValueError, "Unknown speaker style ID"):
                    self.engine.synthesize("こんにちは", style_id, {})

    def test_all_four_voices_synthesize_valid_wav(self):
        for style_id in [0, 8, 14, 24]:
            with self.subTest(style_id=style_id):
                wav_data = self.engine.synthesize("こんにちは", style_id, {})
                with wave.open(io.BytesIO(wav_data)) as audio:
                    self.assertEqual(audio.getframerate(), 24000)
                    self.assertGreater(audio.getnframes(), 1000)

    async def test_wss_lists_only_four_and_rejects_unselected_synthesis(self):
        server = VoicevoxStreamingServer(self.engine)
        with tempfile.TemporaryDirectory() as temp:
            tls = ensure_tls_context(Path(temp) / "localhost.crt", Path(temp) / "localhost.key")
            async with websockets.serve(
                server.handle_client, "127.0.0.1", 0,
                ssl=tls, process_request=serve_page,
            ) as listener:
                port = listener.sockets[0].getsockname()[1]
                async with websockets.connect(
                    f"wss://127.0.0.1:{port}/ws",
                    ssl=ssl._create_unverified_context(),
                ) as client:
                    await client.send(json.dumps({"type": "get_speakers"}))
                    reply = json.loads(await client.recv())
                    self.assertEqual(reply["type"], "speakers")
                    self.assertEqual(voice_set(reply["data"]), EXPECTED)
                    for request_type in ["synthesize", "stream_synthesize"]:
                        await client.send(json.dumps({
                            "type": request_type, "request_id": request_type,
                            "text": "こんにちは", "speaker": 23,
                        }))
                        reply = json.loads(await client.recv())
                        self.assertEqual(reply["type"], "error")
                        self.assertEqual(reply["request_id"], request_type)
                        self.assertIn("Unknown speaker style ID", reply["message"])
                    for style_id in [0, 8, 14, 24]:
                        await client.send(json.dumps({
                            "type": "synthesize", "request_id": str(style_id),
                            "text": "こんにちは", "speaker": style_id,
                        }))
                        reply = json.loads(await client.recv())
                        self.assertEqual(reply["type"], "audio")
                        self.assertEqual(reply["request_id"], str(style_id))
                        with wave.open(io.BytesIO(base64.b64decode(reply["data"]))) as audio:
                            self.assertGreater(audio.getnframes(), 1000)
                    await client.send(json.dumps({
                        "type": "stream_synthesize", "request_id": "selected-stream",
                        "text": "こんにちは", "speaker": 24,
                        "params": {"segmentLength": 0.2},
                    }))
                    self.assertEqual(json.loads(await client.recv())["type"], "stream_start")
                    chunks = 0
                    while True:
                        message = await client.recv()
                        if isinstance(message, bytes):
                            with wave.open(io.BytesIO(message)) as audio:
                                self.assertEqual(audio.getframerate(), 24000)
                                self.assertGreater(audio.getnframes(), 0)
                            chunks += 1
                        else:
                            reply = json.loads(message)
                            self.assertEqual(reply["type"], "stream_end")
                            self.assertEqual(reply["request_id"], "selected-stream")
                            self.assertEqual(reply["total_chunks"], chunks)
                            self.assertGreater(chunks, 1)
                            break


class ModelRebuildTest(unittest.TestCase):
    def test_rebuild_validates_selection_before_writing(self):
        import yaml
        from rebuild_models import ROOT, rebuild

        with tempfile.TemporaryDirectory() as temp:
            selection = yaml.safe_load((ROOT / "voice_selection.yaml").read_text())
            selection["models"][-1]["voices"][0]["style"] = "ノーマル"
            selection_path = Path(temp) / "invalid.yaml"
            selection_path.write_text(yaml.safe_dump(selection), encoding="utf-8")
            output = Path(temp) / "models"
            with self.assertRaisesRegex(ValueError, "Voice does not match"):
                rebuild(ROOT / "assets/models", output, selection_path)
            self.assertFalse(output.exists())

    def test_rebuild_cannot_overwrite_source_models(self):
        from rebuild_models import ROOT, rebuild

        with self.assertRaisesRegex(ValueError, "must be different"):
            rebuild(ROOT / "assets/models", ROOT / "assets/models", ROOT / "voice_selection.yaml")

    def test_rebuilt_archives_keep_only_selected_core_metadata(self):
        import hashlib
        from voicevox_core.blocking import VoiceModelFile
        from zipfile import ZipFile
        from rebuild_models import ROOT, rebuild

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "models"
            report = rebuild(ROOT / "assets/models", output, ROOT / "voice_selection.yaml")
            result = set()
            for model_info in report["models"]:
                path = output / model_info["file"]
                with VoiceModelFile.open(path) as model:
                    result.update(
                        (speaker.name, style.name, style.id)
                        for speaker in model.metas for style in speaker.styles
                    )
                with ZipFile(ROOT / "assets/models" / path.name) as source, ZipFile(path) as derived:
                    for name in ["models/pd.bin", "models/pi.bin", "models/d.bin"]:
                        self.assertEqual(
                            hashlib.sha256(source.read(name)).digest(),
                            hashlib.sha256(derived.read(name)).digest(),
                        )
            self.assertEqual(result, EXPECTED)


if __name__ == "__main__":
    unittest.main()
