"""Process-wide voice recording + TTS API for the TUI gateway.

Wraps ``tools.voice_mode`` (recording/transcription) and ``tools.tts_tool``
(text-to-speech) behind idempotent, stateful entry points that the gateway's
``voice.record``, ``voice.toggle``, and ``voice.tts`` JSON-RPC handlers can
call from a dedicated thread. The gateway imports this module lazily so that
missing optional audio deps (sounddevice, faster-whisper, numpy) surface as
an ``ImportError`` at call time, not at startup.

Two usage modes are exposed:

* **Push-to-talk** (``start_recording`` / ``stop_and_transcribe``) — single
  manually-bounded capture used when the caller drives the start/stop pair
  explicitly.
* **Continuous (VAD)** (``start_continuous`` / ``stop_continuous``) — mirrors
  the classic CLI voice mode: recording auto-stops on silence, transcribes,
  hands the result to a callback, and then auto-restarts for the next turn.
  Three consecutive no-speech cycles stop the loop and fire
  ``on_silent_limit`` so the UI can turn the mode off.
"""
from __future__ import annotations
import json
import logging
import os
import sys
import threading
from typing import Any, Callable, Optional
_VOICE_MOD_ALIASES = {'ctrl': 'c-', 'control': 'c-', 'alt': 'a-', 'option': 'a-', 'opt': 'a-'}
_VOICE_NAMED_KEYS = {'space': 'space', 'spc': 'space', 'enter': 'enter', 'return': 'enter', 'ret': 'enter', 'tab': 'tab', 'escape': 'escape', 'esc': 'escape', 'backspace': 'backspace', 'bs': 'backspace', 'delete': 'delete', 'del': 'delete'}
_VOICE_RESERVED_CTRL_CHARS = frozenset({'c', 'd', 'l'})
_VOICE_RESERVED_ALT_CHARS_MAC = frozenset({'c', 'd', 'l'})
_DEFAULT_PT_KEY = 'c-b'

def voice_record_key_from_config(cfg: Any) -> Any:
    """Shape-safe ``cfg.voice.record_key`` lookup.

    ``load_config()`` deep-merges raw YAML and preserves scalar
    overrides, so a hand-edited ``voice: true`` / ``voice: cmd+b``
    leaves ``cfg["voice"]`` as a bool/str instead of a dict, and the
    naive ``.get("voice", {}).get("record_key")`` chain raises
    AttributeError before voice can even start (Copilot round-11 on
    #19835). Return ``None`` for malformed shapes so call sites can
    feed the result straight into the normalizer/formatter and get
    the documented default.
    """
    if not isinstance(cfg, dict):
        return None
    voice = cfg.get('voice')
    if not isinstance(voice, dict):
        return None
    return voice.get('record_key')

def normalize_voice_record_key_for_prompt_toolkit(raw: Any) -> str:
    """Coerce ``voice.record_key`` into prompt_toolkit's ``c-x`` / ``a-x`` format.

    Mirrors the TUI parser contract (``ui-tui/src/lib/platform.ts``)
    so one config value binds the same shortcut in both runtimes:

    * non-string / empty / typo'd / bare-char / multi-modifier / reserved
      ``ctrl+c|d|l`` → documented default ``c-b``
    * single-char keys: ``ctrl+o`` → ``c-o``
    * named keys: ``ctrl+space`` → ``c-space`` (aliases collapse:
      ``ctrl+return`` → ``c-enter``)
    * ``super`` / ``win`` / ``windows`` → ``c-b`` (TUI-only modifiers —
      prompt_toolkit has no super mod; the CLI binding site is
      expected to warn when this fallback fires so users see the
      cross-runtime split, Copilot round-11 on #19835)
    """
    if not isinstance(raw, str):
        return _DEFAULT_PT_KEY
    lowered = raw.strip().lower()
    if not lowered:
        return _DEFAULT_PT_KEY
    parts = [p.strip() for p in lowered.split('+') if p.strip()]
    if not parts:
        return _DEFAULT_PT_KEY
    if len(parts) > 2:
        return _DEFAULT_PT_KEY
    if len(parts) == 1:
        return _DEFAULT_PT_KEY
    modifier_token, key_token = parts
    if modifier_token in {'super', 'win', 'windows'}:
        return _DEFAULT_PT_KEY
    normalized_mod = _VOICE_MOD_ALIASES.get(modifier_token)
    if not normalized_mod:
        return _DEFAULT_PT_KEY
    if len(key_token) == 1:
        if normalized_mod == 'c-' and key_token in _VOICE_RESERVED_CTRL_CHARS:
            return _DEFAULT_PT_KEY
        if normalized_mod == 'a-' and sys.platform == 'darwin' and (key_token in _VOICE_RESERVED_ALT_CHARS_MAC):
            return _DEFAULT_PT_KEY
        return f'{normalized_mod}{key_token}'
    named = _VOICE_NAMED_KEYS.get(key_token)
    if not named:
        return _DEFAULT_PT_KEY
    return f'{normalized_mod}{named}'

