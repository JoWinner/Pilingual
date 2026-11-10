"""Dub spoken phrases through AirPods and WhatsApp Desktop on this PC."""

import argparse
import io
import json
import shutil
import sys
import tempfile
import wave
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event
from time import perf_counter
from urllib.request import urlopen

import ctranslate2
import numpy as np
import sentencepiece
import sherpa_onnx
import sounddevice as sd
from faster_whisper import WhisperModel, download_model
from huggingface_hub import snapshot_download
from piper import PiperVoice, SynthesisConfig

from airpods_bench import find_endpoint

MODELS = Path(__file__).resolve().parent / ".models"
MANDARIN_VOICE = "vits-zh-aishell3"
MANDARIN_MODEL = "vits-aishell3.int8.onnx"
MANDARIN_RULES = ("phone.fst", "date.fst", "number.fst", "new_heteronym.fst")
SHERPA_RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
INPUT_RATE = 16_000


def download(url, destination):
    if destination.is_file():
        return
    print(f"Downloading {destination.name}…", flush=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    with urlopen(url, timeout=60) as response, partial.open("wb") as output:
        shutil.copyfileobj(response, output)
    partial.replace(destination)


def prepare_recognizer(model="base"):
    MODELS.mkdir(exist_ok=True)
    whisper = MODELS / f"whisper-{model}"
    if not all((whisper / name).is_file() for name in ("model.bin", "tokenizer.json", "config.json")):
        print(f"Downloading multilingual Whisper {model}…", flush=True)
        download_model(model, output_dir=str(whisper))
    return whisper


def prepare_vad():
    MODELS.mkdir(exist_ok=True)
    download(f"{SHERPA_RELEASES}/asr-models/silero_vad.onnx", MODELS / "silero_vad.onnx")


def prepare(target, *, prefer_cached=False, whisper_model="base"):
    whisper = prepare_recognizer(whisper_model)

    direction = "en_zh" if target == "zh" else "zh_en"
    package = f"translate-{direction}-1_9"
    translation = MODELS / package
    if not translation.is_dir():
        with tempfile.TemporaryDirectory(dir=MODELS) as directory:
            temporary = Path(directory)
            archive = temporary / f"{package}.argosmodel"
            download(f"https://data.argosopentech.com/argospm/v1/{archive.name}", archive)
            with zipfile.ZipFile(archive) as model_zip:
                model_zip.extractall(temporary)
            (temporary / package).replace(translation)

    prepare_vad()
    if target == "zh":
        voice = MODELS / MANDARIN_VOICE
        files = (MANDARIN_MODEL, "lexicon.txt", "tokens.txt", *MANDARIN_RULES)
        if not all((voice / name).is_file() for name in files):
            cached_voice = MODELS / "zh_CN-huayan-x_low.onnx"
            if (prefer_cached and cached_voice.is_file()
                    and cached_voice.with_suffix(".onnx.json").is_file()):
                voice = cached_voice
                print("Using the previous Mandarin voice while the new voice is unavailable.",
                      flush=True)
            else:
                print("Downloading the offline Mandarin voice…", flush=True)
                snapshot_download("csukuangfj/vits-zh-aishell3", revision="e3e808e",
                                  allow_patterns=files, local_dir=voice)
    else:
        voice = MODELS / "en_US-lessac-low.onnx"
        url = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/low"
        download(f"{url}/{voice.name}", voice)
        download(f"{url}/{voice.name}.json", voice.with_suffix(".onnx.json"))
    return whisper, translation, voice


def load_translation(model_path):
    tokenizer = sentencepiece.SentencePieceProcessor(
        model_file=str(model_path / "sentencepiece.model"),
    )
    model = ctranslate2.Translator(str(model_path / "model"), device="cpu",
                                  compute_type="int8", intra_threads=2)
    metadata = json.loads((model_path / "metadata.json").read_text(encoding="utf-8"))
    return tokenizer, model, metadata.get("target_prefix")


def translate_text(text, tokenizer, model, prefix):
    translated = model.translate_batch(
        [tokenizer.encode(text, out_type=str)],
        target_prefix=[[prefix]] if prefix else None,
        replace_unknowns=True,
    )[0].hypotheses[0]
    return tokenizer.decode(translated).removeprefix(prefix or "").replace("▁", " ").strip()


def load_voice(path):
    if path.is_file():
        return PiperVoice.load(path)
    config = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                model=str(path / MANDARIN_MODEL),
                lexicon=str(path / "lexicon.txt"),
                tokens=str(path / "tokens.txt"),
            ),
            num_threads=2,
        ),
        rule_fsts=",".join(str(path / rule) for rule in MANDARIN_RULES),
    )
    if not config.validate():
        raise RuntimeError("The Mandarin voice model is incomplete.")
    return sherpa_onnx.OfflineTts(config)


