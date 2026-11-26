"""Speech -> text on the Mac with whisper.cpp (free, local, no data leaves the computer).

    ATLAS_WHISPER_BIN    whisper-cli (Homebrew: brew install whisper-cpp)
    ATLAS_WHISPER_MODEL  path to a ggml model, e.g. ~/Atlas/models/ggml-large-v3-turbo-q5_0.bin
Audio from the iPhone (.m4a) is converted with macOS's built-in `afconvert` to 16 kHz WAV.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

AUDIO_SUFFIXES = {".m4a", ".wav", ".caf", ".aac", ".mp3"}
# A short hint list helps Whisper spell warehouse words the way they are used at work.
VOCABULARY = ("Склад, рампа 31, Ribalta 37, локація 3Q.15.004.01, 2T54, 2O 06, 2F08, 2V30, Polo 1, "
              "Polo 2, Agenzia, Cooperativa, CrossChex, Роберто Полі, етикетки, палети, "
              "навантажувач, ретрак, медогляд, години.")


class TranscribeError(Exception):
    pass


class WhisperCpp:
    def __init__(self, binary: str | None = None, model: str | None = None, language: str = "uk",
                 runner: Any = None, timeout: float = 600.0):
        self.binary = binary or os.environ.get("ATLAS_WHISPER_BIN") or shutil.which("whisper-cli") \
            or "whisper-cli"
        self.model = str(Path(model or os.environ.get("ATLAS_WHISPER_MODEL", "")).expanduser())
        self.language, self.timeout = language, timeout
        self.runner = runner or subprocess.run

    def _run(self, cmd: list[str]) -> str:
        try:
            p = self.runner(cmd, capture_output=True, text=True, timeout=self.timeout)
        except Exception as exc:
            raise TranscribeError(f"{cmd[0]}: {type(exc).__name__}: {exc}") from exc
        if p.returncode != 0:
            raise TranscribeError(f"{Path(cmd[0]).name} exit {p.returncode}: {(p.stderr or '')[-300:]}")
        return p.stdout

    def transcribe(self, audio: Path) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            wav = audio
            if audio.suffix.lower() != ".wav":
                wav = Path(tmp) / "in.wav"
                self._run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
                           str(audio), str(wav)])
            out = self._run([self.binary, "-m", self.model, "-l", self.language, "-nt", "-np",
                             "--prompt", VOCABULARY, "-f", str(wav)])
        return " ".join(out.split()).strip()