def format_voice_record_key_for_status(raw: Any) -> str:
    """Render ``voice.record_key`` for ``/voice status`` in CLI-friendly form.

    Mirrors the TUI's ``formatVoiceRecordKey``: returns ``Ctrl+B`` /
    ``Alt+Space`` / ``Ctrl+Enter``. Malformed configs surface as the
    documented default so status never advertises a shortcut that
    won't bind (Copilot round-10 on #19835).
    """
    normalized = normalize_voice_record_key_for_prompt_toolkit(raw)
    if normalized.startswith('c-'):
        prefix, key = ('Ctrl+', normalized[2:])
    elif normalized.startswith('a-'):
        prefix, key = ('Alt+', normalized[2:])
    elif '+' in normalized:
        mod, key = normalized.split('+', 1)
        prefix = mod[0].upper() + mod[1:] + '+'
    else:
        return 'Ctrl+B'
    if not key:
        return prefix.rstrip('+')
    if len(key) == 1:
        return prefix + key.upper()
    return prefix + key[0].upper() + key[1:]
from tools.voice_mode import create_audio_recorder, is_voice_stop_phrase, is_whisper_hallucination, play_audio_file, transcribe_recording
logger = logging.getLogger(__name__)

def _debug(msg: str) -> None:
    """Emit a debug breadcrumb when HERMES_VOICE_DEBUG=1.

    Goes to stderr so the TUI gateway wraps it as a gateway.stderr event,
    which createGatewayEventHandler shows as an Activity line — exactly
    what we need to diagnose "why didn't the loop auto-restart?" in the
    user's real terminal without shipping a separate debug RPC.

    Any OSError / BrokenPipeError is swallowed because this fires from
    background threads (silence callback, TTS daemon, beep) where a
    broken stderr pipe must not kill the whole gateway — the main
    command pipe (stdin+stdout) is what actually matters.
    """
    if os.environ.get('HERMES_VOICE_DEBUG', '').strip() != '1':
        return
    try:
        print(f'[voice] {msg}', file=sys.stderr, flush=True)
    except (BrokenPipeError, OSError):
        pass

def _beeps_enabled() -> bool:
    """CLI parity: voice.beep_enabled in config.yaml (default True)."""
    try:
        from hermes_cli.config import load_config
        from utils import is_truthy_value
        voice_cfg = load_config().get('voice', {})
        if isinstance(voice_cfg, dict):
            return is_truthy_value(voice_cfg.get('beep_enabled', True), default=True)
    except Exception:
        pass
    return True

def _play_beep(frequency: int, count: int=1) -> None:
    """Audible cue matching cli.py's record/stop beeps.

    880 Hz single-beep on start (cli.py:_voice_start_recording line 7532),
    660 Hz double-beep on stop (cli.py:_voice_stop_and_transcribe line 7585).
    Best-effort — sounddevice failures are silently swallowed so the
    voice loop never breaks because a speaker was unavailable.
    """
    if not _beeps_enabled():
        return
    try:
        from tools.voice_mode import play_beep
        play_beep(frequency=frequency, count=count)
    except Exception as e:
        _debug(f'beep {frequency}Hz failed: {e}')
_recorder = None
_recorder_lock = threading.Lock()
_continuous_lock = threading.Lock()
_continuous_active = False
_continuous_stopping = False
_continuous_auto_restart: bool = True
_continuous_recorder: Any = None
_tts_playing = threading.Event()
_tts_playing.set()
_voice_busy_probe: Optional[Callable[[], bool]] = None

