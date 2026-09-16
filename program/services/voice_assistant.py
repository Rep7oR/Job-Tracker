"""Server-side always-on voice assistant ("Hey JobSync") for the local desktop app.

Runs a background thread that listens on the machine's microphone for a wake
word, then captures a follow-up command, executes it (job search or page
navigation), and publishes results into a thread-safe shared state that the
Streamlit UI polls and renders in a floating panel.

Only works when the app is run locally (there is no way to access a user's
microphone from a server-hosted Streamlit session).
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field

WAKE_WORDS = ("hey jobsync", "hey sync", "hello sync", "hey job sync")
# Speech-to-text engines routinely mangle the made-up word "JobSync" into
# near-homophones ("job sink", "job think", "jobsink", "jobsing", ...), so the
# wake check below also accepts a wake greeting followed by any of these.
WAKE_SOUND_ALIKES = ("sync", "sink", "think", "singh", "sing", "sinc")
WAKE_GREETINGS = ("hey", "hello", "hi")
STOP_WORDS = ("stop", "cancel", "never mind", "nevermind")

NAV_ALIASES = {
    "home": "Home",
    "dashboard": "Dashboard",
    "new search": "New Search",
    "search page": "New Search",
    "applied jobs": "Applied Jobs",
    "applied": "Applied Jobs",
    "updates": "Updates",
    "cv": "CV & Cover Letter",
    "cover letter": "CV & Cover Letter",
    "folders": "Folders",
    "profile": "Profile",
    "settings": "Settings",
}


@dataclass
class VoiceState:
    listening: bool = True
    awake: bool = False
    status: str = "Idle — say “Hey JobSync” to start"
    transcript: str = ""
    last_command: str = ""
    results: list[dict] = field(default_factory=list)
    navigate_to: str | None = None
    error: str | None = None
    busy: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def update(self, **kwargs) -> None:
        with self._lock:
            for key, value in kwargs.items():
                setattr(self, key, value)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "listening": self.listening,
                "awake": self.awake,
                "status": self.status,
                "transcript": self.transcript,
                "last_command": self.last_command,
                "results": list(self.results),
                "navigate_to": self.navigate_to,
                "error": self.error,
                "busy": self.busy,
            }

    def consume_navigation(self) -> str | None:
        with self._lock:
            target = self.navigate_to
            self.navigate_to = None
            return target


_STATE = VoiceState()
_THREAD: threading.Thread | None = None
_STOP_EVENT = threading.Event()
_ABORT_CURRENT = threading.Event()

_TTS_ENGINE = None
_TTS_LOCK = threading.Lock()


def get_state() -> VoiceState:
    return _STATE


def _speak(text: str) -> None:
    """Speak a line out loud (offline, via the OS voice) — best-effort only."""
    if not text:
        return
    global _TTS_ENGINE
    try:
        import pyttsx3
    except ImportError:
        return
    with _TTS_LOCK:
        try:
            if _TTS_ENGINE is None:
                _TTS_ENGINE = pyttsx3.init()
                _TTS_ENGINE.setProperty("rate", 178)
            if _ABORT_CURRENT.is_set():
                return
            _TTS_ENGINE.say(text)
            _TTS_ENGINE.runAndWait()
        except Exception:  # noqa: BLE001
            pass


def _contains_wake_word(text: str) -> bool:
    text = text.lower()
    if any(w in text for w in WAKE_WORDS):
        return True
    words = re.findall(r"[a-z']+", text)
    for i, word in enumerate(words[:-1]):
        if word in WAKE_GREETINGS and any(sound in words[i + 1] for sound in WAKE_SOUND_ALIKES):
            return True
    return False


def _strip_wake_word(text: str) -> str:
    """Remove the wake greeting + sound-alike from the start of an utterance."""
    for w in WAKE_WORDS:
        text = text.replace(w, "")
    words = re.findall(r"[a-z']+", text)
    for i, word in enumerate(words[:-1]):
        if word in WAKE_GREETINGS and any(sound in words[i + 1] for sound in WAKE_SOUND_ALIKES):
            remainder = " ".join(words[i + 2:])
            return remainder.strip(" ,.")
    return text.strip(" ,.")


def _contains_stop_word(text: str) -> bool:
    text = text.lower().strip()
    return any(text == w or text.startswith(w + " ") for w in STOP_WORDS)


def _parse_job_search(command: str) -> dict | None:
    """Very small heuristic parser for "search for <role> in <location>"."""
    m = re.search(r"(?:search|look|find)[^a-z]*(?:for|new)?\s*(.*?)\s+(?:jobs?|openings?|positions?)?\s*in\s+(.+)", command, re.I)
    if m:
        field_ = m.group(1).strip(" .") or "jobs"
        location = m.group(2).strip(" .")
        return {"field": field_, "location": location}
    m = re.search(r"(?:search|find)\s+(.+)", command, re.I)
    if m:
        return {"field": m.group(1).strip(" ."), "location": ""}
    return None


def _parse_navigation(command: str) -> str | None:
    command = command.lower()
    m = re.search(r"(?:open|go to|show|navigate to)\s+(?:the\s+)?(.+)", command)
    target_text = m.group(1).strip(" .") if m else command.strip(" .")
    for alias, page in NAV_ALIASES.items():
        if alias in target_text:
            return page
    return None


def _run_job_search(parsed: dict) -> list[dict]:
    from services.jobs import search_jobs

    jobs = search_jobs(
        field=parsed.get("field") or "",
        location=parsed.get("location") or "",
        industry="",
        experience="",
        limit=15,
        search_mode="free",
    )
    return jobs[:15]


def _handle_command(command: str) -> None:
    if not command:
        return
    if _contains_stop_word(command):
        _ABORT_CURRENT.set()
        _STATE.update(status="Stopped. Say “Hey JobSync” to start again.", awake=False, busy=False)
        _speak("Stopped.")
        return

    _STATE.update(busy=True, last_command=command, status=f"Working on: “{command}”", error=None)
    _ABORT_CURRENT.clear()
    try:
        nav_target = _parse_navigation(command)
        search_parsed = _parse_job_search(command)
        if search_parsed and ("job" in command.lower() or "opening" in command.lower() or "position" in command.lower() or "search" in command.lower()):
            field_label = search_parsed["field"] or "jobs"
            location_label = search_parsed["location"] or "anywhere"
            _STATE.update(status=f"Searching for {field_label} in {location_label}…")
            _speak(f"Searching for {field_label} jobs in {location_label}. One moment.")
            jobs = _run_job_search(search_parsed)
            if _ABORT_CURRENT.is_set():
                _STATE.update(status="Stopped.", busy=False)
                return
            _STATE.update(
                results=jobs,
                status=f"Found {len(jobs)} result(s) for {field_label} in {location_label}.",
                busy=False,
            )
            if jobs:
                top = jobs[0]
                _speak(
                    f"Found {len(jobs)} openings for {field_label} in {location_label}. "
                    f"Top result: {top.get('title') or 'a role'} at {top.get('company') or 'an unnamed company'}. "
                    "The full list is in your assistant panel."
                )
            else:
                _speak(f"I didn't find any openings for {field_label} in {location_label}.")
        elif nav_target:
            _STATE.update(status=f"Opening {nav_target}…", navigate_to=nav_target, busy=False)
            _speak(f"Opening {nav_target}.")
        else:
            _STATE.update(status="Sorry, I didn't catch a job search or page to open. Try again after the wake word.", busy=False)
            _speak("Sorry, I didn't catch that. Try again after the wake word.")
    except Exception as exc:  # noqa: BLE001
        _STATE.update(error=str(exc), status="Something went wrong with that request.", busy=False)
        _speak("Something went wrong with that request.")
    finally:
        _STATE.update(awake=False)


def _listen_loop() -> None:
    try:
        import speech_recognition as sr
    except ImportError:
        _STATE.update(
            listening=False,
            status="Voice assistant unavailable: install the 'SpeechRecognition' and 'PyAudio' packages to enable it.",
            error="missing-dependency",
        )
        return

    recognizer = sr.Recognizer()
    recognizer.dynamic_energy_threshold = True
    # A built-in laptop mic is noisy and the wake phrase is two full words, so
    # give the recognizer more room before it decides speech has ended —
    # the defaults (pause_threshold=0.8) tend to cut the phrase mid-word and
    # produce garbage transcriptions that never contain the wake word.
    recognizer.pause_threshold = 1.0
    recognizer.non_speaking_duration = 0.5
    try:
        mic = sr.Microphone()
    except OSError as exc:
        _STATE.update(listening=False, status="No microphone detected.", error=str(exc))
        return

    # Keep one persistent mic stream open for the whole loop instead of
    # reopening it every iteration — reopening has a brief startup lag during
    # which the first syllable of the next phrase is dropped, which is what
    # was turning "Hey JobSync" into unrelated noise like "call 9676".
    with mic as source:
        recognizer.adjust_for_ambient_noise(source, duration=2)
        _STATE.update(status="Listening for “Hey JobSync”…")
        while not _STOP_EVENT.is_set():
            try:
                audio = recognizer.listen(source, timeout=5, phrase_time_limit=6)
            except sr.WaitTimeoutError:
                continue
            except Exception as exc:  # noqa: BLE001
                _STATE.update(error=str(exc))
                time.sleep(1)
                continue
            _process_audio(recognizer, sr, audio)


def _process_audio(recognizer, sr, audio) -> None:
    try:
        text = recognizer.recognize_google(audio)
    except sr.UnknownValueError:
        return
    except sr.RequestError as exc:
        _STATE.update(error=f"Speech recognition service error: {exc}")
        time.sleep(2)
        return

    _STATE.update(transcript=text)

    if not _STATE.snapshot()["awake"]:
        if _contains_wake_word(text):
            remainder = _strip_wake_word(text.lower())
            _STATE.update(awake=True, status="I'm listening…")
            _speak("Yes?" if not remainder else "Yes, right away.")
            if remainder:
                _handle_command(remainder)
    else:
        _handle_command(text)


def ensure_started() -> None:
    """Start the background listener thread once per process."""
    global _THREAD
    if _THREAD is not None and _THREAD.is_alive():
        return
    _STOP_EVENT.clear()
    _THREAD = threading.Thread(target=_listen_loop, name="jobsync-voice-assistant", daemon=True)
    _THREAD.start()


def stop_assistant() -> None:
    _STOP_EVENT.set()
    _ABORT_CURRENT.set()
    _STATE.update(listening=False, awake=False, busy=False, status="Voice assistant stopped.")