def synthesize(text, voice):
    if isinstance(voice, sherpa_onnx.OfflineTts):
        config = sherpa_onnx.GenerationConfig()
        config.sid = 21
        config.speed = 0.85
        audio = voice.generate(text, config)
        samples = np.clip(audio.samples, -1.0, 1.0)
        return (samples * 32767).astype("<i2").tobytes(), audio.sample_rate
    audio = io.BytesIO()
    with wave.open(audio, "wb") as output:
        voice.synthesize_wav(text, output, syn_config=SynthesisConfig(length_scale=1.35))
    audio.seek(0)
    with wave.open(audio, "rb") as output:
        return output.readframes(output.getnframes()), output.getframerate()


def make_vad(seconds, pause):
    config = sherpa_onnx.VadModelConfig()
    config.silero_vad.model = str(MODELS / "silero_vad.onnx")
    config.silero_vad.min_speech_duration = 0.18
    config.silero_vad.min_silence_duration = pause
    config.silero_vad.max_speech_duration = seconds
    config.sample_rate = INPUT_RATE
    config.num_threads = 1
    vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=seconds + 2)
    return vad, config.silero_vad.window_size


def load_denoiser():
    path = MODELS / "gtcrn_simple.onnx"
    download(f"{SHERPA_RELEASES}/speech-enhancement-models/{path.name}", path)
    config = sherpa_onnx.OfflineSpeechDenoiserConfig(
        model=sherpa_onnx.OfflineSpeechDenoiserModelConfig(
            gtcrn=sherpa_onnx.OfflineSpeechDenoiserGtcrnModelConfig(model=str(path)),
            num_threads=1,
        ),
    )
    if not config.validate():
        raise RuntimeError("The microphone noise reduction model is incomplete.")
    return sherpa_onnx.OfflineSpeechDenoiser(config)


def open_input(device, settings):
    return sd.RawInputStream(device=device, samplerate=INPUT_RATE, channels=1,
                             dtype="int16", extra_settings=settings)


def read_audio(stream, window):
    pcm, overflow = stream.read(window)
    if overflow:
        raise RuntimeError("Windows dropped input audio. Check the audio device and try again.")
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768