def set_voice_busy_probe(probe: Optional[Callable[[], bool]]) -> None:
    """Register a callable that returns True while the agent is mid-turn.

    Called by the hosting surface (tui_gateway registers one that checks
    every session's ``running`` flag). ``None`` clears it. The probe must
    be cheap and thread-safe — it runs on the silence-callback thread.
    """
    global _voice_busy_probe
    _voice_busy_probe = probe

def _voice_activity_held() -> bool:
    """True while silent cycles must NOT count toward the no-speech limit.

    Held when TTS is playing (the user is listening) or when the
    registered busy probe reports the agent mid-turn (the user is
    waiting). Fail-open to "not held" so a broken probe can never make
    the voice chat immortal.
    """
    if not _tts_playing.is_set():
        return True
    probe = _voice_busy_probe
    if probe is None:
        return False
    try:
        return bool(probe())
    except Exception:
        return False
_continuous_on_transcript: Optional[Callable[[str], None]] = None
_continuous_on_status: Optional[Callable[[str], None]] = None
_continuous_on_silent_limit: Optional[Callable[[], None]] = None
_continuous_on_stop_phrase: Optional[Callable[[str], None]] = None
_continuous_no_speech_count = 0
_CONTINUOUS_NO_SPEECH_LIMIT = 3

def start_recording() -> None:
    """Begin capturing from the default input device (push-to-talk).

    Idempotent — calling again while a recording is in progress is a no-op.
    """
    global _recorder
    with _recorder_lock:
        if _recorder is not None and getattr(_recorder, 'is_recording', False):
            return
        rec = create_audio_recorder()
        rec.start()
        _recorder = rec

def stop_and_transcribe() -> Optional[str]:
    """Stop the active push-to-talk recording, transcribe, return text.

    Returns ``None`` when no recording is active, when the microphone
    captured no speech, or when Whisper returned a known hallucination.
    """
    global _recorder
    with _recorder_lock:
        rec = _recorder
        _recorder = None
    if rec is None:
        return None
    wav_path = rec.stop()
    if not wav_path:
        return None
    try:
        result = transcribe_recording(wav_path)
    except Exception as e:
        logger.warning('voice transcription failed: %s', e)
        return None
    finally:
        try:
            if os.path.isfile(wav_path):
                os.unlink(wav_path)
        except Exception:
            pass
    if not result.get('success'):
        return None
    text = (result.get('transcript') or '').strip()
    if not text or is_whisper_hallucination(text):
        return None
    return text

