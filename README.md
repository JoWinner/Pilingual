# Pi-lingual AirPods bench

## Control board

Double-click **Open-Pilingual.pyw** in this folder. It opens the local control board in your default
browser without a terminal window. The launcher uses the existing `.venv`; see the Python setup
below if you are starting from a fresh checkout.

- Choose **Two-way dubbing** for WhatsApp or **Transcribe microphone** for a text-only AirPods session.
  The WhatsApp mode uses the same cable and device setup described below.
- Set the language direction or speech language and choose **Whisper Base** or **Whisper Small**.
  The first use of a model can download its files. Settings apply to the next session.
- Use **Check audio** to verify the selected mode, then **Start session**. The board shows the input
  level, transcript, and activity. Select a playback event to see its timing and the matching phrase.
- Use **Session history** to reopen saved sessions. **Export report** downloads JSON with the
  transcript, events, configuration, and timing summary. The database is stored in
  `%LOCALAPPDATA%\Pi-lingual\control-board.sqlite3`. It contains text and events, with no raw audio.
- Select **Stop session** to release the audio devices, then **Close board** to exit the local app.
  Closing the browser tab alone leaves the app running. Double-click the launcher to reopen it.

## Two-way WhatsApp dubbing (current mode)

Run one translator for the whole call. Both directions listen continuously, with no Enter key and no switching devices mid-call. English from the AirPods mic becomes Mandarin for the caller. Mandarin from the caller becomes English in the AirPods.

### One-time audio setup

