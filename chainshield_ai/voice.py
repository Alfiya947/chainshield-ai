"""Spoken case briefings (ElevenLabs text-to-speech, standard library only).

* Reads ONLY text the deterministic engine already produced. Voice never changes a decision.
* Needs ELEVENLABS_API_KEY. Without it (or if the call fails) the UI falls back to the browser's built-in voice.
* Audio is cached on disk per script, so replaying a case costs nothing.
* The key is read from the environment and never logged or shown.

Optional env vars: ELEVENLABS_VOICE_ID, ELEVENLABS_MODEL_ID.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = "https://api.elevenlabs.io"
DEFAULT_VOICE = "JBFqnCBsd6RMkjVDRZzb"      # a pre-made ElevenLabs voice; override with ELEVENLABS_VOICE_ID
DEFAULT_MODEL = "eleven_flash_v2_5"         # fast, lower-cost model; override with ELEVENLABS_MODEL_ID
CACHE_DIR = Path(__file__).with_name(".audio_cache")
MAX_CHARS = 900                              # keeps free-tier credit use small


def available() -> bool:
    return bool(os.environ.get("ELEVENLABS_API_KEY"))


def _inr_spoken(amount: float) -> str:
    return f"{float(amount):,.0f} rupees"


def speakable(text: str) -> str:
    """Make text TTS-friendly: read the rupee sign as a word."""
    return re.sub(r"₹([\d,]+)", r"\1 rupees", text)


def build_briefing(report: dict, max_chars: int = MAX_CHARS) -> str:
    """A short, plain-language script (about 20-40 seconds) built from the finished case report."""
    tx, n = report["transaction"], report["narrative"]
    who = tx["employee_role"].replace("_", " ")
    parts = [
        f"Case {report['case_id']}.",
        n["headline"],
        f"A {who} asked to refund {_inr_spoken(tx['amount'])}"
        + (f" on order {tx['original_transaction_id']}." if tx.get("original_transaction_id") else "."),
    ]
    reasons = [r.rstrip(".") + "." for r in report["reasons"][:3]]
    if len(report["reasons"]) > 3:
        reasons.append(f"There are {len(report['reasons']) - 3} more findings in the report.")
    parts += reasons
    parts.append(f"Risk score {report['risk_score']} out of 100. This is a triage priority, not a fraud probability.")
    parts.append(n["next_step"])
    if report["missing_information"]:
        parts.append("Missing information: " + "; ".join(report["missing_information"]) + ".")
    text = speakable(" ".join(parts))
    if len(text) > max_chars:                       # cut at a sentence boundary
        text = text[:max_chars].rsplit(". ", 1)[0] + "."
    return text


def _cache_path(text: str, voice: str, model: str) -> Path:
    digest = hashlib.sha256(f"{voice}|{model}|{text}".encode()).hexdigest()[:24]
    return CACHE_DIR / f"{digest}.mp3"


def synthesize(text: str, timeout: int = 25) -> tuple[bytes | None, str]:
    """Return (mp3 bytes, message). On any problem returns (None, short reason) so the UI can fall back."""
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        return None, ""
    voice = os.environ.get("ELEVENLABS_VOICE_ID", DEFAULT_VOICE)
    model = os.environ.get("ELEVENLABS_MODEL_ID", DEFAULT_MODEL)
    path = _cache_path(text, voice, model)
    try:
        if path.exists():
            return path.read_bytes(), "cached"
    except OSError:
        pass
    req = urllib.request.Request(
        f"{BASE_URL}/v1/text-to-speech/{voice}?output_format=mp3_44100_128",
        data=json.dumps({"text": text, "model_id": model}).encode(),
        headers={"xi-api-key": key, "content-type": "application/json", "accept": "audio/mpeg"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            audio = resp.read()
    except urllib.error.HTTPError as exc:
        hint = {401: "the API key was rejected", 402: "this voice or plan is not available",
                422: "the request was not accepted (check the voice ID)", 429: "rate or credit limit reached"}
        return None, f"ElevenLabs error {exc.code}: {hint.get(exc.code, 'request failed')}."
    except Exception as exc:
        return None, f"Could not reach ElevenLabs ({type(exc).__name__})."
    if not audio:
        return None, "ElevenLabs returned no audio."
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        path.write_bytes(audio)
    except OSError:
        pass
    return audio, "generated"


STT_MODEL = "scribe_v2"          # override with ELEVENLABS_STT_MODEL_ID (docs list scribe_v1 and scribe_v2)
MAX_AUDIO_BYTES = 5_000_000      # about a minute of WAV; a question should be a few seconds


def _multipart(fields: dict, file_field: str, filename: str, content: bytes, mime: str) -> tuple[bytes, str]:
    boundary = "----chainshield" + hashlib.sha256(content[:64] + filename.encode()).hexdigest()[:16]
    out = b""
    for k, v in fields.items():
        out += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    out += (f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n").encode() + content + f"\r\n--{boundary}--\r\n".encode()
    return out, f"multipart/form-data; boundary={boundary}"


def transcribe(audio: bytes, filename: str = "question.wav", mime: str = "audio/wav",
               timeout: int = 30) -> tuple[str | None, str]:
    """Speech to text via ElevenLabs. Returns (text, message); text is None on any problem."""
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        return None, "Voice input needs ELEVENLABS_API_KEY."
    if not audio:
        return None, "No audio was recorded."
    if len(audio) > MAX_AUDIO_BYTES:
        return None, "That recording is too long. Please ask a shorter question."
    body, ctype = _multipart({"model_id": os.environ.get("ELEVENLABS_STT_MODEL_ID", STT_MODEL)},
                             "file", filename, audio, mime)
    req = urllib.request.Request(f"{BASE_URL}/v1/speech-to-text", data=body,
                                 headers={"xi-api-key": key, "content-type": ctype, "accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        hint = {401: "the API key was rejected", 402: "this feature is not available on the plan",
                422: "the audio or model was not accepted", 429: "rate or credit limit reached"}
        return None, f"ElevenLabs error {exc.code}: {hint.get(exc.code, 'request failed')}."
    except Exception as exc:
        return None, f"Could not reach ElevenLabs ({type(exc).__name__})."
    text = str(data.get("text", "")).strip() if isinstance(data, dict) else ""
    return (text, "ok") if text else (None, "I couldn't hear a question in that recording. Please try again or type it.")


def browser_voice_html(text: str) -> str:
    """Self-contained button using the browser's built-in speech (no key, no network)."""
    safe = json.dumps(text).replace("</", "<\\/")
    return (
        '<div style="font-family:sans-serif">'
        '<button id="b" style="background:#2dd4bf;color:#06151b;border:0;border-radius:10px;padding:9px 16px;'
        'font-weight:700;cursor:pointer">▶ Play browser voice</button> '
        '<button id="s" style="background:#1c2a40;color:#e8eef8;border:1px solid #3a526e;border-radius:10px;'
        'padding:9px 16px;font-weight:700;cursor:pointer">■ Stop</button></div>'
        f"<script>const t={safe};"
        "document.getElementById('b').onclick=()=>{speechSynthesis.cancel();speechSynthesis.speak(new SpeechSynthesisUtterance(t));};"
        "document.getElementById('s').onclick=()=>speechSynthesis.cancel();</script>"
    )