def start_continuous(on_transcript: Callable[[str], None], on_status: Optional[Callable[[str], None]]=None, on_silent_limit: Optional[Callable[[], None]]=None, silence_threshold: int=200, silence_duration: float=3.0, auto_restart: bool=True, max_recording_seconds: float=0.0, on_stop_phrase: Optional[Callable[[str], None]]=None) -> bool:
    """Start a VAD-driven continuous recording loop.

    The loop calls ``on_transcript(text)`` each time speech is detected and
    transcribed successfully. If ``auto_restart`` is True, it auto-restarts
    for the next turn and resets the no-speech counter for that loop. If
    ``auto_restart`` is False, the first silence-triggered transcription ends
    the loop and reports ``"idle"``; no-speech counts are retained across
    starts so a push-to-talk caller can still enforce the three-strikes guard.
    After ``_CONTINUOUS_NO_SPEECH_LIMIT`` consecutive silent cycles (no speech
    picked up at all) the loop stops itself and calls ``on_silent_limit`` so the
    UI can reflect "voice off". Returns False if a previous stop is still
    transcribing/cleaning up; otherwise returns True. Idempotent — calling while
    already active is a successful no-op.

    ``on_status`` is called with ``"listening"`` / ``"transcribing"`` /
    ``"idle"`` so the UI can show a live indicator.

    ``max_recording_seconds`` is the hard cap on a single recording's length
    (``voice.max_recording_seconds``); any non-positive or non-numeric value
    disables the cap, preserving the previous unbounded behaviour.

    ``on_stop_phrase`` is called with the (stripped) transcript when the user
    utters a bare voice stop phrase (``voice.stop_phrases``, default "stop").
    The loop halts first, so the consumer only needs to reflect "voice off" —
    exactly like the user pressing the manual stop control. When omitted,
    ``on_silent_limit`` fires instead so legacy callers still turn voice off.
    """
    global _continuous_active, _continuous_recorder, _continuous_auto_restart
    global _continuous_on_transcript, _continuous_on_status, _continuous_on_silent_limit
    global _continuous_on_stop_phrase
    global _continuous_no_speech_count
    with _continuous_lock:
        if _continuous_active:
            _debug('start_continuous: already active — no-op')
            return True
        if _continuous_stopping:
            _debug('start_continuous: stop/transcribe in progress — busy')
            return False
        _continuous_active = True
        _continuous_auto_restart = auto_restart
        _continuous_on_transcript = on_transcript
        _continuous_on_status = on_status
        _continuous_on_silent_limit = on_silent_limit
        _continuous_on_stop_phrase = on_stop_phrase
        if auto_restart:
            _continuous_no_speech_count = 0
        if _continuous_recorder is None:
            _continuous_recorder = create_audio_recorder()
        _continuous_recorder._silence_threshold = silence_threshold
        _continuous_recorder._silence_duration = silence_duration
        _continuous_recorder._max_recording_seconds = max_recording_seconds if isinstance(max_recording_seconds, (int, float)) and (not isinstance(max_recording_seconds, bool)) and (max_recording_seconds > 0) else 0.0
        rec = _continuous_recorder
    _debug(f'start_continuous: begin (threshold={silence_threshold}, duration={silence_duration}s)')
    _play_beep(frequency=880, count=1)
    try:
        rec.start(on_silence_stop=_continuous_on_silence)
    except Exception as e:
        logger.error('failed to start continuous recording: %s', e)
        _debug(f'start_continuous: rec.start raised {type(e).__name__}: {e}')
        with _continuous_lock:
            _continuous_active = False
        raise
    if on_status:
        try:
            on_status('listening')
        except Exception:
            pass
    return True

def stop_continuous(force_transcribe: bool=False) -> None:
    """Stop the active continuous loop and release the microphone.

    Idempotent — calling while not active is a no-op. If ``force_transcribe`` is
    True, the recorder stops synchronously, then transcription/cleanup runs on a
    background thread before reporting ``"idle"``. Otherwise the buffer is
    discarded.
    """
    global _continuous_active, _continuous_on_transcript, _continuous_stopping
    global _continuous_on_status, _continuous_on_silent_limit
    global _continuous_on_stop_phrase
    global _continuous_recorder, _continuous_no_speech_count
    with _continuous_lock:
        if not _continuous_active:
            return
        _continuous_active = False
        rec = _continuous_recorder
        on_status = _continuous_on_status
        on_transcript = _continuous_on_transcript
        on_silent_limit = _continuous_on_silent_limit
        on_stop_phrase = _continuous_on_stop_phrase
        auto_restart = _continuous_auto_restart
        track_no_speech = force_transcribe and (not auto_restart)
        _continuous_stopping = rec is not None
        _continuous_on_transcript = None
        _continuous_on_status = None
        _continuous_on_silent_limit = None
        _continuous_on_stop_phrase = None
        if not track_no_speech:
            _continuous_no_speech_count = 0
    if rec is not None:
        if force_transcribe and on_transcript:
            if on_status:
                try:
                    on_status('transcribing')
                except Exception:
                    pass
            try:
                wav_path = rec.stop()
            except Exception as e:
                logger.warning('failed to stop recorder: %s', e)
                try:
                    rec.cancel()
                except Exception as cancel_error:
                    logger.warning('failed to cancel recorder: %s', cancel_error)
                wav_path = None

            def _transcribe_and_cleanup():
                global _continuous_no_speech_count, _continuous_stopping
                transcript: Optional[str] = None
                should_halt = False
                try:
                    if wav_path:
                        try:
                            result = transcribe_recording(wav_path)
                            if result.get('success'):
                                text = (result.get('transcript') or '').strip()
                                if text and (not is_whisper_hallucination(text)):
                                    transcript = text
                        finally:
                            if os.path.isfile(wav_path):
                                os.unlink(wav_path)
                except Exception as e:
                    logger.warning('failed to stop/transcribe recorder: %s', e)
                finally:
                    stop_phrase = bool(transcript and is_voice_stop_phrase(transcript))
                    if stop_phrase:
                        _debug(f'stop_continuous: stop phrase {transcript!r} — ending voice chat')
                        stop_text = transcript or ''
                        transcript = None
                        try:
                            if on_stop_phrase is not None:
                                on_stop_phrase(stop_text)
                            elif on_silent_limit is not None:
                                on_silent_limit()
                        except Exception:
                            pass
                    if transcript:
                        try:
                            on_transcript(transcript)
                        except Exception as e:
                            logger.warning('on_transcript callback raised: %s', e)
                    if track_no_speech:
                        held = _voice_activity_held()
                        with _continuous_lock:
                            if transcript or stop_phrase:
                                _continuous_no_speech_count = 0
                            elif held:
                                _debug('stop_continuous: silent cycle ignored (agent busy or TTS playing)')
                            else:
                                _continuous_no_speech_count += 1
                                should_halt = _continuous_no_speech_count >= _CONTINUOUS_NO_SPEECH_LIMIT
                                if should_halt:
                                    _continuous_no_speech_count = 0
                        if should_halt and on_silent_limit:
                            try:
                                on_silent_limit()
                            except Exception:
                                pass
                    _play_beep(frequency=660, count=2)
                    with _continuous_lock:
                        _continuous_stopping = False
                    if on_status:
                        try:
                            on_status('idle')
                        except Exception:
                            pass
            threading.Thread(target=_transcribe_and_cleanup, daemon=True).start()
            return
        else:
            try:
                rec.cancel()
            except Exception as e:
                logger.warning('failed to cancel recorder: %s', e)
    with _continuous_lock:
        _continuous_stopping = False
    _play_beep(frequency=660, count=2)
    if on_status:
        try:
            on_status('idle')
        except Exception:
            pass

