# TooGee

**A local-first voice assistant with an animated face, built on a first-generation
[FREE-WILi](https://freewili.com) board.**

Say "TooGee", ask a question, and it answers out loud from the board's own speaker
while its face listens, thinks, talks, gets bored, and falls asleep. The board runs
custom firmware; a Mac does the thinking with your own
[Open WebUI](https://github.com/open-webui/open-webui) server, a local wake word,
and a local voice.

- **Wake word, locally:** speech bursts are checked by a small on-device Whisper model
  (`faster-whisper` `base.en`); nothing leaves the Mac until it hears its name.
- **Brains:** any chat model on your Open WebUI, plus Open WebUI's Whisper for speech
  to text.
- **Web search:** the model can search the web through your Open WebUI's own search
  (e.g. SearXNG), and is told to answer only from what it finds.
- **Voice:** [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) via
  [kokoro-js](https://www.npmjs.com/package/kokoro-js), running locally.
- **A face with moods:** idle, listening, thinking, talking (lip-synced to the audio),
  happy, confused, sad, bored, asleep, and a startled wake-up; plus mood LEDs and
  chimes, so it's always clear when it's listening.

```
          FREE-WILi OG (custom "wili" firmware)                     Mac
 ┌───────────────────────────────────────────────┐        ┌──────────────────────────────┐
 │ display CPU                                   │  USB   │ wili_assistant.py            │
 │   core 1: animated face (ST7789 320x240)      │◄──────►│   speech bursts -> local     │
 │   core 0: PDM mic ──► 8 kHz stream ───────────┼──────► │   Whisper: "TooGee"?         │
 │           speaker ◄── 8 kHz stream ◄──────────┼─────── │   Open WebUI: STT + chat     │
 │           buttons, mood LEDs, chimes          │ binary │     (+ web_search tool)      │
 │ main CPU: watchdog, boots the display         │ frames │   Kokoro TTS (tts/, node)    │
 └───────────────────────────────────────────────┘        └──────────────────────────────┘
```

## Repository layout

| Path | What it is |
| --- | --- |
| `wili_assistant.py` | The assistant: wake word, listening, Open WebUI, web search, speech |
| `wili_board.py` | Host side of the board's USB protocol (mic, speaker, face, buttons, LEDs) |
| `tts/` | Kokoro text-to-speech helper (`kokoro_server.mjs`, run by the assistant) |
| `firmware/apps/wili/` | The board firmware: `display/` (face, audio, protocol) and `main/` |
| `firmware/patches/` | Small additions to the toolkit's drivers (speaker stream mode, async LCD blit) |
| `firmware/wiliOGbsp/` | Free-Wili's [wiliOGbsp](https://github.com/freewili/wiliOGbsp) toolkit, as a submodule pinned to `3426c46` |
| `firmware/build.sh` | Applies the patch, links the app in, builds/flashes with the toolkit |
| `launchd/`, `scripts/` | Launch agent template, install/uninstall scripts, run wrapper |

## What you need

- A **FREE-WILi OG** (the original, two-RP2040 FREE-WILi) and a USB cable.
- A Mac with Apple silicon, **Python 3.13**, **Node.js** (18+) and Homebrew.
- An **Open WebUI** server you can reach, an API key for it (Settings → Account →
  API Keys), a chat model, speech to text enabled, and (optionally) web search.
- To build firmware: the **Pico SDK 2.3.0** toolchain (below).

## Setup

### 1. Get the code

```sh
git clone --recurse-submodules https://github.com/2GT-Rich/toogee.git
cd toogee
```

### 2. Python and the voice

```sh
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
(cd tts && npm install)
```

The first run downloads the Kokoro model (~90 MB) and the Whisper `base.en` model
(~150 MB) and caches them.

### 3. Settings

```sh
cp .env.example .env
```

Fill in `OPENWEBUI_URL`, `OPENWEBUI_API_KEY` and `OPENWEBUI_MODEL`, then check
everything is reachable:

```sh
.venv/bin/python wili_assistant.py --check
```

### 4. Firmware

**Toolchain.** The toolkit expects the Raspberry Pi Pico extension's layout under
`~/.pico-sdk` (installing that VS Code extension creates it), or by hand:

| Component | Version | Location |
| --- | --- | --- |
| [Pico SDK](https://github.com/raspberrypi/pico-sdk) (with submodules) | 2.3.0 | `~/.pico-sdk/sdk/2.3.0` |
| [Arm GNU Toolchain](https://developer.arm.com/downloads/-/arm-gnu-toolchain-downloads) `arm-none-eabi`, darwin-arm64 | 15.2.Rel1 | `~/.pico-sdk/toolchain/15_2_Rel1` |
| [Ninja](https://github.com/ninja-build/ninja/releases) | 1.13.2 | `~/.pico-sdk/ninja/v1.13.2` |

```sh
git clone --depth 1 --branch 2.3.0 https://github.com/raspberrypi/pico-sdk.git ~/.pico-sdk/sdk/2.3.0
git -C ~/.pico-sdk/sdk/2.3.0 submodule update --init --depth 1
```

**First time on a board** (converts it from the stock firmware; this erases the main
CPU's storage, so copy off anything you want first):

```sh
firmware/build.sh bootloader   # once per board: installs the display bootloader
firmware/build.sh flash        # builds and flashes TooGee's firmware
```

**After that**, any change is just `firmware/build.sh flash`. The board appears as
`FWOG main wili 00N` and `FWOG display wili 00N`.

> **Read [`firmware/wiliOGbsp/AGENTS.md`](https://github.com/freewili/wiliOGbsp/blob/main/AGENTS.md)
> before changing firmware.** In short: the display CPU has no BOOTSEL button, so
> never UF2-copy a display app onto it — display firmware always travels inside the
> main CPU's image, which `build.sh flash` handles. If a build ever leaves the board
> dark, the toolkit's documented recovery is to hold **red** through a reset to put the
> main CPU in BOOTSEL and flash again (its notes say that step is reasoned, not yet
> measured on hardware).
> To go back to stock, Free-Wili publishes the original images in
> [freewili-firmware](https://github.com/freewili/freewili-firmware/releases).

### 5. Run it

```sh
.venv/bin/python wili_assistant.py
```

or have it start at login and stay running:

```sh
scripts/install-launch-agent.sh
tail -f ~/Library/Logs/TooGee/toogee.log
```

`scripts/uninstall-launch-agent.sh` removes it. The agent restarts TooGee if it
exits, and TooGee itself waits and reconnects if the board is unplugged or reflashed.

## Using TooGee

- **"TooGee, what time is it?"** in one breath, or **"TooGee"** … *(rising chime,
  the face perks up)* … "what's the weather in Seattle?".
- A **falling chime** means it heard you and is thinking; a **low tone** means
  something went wrong.
- **Buttons:** white/gray starts a new conversation; any other button is
  push-to-talk. Holding **red for 6 s** powers the board off (on USB power it stays
  on until unplugged); holding gray wakes it.
- Left alone it gets **bored** after 45 s and **falls asleep** after 2.5 minutes
  (dim screen, Zzz). Saying its name wakes it with a start; poking a button makes it
  giggle.
- For current questions it says "let me look that up" and searches; the log shows
  which sites each answer came from.

Other modes: `--no-wake` (buttons only), `--text "question"` (typed, spoken reply),
`--mac-speaker`, `--wav file.wav` (transcribe), `--check`.

## Configuration (`.env`)

| Setting | Default | |
| --- | --- | --- |
| `OPENWEBUI_URL` | `http://localhost:3000` | your Open WebUI |
| `OPENWEBUI_API_KEY` | — | required |
| `OPENWEBUI_MODEL` | — | required; `--check` lists them |
| `WILI_NAME` | `TooGee` | its name and wake word |
| `WILI_WAKE` | `name` | `name` or `off` (buttons only) |
| `WILI_WAKE_MODEL` | `base.en` | local Whisper model for the wake word |
| `WILI_VOCAB` | — | comma-separated names speech recognition misspells; used as spelling hints |
| `WILI_WEB_SEARCH` | `on` | let the model search via Open WebUI |
| `WILI_TTS` | `kokoro` | `kokoro` or `say` (macOS) |
| `WILI_KOKORO_VOICE` / `_SPEED` | `af_sky` / `1.1` | any Kokoro voice (`af_heart`, `am_michael`, `bf_emma`, …) |
| `WILI_SPEAKER` | `board` | `board` or `mac` |
| `WILI_VOLUME` | `9` | board speaker, 0–10 |
| `WILI_LED_BRIGHTNESS` | `25` | LED bar, percent |
| `WILI_SILENCE_SEC` | `1.2` | pause that ends your turn |
| `WILI_MAX_LISTEN_SEC` | `12` | longest turn |
| `WILI_HISTORY_TURNS` | `6` | exchanges it remembers |
| `WILI_SYSTEM_PROMPT` | built in | its personality |

## How it works

**Firmware** (`firmware/apps/wili`, on the toolkit's BSP). The display CPU's
second core renders the face: each frame is a small list of anti-aliased primitives
(rounded rects, arcs, parabola strokes, eyelid half-planes, font glyphs) rasterised in
16-row bands, DMA'd to the panel while the next band is drawn; bands that stay empty
are skipped. Core 0 streams the PDM mic (CIC decimation to 8 kHz, a streaming DC
blocker, +30 dB), plays host audio through a 2-second ring buffer into the I2S
driver's stream mode, releases lip-sync levels when that audio is actually audible,
synthesises chimes, and drives the LEDs by mood. The face runs its own life — blinks,
glances, breathing, boredom, sleep — and the host only says which mood it wants.

**Protocol** (`wili_proto.h` / `wili_board.py`): binary frames on the display CPU's
USB serial port, `F7 1A | type | len16 | payload | crc8`, resynced on the magic bytes.
Mic and speaker are 8 kHz int16 mono. The host paces audio against the board's
reported ring level. Opening the port at 1200 baud still reboots into BOOTSEL.

| Board → Mac | | Mac → board | |
| --- | --- | --- | --- |
| `HELLO` | version | `PING` | → `HELLO` + speaker status |
| `MIC` | 256 samples | `MIC` | start/stop mic stream |
| `BUTTON` | down/pressed/released | `AUDIO` / `AUDIO_END` / `FLUSH` | speaker stream |
| `SPK` | played/received/ring/underruns | `FACE` | mood |
| `FACE` | mood now shown | `CHIME`, `VOLUME`, `LEDS`, `LED_LEVEL` | |

**Wake word.** The mic stream is cut into speech bursts by level against a running
noise floor. Each burst goes to a local Whisper model nudged with a prompt and hotword
to spell "TooGee" (on real recordings that took it from 3/8 to 7/8 heard correctly,
with no false wakes); a burst that *starts* with the name wakes it. For "TooGee,
&lt;request&gt;" the word timestamps cut the name off and only the request goes to Open
WebUI's (stronger) Whisper.

**Web search.** Open WebUI's own `features.web_search` switch only works inside a
browser chat session, so TooGee offers the model an OpenAI-style `web_search` tool and
runs it through Open WebUI's retrieval API (`process/web/search` then
`query/collection`) — the same steps its chat performs. The model gets every result's
title and snippet plus the most relevant passages (at most two per page), and
instructions to answer only from them and say when it couldn't find something.

## Privacy

Until it hears its name, room audio is only ever processed on the Mac. After a wake,
the request audio goes to your Open WebUI server for transcription and the text to
your chosen model; web searches go to your Open WebUI's configured search engine.
The voice is generated locally.

## Troubleshooting

- **`--check`** tests Open WebUI, speech to text, web search, the board and the voice.
- **"The Wili board is already in use"** — another copy is running (e.g. the launch
  agent). `scripts/uninstall-launch-agent.sh` or `pkill -f wili_assistant.py`.
- **It mishears its name** — the log prints what the local model heard for every
  burst; add stubborn spellings to the pattern in `WakeName._pattern`.
- **Names come out wrong in answers** — add them to `WILI_VOCAB`.
- **A hang** — `kill -USR1 <pid>` prints every thread's stack to the log.

## Credits and licenses

TooGee's own code — the assistant, the board protocol, the TTS helper, the scripts
and `firmware/apps/wili` — is released under the [MIT License](LICENSE). Third-party
components keep their own licenses:

- [wiliOGbsp](https://github.com/freewili/wiliOGbsp) by Free-Wili / Intrepid Control
  Systems — included as a submodule, unmodified except for `firmware/patches/`.
  Licensed under the **FreeWili Hardware License** (MIT text with a field-of-use
  condition: free for firmware targeting FreeWili hardware; not an open-source
  license). The patches are offered under the same terms as the files they modify,
  and firmware built from this repository includes that code, so the binaries are
  subject to those terms.
- [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) (Apache-2.0) via
  [kokoro-js](https://github.com/hexgrad/kokoro).
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (MIT) and OpenAI's
  Whisper models.
- [Open WebUI](https://github.com/open-webui/open-webui), and whatever search engine
  it's configured with.
