"""Local VOICEVOX Core 0.17 adapter for the WebSocket service."""

import dataclasses
import io
import threading
import wave
from pathlib import Path

from voicevox_core.blocking import Onnxruntime, OpenJtalk, Synthesizer, VoiceModelFile


class LocalVoicevoxEngine:
    def __init__(self, assets_dir: Path, model_dir: Path, cpu_num_threads: int = 0):
        runtime = assets_dir / "onnxruntime/lib/libvoicevox_onnxruntime.1.17.3.dylib"
        dictionary = assets_dir / "dict/open_jtalk_dic_utf_8-1.11"
        if not runtime.is_file() or not (dictionary / "sys.dic").is_file():
            raise FileNotFoundError("Local ONNX Runtime or Open JTalk dictionary is missing")
        models = sorted(model_dir.glob("*.vvm"))
        if not models:
            raise FileNotFoundError(f"No .vvm models in {model_dir}")

        onnxruntime = Onnxruntime.load_once(filename=str(runtime))
        self.synthesizer = Synthesizer(
            onnxruntime,
            OpenJtalk(dictionary),
            acceleration_mode="CPU",
            cpu_num_threads=cpu_num_threads or 4,
        )
        self._lock = threading.Lock()
        self._style_to_model = {}
        self._speakers = {}
        self._loaded_models = set()
        for path in models:
            with VoiceModelFile.open(path) as model:
                for speaker in model.metas:
                    talk_styles = [style for style in speaker.styles if style.type == "talk"]
                    if not talk_styles:
                        continue
                    entry = self._speakers.setdefault(
                        speaker.speaker_uuid,
                        {"name": speaker.name, "speaker_uuid": speaker.speaker_uuid,
                         "styles": [], "version": speaker.version},
                    )
                    for style in talk_styles:
                        if style.id in self._style_to_model:
                            raise ValueError(f"Duplicate style ID: {style.id}")
                        self._style_to_model[style.id] = path
                        entry["styles"].append(dataclasses.asdict(style))

    def get_speakers(self) -> list[dict]:
        return list(self._speakers.values())

    def synthesize(self, text: str, style_id: int, params: dict) -> bytes:
        model_path = self._style_to_model.get(style_id)
        if model_path is None:
            raise ValueError(f"Unknown speaker style ID: {style_id}")
        with self._lock:
            if model_path not in self._loaded_models:
                with VoiceModelFile.open(model_path) as model:
                    self.synthesizer.load_voice_model(model)
                self._loaded_models.add(model_path)
            query = self.synthesizer.create_audio_query(text, style_id)
            for key, attr in (
                ("speedScale", "speed_scale"),
                ("pitchScale", "pitch_scale"),
                ("intonationScale", "intonation_scale"),
                ("volumeScale", "volume_scale"),
            ):
                if key in params:
                    setattr(query, attr, float(params[key]))
            query.validate()
            return self.synthesizer.synthesis(query, style_id)


def split_wav(wav_bytes: bytes, segment_length: float):
    """Split a complete WAV into standalone WAV files for browser playback."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as source:
        rate = source.getframerate()
        channels = source.getnchannels()
        sample_width = source.getsampwidth()
        frames_per_segment = max(1, int(rate * segment_length))
        while frames := source.readframes(frames_per_segment):
            output = io.BytesIO()
            with wave.open(output, "wb") as chunk:
                chunk.setnchannels(channels)
                chunk.setsampwidth(sample_width)
                chunk.setframerate(rate)
                chunk.writeframes(frames)
            yield output.getvalue()