def is_continuous_active() -> bool:
    """Whether a continuous voice loop is currently running."""
    with _continuous_lock:
        return _continuous_active

def _continuous_on_silence() -> None:
    """AudioRecorder silence callback — runs in a daemon thread.

    Stops the current capture, transcribes, delivers the text via
    ``on_transcript``, and — if the loop is still active — starts the
    next capture. Three consecutive silent cycles end the loop.
    """
    global _continuous_active, _continuous_no_speech_count
    _debug('_continuous_on_silence: fired')
    with _continuous_lock:
        if not _continuous_active:
            _debug('_continuous_on_silence: loop inactive — abort')
            return
        rec = _continuous_recorder
        on_transcript = _continuous_on_transcript
        on_status = _continuous_on_status
        on_silent_limit = _continuous_on_silent_limit
        on_stop_phrase = _continuous_on_stop_phrase
    if rec is None:
        _debug('_continuous_on_silence: no recorder — abort')
        return
    if on_status:
        try:
            on_status('transcribing')
        except Exception:
            pass
    wav_path = rec.stop()
    peak_rms = getattr(rec, '_peak_rms', -1)
    _debug(f'_continuous_on_silence: rec.stop -> {wav_path!r} (peak_rms={peak_rms})')
    _play_beep(frequency=660, count=2)
    transcript: Optional[str] = None
    if wav_path:
        try:
            result = transcribe_recording(wav_path)
            success = bool(result.get('success'))
            text = (result.get('transcript') or '').strip()
            err = result.get('error')
            _debug(f'_continuous_on_silence: transcribe -> success={success} text={text!r} err={err!r}')
            if success and text and (not is_whisper_hallucination(text)):
                transcript = text
        except Exception as e:
            logger.warning('continuous transcription failed: %s', e)
            _debug(f'_continuous_on_silence: transcribe raised {type(e).__name__}: {e}')
        finally:
            try:
                if os.path.isfile(wav_path):
                    os.unlink(wav_path)
            except Exception:
                pass
    stop_phrase = bool(transcript and is_voice_stop_phrase(transcript))
    stop_text = transcript or '' if stop_phrase else ''
    if stop_phrase:
        _debug(f'_continuous_on_silence: stop phrase {transcript!r} — ending loop')
        transcript = None
    _silence_held = transcript is None and (not stop_phrase) and _voice_activity_held()
    with _continuous_lock:
        if not _continuous_active:
            _debug('_continuous_on_silence: stopped during transcribe — no restart')
            return
        if transcript:
            _continuous_no_speech_count = 0
        elif _silence_held:
            _debug('_continuous_on_silence: silent cycle ignored (agent busy or TTS playing)')
        elif not stop_phrase:
            _continuous_no_speech_count += 1
        should_halt = stop_phrase or _continuous_no_speech_count >= _CONTINUOUS_NO_SPEECH_LIMIT
        no_speech = _continuous_no_speech_count
    if transcript and on_transcript:
        try:
            on_transcript(transcript)
        except Exception as e:
            logger.warning('on_transcript callback raised: %s', e)
    if should_halt:
        _debug(f"_continuous_on_silence: halting ({('stop phrase' if stop_phrase else f'{no_speech} silent cycles')})")
        with _continuous_lock:
            _continuous_active = False
            _continuous_no_speech_count = 0
        if stop_phrase and on_stop_phrase is not None:
            try:
                on_stop_phrase(stop_text)
            except Exception:
                pass
        elif on_silent_limit:
            try:
                on_silent_limit()
            except Exception:
                pass
        try:
            rec.cancel()
        except Exception:
            pass
        if on_status:
            try:
                on_status('idle')
            except Exception:
                pass
        return
    if not _tts_playing.is_set():
        _debug('_continuous_on_silence: waiting for TTS to finish')
        _tts_playing.wait(timeout=60)
        import time as _time
        _time.sleep(0.3)
        with _continuous_lock:
            if not _continuous_active:
                _debug('_continuous_on_silence: stopped while waiting for TTS')
                return
    if _continuous_auto_restart:
        _debug(f'_continuous_on_silence: restarting loop (no_speech={no_speech})')
        _play_beep(frequency=880, count=1)
        try:
            rec.start(on_silence_stop=_continuous_on_silence)
        except Exception as e:
            logger.error('failed to restart continuous recording: %s', e)
            _debug(f'_continuous_on_silence: restart raised {type(e).__name__}: {e}')
            with _continuous_lock:
                _continuous_active = False
            if on_status:
                try:
                    on_status('idle')
                except Exception:
                    pass
            return
        if on_status:
            try:
                on_status('listening')
            except Exception:
                pass
    else:
        _debug('_continuous_on_silence: auto_restart=False, stopping loop')
        with _continuous_lock:
            _continuous_active = False
        if on_status:
            try:
                on_status('idle')
            except Exception:
                pass

