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
| `alarms.py` | Timers and reminders: storage, time parsing, the alarm bell |
| `weather.py` | Weather from Open-Meteo: place lookup, forecast, wording |
| `clock.py` | Local time and date questions, answered from the Mac's clock |
| `status.py` | "Are you online?": checks the Open WebUI server and the language model |
| `motion.py` | Double taps, pose (face down, upright) and screen rotation from the board's accelerometer |
| `battery.py` | "How's your battery?": the charger's readings, in words |
| `tts/` | Kokoro text-to-speech helper (`kokoro_server.mjs`, run by the assistant) |
| `firmware/apps/wili/` | The board firmware: `display/` (face, audio, protocol) and `main/` |
| `firmware/patches/` | Small additions to the toolkit's drivers (speaker stream mode, async LCD blit, LCD rotation) |
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
- **When TooGee asks you something** ("Which city?", "Want me to set a
  reminder?"), it chimes and listens for your answer, with no name or button
  needed, and keeps going like that for as long as it keeps asking. Say nothing
  for 6 seconds, press a button, or lay it face down, and it goes back to waiting
  for its name.
- **Buttons:** white/gray starts a new conversation; any other button is
  push-to-talk. Holding **red for 6 s** powers the board off (on USB power it stays
  on until unplugged); holding gray wakes it.
- **Double-tap the case** (two quick knocks, anywhere on it) to talk, the same as
  pressing a button; it also stops a ringing timer. It goes by rhythm: two jolts a
  fifth to half a second apart, with stillness before and after. Single taps,
  picking it up or turning it don't count, and neither does TooGee's own voice
  (taps are ignored while it speaks). A double knock on the desk right next to it,
  or a mug set down hard enough to bounce, can.
- **Turn it on its side or upside down** and the face swings round to stay upright,
  overshooting a little like it's on a spring (on its side the screen is portrait,
  so the face is laid out narrower and taller). It goes wide-eyed while it swings
  and is dizzy for a moment after, with spinning spiral eyes, unless it's busy
  listening, thinking or talking.
- **Lay it face down** to stop it listening: the face sleeps and it ignores its
  name, taps and buttons. Timers and reminders still ring. Stand it back up and
  it perks up and listens again.
- **Without the Mac** (unplugged and on battery, or TooGee not running) it can't
  hear or talk, but it's still a face: it turns with the board, sleeps face down
  and wakes when stood up, gets bored and dozes off, and giggles when you press a
  button. If the Mac goes away mid-answer, the face goes back to idle.
- Left alone it gets **bored** after 45 s and **falls asleep** after 2.5 minutes
  (dim screen, Zzz). Saying its name wakes it with a start; poking a button makes it
  giggle.
- For current questions it says "let me look that up" and searches; the log shows
  which sites each answer came from.