Two isolated virtual audio paths are required. The existing VB-CABLE carries only outgoing Mandarin. [VB-Audio Hi-Fi Cable](https://vb-audio.com/Cable/index.htm) carries only incoming WhatsApp audio and is free for end users. Its signed installer has been downloaded into `.downloads/HiFiCable` and its publisher is Vincent Burel.

From this folder, open the installer:

```powershell
.\Install-Second-Cable.ps1
```

Complete **Install Driver** and reboot if requested. The script checks the publisher signature again before opening it. Do not enable ASIO routing in ASIO Bridge; use the cable's normal pass-through. In Windows Sound's Playback and Recording device properties, Hi-Fi Cable Input and Output must use the **same default sample rate** (e.g. both 48000 Hz). The translator checks this before opening audio.

Set these in WhatsApp once and keep them for the entire call:

| WhatsApp setting | Device |
| --- | --- |
| Microphone | **CABLE Output (VB-Audio Virtual Cable)** |
| Speaker | **Hi-Fi Cable Input (VB-Audio Hi-Fi Cable)** |

Do not choose AirPods as WhatsApp's microphone in this mode: the translator owns the AirPods mic. Do not enable Windows **Listen to this device** for either cable. Keep the computer's regular speaker output on AirPods, not either cable, so notification audio is not transmitted or translated.

### Start and stop

Stop any old translator with Ctrl+C. Check the installed audio devices:

```powershell
.\.venv\Scripts\python.exe airpods_call.py --check
```

Then double-click **Start-Dubbing.cmd**, or run:

```powershell
.\.venv\Scripts\python.exe airpods_call.py
```

Wait for **READY**. Both participants speak normally and pause between sentences. Ctrl+C stops both directions. The launcher stays open so any error is readable. Only one translator can run at a time; the one-way scripts also honor the session lock.

`--pause 1.3` allows longer natural pauses. Speech over 20 seconds is split into chunks and capture continues. `--denoise` adds optional local microphone cleanup. Processing stays offline. Internet quality still affects the WhatsApp call audio, but local recognition does not use a network connection.

### Current limits

- The English playback guard discards local mic capture during the English dub and for 350 ms afterwards. Wait until it finishes before replying. This prevents acoustic feedback at the expense of talking over the dub; it is not full acoustic echo cancellation. A sentence overlapping the guard can be discarded.
- Each direction captures while translating and playing earlier sentences. Each holds at most three waiting phrases and skips phrases that have waited more than 15 seconds, with a warning, to avoid an ever-growing delay.
- This remains sentence-based dubbing with the existing local models. Recognition errors and several seconds of delay are still possible, particularly with both directions processing on this four-core CPU. This change does not add a paid API or voice cloning.
- The program validates Windows endpoints, rates and stream availability, but cannot verify WhatsApp's selected devices or every Windows monitoring setting. If the caller hears English dubbing, recheck the microphone setting and Windows monitoring. The remote caller should use headphones if their speaker audio is echoing back into the call.
- After leaving dubbing mode, restore WhatsApp's microphone and speaker to AirPods for normal calls. The translator does not change system defaults automatically.

### Developer checks

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe verify_dubbing.py
```

The segmentation tests use the downloaded English voice and VAD models to generate local test speech, without opening audio hardware. They check exact cable selection, separate destinations, sample-rate mismatch rejection, longer utterances, automatic chunking, and playback suppression/resumption. The integration script runs both real translation pipelines concurrently using generated speech and captures output in memory. It verifies routing and nonempty synthesized audio, not recognition accuracy. A real WhatsApp call is still needed to verify both participants' complete audio routes after driver setup.

## Earlier bench and one-way tests

This Windows tool checks the Bluetooth microphone and playback paths before adding translation or call routing. The test pair is AirPods Pro (1st generation), model A2084, reported firmware 2B588.

Connect the AirPods in Windows Settings and confirm they are available for both input and output under **System → Sound**. From this folder, run:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe airpods_bench.py
```

The probe opens the AirPods input and output together, writes silence, and discards the microphone buffer. To confirm the real microphone and playback by ear, run:

```powershell
.\.venv\Scripts\python.exe airpods_bench.py --loopback
```

After you press Enter, it records three seconds and plays the sample back through the AirPods. The sample stays in memory. No audio is sent over the internet.

## Spoken translation test

With the AirPods connected to Windows, run:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe airpods_translate.py --to zh
```

The first run downloads the recognition, translation, pause detection, and voice models. Add `--prepare` to download them without recording. Processing takes place on the PC with no API key or per-minute charges. Press Enter, wait for the cue, and speak a short English sentence at your normal pace. Recording ends when you pause. The tool prints the Mandarin translation and plays it through the AirPods. Use `--to en` and speak Mandarin to test the reverse direction.

## WhatsApp Desktop outgoing test

Install [VB-CABLE](https://vb-audio.com/Cable/index.htm) for Windows. In a WhatsApp Desktop call, select **CABLE Output (VB-Audio Virtual Cable)** as the microphone and your AirPods as the speaker. Keep the call microphone enabled so WhatsApp can transmit the cable audio.

```powershell
.\.venv\Scripts\python.exe airpods_translate.py --to zh --send-to-whatsapp --repeat
```

Press Enter for each phrase. The translator plays the Mandarin result into **CABLE Input** so WhatsApp can send it to the caller. `--repeat` keeps the models loaded between phrases. Use Ctrl+C to exit. The reported time is from the end of recording to queuing audio in Windows. Incoming audio still plays directly from WhatsApp.

Add `--denoise` to reduce steady background noise in the microphone signal before recognition. Compare the same phrases with and without it in the same room. The timing line reports cleanup, recognition, translation, voice generation, and audio output separately.

The outgoing recording now waits for about 0.9 seconds of silence and allows up to 20 seconds per turn. Use `--pause 1.3` if natural pauses end a turn early.

## WhatsApp Desktop incoming test

Stop the outgoing translator. In the call's audio settings, set **Speaker** to **CABLE Input (VB-Audio Virtual Cable)** and **Microphone** to the **AirPods headset**. The caller will hear your original voice for this test.

```powershell
.\.venv\Scripts\python.exe airpods_translate.py --to en --from-whatsapp
```

Ask the caller to speak Mandarin and pause after a sentence. The tool listens continuously and plays the English translation through the AirPods. Use Ctrl+C to stop. The cable is shared between the two tests, so use one direction at a time. If phrases arrive faster than the PC can dub them, the tool keeps up to three waiting phrases and reports when it drops an older one.