def _speak_text_streaming(text: str, stop_event: Optional[threading.Event]=None) -> bool:
    """Speak ``text`` via the generic streaming dispatcher; True on success.

    Bridges the one-shot ``speak_text`` contract onto the shared
    ``stream_tts_to_speaker`` pipeline (tools.tts_tool): the full reply is
    fed as a single delta + end-of-text sentinel, and we block until the
    pipeline's done event fires — same blocking semantics the sync path
    has, so callers (and the mic re-arm logic in ``speak_text``) see no
    behavioral difference beyond earlier first audio.

    ``stop_event`` (optional) is wired straight into the pipeline so
    external barge-in / stop paths can cut streaming playback — without
    it the pipeline's stop event was private and speech over this path
    was uninterruptible (the desktop/TUI fallback-speak hole).

    Returns False when playback produced nothing (caller falls back to the
    whole-file sync path).
    """
    import queue as _queue
    import threading as _threading
    from tools.tts_tool import stream_tts_to_speaker
    text_queue: '_queue.Queue' = _queue.Queue()
    text_queue.put(text)
    text_queue.put(None)
    if stop_event is None:
        stop_event = _threading.Event()
    done_event = _threading.Event()
    stream_tts_to_speaker(text_queue, stop_event, done_event)
    return done_event.is_set()

