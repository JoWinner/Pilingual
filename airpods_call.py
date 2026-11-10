"""Continuous English/Mandarin WhatsApp dubbing with two isolated virtual cables."""

import argparse
import ctypes
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass
from queue import Empty, Full, Queue
from threading import Event, Lock
from time import perf_counter
from uuid import uuid4

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel

from airpods_bench import find_endpoint
from airpods_translate import (
    INPUT_RATE, load_denoiser, load_translation, load_voice, make_vad,
    open_input, prepare, prepare_recognizer, prepare_vad, read_audio, recognize,
    synthesize, translate_text,
)

LANGUAGES = {'en': 'English', 'zh': 'Mandarin'}


def ignore_event(kind, **details):
    pass


@dataclass(frozen=True)
class Routes:
    microphone: int
    to_caller: int
    from_caller: int
    headphones: int


def select_routes():
    mic, _ = find_endpoint('input', 'AirPods')
    phones, _ = find_endpoint('output', 'AirPods')
    send, _ = find_endpoint('output', 'CABLE Input')
    find_endpoint('input', 'CABLE Output')
    try:
        receive, capture = find_endpoint('input', 'Hi-Fi Cable Output')
        _, render = find_endpoint('output', 'Hi-Fi Cable Input')
    except RuntimeError as error:
        raise RuntimeError(
            'Two-way mode needs the free VB-Audio Hi-Fi Cable as a second audio path. '
            'Install it with Install-Second-Cable.ps1, reboot if requested, then retry. '
            'The original VB-CABLE remains the outgoing path.'
        ) from error
    if capture['default_samplerate'] != render['default_samplerate']:
        raise RuntimeError(
            'Hi-Fi Cable input and output must have the same Windows sample rate. '
            'Set both to 48000 Hz in Sound > More sound settings > '
            'Playback/Recording > Hi-Fi Cable > Properties > Advanced.'
        )
    if len({mic, phones, send, receive}) != 4:
        raise RuntimeError('Audio routes overlap. Refusing to start a feedback loop.')
    return Routes(mic, send, receive, phones)


