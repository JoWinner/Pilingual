"""Check the AirPods microphone and speaker path on Windows."""

import argparse
import sys

import sounddevice as sd


def find_endpoint(direction, name="AirPods"):
    host_apis = sd.query_hostapis()
    matches = [
        (index, device)
        for index, device in enumerate(sd.query_devices())
        if name.casefold() in device["name"].casefold()
        and host_apis[device["hostapi"]]["name"] == "Windows WASAPI"
        and device[f"max_{direction}_channels"] > 0
    ]
    # "CABLE Input" is also a substring of "Hi-Fi Cable Input".
    # Prefer the exact Windows endpoint label, retaining substring lookup for AirPods.
    exact = [(index, device) for index, device in matches
             if device['name'].split(' (', 1)[0].casefold() == name.casefold()]
    if exact:
        matches = exact
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected one {name} {direction} in Windows Sound; found {len(matches)}. "
            "Check that the device is connected and enabled."
        )
    return matches[0]


def run(loopback):
    mic_id, mic = find_endpoint("input")
    speaker_id, speaker = find_endpoint("output")
    print(f"Microphone: {mic['name']}")
    print(f"Playback:   {speaker['name']}")

    rate = int(speaker["default_samplerate"])
    settings = sd.WasapiSettings(auto_convert=True)
    # A short silent duplex transfer checks both paths without replaying room audio.
    frames = rate // 10
    with sd.RawStream(
        device=(mic_id, speaker_id), channels=(1, 2), dtype="int16",
        samplerate=rate, extra_settings=(settings, settings),
    ) as stream:
        stream.write(bytes(frames * 4))
        samples, overflowed = stream.read(frames)
        if overflowed:
            raise RuntimeError("AirPods input overflowed during the duplex probe.")
        if len(samples) != frames * 2:
            raise RuntimeError("AirPods returned an incomplete audio buffer.")
    print(f"Duplex probe: transferred {frames} frames at {rate} Hz.")

    if loopback:
        input("Wear the AirPods, set a comfortable volume, and press Enter to record.")
        print("Speak now (3 seconds)…", flush=True)
        with sd.RawInputStream(
            device=mic_id, channels=1, dtype="int16", samplerate=rate,
            extra_settings=settings,
        ) as stream:
            samples, overflowed = stream.read(rate * 3)
        if overflowed:
            raise RuntimeError("AirPods input overflowed during recording.")
        print("Playing back through the AirPods…", flush=True)
        with sd.RawOutputStream(
            device=speaker_id, channels=1, dtype="int16", samplerate=rate,
            extra_settings=settings,
        ) as stream:
            stream.write(samples)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loopback", action="store_true", help="Record and replay a 3-second sample in memory.")
    args = parser.parse_args()
    try:
        run(args.loopback)
    except (RuntimeError, sd.PortAudioError) as error:
        print(f"Audio check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