def speak_text(text: str, stop_event: Optional[threading.Event]=None) -> None:
    """Synthesize ``text`` with the configured TTS provider and play it.

    Mirrors cli.py:_voice_speak_response exactly — same markdown strip
    pipeline, same 4000-char cap, same explicit mp3 output path, same
    MP3-over-OGG playback choice (afplay misbehaves on OGG), same cleanup
    of both extensions. Keeping these in sync means a voice-mode TTS
    session in the TUI sounds identical to one in the classic CLI.

    While playback is in flight the module-level _tts_playing Event is
    cleared so the continuous-recording loop knows to wait before
    re-arming the mic (otherwise the agent's spoken reply feedback-loops
    through the microphone and the agent ends up replying to itself).
    """
    if not text or not text.strip():
        return
    import re
    import tempfile
    import time
    paused_recording = False
    with _continuous_lock:
        if _continuous_active and _continuous_recorder is not None and getattr(_continuous_recorder, 'is_recording', False):
            try:
                _continuous_recorder.cancel()
                paused_recording = True
            except Exception as e:
                logger.warning('failed to pause recorder for TTS: %s', e)
    _tts_playing.clear()
    _debug(f'speak_text: TTS begin (paused_recording={paused_recording})')
    try:
        from tools.tts_tool import text_to_speech_tool
        try:
            from tools.tts_streaming import resolve_streaming_provider
            from tools.tts_tool import _load_tts_config
            if resolve_streaming_provider(_load_tts_config()) is not None:
                if _speak_text_streaming(text, stop_event):
                    return
        except Exception as e:
            _debug(f'speak_text: streaming dispatch unavailable ({e}); using sync path')
        try:
            from tools.tts_text_normalize import prepare_spoken_text
            tts_text = prepare_spoken_text(text, max_chars=4000)
        except Exception:
            tts_text = text[:4000] if len(text) > 4000 else text
            tts_text = re.sub('```[\\s\\S]*?```', ' ', tts_text)
            tts_text = re.sub('\\[([^\\]]+)\\]\\([^)]+\\)', '\\1', tts_text)
            tts_text = re.sub('https?://\\S+', '', tts_text)
            tts_text = re.sub('\\*\\*(.+?)\\*\\*', '\\1', tts_text)
            tts_text = re.sub('\\*(.+?)\\*', '\\1', tts_text)
            tts_text = re.sub('`(.+?)`', '\\1', tts_text)
            tts_text = re.sub('^#+\\s*', '', tts_text, flags=re.MULTILINE)
            tts_text = re.sub('^\\s*[-*]\\s+', '', tts_text, flags=re.MULTILINE)
            tts_text = re.sub('---+', '', tts_text)
            tts_text = re.sub('\\n{3,}', '\n\n', tts_text)
            tts_text = tts_text.strip()
        if not tts_text:
            return
        os.makedirs(os.path.join(tempfile.gettempdir(), 'hermes_voice'), exist_ok=True)
        mp3_path = os.path.join(tempfile.gettempdir(), 'hermes_voice', f"tts_{time.strftime('%Y%m%d_%H%M%S')}.mp3")
        _debug(f'speak_text: synthesizing {len(tts_text)} chars -> {mp3_path}')
        raw_result = text_to_speech_tool(text=tts_text, output_path=mp3_path)
        try:
            tts_result = json.loads(raw_result) if isinstance(raw_result, str) else {}
        except Exception:
            tts_result = {}
        audio_path = mp3_path
        if not os.path.isfile(mp3_path) or os.path.getsize(mp3_path) == 0:
            audio_path = tts_result.get('file_path') or mp3_path
        if os.path.isfile(audio_path) and os.path.getsize(audio_path) > 0:
            _debug(f'speak_text: playing {audio_path} ({os.path.getsize(audio_path)} bytes)')
            play_audio_file(audio_path)
            try:
                cleanup_paths = {audio_path, mp3_path}
                for path in list(cleanup_paths):
                    ogg_path = path.rsplit('.', 1)[0] + '.ogg'
                    cleanup_paths.add(ogg_path)
                for path in cleanup_paths:
                    if os.path.isfile(path):
                        os.unlink(path)
            except OSError:
                pass
        else:
            _debug(f'speak_text: TTS tool produced no audio at {audio_path}')
    except Exception as e:
        logger.warning('Voice TTS playback failed: %s', e)
        _debug(f'speak_text raised {type(e).__name__}: {e}')
    finally:
        _tts_playing.set()
        _debug('speak_text: TTS done')
        if paused_recording:
            time.sleep(0.3)
            with _continuous_lock:
                if _continuous_active and _continuous_recorder is not None:
                    try:
                        _continuous_recorder.start(on_silence_stop=_continuous_on_silence)
                        _debug('speak_text: recording resumed after TTS')
                    except Exception as e:
                        logger.warning('failed to resume recorder after TTS: %s', e)