- **Volume by voice:** "volume up", "volume down", "louder", "a little quieter",
  "much louder", "volume 6", "max volume", "mute", "unmute". Short, clear commands
  like these are handled instantly on the Mac; looser ones ("could you be a touch
  less shouty?") go to the model, which has a `set_volume` tool. It answers at the
  new volume and shows the level on the LED bar. "Quieter" stops at 1 — only "mute"
  silences it. The volume you set is remembered across restarts.
- **Timers and reminders:** "set a timer for 5 minutes", "ten minute pasta timer",
  "remind me to take the trash out at 7 pm", "remind me in half an hour to check the
  laundry", "what timers do I have?", "how long is left?", "cancel the pasta timer".
  Plain timers are set instantly; everything else goes through the model's
  `set_timer` / `set_reminder` / `list_timers` / `cancel_timer` tools. When one is
  due, TooGee interrupts its idle listening: a bell, flashing amber LEDs, and "Your
  pasta timer is done!" or "Reminder: take the trash out." Then it listens for a few
  seconds **without needing its name** — say anything ("okay, thanks") or press a
  button to dismiss, or "snooze for ten minutes" (any duration means snooze). If
  nobody answers it tries again, up to three times for timers and twice for
  reminders. They survive restarts; one that came due while TooGee was off is
  announced when it's back ("While I was off, you had a reminder…") if it's under
  six hours old.
- **Weather:** "what's the weather?", "how's the weather in Portland, Maine?",
  "will it rain tomorrow?", "do I need a jacket tonight?", "what's the weekend
  looking like?". Plain "what's the weather (in X)" is answered instantly from
  [Open-Meteo](https://open-meteo.com) (free, no API key); anything about later or
  specifics goes to the model's `get_weather` tool, which gets the next hours and up
  to 7 days. Set `WILI_LOCATION` for questions without a place; until then it asks
  which city. "Portland" means the biggest one (Oregon); say "Portland, Maine" or
  "Portland ME" for another.
- **Time and date:** "what time is it?", "what's the time?", "tell me the time",
  "what's today's date?", "what day is it?", "what's the date and time?", "what
  year is it?". These are read off the Mac's clock and answered instantly ("It's
  3:05 PM.", "Today is Saturday, November 21st.") without asking the model, so they
  work even when the server is down. Anything more ("what time does Costco close?",
  "what time is it in Tokyo?", "how many days until Christmas?") goes to the model,
  which is told the current date and time.
- **Battery:** "how's your battery?", "are you charging?", "are you plugged in?".
  The board's charger reports the battery voltage and charging state; with no fuel
  gauge on the board, the level is estimated from the voltage and said as "about".
- **Online check:** "are you online?", "are you connected?", "can you reach the
  server?", "is the LLM up?", "status check". TooGee checks that Open WebUI answers,
  then asks the language model for a few words, and says which part works: "Yes, I'm
  online. I'm connected to the server, and the language model answered in under a
  second." If something's wrong it says what: no internet, the server (or its
  Cloudflare tunnel) down, the API key turned down, the model missing, or the model
  not answering within 30 seconds. It works with the server down, because TooGee's
  local speech model hears the question.
- If the model is slow to answer (a busy server), it says so once ("my brain's a bit
  slow right now") instead of sitting silent.

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
| `WILI_REQUEST_MODEL` | `small.en` | local Whisper model that also transcribes "TooGee, &lt;request&gt;" said in one breath; `off` to use only Open WebUI's |
| `WILI_VOCAB` | — | comma-separated names speech recognition misspells; used as spelling hints |
| `WILI_WEB_SEARCH` | `on` | let the model search via Open WebUI |
| `WILI_LOCATION` | — | home city for weather, e.g. `Portland, Maine` |
| `WILI_UNITS` | `imperial` | `imperial` (°F, mph) or `metric` |
| `WILI_TTS` | `kokoro` | `kokoro` or `say` (macOS) |
| `WILI_KOKORO_VOICE` / `_SPEED` | `af_sky` / `1.1` | any Kokoro voice (`af_heart`, `am_michael`, `bf_emma`, …) |
| `WILI_SPEAKER` | `board` | `board` or `mac` |
| `WILI_VOLUME` | `9` | board speaker, 0–10; a volume set by voice is saved in `.toogee_state.json` (with your timers and reminders) and wins |
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
| `FACE` | mood now shown | `CHIME`, `VOLUME`, `LEDS`, `LED_LEVEL`, `ROTATE` (006+; unused from 008) | |
| `ACCEL` | x/y/z milli-g at 100 Hz (005+) | | |
| `POWER` | charger: USB, charging, battery mV (005+) | | |

The accelerometer (LIS3DH, ±2 g) is streamed raw and the gestures are worked out on
the Mac (`motion.py`), so tuning them never needs a reflash. Only what has to work
without the Mac runs on the board too, with the same thresholds: from firmware 008
it turns the face itself (always), and sleeps face down (only while no Mac is
connected; with one, TooGee sets the face). The charger (BQ25896) is
only read, every 2 seconds; the firmware never reconfigures it. Turning the face turns the
picture with the LCD controller's scan-direction register (a small addition to the
toolkit's ST7789 driver, in `firmware/patches/`), and the face re-lays itself out
for 240×320 when it's on its side. The swing is drawn by the face itself: it turns
the screen at once, then draws the face rotated back by the same amount and eases
that to zero, mapping each pixel into the face's own coordinates for the ~0.6 s it
takes.

**Wake word.** The mic stream is cut into speech bursts by level against a running
noise floor. Each burst goes to a local Whisper model nudged with a prompt and hotword
to spell "TooGee" (on real recordings that took it from 3/8 to 7/8 heard correctly,
with no false wakes); a burst that *starts* with the name wakes it. For "TooGee,
&lt;request&gt;" the word timestamps cut the name off and only the request goes to Open
WebUI's (stronger) Whisper. In fast speech the name runs into the request and that
cut audio is hard to hear: on recordings of fast one-breath requests, Open WebUI got 5
of 11 right ("attempted." for "what time is it?"). So the request is also transcribed
locally by a bigger model (`small.en`, ~0.9 s, at the same time as Open WebUI) from the
whole burst, trailing quiet trimmed, which got 9 of 11. The local text wins unless Open
WebUI's says nearly the same thing (after repairing its first word or two), in which
case Open WebUI's spelling is used.
If you say the name, pause, and start the request before the chime, it skips the chime
and keeps listening rather than playing over you.

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
Weather lookups send only the place name (or `WILI_LOCATION`) and its coordinates
to Open-Meteo.
The voice is generated locally.

## Troubleshooting

- **`--check`** tests Open WebUI, the language model, speech to text, web search, weather, the board and the voice.
- **Ask it "are you online?"** for a spoken check of the server and the language model.
- **"The Wili board is already in use"** — another copy is running (e.g. the launch
  agent). `scripts/uninstall-launch-agent.sh` or `pkill -f wili_assistant.py`.
- **It mishears its name** — the log prints what the local model heard for every
  burst; add stubborn spellings to the pattern in `WakeName._pattern`.
- **Names come out wrong in answers** — add them to `WILI_VOCAB`.
- **A hang** — `kill -USR1 <pid>` prints every thread's stack to the log.

## Credits and licenses

TooGee's own code — the assistant, the board protocol, the TTS helper, the scripts
and `firmware/apps/wili` — is released under the [MIT License](LICENSE). Third-party
components keep their own licenses (summarised in [NOTICE](NOTICE)):

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
- Weather data by [Open-Meteo.com](https://open-meteo.com) (CC BY 4.0), free for
  non-commercial use.
