"""Build narrated Reels locally: commentary over a source clip, or original Reels from free stock footage (Pexels or Pixabay).

Everything in this module runs on this computer and costs nothing per Reel:
Kokoro (voiceover), faster-whisper (speech transcript), Pillow (on-screen text)
and FFmpeg (video). The only paid step stays the single AI text call made in
pipeline.py.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import requests

ROOT = Path(__file__).resolve().parent
MODELS = ROOT / "models"
WIDTH, HEIGHT, FPS = 1080, 1920, 30
KOKORO_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
KOKORO_MODEL = "kokoro-v1.0.int8.onnx"
KOKORO_VOICES = "voices-v1.0.bin"
STOCK_USED_FILE = ROOT / "media" / "stock-used.json"
MAX_STOCK_BYTES = 150 * 1024 * 1024
# Keep on-screen and spoken text to characters the Windows fonts and the voice
# model handle (Latin + Devanagari); emoji become empty boxes in the video.
_DISPLAY_SAFE = re.compile(r"[^\x20-\x7E\u00A0-\u024F\u2018-\u201F\u2026\u2013\u2014\u0900-\u097F\u200C\u200D]")
_DEVANAGARI = re.compile(r"[\u0900-\u097F]")


def setting(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", _DISPLAY_SAFE.sub("", str(text or ""))).strip()


def is_hindi() -> bool:
    return setting("COMMENTARY_LANGUAGE", "English").lower() in {"hindi", "hinglish"}


def voice_settings() -> tuple[str, str, float]:
    """(voice, language, speed) for Kokoro; Hindi gets a Hindi voice unless one is set."""
    hindi = is_hindi()
    voice = setting("TTS_VOICE") or ("hm_psi" if hindi else "af_heart")
    lang = setting("TTS_LANG") or ("hi" if hindi else "en-us")
    return voice, lang, float(setting("TTS_SPEED", "1.05") or 1.05)


# --------------------------------------------------------------------------- FFmpeg


def ffmpeg_exe() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def run_ffmpeg(args: list[str], timeout: int = 1200) -> None:
    result = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *map(str, args)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed: {result.stderr.strip()[-400:]}")


def probe(path: Path) -> tuple[float, bool]:
    """Return (duration in seconds, has an audio stream) from FFmpeg's input summary."""
    result = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-i", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match:
        raise RuntimeError("Could not read the video duration")
    hours, minutes, seconds = match.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    has_audio = bool(re.search(r"Stream #\S+.*Audio:", result.stderr))
    return duration, has_audio


# --------------------------------------------------------------------------- Transcript

_WHISPER = None