@contextmanager
def single_session():
    """Prevent a second translator process from mixing into the active call."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_bool
    handle = kernel.CreateMutexW(None, False, 'Local\\PiLingualTranslator')
    error = ctypes.get_last_error()
    if not handle:
        raise ctypes.WinError(error)
    try:
        if error == 183:
            raise RuntimeError('Another translator is running. Stop it before starting another.')
        yield
    finally:
        kernel.CloseHandle(handle)


class PlaybackGuard:
    """Mute local capture during headphone playback and its acoustic tail."""
    def __init__(self):
        self.lock = Lock()
        self.playing = False
        self.until = 0.0

    def blocked(self):
        with self.lock:
            return self.playing or perf_counter() < self.until

    @contextmanager
    def hold(self):
        with self.lock:
            self.playing = True
        try:
            yield
        finally:
            with self.lock:
                self.until = perf_counter() + 0.35
                self.playing = False


class SpeechCapture:
    def __init__(self, device, seconds, pause, label, stopped, guard=None, *, emit=ignore_event):
        self.device = device
        self.seconds = seconds
        self.label = label
        self.stopped = stopped
        self.guard = guard
        self.emit = emit
        self.ready = Event()
        self.pending = Queue(maxsize=3)
        self.vad, self.window = make_vad(seconds, pause)
        self.active_frames = 0

    def feed(self, samples, *, suppressed=False):
        if suppressed:
            self.vad.reset()
            self.active_frames = 0
            return
        self.vad.accept_waveform(samples)
        self.active_frames = (self.active_frames + len(samples)
                              if self.vad.is_speech_detected() else 0)
        if self.active_frames >= INPUT_RATE * self.seconds:
            self.vad.flush()
            self.active_frames = 0
        while not self.vad.empty():
            phrase = np.array(self.vad.front.samples, dtype=np.float32, copy=True)
            self.vad.pop()
            entry = phrase, perf_counter()
            try:
                self.pending.put_nowait(entry)
            except Full:
                try:
                    self.pending.get_nowait()
                except Empty:
                    pass
                else:
                    print(f'[{self.label}] Falling behind; skipped oldest waiting phrase.', flush=True)
                    self.emit('warning', route=self.label, message='Skipped oldest waiting phrase.')
                self.pending.put_nowait(entry)

    def run(self):
        settings = sd.WasapiSettings(auto_convert=True)
        with open_input(self.device, settings) as stream:
            self.ready.set()
            next_meter = 0
            while not self.stopped.is_set():
                blocked = self.guard is not None and self.guard.blocked()
                samples = read_audio(stream, self.window)
                blocked = blocked or (self.guard is not None and self.guard.blocked())
                self.feed(samples, suppressed=blocked)
                if self.emit is not ignore_event and perf_counter() >= next_meter:
                    level = 0.0 if blocked else float(np.sqrt(np.mean(samples ** 2)))
                    self.emit('level', route=self.label, level=level, suppressed=blocked)
                    next_meter = perf_counter() + 0.15


def play(audio, rate, device, stopped, guard=None, *, language='en'):
    """Keep cancellation responsive; keep guard held until Windows drains output."""
    settings = sd.WasapiSettings(auto_convert=True)
    with guard.hold() if guard else nullcontext():
        if guard:
            print(f'[You hear] Playing {LANGUAGES[language]}; microphone paused to prevent echo.',
                  flush=True)
        try:
            with sd.RawOutputStream(device=device, samplerate=rate, channels=1,
                                    dtype='int16', extra_settings=settings) as stream:
                lead_ms = 350 if guard else 100
                stream.write(bytes(rate * 2 * lead_ms // 1000))
                started = perf_counter()
                chunk = rate * 2 // 10
                for offset in range(0, len(audio), chunk):
                    if stopped.is_set():
                        stream.abort()
                        return started
                    stream.write(audio[offset:offset + chunk])
                stream.write(bytes(rate * 2 * 150 // 1000))
            return started
        finally:
            if guard:
                print('[You hear] Playback ended; microphone resumes after echo tail.', flush=True)


def dub(capture, recognizer, translation, voice, source, destination, stopped,
        guard=None, denoiser=None, *, emit=ignore_event):
    target = 'zh' if source == 'en' else 'en'
    while not stopped.is_set():
        try:
            samples, captured_at = capture.pending.get(timeout=0.2)
        except Empty:
            continue
        began = perf_counter()
        if began - captured_at > 15:
            print(f'[{capture.label}] Skipped stale phrase (more than 15s waiting).', flush=True)
            emit('warning', route=capture.label, message='Skipped stale phrase (over 15 s waiting).')
            continue
        if denoiser:
            samples = np.asarray(denoiser(samples, INPUT_RATE).samples, dtype=np.float32)
        spoken = recognize(samples, recognizer, source)
        recognized_at = perf_counter()
        if not spoken or stopped.is_set():
            continue
        phrase = uuid4().hex
        translated = translate_text(spoken, *translation)
        translated_at = perf_counter()
        emit('phrase', phrase=phrase, route=capture.label, spoken=spoken,
             translation=translated, source=source, target=target)
        if not translated:
            print(f'[{capture.label}] Empty translation; skipping.', flush=True)
            emit('warning', phrase=phrase, route=capture.label, message='Translation was empty.')
            continue
        print(f'[{capture.label}] {spoken}\n[{capture.label}] Dub: {translated}', flush=True)
        audio, rate = synthesize(translated, voice)
        synthesized_at = perf_counter()
        if not audio:
            raise RuntimeError(f'{capture.label}: voice model returned no audio.')
        if stopped.is_set():
            return
        playback_options = {'language': target} if guard and target != 'en' else {}
        queued_at = play(audio, rate, destination, stopped, guard, **playback_options)
        if not stopped.is_set():
            emit('playback', phrase=phrase, route=capture.label,
                 delay=round(queued_at - captured_at, 3), queue=round(began - captured_at, 3),
                 recognition=round(recognized_at - began, 3),
                 translation_time=round(translated_at - recognized_at, 3),
                 voice=round(synthesized_at - translated_at, 3))
        print(f'[{capture.label}] Audio queued {queued_at - captured_at:.2f}s after capture '
              f'(queue {began - captured_at:.2f}s, recognition/cleanup '
              f'{recognized_at - began:.2f}s, translation '
              f'{translated_at - recognized_at:.2f}s, voice '
              f'{synthesized_at - translated_at:.2f}s).', flush=True)


def run_call(routes, seconds=20, pause=0.9, denoise=False, *,
             language='en', model='base', stopped=None, emit=ignore_event):
    print('Loading both offline directions...', flush=True)
    stopped = stopped if stopped is not None else Event()
    other = 'zh' if language == 'en' else 'en'
    outgoing = prepare(other, prefer_cached=True, whisper_model=model)
    if stopped.is_set():
        return
    incoming = prepare(language, prefer_cached=True, whisper_model=model)
    if stopped.is_set():
        return
    recognizer = WhisperModel(str(outgoing[0]), device='cpu', compute_type='int8',
                              cpu_threads=2, num_workers=2)
    outgoing_translation = load_translation(outgoing[1])
    incoming_translation = load_translation(incoming[1])
    outgoing_voice = load_voice(outgoing[2])
    incoming_voice = load_voice(incoming[2])
    denoiser = load_denoiser() if denoise else None
    guard = PlaybackGuard()
    local = SpeechCapture(routes.microphone, seconds, pause, 'To caller', stopped, guard, emit=emit)
    remote = SpeechCapture(routes.from_caller, seconds, pause, 'You hear', stopped, emit=emit)
    # Each direction has independent capture, translation state, and playback.
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [pool.submit(local.run), pool.submit(remote.run)]
        try:
            deadline = perf_counter() + 10
            while not (local.ready.is_set() and remote.ready.is_set()):
                if stopped.is_set():
                    return
                for job in jobs:
                    if job.done():
                        job.result()
                        raise RuntimeError('Audio capture stopped before startup.')
                if perf_counter() >= deadline:
                    raise RuntimeError('Timed out opening audio inputs.')
                stopped.wait(0.05)
            if stopped.is_set():
                return
            jobs.extend([
                pool.submit(dub, local, recognizer, outgoing_translation, outgoing_voice,
                            language, routes.to_caller, stopped, denoiser=denoiser, emit=emit),
                pool.submit(dub, remote, recognizer, incoming_translation, incoming_voice,
                            other, routes.headphones, stopped, guard=guard, emit=emit),
            ])
            print('READY. Both directions are listening. Speak naturally and pause.\n'
                  'No Enter key or device switching. Ctrl+C stops both directions.\n'
                  f'Wait for the {LANGUAGES[language]} playback to finish before replying.',
                  flush=True)
            emit('ready', message='Both directions are listening.')
            while not stopped.wait(0.2):
                for job in jobs:
                    if job.done():
                        job.result()
                        raise RuntimeError('A dubbing worker stopped unexpectedly.')
        finally:
            stopped.set()


def run_transcription(device, *, language, model, seconds, pause, denoise, stopped, emit):
    path = prepare_recognizer(model)
    if stopped.is_set():
        return
    prepare_vad()
    recognizer = WhisperModel(str(path), device='cpu', compute_type='int8', cpu_threads=2)
    denoiser = load_denoiser() if denoise else None
    capture = SpeechCapture(device, seconds, pause, 'Microphone', stopped, emit=emit)
    with ThreadPoolExecutor(max_workers=1) as pool:
        job = pool.submit(capture.run)
        try:
            deadline = perf_counter() + 10
            while not capture.ready.wait(0.05):
                if stopped.is_set():
                    return
                if job.done():
                    job.result()
                if perf_counter() >= deadline:
                    raise RuntimeError('Timed out opening the microphone.')
            if stopped.is_set():
                return
            emit('ready', message='Listening to the AirPods microphone.')
            while not stopped.is_set():
                if job.done():
                    job.result()
                    raise RuntimeError('Microphone capture stopped unexpectedly.')
                try:
                    samples, captured_at = capture.pending.get(timeout=0.2)
                except Empty:
                    continue
                if perf_counter() - captured_at > 15:
                    emit('warning', route=capture.label, message='Skipped stale phrase (over 15 s waiting).')
                    continue
                if denoiser:
                    samples = np.asarray(denoiser(samples, INPUT_RATE).samples, dtype=np.float32)
                spoken = recognize(samples, recognizer, language)
                if spoken and not stopped.is_set():
                    emit('phrase', phrase=uuid4().hex, route=capture.label, spoken=spoken,
                         source=language)
        finally:
            stopped.set()


def check_audio(routes):
    """Open all four streams together with silence; no speech is recorded or sent."""
    settings = sd.WasapiSettings(auto_convert=True)
    with ExitStack() as stack:
        for device in (routes.microphone, routes.from_caller):
            stack.enter_context(open_input(device, settings))
        for device in (routes.to_caller, routes.headphones):
            stream = stack.enter_context(sd.RawOutputStream(
                device=device, samplerate=INPUT_RATE, channels=1,
                dtype='int16', extra_settings=settings))
            stream.write(bytes(INPUT_RATE // 10 * 2))
    print('All four audio streams opened together successfully.')


def main(argv=None):
    for output in (sys.stdout, sys.stderr):
        if hasattr(output, 'reconfigure'):
            output.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=20, help='Maximum phrase length, 2–25 seconds.')
    parser.add_argument('--pause', type=float, default=0.9, help='Silence ending a phrase, 0.3–3 seconds.')
    parser.add_argument('--denoise', action='store_true', help='Clean local microphone noise offline.')
    parser.add_argument('--check', action='store_true', help='Check four streams with silence, then exit.')
    parser.add_argument('--prepare', action='store_true', help='Download both model sets, then exit.')
    args = parser.parse_args(argv)
    if not 2 <= args.seconds <= 25 or not 0.3 <= args.pause <= 3:
        parser.error('Use --seconds 2–25 and --pause 0.3–3.')
    try:
        with single_session():
            if args.prepare:
                prepare('zh')
                prepare('en')
                print('Both model sets ready.')
                return 0
            routes = select_routes()
            print('Keep these WhatsApp settings for the whole call:\n'
                  '  Microphone: CABLE Output (VB-Audio Virtual Cable)\n'
                  '  Speaker:    Hi-Fi Cable Input (VB-Audio Hi-Fi Cable)\n'
                  'English microphone -> Chinese dub -> caller\n'
                  'Chinese caller -> English dub -> AirPods', flush=True)
            if args.check:
                check_audio(routes)
            else:
                run_call(routes, args.seconds, args.pause, args.denoise)
        return 0
    except KeyboardInterrupt:
        print('\nDubbing stopped.')
        return 0
    except (RuntimeError, OSError, ValueError, sd.PortAudioError) as error:
        print(f'Dubbing stopped: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
