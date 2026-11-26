"""Speech -> text on the Mac with whisper.cpp (free, local, no data leaves the computer).

    ATLAS_WHISPER_BIN    whisper-cli (Homebrew: brew install whisper-cpp)
    ATLAS_WHISPER_MODEL  path to a ggml model, e.g. ~/Atlas/models/ggml-large-v3-turbo-q5_0.bin
Audio from the iPhone (.m4a) is converted with macOS's built-in `afconvert` to 16 kHz WAV.
"""
from __future__ import annotations

import array
import os
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from typing import Any

AUDIO_SUFFIXES = {".m4a", ".wav", ".caf", ".aac", ".mp3"}
# A short hint list helps Whisper spell warehouse words the way they are used at work.
VOCABULARY = ("Склад, рампа 31, Ribalta 37, локація 3Q.15.004.01, 2T54, 2O 06, 2F08, 2V30, Polo 1, "
              "Polo 2, Agenzia, Cooperativa, CrossChex, Роберто Полі, етикетки, палети, "
              "навантажувач, ретрак, медогляд, години.")


# On silence Whisper tends to "hear" a stock phrase from video subtitles. Such a transcript is
# treated as an empty recording, never as a message.
HALLUCINATIONS = (
    "дякую за перегляд", "дякуємо за перегляд", "дякую за увагу", "спасибо за просмотр",
    "продовження слідує", "продолжение следует", "субтитри", "субтитры", "редактор субтитрів",
    "thank you for watching", "thanks for watching", "you",
)
MIN_SECONDS = 0.8   # shorter than this is a mis-tap, not a message
MIN_PEAK = 500      # of 32767; a voice on the iPhone mic peaks far higher (about -36 dBFS)


class TranscribeError(Exception):
    pass


class NotReady(TranscribeError):
    """The audio file cannot be opened yet (iCloud is still downloading it). Not a real failure."""


def is_hallucination(text: str) -> bool:
    words = re.sub(r"[^\w']+", " ", text.lower()).strip()
    return not words or any(words == h or words.startswith(h + " ") and len(words) < len(h) + 25
                            for h in HALLUCINATIONS)


def audio_level(wav: Path) -> tuple[float, int] | None:
    """(seconds, peak) of a 16-bit PCM WAV, or None if it is not one we can read."""
    try:
        with wave.open(str(wav), "rb") as w:
            if w.getsampwidth() != 2:
                return None
            frames = w.readframes(w.getnframes())
            seconds = w.getnframes() / float(w.getframerate() or 1)
    except (wave.Error, EOFError, OSError):
        return None
    samples = array.array("h")
    samples.frombytes(frames[: len(frames) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    return seconds, max((abs(x) for x in samples), default=0)


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
            err = p.stderr or ""
            if "Couldn't open input file" in err or "No such file" in err:
                raise NotReady(f"{Path(cmd[0]).name}: file not readable yet")
            raise TranscribeError(f"{Path(cmd[0]).name} exit {p.returncode}: {(p.stderr or '')[-300:]}")
        return p.stdout

    def transcribe(self, audio: Path) -> str:
        """Text of the recording; "" when it is silent, too short, or Whisper only invented a
        stock phrase. Raises NotReady while the file is still arriving."""
        with tempfile.TemporaryDirectory() as tmp:
            # Read it ourselves first: on macOS this makes iCloud finish the download, and a
            # local copy cannot change under afconvert.
            src = Path(tmp) / ("in" + audio.suffix.lower())
            try:
                data = audio.read_bytes()
            except OSError as exc:
                raise NotReady(f"{audio.name}: {exc}") from exc
            if not data:
                raise NotReady(f"{audio.name}: empty so far")
            src.write_bytes(data)
            wav = src
            if src.suffix != ".wav":
                wav = Path(tmp) / "in16k.wav"
                self._run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
                           str(src), str(wav)])
            level = audio_level(wav)
            if level and (level[0] < MIN_SECONDS or level[1] < MIN_PEAK):
                return ""
            out = self._run([self.binary, "-m", self.model, "-l", self.language, "-nt", "-np",
                             "--prompt", VOCABULARY, "-f", str(wav)])
        text = " ".join(out.split()).strip()
        return "" if is_hallucination(text) else text