def transcribe_local(video_path: Path) -> str:
    """Free speech-to-text on this PC. Returns "" for music-only or silent clips."""
    global _WHISPER
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return ""
    try:
        # Decode with FFmpeg ourselves (16 kHz mono) instead of relying on PyAV's version.
        decoded = subprocess.run(
            [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(video_path), "-map", "0:a:0?",
             "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
            capture_output=True, timeout=180, check=False,
        )
        samples = np.frombuffer(decoded.stdout, dtype="<i2").astype(np.float32) / 32768.0
        if samples.size < 16000:  # no audio stream, or under one second of sound
            return ""
        if _WHISPER is None:
            os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
            _WHISPER = WhisperModel(
                setting("WHISPER_MODEL", "base"), device="cpu", compute_type="int8",
                download_root=str(MODELS / "whisper"),
            )
        segments, _info = _WHISPER.transcribe(samples, vad_filter=True, beam_size=1)
        # Whisper invents words for music, laughing and noise; those come back with a
        # low average log-probability, so keep only segments it is reasonably sure of.
        text = " ".join(s.text.strip() for s in segments
                        if s.no_speech_prob < 0.6 and s.avg_logprob > -1.0)
    except Exception as exc:  # no audio stream, unreadable audio, model download issue
        print(f"Local transcript skipped ({type(exc).__name__})")
        return ""
    return text.strip()[:1500]


# --------------------------------------------------------------------------- Voice

_TTS = None


@dataclass
class Cue:
    text: str
    start: float
    end: float


def download_voice_model() -> None:
    MODELS.mkdir(parents=True, exist_ok=True)
    for name in (KOKORO_MODEL, KOKORO_VOICES):
        target = MODELS / name
        if target.is_file() and target.stat().st_size > 1_000_000:
            print(f"{name}: already present")
            continue
        partial = target.with_suffix(target.suffix + ".part")
        with requests.get(f"{KOKORO_RELEASE}/{name}", stream=True, timeout=(15, 120)) as response:
            response.raise_for_status()
            with partial.open("wb") as handle:
                for chunk in response.iter_content(1024 * 1024):
                    handle.write(chunk)
        partial.replace(target)
        print(f"{name}: downloaded")


def tts():
    global _TTS
    if _TTS is None:
        from kokoro_onnx import Kokoro

        model = MODELS / setting("KOKORO_MODEL_FILE", KOKORO_MODEL)
        voices = MODELS / KOKORO_VOICES
        if not model.is_file() or not voices.is_file():
            raise RuntimeError("Voice model is missing; run: pipeline.py setup-voice")
        _TTS = Kokoro(str(model), str(voices))
    return _TTS


SAMPLE_RATE = 24000
_EDGE_FAILED = False
_NARRATOR = ""  # "male" or "female" for the Reel being voiced; empty = EDGE_VOICE


def edge_voice() -> str:
    """The Edge voice for the current Reel: the AI picks male or female per clip
    (AUTO_VOICE=true); otherwise EDGE_VOICE."""
    auto = setting("AUTO_VOICE", "true").lower() not in {"0", "false", "no", "off"}
    if auto and _NARRATOR == "female":
        return setting("EDGE_VOICE_FEMALE", "en-US-AvaMultilingualNeural")
    if auto and _NARRATOR == "male":
        return setting("EDGE_VOICE_MALE", "en-US-BrianMultilingualNeural")
    return setting("EDGE_VOICE") or ("hi-IN-MadhurNeural" if is_hindi() else "en-US-GuyNeural")


def _percent(value: str, default: int) -> int:
    match = re.fullmatch(r"\s*([+-]?\d+)\s*(%|Hz)?\s*", value or "")
    return int(match.group(1)) if match else default


def _prosody(text: str) -> tuple[str, str]:
    """Edge rate and pitch for one beat, so the delivery rises and falls like a person
    joking instead of reading: excited lines faster and higher, questions lifted."""
    rate = _percent(setting("EDGE_RATE", "+5%"), 5)
    pitch = _percent(setting("EDGE_PITCH", "+0Hz"), 0)
    if setting("EXPRESSIVE_VOICE", "true").lower() not in {"0", "false", "no", "off"}:
        ending = text.rstrip()[-1:] if text.strip() else ""
        if ending == "!":
            rate, pitch = rate + 12, pitch + 18
        elif ending == "?":
            rate, pitch = rate + 2, pitch + 12
        else:
            pitch += 4
    return f"{rate:+d}%", f"{pitch:+d}Hz"


def _speak_edge(text: str) -> np.ndarray:
    """Microsoft Edge neural voice (free, natural, needs internet). 24 kHz mono float samples."""
    import asyncio

    import edge_tts

    rate, pitch = _prosody(text)
    voice = edge_voice()
    with tempfile.TemporaryDirectory(prefix="edge-tts-") as temp:
        mp3 = Path(temp) / "line.mp3"
        communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
        asyncio.run(communicate.save(str(mp3)))
        decoded = subprocess.run(
            [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(mp3), "-ac", "1",
             "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"],
            capture_output=True, timeout=120, check=True,
        )
    audio = np.frombuffer(decoded.stdout, dtype="<i2").astype(np.float32) / 32768.0
    if audio.size < SAMPLE_RATE // 10:
        raise RuntimeError("Edge voice returned no audio")
    return audio


def speak(text: str) -> np.ndarray:
    """One spoken line, voiced beat by beat: '...' gets a comic pause, and each sentence
    gets its own tone (see _prosody)."""
    beats: list[tuple[str, float]] = []
    for chunk in re.split(r"\.{3,}|…", text):
        sentences = [s.strip() for s in re.findall(r"[^!?।]+[!?।]*", chunk) if s.strip(" .")]
        for index, sentence in enumerate(sentences):
            last = index == len(sentences) - 1
            beats.append((sentence.strip(), 0.3 if last else 0.12))
    if len(beats) < 2:
        return _speak_one(text)
    parts: list[np.ndarray] = []
    for beat, pause in beats:
        parts += [_speak_one(beat), np.zeros(int(pause * SAMPLE_RATE), dtype=np.float32)]
    return np.concatenate(parts[:-1])


class EdgeVoiceDown(RuntimeError):
    pass


def with_voice_fallback(make):
    """Voice a whole Reel with Edge; if Edge is down, redo the whole Reel with Kokoro so
    one video never mixes two voices. Every new Reel tries Edge again."""
    global _EDGE_FAILED
    _EDGE_FAILED = False
    try:
        return make()
    except EdgeVoiceDown as exc:
        print(f"Edge voice unavailable ({exc}); this Reel uses the local Kokoro voice")
        _EDGE_FAILED = True
        try:
            return make()
        finally:
            _EDGE_FAILED = False


def _speak_one(text: str) -> np.ndarray:
    """One phrase as 24 kHz samples with the Edge voice (TTS_ENGINE=edge) or Kokoro."""
    if setting("TTS_ENGINE", "edge").lower() == "edge" and not _EDGE_FAILED:
        problem = ""
        for attempt in range(3):  # brief network or service hiccups
            try:
                return _speak_edge(text)
            except Exception as exc:
                problem = type(exc).__name__
                time.sleep(2 * (attempt + 1))
        raise EdgeVoiceDown(problem)
    voice, lang, speed = voice_settings()
    audio, _rate = tts().create(text, voice=voice, speed=speed, lang=lang)
    return np.asarray(audio, dtype=np.float32)


def speak_lines(lines: list[str | tuple[str, str]], gap: float = 0.18) -> tuple[np.ndarray, list[Cue]]:
    """Speak each line; return the samples and when each line is spoken (from 0).

    A line is either one string, or (what to say, what to show on screen) - e.g.
    Devanagari for the Hindi voice and the same sentence in Hinglish for captions.
    """
    parts: list[np.ndarray] = []
    cues: list[Cue] = []
    t = 0.0
    for raw in lines:
        say, show = (raw, raw) if isinstance(raw, str) else raw
        say, show = clean_text(say), clean_text(show) or clean_text(say)
        if not say:
            continue
        audio = speak(say)
        duration = len(audio) / SAMPLE_RATE
        cues.append(Cue(show, t, t + duration))
        parts += [audio, np.zeros(int(gap * SAMPLE_RATE), dtype=np.float32)]
        t += duration + gap
    if not cues:
        raise RuntimeError("The voiceover script is empty")
    return np.concatenate(parts), cues


def write_wav(samples: np.ndarray, out_wav: Path) -> None:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(out_wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm.tobytes())


def synthesize_lines(lines: list[str | tuple[str, str]], out_wav: Path, gap: float = 0.18) -> list[Cue]:
    """Speak lines back to back into one WAV (used for original Reels)."""
    samples, cues = speak_lines(lines, gap)
    write_wav(samples, out_wav)
    return cues


def caption_cues(lines: list[Cue], max_words: int = 4) -> list[Cue]:
    """Split each spoken line into short on-screen chunks timed by their length."""
    cues: list[Cue] = []
    for line in lines:
        words = line.text.split()
        groups = [" ".join(words[i:i + max_words]) for i in range(0, len(words), max_words)]
        total = sum(len(g) for g in groups) or 1
        t = line.start
        for group in groups:
            span = (line.end - line.start) * len(group) / total
            cues.append(Cue(group, t, t + span))
            t += span
    return cues


# --------------------------------------------------------------------------- Text images


def _font(size: int, devanagari: bool = False):
    from PIL import ImageFont

    # Nirmala UI has Devanagari glyphs; it is only a fallback because without
    # libraqm Pillow cannot join Hindi vowel signs properly (captions are Hinglish).
    preferred = ("C:/Windows/Fonts/NirmalaB.ttf",) if devanagari else ()
    for candidate in (*preferred, setting("CAPTION_FONT"), "C:/Windows/Fonts/segoeuib.ttf",
                      "C:/Windows/Fonts/arialbd.ttf"):
        if candidate and Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default(size)


def text_image(path: Path, text: str, size: int, *, fill=(255, 255, 255, 255),
               stroke: int = 6, background=None, max_width: int = 920, pad: int = 26) -> Path:
    from PIL import Image, ImageDraw

    font = _font(size, devanagari=bool(_DEVANAGARI.search(text)))
    measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    lines: list[str] = []
    current = ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if not current or measure.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    line_height = int(size * 1.22)
    widths = [measure.textlength(line, font=font) for line in lines] or [0]
    width = int(max(widths)) + 2 * (pad + stroke)
    height = line_height * max(1, len(lines)) + 2 * pad
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if background:
        draw.rounded_rectangle([0, 0, width - 1, height - 1], radius=min(34, height // 3), fill=background)
    y = pad
    for line, line_width in zip(lines, widths):
        draw.text(((width - line_width) / 2, y), line, font=font, fill=fill,
                  stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
        y += line_height
    image.save(path)
    return path


# --------------------------------------------------------------------------- Compose


def compose(base_video: Path, voice_wav: Path, spoken: list[Cue], out_path: Path, work: Path, *,
            hook: str = "", credit: str = "", voice_delay: float = 0.3,
            keep_source_audio: bool = True) -> Path:
    """Lay the voiceover, captions, hook title and credit tag over a 9:16 version of the clip."""
    duration, has_audio = probe(base_video)
    with wave.open(str(voice_wav), "rb") as handle:
        voice_seconds = handle.getnframes() / handle.getframerate()
    total = max(duration, voice_seconds + voice_delay + 0.5)
    pad_seconds = max(0.0, total - duration)

    images: list[tuple[Path, str]] = []  # (png, overlay options)
    hook = clean_text(hook)
    if hook:
        png = text_image(work / "hook.png", hook, 64, fill=(20, 20, 20, 255), stroke=0,
                         background=(255, 255, 255, 235), max_width=900)
        images.append((png, "x=(W-w)/2:y=170"))
    credit = clean_text(credit).lstrip("@")
    if credit:
        png = text_image(work / "credit.png", f"via @{credit}", 34, stroke=0,
                         background=(0, 0, 0, 150), pad=14)
        images.append((png, "x=40:y=H-h-360"))
    for index, cue in enumerate(caption_cues(spoken)):
        png = text_image(work / f"cue{index:03d}.png", cue.text, 76, stroke=7)
        start, end = cue.start + voice_delay, cue.end + voice_delay
        images.append((png, f"x=(W-w)/2:y=1150:enable='between(t,{start:.3f},{end:.3f})'"))

    inputs: list[str] = ["-i", str(base_video), "-i", str(voice_wav)]
    for png, _ in images:
        inputs += ["-i", str(png)]

    graph = [
        f"[0:v]fps={FPS},split=2[s1][s2]",
        f"[s1]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT},"
        "boxblur=24:2,eq=brightness=-0.12[bg]",
        f"[s2]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease[fg]",
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,"
        f"tpad=stop_mode=clone:stop_duration={pad_seconds + 0.5:.3f}[v0]",
    ]
    label = "v0"
    for index, (_png, options) in enumerate(images):
        nxt = f"v{index + 1}"
        graph.append(f"[{label}][{index + 2}:v]overlay={options}[{nxt}]")
        label = nxt

    delay_ms = int(voice_delay * 1000)
    gain = setting("VOICE_GAIN", "1.5") or "1.5"
    graph.append(f"[1:a]aresample=48000,aformat=channel_layouts=stereo,volume={gain},"
                 f"adelay={delay_ms}|{delay_ms},apad[voice]")
    if keep_source_audio and has_audio:
        source_volume = setting("SOURCE_AUDIO_VOLUME", "1.0") or "1.0"
        graph += [
            "[voice]asplit=2[voice1][voice2]",
            f"[0:a]aresample=48000,aformat=channel_layouts=stereo,volume={source_volume},apad[src]",
            # Duck the clip's own sound while the commentary is speaking.
            "[src][voice2]sidechaincompress=threshold=0.02:ratio=12:attack=15:release=350[duck]",
            "[duck][voice1]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95[aout]",
        ]
    else:
        graph.append("[voice]alimiter=limit=0.95[aout]")

    partial = out_path.with_name(out_path.stem + ".rendering.mp4")
    try:
        run_ffmpeg([
            *inputs, "-filter_complex", ";".join(graph), "-map", f"[{label}]", "-map", "[aout]",
            "-t", f"{total:.3f}", "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
            "-movflags", "+faststart", str(partial),
        ])
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    partial.replace(out_path)
    return out_path


def render_commentary(source: Path, intro: list, outro: list, out_path: Path, *, hook: str, credit: str,
                      reactions: list | tuple = (), voice: str = "") -> Path:
    """Commentary Reel: a spoken intro on what the clip is about, the clip with its own sound
    plus short reactions at the moments they react to, and a spoken ending that asks viewers
    to comment; plus captions, hook and creator credit.

    reactions: [(seconds into the clip, (say, show)), ...]; voice: "male", "female" or "".
    """
    global _NARRATOR
    out_path.parent.mkdir(parents=True, exist_ok=True)
    duration, _has_audio = probe(source)
    _NARRATOR = voice
    try:
        (intro_audio, intro_cues), (outro_audio, outro_cues), beats = with_voice_fallback(
            lambda: (speak_lines(intro), speak_lines(outro),
                     [(moment, speak_lines([line])) for moment, line in sorted(reactions, key=lambda r: r[0])]))
    finally:
        _NARRATOR = ""
    lead = 0.3
    segments = [(lead, intro_audio, intro_cues)]
    cursor = lead + len(intro_audio) / SAMPLE_RATE
    outro_seconds = len(outro_audio) / SAMPLE_RATE
    latest_outro = max(cursor + 1.0, duration - outro_seconds - 0.3)
    for moment, (beat_audio, beat_cues) in beats:
        # Just after the action is visible, never on top of another line.
        start = max(moment + 0.25, cursor + 0.3)
        end = start + len(beat_audio) / SAMPLE_RATE
        if end + 0.3 > latest_outro:
            continue  # no room before the ending; skip this reaction
        segments.append((start, beat_audio, beat_cues))
        cursor = end
    # The ending plays over the last moments of the clip; a short clip is
    # extended with its last frame so voice parts never overlap.
    outro_start = max(cursor + 0.6, duration - outro_seconds - 0.3)
    segments.append((outro_start, outro_audio, outro_cues))
    samples = np.zeros(int((outro_start + outro_seconds + 0.1) * SAMPLE_RATE), dtype=np.float32)
    cues: list[Cue] = []
    for start, audio, spoken in segments:
        index = int(start * SAMPLE_RATE)
        samples[index:index + len(audio)] += audio[:len(samples) - index]
        cues += [Cue(c.text, c.start + start, c.end + start) for c in spoken]
    with tempfile.TemporaryDirectory(prefix="reel-render-") as temp:
        work = Path(temp)
        write_wav(samples, work / "voice.wav")
        return compose(source, work / "voice.wav", cues, out_path, work, hook=hook, credit=credit,
                       voice_delay=0.0)


# --------------------------------------------------------------------------- Original Reels


def stock_provider() -> str:
    """Which free stock-video source to use: Pexels if it has a key, otherwise Pixabay."""
    if setting("PEXELS_API_KEY"):
        return "pexels"
    if setting("PIXABAY_API_KEY"):
        return "pixabay"
    return ""


def _used_stock() -> set[str]:
    try:
        return {str(item) for item in json.loads(STOCK_USED_FILE.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return set()


def _remember_stock(clip_id: str) -> None:
    used = _used_stock()
    used.add(clip_id)
    STOCK_USED_FILE.parent.mkdir(parents=True, exist_ok=True)
    STOCK_USED_FILE.write_text(json.dumps(sorted(used)[-3000:]), encoding="utf-8")


def _pexels_candidates(query: str) -> list[tuple[str, float, str]]:
    """(clip id, duration, mp4 url) for portrait Pexels clips, closest to 1080x1920 first."""
    response = requests.get(
        "https://api.pexels.com/videos/search", headers={"Authorization": setting("PEXELS_API_KEY")},
        params={"query": query, "orientation": "portrait", "size": "medium", "per_page": 15},
        timeout=(10, 30),
    )
    response.raise_for_status()
    candidates = []
    for video in response.json().get("videos", []):
        files = [f for f in video.get("video_files", [])
                 if f.get("file_type") == "video/mp4" and f.get("link")
                 and (f.get("height") or 0) >= (f.get("width") or 1) and (f.get("height") or 0) >= 960]
        if files:
            best = min(files, key=lambda f: abs((f.get("height") or 0) - HEIGHT))
            candidates.append((f"pexels:{video.get('id')}", float(video.get("duration") or 0), best["link"]))
    return candidates


def _pixabay_candidates(query: str) -> list[tuple[str, float, str]]:
    """(clip id, duration, mp4 url) for Pixabay clips; portrait ones first.

    Pixabay has no orientation filter and most clips are landscape, so those are
    centre-cropped to 9:16 when the scene is rendered.
    """
    response = requests.get(
        "https://pixabay.com/api/videos/",
        params={"key": setting("PIXABAY_API_KEY"), "q": query[:100], "safesearch": "true",
                "per_page": 20, "min_height": 720},
        timeout=(10, 30),
    )
    if response.status_code == 429:
        raise RuntimeError("Pixabay rate limit reached; try again later")
    response.raise_for_status()
    portrait, landscape = [], []
    for hit in response.json().get("hits", []):
        renditions = [r for r in (hit.get("videos") or {}).values()
                      if isinstance(r, dict) and r.get("url") and (r.get("height") or 0) >= 720]
        if not renditions:
            continue
        upright = (renditions[0].get("height") or 0) > (renditions[0].get("width") or 0)
        # Portrait: closest to 1920 tall. Landscape: 1080p, since the crop keeps
        # only the middle third of the width.
        target = HEIGHT if upright else 1080
        best = min(renditions, key=lambda r: (abs((r.get("height") or 0) - target), r.get("size") or 0))
        entry = (f"pixabay:{hit.get('id')}", float(hit.get("duration") or 0), best["url"])
        (portrait if upright else landscape).append(entry)
    return portrait + landscape


def stock_clip(query: str, dest: Path, min_seconds: float) -> Path:
    """Download a free stock clip for one scene, never reusing a clip already used."""
    provider = stock_provider()
    if not provider:
        raise RuntimeError("Set PEXELS_API_KEY or PIXABAY_API_KEY in .env to make original Reels")
    candidates = _pexels_candidates(query) if provider == "pexels" else _pixabay_candidates(query)
    used = _used_stock()
    fresh = [c for c in candidates if c[0] not in used]
    fresh.sort(key=lambda c: c[1] < min_seconds)  # long enough clips first, order kept otherwise
    for clip_id, _duration, url in fresh:
        with requests.get(url, stream=True, timeout=(10, 120)) as download:
            download.raise_for_status()
            size = 0
            with dest.open("wb") as handle:
                for chunk in download.iter_content(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_STOCK_BYTES:
                        raise RuntimeError("Stock clip is unexpectedly large")
                    handle.write(chunk)
        _remember_stock(clip_id)
        return dest
    raise RuntimeError(f"No unused stock clip found for '{query}'")


def render_original(plan: dict, out_path: Path) -> Path:
    """Original Reel: our script and voice over free stock scenes, one scene per spoken line."""
    scenes = [s for s in plan.get("scenes", []) if clean_text(s.get("say") or s.get("line", ""))]
    if len(scenes) < 3:
        raise RuntimeError("The original Reel plan needs at least three scenes")
    fallback_query = setting("ORIGINAL_FALLBACK_SEARCH", "nature")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="reel-original-") as temp:
        work = Path(temp)
        spoken = with_voice_fallback(lambda: synthesize_lines(
            [(s.get("say") or s.get("line", ""), s.get("show") or s.get("line", "")) for s in scenes],
            work / "voice.wav",
        ))
        parts: list[Path] = []
        for index, cue in enumerate(spoken):
            end = spoken[index + 1].start if index + 1 < len(spoken) else cue.end + 0.6
            seconds = max(1.5, end - cue.start)
            stock = work / f"stock{index}.mp4"
            query = clean_text(scenes[index].get("search", "")) or fallback_query
            try:
                stock_clip(query, stock, seconds)
            except (RuntimeError, requests.RequestException):
                stock_clip(fallback_query, stock, seconds)
            part = work / f"scene{index}.mp4"
            run_ffmpeg([
                "-stream_loop", "-1", "-i", str(stock), "-t", f"{seconds:.3f}", "-an",
                "-vf", f"fps={FPS},scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
                       f"crop={WIDTH}:{HEIGHT},setsar=1",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", str(part),
            ])
            parts.append(part)
        listing = work / "scenes.txt"
        listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
        body = work / "body.mp4"
        run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(body)])
        return compose(body, work / "voice.wav", spoken, out_path, work,
                       hook=plan.get("hook", ""), voice_delay=0.0, keep_source_audio=False)
