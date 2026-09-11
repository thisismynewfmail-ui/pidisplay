# Aperture Terminal

A self-contained chat terminal for a **Raspberry Pi 5** and a **20x4 I2C
character display**. A language model runs locally through
[llama.cpp](https://github.com/ggml-org/llama.cpp); a keyboard plugged into the
Pi drives it; nothing leaves the machine.

Eighty character cells is not much room, so the interface is built around that
constraint rather than in spite of it: a one-row instrument header, a three-row
transcript with a proportional scrollbar and a per-speaker rail, and a compose
line that appears the moment you type and gets out of the way when you stop.
Animation is done by rewriting the display's eight user-definable characters in
place, and only the cells that actually changed are sent to the panel.

```
 ┌────────────────────┐
 │◍ 12.4 tok/s ▓▓▒░38%│   state · readout · context gauge
 │▪Address 0x27      ▐│   machine turn, dotted rail
 │▪answered on the   ▐│   proportional scrollbar ──┘
 │>what responded?    │   compose line, hardware caret
 └────────────────────┘
```

---

## Contents

- [Hardware](#hardware)
- [Wiring](#wiring)
- [Install](#install)
- [Running it](#running-it)
- [The interface](#the-interface)
- [Keys](#keys)
- [Settings](#settings)
- [How it stays fast](#how-it-stays-fast)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

> **Nothing on the display?** Run `./run.sh --doctor`. It walks through the
> causes in order, asking only questions you answer by looking at the panel,
> and writes the answer into your settings. Start there rather than rewiring —
> the most common causes are contrast and the backpack's pin mapping, and
> neither is visible from the outside.

---

## Hardware

| Part | Notes |
| --- | --- |
| Raspberry Pi 5 (64-bit OS) | 8 GB recommended. A 4 GB Pi runs 1–3 B models comfortably. |
| Freenove I2C LCD2004 (or any HD44780 20x4 with a PCF8574 backpack) | The backpack is the small board soldered to the back with four pins. |
| Four female-to-female jumper wires | |
| A keyboard | Any USB keyboard, or a Bluetooth one. See [Keyboards](#keyboards). |
| microSD card with room for a model | A 3 B model at Q4\_K\_M is about 2 GB. |

The display is a standard HD44780 controller behind a PCF8574 I2C expander.
The backpack is strapped to address **0x27** or **0x3F** from the factory with
nothing printed on the board to say which; the program probes both, so you do
not need to know.

---

## Wiring

Four wires. The display connects to the I2C pins in the corner of the header.

```
                 Raspberry Pi 5 — 40-pin GPIO header
                       (pin 1 is nearest the SD card)

                         ┌─────┬─────┐
                3V3  (1) │  ○  │  ●  │ (2)  5V
      GPIO 2 / SDA  (3) ─┼──●  │  ●  │ (4)  5V ──────┐
      GPIO 3 / SCL  (5) ─┼──●  │  ●  │ (6)  GND ───┐ │
             GPIO 4  (7) │  ○  │  ○  │ (8)  GPIO14 │ │
                GND  (9) │  ○  │  ○  │ (10) GPIO15 │ │
                         │ ... │ ... │             │ │
                         └─────┴─────┘             │ │
                           │   │                   │ │
                           │   └───────────────┐   │ │
                           └───────────────┐   │   │ │
                                           │   │   │ │
                        ┌──────────────────┼───┼───┼─┼────┐
                        │  PCF8574 backpack│   │   │ │    │
                        │        ┌─────────┴───┴───┴─┴─┐  │
                        │        │ GND  VCC  SDA  SCL  │  │
                        │        └─────────────────────┘  │
                        │   ▣ contrast trimmer            │
                        └─────────────────────────────────┘
                                 (back of the LCD2004)
```

### Pin guide

| LCD pin | Wire to | Header pin | Signal |
| --- | --- | --- | --- |
| **GND** | Ground | **6** | 0 V — connect this first |
| **VCC** | 5 V | **4** | 5 V supply (pin 2 is equivalent) |
| **SDA** | GPIO 2 | **3** | I2C1 data |
| **SCL** | GPIO 3 | **5** | I2C1 clock |

Pins 3 and 5 are I2C bus 1 on every Raspberry Pi ever made, which is why the
program defaults to bus 1 and why you should not need to change it.

> **Get the ground on first, and check it twice.** A missing ground is the most
> common wiring fault here, and its symptom is misleading: the backlight comes
> on (it is powered through VCC) while the controller never responds, so the
> panel looks alive and `i2cdetect` finds nothing.

### A note on 5 V and the 3.3 V GPIO

This deserves a straight answer, because most guides skip it.

The HD44780 needs 5 V for a properly dark, readable contrast, so VCC goes to
5 V. But the PCF8574 backpack has its I2C pull-up resistors tied to **VCC** —
so with VCC at 5 V, the SDA and SCL lines idle at 5 V, and the Pi's GPIO pins
are rated for 3.3 V. Their protection diodes clamp the excess, which is why
this works in practice and why thousands of people do it. It is still outside
the Pi's specification and it does stress the SoC.

Three ways to handle it, in order of how correct they are:

1. **Bidirectional I2C level shifter** between the Pi and the backpack, with the
   backpack on 5 V. In specification, fully bright. The right answer if you are
   building something to keep.
2. **Remove the two pull-up resistors from the backpack** (usually marked `R1`
   and `R2`, or a small 4-pin resistor network) and keep VCC at 5 V. The Pi's
   own 1.8 kΩ pull-ups to 3.3 V then set the bus voltage, which is in
   specification. Requires a soldering iron and thirty seconds.
3. **Power the backpack from 3.3 V** (header pin 1) instead of 5 V. Everything
   is then in specification with no modification. The Freenove module is rated
   for 3.3–5 V and will work, but the display is noticeably dimmer and you will
   need to turn the contrast trimmer further. Try this first if you just want
   to see it work safely.

Connecting directly at 5 V is the fourth option, it is what most people do, and
it will very probably be fine. Now you know what you are choosing.

### Contrast

If the backlight is on but you see only a row of solid blocks, or nothing at
all, the contrast is wrong — this is not a fault. Turn the blue trimmer
potentiometer on the back of the backpack with a small screwdriver while
`./run.sh --self-test` is displayed. There is a narrow band where the
characters appear; go slowly. Trimmers are often shipped at one extreme and
can need fifteen or more turns to cross their range.

### The backpack's pin mapping

The backpack is eight expander outputs wired to the display's control and data
lines, and **which pin goes where is a property of the board, not of the
protocol**. Two layouts exist:

| Layout | Wiring | Seen on |
| --- | --- | --- |
| `standard` | `P0=RS P1=RW P2=E P3=LED P4..P7=D4..D7` | Almost everything, including Freenove |
| `ywrobot` | `P0..P3=D4..D7 P4=E P5=RW P6=RS P7=LED` | YwRobot and relabelled clones |

Driven with the wrong one, a module receives perfectly valid I2C traffic,
acknowledges every byte, and displays nothing — and the backlight may still
work, because that pin happens to be independent. It is the hardest fault here
to spot from software, which is why `--doctor` exists and why the mapping is a
setting (Settings → DISPLAY → `Pin map`, or `./run.sh --pinmap ywrobot`).

---

## Install

```bash
git clone <this-repository> pidisplay
cd pidisplay
./install.sh
```

The installer:

1. installs `i2c-tools`, `bluez`, `network-manager` and the Python tooling;
2. enables I2C and raises the bus from 100 kHz to **400 kHz** (the program's
   animation budget assumes the faster speed);
3. adds you to the `i2c`, `input` and `bluetooth` groups;
4. creates a virtualenv and installs `smbus2`, the only dependency;
5. offers to build llama.cpp from source (10–25 minutes on a Pi 5);
6. offers to install a systemd unit so it starts at boot.

Every step is idempotent — rerun it freely.

```bash
./install.sh --yes --with-llama --service   # unattended, everything
./install.sh --no-apt --no-llama            # offline, minimal
./install.sh --help
```

**Reboot after the first install.** Enabling I2C needs it, and so does the
group membership that lets the program read your keyboard.

Then put a GGUF model in `models/`:

```bash
# Any GGUF will do. For a Pi 5, a 1-4 B model at Q4_K_M is the sweet spot.
curl -L -o models/qwen2.5-3b-instruct-q4_k_m.gguf \
  https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf
```

| Model size | Quantisation | Roughly, on a Pi 5 |
| --- | --- | --- |
| 1–2 B | Q4\_K\_M | 8–15 tokens/second |
| 3–4 B | Q4\_K\_M | 4–7 tokens/second |
| 7–8 B | Q4\_K\_M | 2–3 tokens/second |

---

## Running it

```bash
./run.sh                # normal operation
./run.sh --probe        # what is on the I2C bus, which keyboards, which models
./run.sh --self-test    # a test pattern for checking wiring and contrast
./run.sh --doctor       # diagnose a panel that shows nothing
./run.sh --sim          # no hardware: mirror the panel in this terminal
```

`--sim` renders the panel at pixel resolution from a real HD44780 emulator fed
by the real driver, so custom glyphs, the blinking cursor and the backlight all
behave as they would on the hardware. It is how most of this was built.

### Startup order

The boot sequence is ordered by dependency, not decoration:

1. **Panel** — a self-test that exercises every user-defined character and both
   halves of the display's address map. If the panel is wrong, every later
   error message would be invisible, so this runs before anything can report.
2. **Input** — waits for a keyboard, and works to produce one: it reconnects
   trusted Bluetooth devices, and failing that discovers and pairs a Bluetooth
   keyboard *with no existing input device*, by showing the passkey on the panel
   for you to type on the keyboard being paired. That is the only bootstrap out
   of having no input at all, which is why it comes before the engine.
3. **Network** — reported, never waited on. A terminal with no network is a
   working terminal.
4. **Engine** — the slowest step, so it runs last, once you can already
   interact. If a llama.cpp server is already listening on the configured port
   it is adopted rather than duplicated.

Any key skips a stage. If the engine fails, `ENTER` retries, `F3` picks a
different model and `ESC` continues offline with the settings and diagnostics
still usable.

### Keyboards

Any keyboard attached to the Pi works, with no desktop and no terminal focus:
the program reads `/dev/input/event*` directly and takes an exclusive grab, so
keystrokes reach the chat and **not** a login shell on the console behind it.
Keyboards may be plugged and unplugged at any time.

Running over SSH works too — stdin is read in parallel and merged into the same
event stream, so you can drive the panel from a laptop while it sits on a bench.

To pair a Bluetooth keyboard from the menus: `F7` → `INPUT` → `Bluetooth`.

---

## The interface

```
 col 0                                    col 19
  ┌─┬──────────────────────────────────┬─┐
  │◍│ 12.4 tok/s            ▓▓▒░   38% │ │  row 0  status
  ├─┼──────────────────────────────────┼─┤
  │▪│ Address 0x27 answered            │▐│  row 1  ┐
  │▪│ on the first attempt.            │▐│  row 2  ├ transcript
  │>│ what responded?_                 │ │  row 3  ┘ or compose
  └─┴──────────────────────────────────┴─┘
   │                                     └── proportional scrollbar
   └── speaker rail
```

**Row 0** is the instrument header. The leftmost character is an iris that sits
shut while idle and breathes while the model works — readable across a bench
without reading a word. Beside it is a context-sensitive readout (model name,
live tokens per second, elapsed prompt time, scroll position), then a
half-cell-resolution gauge and percentage for how full the context window is.

**Column 0** identifies the speaker without spending a whole row on a header
line: `>` opens your turn with a solid rail beneath it, a dotted rail marks the
machine's, `~` marks reasoning, `!` marks an error.

**Column 19** is a scrollbar, present only when there is more than fits. It is
drawn from three redefinable characters giving 24 pixel rows of resolution, so
the thumb creeps rather than jumping.

**Row 3** is shared. It shows transcript until you type a character, at which
point it becomes the compose line and stays there until you send or press
`ESC`. Reading is what you do most, so three rows of transcript is the resting
state. The caret is the display controller's own hardware cursor — it blinks on
the panel's clock and cannot tear against a streaming redraw.

While a reply is being produced, an activity meter appears in the transcript
exactly where the text will land, labelled with what is actually happening
(`READING PROMPT`, `COMPOSING`, `REASONING`) and how long it has taken.

---

## Keys

`F7` opens settings. `F4` does the same thing — some compact keyboards put F4
where a full-size layout puts F7, and a settings menu you cannot reach is worse
than a duplicated binding.

<!-- generated:keymap -->
| Key | Action |
| --- | --- |
| `TYPE` | compose a message |
| `ENTER` | send / open compose |
| `ESC` | halt reply, clear line, or leave compose |
| `UP DOWN` | scroll transcript |
| `PGUP PGDN` | scroll by a page |
| `HOME END` | jump to start / resume following |
| `LEFT RIGHT` | move the caret |
| `F1` | this help |
| `F2` | clear conversation |
| `F3` | choose model |
| `F5` | regenerate last reply |
| `F6` | diagnostics |
| `F7` | settings (F4 also works) |
| `F8` | show or hide reasoning |
| `F9` | export transcript |
| `F10` | shut down |
| `F12` | backlight |
| `CTRL+U` | clear the line |
| `CTRL+W` | delete the previous word |
| `CTRL+A/E` | start / end of line |
| `CTRL+L` | force a full repaint |
| `CTRL+C` | quit immediately |
<!-- /generated:keymap -->

In menus: arrows move, `ENTER` selects, `ESC` goes back, `F1` explains the
selected item. On a numeric setting, `ENTER` enters adjust mode where `LEFT`
and `RIGHT` step the value and `UP`/`DOWN` take larger steps; `+` and `-`
(including the keypad keys) work without entering adjust mode at all.

---

## Settings

Settings are stored in `~/.config/aperture/config.json`, written atomically,
and revalidated on load — a corrupt or hand-edited file falls back to defaults
and says so rather than refusing to start.

**Context** is the headline control and moves in **512-token steps** in both
directions, from 512 up to 131072. Raising it past what the loaded model was
actually trained for is refused, not silently allowed: a model run beyond its
trained window still works and quietly produces worse output, which is the
hardest kind of fault to attribute.

<!-- generated:settings -->
| Section | Setting | Default | Range | Notes |
| --- | --- | --- | --- | --- |
| INFERENCE | Context | `4K` | 512 to 131072, step 512 | KV cache window in tokens. Larger holds more conversation but costs memory and slows the first pass. Restarts the engine. |
| INFERENCE | Model | — | action | Choose a GGUF file from the models directory. |
| INFERENCE | Engine | `LOCAL` | LOCAL / REMOTE | LOCAL starts and supervises llama-server on this machine. REMOTE attaches to a llama.cpp server already running. Restarts the engine. |
| INFERENCE | Threads | `4` | 0 to 64, step 1 | Generation threads. The Pi 5 has four Cortex-A76 cores; above four the cores contend and throughput drops. Restarts the engine. |
| INFERENCE | Batch | `256` | 32 to 4096, step 32 | Prompt-ingest batch size. Larger is faster to first token but uses more scratch memory. Restarts the engine. |
| INFERENCE | GPU layers | `CPU` | -1 to 999, step 1 | Layers offloaded to the GPU. The Pi 5 has no supported accelerator, so 0 is correct here; kept for other hosts. Restarts the engine. |
| INFERENCE | Cache reuse | `256` | 0 to 4096, step 64 | Minimum chunk the server will salvage from the KV cache after an edit. 0 disables partial reuse. Restarts the engine. |
| INFERENCE | Flash attn | `ON` | on / off | Use the fused attention kernel when the build supports it. Lower memory, usually faster. Restarts the engine. |
| INFERENCE | Lock in RAM | `OFF` | on / off | mlock the weights so Linux cannot swap them out. Only safe when the model comfortably fits in memory. Restarts the engine. |
| INFERENCE | Prefill | `ON` | on / off | After each reply, pre-ingest the conversation so the next turn only evaluates what you actually typed. |
| INFERENCE | Type-ahead | `OFF` | on / off | Also pre-ingest your partial line while you pause typing. Fastest replies, but keeps a core busy as you type. |
| INFERENCE | Restart eng | — | action | Stop and relaunch llama-server with current settings. |
| ENDPOINT | Host | — | action | Address of the llama.cpp server. |
| ENDPOINT | Port | `8080` | 1 to 65535, step 1 | TCP port of the llama.cpp server. Restarts the engine. |
| ENDPOINT | API key | — | action | Bearer token, if the server requires one. |
| ENDPOINT | Timeout | `600s` | 10 to 3600, step 10 | Seconds to wait on a stalled response before giving up. |
| SAMPLING | Temperature | `0.7` | 0 to 2, step 0.05 | Higher is more varied, lower is more deterministic. |
| SAMPLING | Top P | `0.95` | 0.05 to 1, step 0.05 | Nucleus sampling cutoff. |
| SAMPLING | Top K | `40` | 0 to 200, step 5 | Consider only this many candidates. 0 disables the filter. |
| SAMPLING | Repeat pen | `1.1` | 1 to 2, step 0.02 | Discourages verbatim repetition. 1.0 is off. |
| SAMPLING | Max reply | `512` | 32 to 8192, step 32 | Hard ceiling on one reply. |
| SAMPLING | Seed | `RANDOM` | -1 to 2.14748e+09, step 1 | -1 draws a fresh seed each turn; any other value makes replies reproducible. |
| CONVERSATION | Persona | `TERMINAL` | TERMINAL / TERSE / TECHNICAL / PLAIN / CUSTOM | System prompt. All of them instruct the model to write for a twenty-column display. |
| CONVERSATION | Edit prompt | — | action | Write a custom system prompt. |
| CONVERSATION | Reply room | `640` | 128 to 4096, step 64 | Tokens held back from the context window so a reply always has somewhere to go. |
| CONVERSATION | Show think | `OFF` | on / off | Display a reasoning model's internal notes as they stream. They are summarised on the status row either way. |
| CONVERSATION | Autoscroll | `ON` | on / off | Follow the newest line while a reply streams. Scrolling up by hand suspends it until you return to the bottom. |
| CONVERSATION | Stream fps | `12` | 2 to 30, step 1 | Upper bound on display refreshes per second. The bus, not the model, is the limit here. |
| CONVERSATION | Keep log | `ON` | on / off | Append each exchange to a transcript file under the state directory. |
| DISPLAY | Backlight | `ON` | on / off | Panel backlight. |
| DISPLAY | Pin map | `STANDARD` | STANDARD / YWROBOT / STANDARD-INV / YWROBOT-INV | How the backpack wires the expander to the display. Two layouts exist; the wrong one shows nothing at all. Run ./run.sh --doctor to find yours. Restarts the engine. |
| DISPLAY | Dim after | `NEVER` | 0 to 3600, step 30 | Switch the backlight off after this long with no keystroke. NEVER keeps it on. |
| DISPLAY | Refresh | `20` | 5 to 40, step 1 | Render loop target. Frames with nothing to redraw cost nothing, so this is a ceiling rather than a load. |
| DISPLAY | I2C bus | `1` | 0 to 20, step 1 | Bus number. Header pins 3 and 5 are bus 1 on every Pi. Restarts the engine. |
| DISPLAY | I2C addr | `AUTO` | 0 to 127, step 1 | Backpack address. AUTO probes 0x27 and 0x3F, which is where these modules ship. Restarts the engine. |
| INPUT | Grab keys | `ON` | on / off | Claim keyboards exclusively so keystrokes do not also reach a login shell on the console behind this program. |
| INPUT | Key repeat | `ON` | on / off | Let held arrow keys repeat for menu navigation. |
| INPUT | Bluetooth | — | action | Pair or reconnect a Bluetooth keyboard. |
| NETWORK | Wi-Fi | — | action | Join a wireless network. |
| NETWORK | Address | — | action | This machine's current IP address. |
| SYSTEM | Save now | — | action | Write settings to disk immediately. |
| SYSTEM | Defaults | — | action | Restore every setting to its default. |
| SYSTEM | Exit | — | action | Stop the terminal and return to a shell. |
<!-- /generated:settings -->

---

## How it stays fast

The Pi 5 is not a fast inference host, so the design spends its effort on the
things that are not the model's fault.

**One server, kept alive.** `llama-server` is started once and supervised for
the life of the program. Starting a process per message would re-map the
weights and re-read the whole conversation every time.

**The prompt cache is never invalidated needlessly.** Every request goes to the
native `/completion` endpoint with `cache_prompt` set and a fixed `id_slot`, so
the server compares the new prompt against the tokens that slot already holds
and evaluates only the divergent tail. Because a conversation grows by
appending, that tail is just your newest message — turn twenty costs the same
prompt work as turn two. Pinning one slot is what keeps this true; letting the
server assign slots round-robin would scatter the conversation and re-read it
each time. The chat template is rendered server-side through `/apply-template`,
so this works with any GGUF without guessing at its format.

Measured against the test suite's stand-in server, prompt tokens evaluated per
turn across a four-turn conversation: **14, 15, 15, 15** — against a prompt
that grew to 345 cached tokens.

**Trimming is rare and deep.** Dropping old turns changes the prompt's prefix
and therefore throws the cache away. So when the window fills, the oldest
exchanges are dropped back to roughly 60% occupancy in one go rather than one
exchange at a time, and you are told it happened.

**The cache is warmed ahead of you.** The system prompt is evaluated during
startup, so your first message pays only for your own tokens. `Type-ahead`
(off by default) goes further and evaluates the line you are typing during
pauses, since it is a prefix of what you will send.

**The panel is redrawn differentially.** A full 20x4 repaint is about 11 ms at
400 kHz; a typical frame changes a handful of cells and costs under 2 ms.
Animation is done by rewriting a character's bitmap in its CGRAM slot — nine
controller writes, about the same as drawing nine characters — rather than by
cycling through several characters. An idle frame costs one buffer comparison
and no bus traffic at all.

`F6` shows all of this live: tokens evaluated versus reused, cache hit ratio,
time to first token, and the panel's own bytes-per-frame.

---

## Troubleshooting

### The display shows nothing

```bash
./run.sh --doctor
```

This is the tool for it. It bisects the causes rather than guessing, using one
fact: of the eight expander pins, **the backlight is the only one whose effect
needs no part of the display protocol** — no four-bit handshake, no enable
timing, no register select, no contrast. Writing one byte toggles it. So:

- **the backlight responds** → the bus, address, wiring, power and ground are
  all proven good, and the fault is above the bus: pin mapping, or contrast.
  It then works out which.
- **the backlight does not** → the fault is at or below the bus, and no
  protocol work will help. It says what is left and stops.

It writes the bus, address and pin mapping it finds into your settings.

### Reading the self-test

`./run.sh --self-test` draws a pattern designed so each fault looks *different*
rather than all of them looking blank:

| What you see | What it means |
| --- | --- |
| Nothing, backlight on | Contrast, or the pin mapping. Run `--doctor`. |
| Nothing, backlight off too | Power, ground, or address. Run `--doctor`. |
| Solid blocks on every row | Contrast turned too far up. |
| Only rows 1 and 3 | It is a 16x2 panel, not 20x4. |
| Rows in the order 1, 3, 2, 4 | The row address map is wrong for this panel. |
| Text, but wrong characters | Bus too fast. Set `dtparam=i2c_arm_baudrate=100000`. |

### Everything else

`./run.sh --probe` reports every I2C bus and what answers on each, every
keyboard and whether it is readable, where `llama-server` is, and which models
were found. On a Pi 5 it scans *all* buses, not just bus 1 — which bus the
header lands on has moved between kernel releases.

| Symptom | Cause and fix |
| --- | --- |
| `no /dev/i2c-* devices` | I2C is not enabled. `sudo raspi-config nonint do_i2c 0`, then reboot. |
| Nothing responds on any bus | Wiring. Ground first (pin 6), then check SDA and SCL are not swapped. |
| `i2cdetect` sees it but this does not | Report it — that is a bug. Include `./run.sh --probe` output. |
| Garbled or drifting characters | Loose jumper, or a long cable at 400 kHz. Reseat; if it persists set `dtparam=i2c_arm_baudrate=100000`. |
| `permission denied` on input devices | Not in the `input` group yet. `sudo usermod -aG input $USER`, then log out and back in. |
| Keys reach a login shell as well | The exclusive grab failed. Settings → INPUT → `Grab keys`, and check `F6` → `INPUT`. |
| Bluetooth keyboard will not pair | Put it in pairing mode *first*, then `F7` → INPUT → Bluetooth. Type the passkey shown on the panel on that keyboard, then ENTER. |
| `NO llama-server` | Not built. `./install.sh --with-llama`, or set the path in Settings → ENDPOINT. |
| `NO MODEL FOUND` | No `.gguf` in `models/`. Drop one in and press `ENTER`. |
| `OUT OF MEMORY` on startup | Model too large, or context too high. Lower Context (it moves in 512s) or use a smaller quantisation. |
| Engine times out loading | Normal for a large model on a cold page cache. `F6` → `ENGINE LOG` shows progress. |
| Replies slow but correct | Check `F6` → `HOST` for `POWER UNDERVOLT`. An inadequate supply throttles the Pi hard. |
| Replies arrive as markdown | Small models drift despite the persona. Settings → CONVERSATION → `Persona` → `TERSE`. |

---

## Development

```bash
python3 -m unittest discover -s tests -v     # the whole suite
./run.sh --sim                               # drive the UI with no hardware
python3 tools/fake_llama.py --port 8080      # a stand-in inference server
python3 tools/gen_docs.py                    # regenerate the tables above
```

The test suite runs the real driver, the real screens and the real streaming
client against an HD44780 emulator that decodes actual enable-pin edges and
four-bit nibble pairs. Nothing above the transport is mocked, so a passing run
means the panel would show what the tests assert. It covers the controller's
address map, differential rendering cost, the equivalence of streamed and
batch word wrapping under randomly chunked input, keyboard decoding on both
input paths, GGUF parsing, and the prompt-cache behaviour end to end.

The emulator decodes through a pin map rather than fixed bit masks, so a test
can drive one wiring and decode with another — which is how the "valid traffic,
blank panel" failure is asserted to be real rather than assumed. `--doctor`
itself is tested against a simulated module of each wiring, with a stand-in
operator answering from what the emulated panel would be showing, because a
diagnostic that reaches the wrong conclusion is worse than none.

```
aperture/
  hal/          panel driver, framebuffer, glyph engine, keyboards
    emulator.py an HD44780 faithful enough to develop against
  llm/          llama.cpp supervision, streaming client, conversation state
  services/     Wi-Fi, Bluetooth, host telemetry
  ui/           screens, widgets, text layout, the render loop
  config.py     the typed schema that drives both validation and the menus
tools/          simulator harness, stand-in server, doc generator
tests/          unit and end-to-end tests
```

The settings schema in `config.py` is the single source of truth: the
validator, the on-screen menu and the table in this README are all generated
from it. Adding a setting means adding one `Setting(...)` and one row in
`ui/settings.py`.

---

## Licence

MIT. llama.cpp is MIT and is built separately by the installer; model weights
carry their own licences.