def record_phrase(mic, settings, vad, window, seconds):
    vad.reset()
    chunks = []
    with open_input(mic, settings) as stream:
        print("Speak naturally, then pause.", flush=True)
        for _ in range(INPUT_RATE * seconds // window):
            samples = read_audio(stream, window)
            chunks.append(samples)
            vad.accept_waveform(samples)
            if not vad.empty():
                break
    vad.flush()
    if vad.empty():
        return np.empty(0, dtype=np.float32)

    # Include a small amount of audio before the detected onset.
    phrase = vad.front
    start = max(0, phrase.start - INPUT_RATE // 8)
    end = phrase.start + len(phrase.samples)
    return np.concatenate(chunks)[start:end]


def microphone_phrases(device, settings, vad, window, seconds, repeat):
    while True:
        input("Press Enter to speak through the AirPods (Ctrl+C to quit).")
        samples = record_phrase(device, settings, vad, window, seconds)
        yield samples, perf_counter()
        if not repeat:
            return


def incoming_phrases(device, settings, vad, window, seconds):
    pending = Queue(maxsize=3)
    stopped = Event()

    def capture():
        with open_input(device, settings) as stream:
            print("Listening for the WhatsApp caller (Ctrl+C to quit).", flush=True)
            active_frames = 0
            while not stopped.is_set():
                vad.accept_waveform(read_audio(stream, window))
                active_frames = active_frames + window if vad.is_speech_detected() else 0
                if active_frames >= INPUT_RATE * seconds:
                    vad.flush()
                    active_frames = 0
                while not vad.empty():
                    phrase = np.array(vad.front.samples, dtype=np.float32, copy=True)
                    vad.pop()
                    entry = phrase, perf_counter()
                    try:
                        pending.put_nowait(entry)
                    except Full:
                        try:
                            pending.get_nowait()
                        except Empty:
                            pass
                        else:
                            print("Incoming dubbing fell behind and skipped an older phrase.", flush=True)
                        pending.put_nowait(entry)

    with ThreadPoolExecutor(max_workers=1) as pool:
        reader = pool.submit(capture)
        try:
            while True:
                try:
                    entry = pending.get(timeout=0.25)
                except Empty:
                    if reader.done():
                        reader.result()
                        return
                    continue
                yield entry
        finally:
            stopped.set()


def recognize(samples, recognizer, language):
    segments, _ = recognizer.transcribe(samples, language=language, beam_size=3,
                                        temperature=0.0, condition_on_previous_text=False)
    return " ".join(segment.text.strip() for segment in segments).strip()


def main():
    for output in (sys.stdout, sys.stderr):
        if hasattr(output, 'reconfigure'):
            output.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--to", choices=("zh", "en"), required=True,
                        help="Output language (zh for Mandarin, en for English).")
    parser.add_argument("--seconds", type=int, metavar="2–25",
                        help="Maximum seconds per phrase (default 20 outgoing, 12 incoming).")
    parser.add_argument("--pause", type=float, metavar="SECONDS",
                        help="Silence that ends a phrase (default 0.9 outgoing, 0.7 incoming).")
    parser.add_argument("--prepare", action="store_true", help="Download models without recording.")
    route = parser.add_mutually_exclusive_group()
    route.add_argument("--send-to-whatsapp", action="store_true",
                       help="Play translated speech into VB-CABLE for WhatsApp Desktop.")
    route.add_argument("--from-whatsapp", action="store_true",
                       help="Continuously translate WhatsApp audio from VB-CABLE into the AirPods.")
    parser.add_argument("--repeat", action="store_true",
                        help="Keep models loaded for multiple phrases. Press Ctrl+C to quit.")
    parser.add_argument("--denoise", action="store_true",
                        help="Reduce microphone background noise locally before recognition.")
    args = parser.parse_args()
    seconds = args.seconds if args.seconds is not None else (12 if args.from_whatsapp else 20)
    pause = args.pause if args.pause is not None else (0.7 if args.from_whatsapp else 0.9)
    if not 2 <= seconds <= 25:
        parser.error("--seconds must be between 2 and 25.")
    if not 0.3 <= pause <= 3:
        parser.error("--pause must be between 0.3 and 3 seconds.")

    try:
        whisper, translation, voice_path = prepare(args.to, prefer_cached=not args.prepare)
        denoiser = load_denoiser() if args.denoise else None
        if args.prepare:
            print("Models ready.")
            return 0

        input_name = "CABLE Output" if args.from_whatsapp else "AirPods"
        mic, input_device = find_endpoint("input", input_name)
        destination = "CABLE Input" if args.send_to_whatsapp else "AirPods"
        speaker, endpoint = find_endpoint("output", destination)
        settings = sd.WasapiSettings(auto_convert=True)
        print(f"Listening to {input_device['name']}.", flush=True)
        print(f"Translated speech will play through {endpoint['name']}.", flush=True)
        print("Loading offline models…", flush=True)
        recognizer = WhisperModel(str(whisper), device="cpu", compute_type="int8", cpu_threads=4)
        translation_models = load_translation(translation)
        voice = load_voice(voice_path)
        vad, window = make_vad(seconds, pause)
        source = "en" if args.to == "zh" else "zh"

        phrases = (
            incoming_phrases(mic, settings, vad, window, seconds) if args.from_whatsapp
            else microphone_phrases(mic, settings, vad, window, seconds, args.repeat)
        )
        with closing(phrases):
            return dub_phrases(phrases, recognizer, translation_models, voice, denoiser,
                               source, speaker, settings, args)
    except (RuntimeError, OSError, ValueError, sd.PortAudioError) as error:
        print(f"Translation test failed: {error}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        return 0


def dub_phrases(phrases, recognizer, translation_models, voice, denoiser,
                source, speaker, settings, args):
    for samples, captured_at in phrases:
        processing_started_at = perf_counter()
        if denoiser is not None and samples.size:
            samples = np.asarray(denoiser(samples, INPUT_RATE).samples, dtype=np.float32)
        decode_started_at = perf_counter()
        spoken = recognize(samples, recognizer, source) if samples.size else ""
        if not spoken:
            print("No speech was recognized.", flush=True)
            if args.repeat or args.from_whatsapp:
                continue
            return 1
        recognized_at = perf_counter()
        translated = translate_text(spoken, *translation_models)
        translated_at = perf_counter()
        if not translated:
            raise RuntimeError("The model returned an empty translation.")
        print(f"Heard: {spoken}\nTranslation: {translated}", flush=True)
        audio, rate = synthesize(translated, voice)
        synthesized_at = perf_counter()
        if not audio:
            raise RuntimeError("The voice model returned no audio.")

        with sd.RawOutputStream(device=speaker, samplerate=rate, channels=1,
                                dtype="int16", extra_settings=settings) as stream:
            lead_ms = 100 if args.send_to_whatsapp else 350
            stream.write(bytes(rate * 2 * lead_ms // 1000))
            queued_at = perf_counter()
            stream.write(audio)
            stream.write(bytes(rate * 2 * 150 // 1000))
        timings = []
        if denoiser is not None:
            timings.append(f"cleanup {decode_started_at - processing_started_at:.2f}s")
        if args.from_whatsapp:
            timings.append(f"queue {processing_started_at - captured_at:.2f}s")
        timings.extend([
            f"recognition {recognized_at - decode_started_at:.2f}s",
            f"translation {translated_at - recognized_at:.2f}s",
            f"voice {synthesized_at - translated_at:.2f}s",
            f"audio output {queued_at - synthesized_at:.2f}s",
        ])
        print(f"Audio queued {queued_at - captured_at:.2f}s after capture "
              f"({', '.join(timings)}).", flush=True)
    return 0


if __name__ == "__main__":
    from airpods_call import single_session
    try:
        with single_session():
            sys.exit(main())
    except (RuntimeError, OSError) as error:
        print(f"Translation stopped: {error}", file=sys.stderr)
        sys.exit(1)
