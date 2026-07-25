"""Status Buddy for the M5 Cardputer-Adv.

Display-only companion to `claude_buddy.py`. Where that app pairs with
Claude Desktop's Hardware Buddy for tool-call approve/deny, this app
mirrors the *other* Claude Buddy — the terminal companion from the
`claude-buddy` Claude Code plugin (github.com/ramarivera/claude-buddy),
which lives entirely on the host machine as local JSON state files and
shell hooks, with no BLE of its own.

A host-side bridge script (bleak, in the claude-buddy repo) watches
that plugin's `status.json` and pushes a JSON line over BLE any time it
changes. This app just renders whatever it last received — name,
species, level, mood, current reaction. There is no approve/deny
protocol here, so we reuse `buddy_ble.BuddyBLE` directly for the
connection/advertising plumbing but skip `buddy_protocol.py` entirely.

### Wire format

One JSON object per line (NUS line-framing, same as the rest of the
buddy_ble family):

    {"name":"Waffle","species":"axolotl","rarity":"common",
     "reaction":"*happy gill flutter* shipped!","level":1,"xp":0,
     "mood":"happy"}

Unknown/missing fields are tolerated — the renderer falls back to
placeholders so a partial or out-of-order payload doesn't crash the
app.

### Naming

We advertise as "Status_XXXXXX" (name_prefix="Status") rather than
"Claude_XXXXXX" so a BLE scanner — or a human eyeballing one — can
tell this apart from the Hardware Buddy peripheral `claude_buddy.py`
advertises. They still share the same NUS service/characteristic UUIDs
(buddy_ble.py hard-codes those at module scope), which is fine: only
one app runs on the device at a time, and the BLE stack singleton
resets across the machine.reset() launcher boundary between app runs.
"""

import sys

for _p in ("/flash", "/flash/apps"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import json
import time

import M5
import machine
from hardware import MatrixKeyboard

import buddy_ble


_BLACK = 0x000000
_ORANGE = 0xCC785C
_CREAM = 0xF0EEE6
_DARK = 0x1F1F1F
_GRAY_MID = 0x777777
_GREEN = 0x6BAA75

_LCD = M5.Lcd
_W = 240
_H = 135

# Wrapped reaction text tops out around here — 3 lines at DejaVu9 fits
# the content area between the identity block and the footer without
# crowding either.
_REACTION_MAX_LINES = 3


def _set_font():
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu9)
    except Exception as e:
        print("status_buddy: setFont fallback:", e)


def _wrap(text, max_width_px, max_lines):
    """Greedy word-wrap using textWidth for accurate proportional-font
    measurement (naive char-count estimates are unreliable on this
    build — see buddy_ui_cp's measurement notes)."""
    words = text.split()
    lines = []
    cur = ""
    for w in words:
        cand = (cur + " " + w) if cur else w
        if _LCD.textWidth(cand) <= max_width_px or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = w
            if len(lines) == max_lines - 1:
                break
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    return lines


def _draw_chrome():
    _LCD.fillScreen(_BLACK)
    _LCD.fillRect(0, 0, _W, 20, _DARK)
    _LCD.fillRect(0, 20, _W, 1, _ORANGE)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_ORANGE, _DARK)
    _LCD.drawString("Status Buddy", 6, 5)

    _LCD.fillRect(0, _H - 18, _W, 18, _DARK)
    _LCD.setTextColor(_GRAY_MID, _DARK)
    hint = "Q/ESC  back to menu"
    _LCD.drawString(hint, (_W - _LCD.textWidth(hint)) // 2, _H - 14)


def _draw_conn(state_label):
    color = _GREEN if state_label == "linked" else _GRAY_MID
    _LCD.fillRect(_W - 70, 4, 66, 12, _DARK)
    _LCD.setTextSize(1)
    _LCD.setTextColor(color, _DARK)
    _LCD.drawString(state_label, _W - 68, 5)


def _draw_body(state):
    # Blank the content area (below header, above footer) each redraw.
    # Same tradeoff hello_cardputer.py makes: full repaint is cheap
    # enough at this size and avoids stale-glyph diffing bugs.
    body_y = 24
    body_h = _H - 18 - body_y
    _LCD.fillRect(0, body_y, _W, body_h, _BLACK)

    name = state.get("name") or "???"
    species = state.get("species") or "unknown"
    rarity = state.get("rarity") or ""
    level = state.get("level")
    mood = state.get("mood") or ""
    reaction = state.get("reaction") or ""

    _LCD.setTextSize(2)
    _LCD.setTextColor(_CREAM, _BLACK)
    _LCD.drawString(name, 8, body_y + 4)

    _LCD.setTextSize(1)
    _LCD.setTextColor(_GRAY_MID, _BLACK)
    meta_bits = [b for b in (rarity, species) if b]
    meta = " ".join(meta_bits)
    if level is not None:
        meta += "  ·  Lv {}".format(level)
    if mood:
        meta += "  ·  {}".format(mood)
    _LCD.drawString(meta, 8, body_y + 26)

    if reaction:
        _LCD.setTextColor(_ORANGE, _BLACK)
        lines = _wrap(reaction, _W - 16, _REACTION_MAX_LINES)
        for i, line in enumerate(lines):
            _LCD.drawString(line, 8, body_y + 44 + i * 12)


def _is_exit(k):
    if k is None:
        return False
    if isinstance(k, int):
        if k == 0x1B:
            return True
        if 0x20 <= k <= 0x7E:
            k = chr(k)
        else:
            return False
    if isinstance(k, str) and k:
        return k.lower() == "q"
    return False


def run():
    print("status_buddy: run() start")
    _set_font()
    _draw_chrome()

    state = {}
    _draw_body(state)
    _draw_conn("waiting")

    # Deferred-draw pattern from claude_buddy.py: BLE IRQ context must
    # not touch the LCD directly (mid-SPI-transaction risk if a
    # callback interrupts a UI routine already writing to the panel),
    # so callbacks only stash data and the main loop drains it.
    pending_state = [None]
    pending_line = [None]

    def on_state(s):
        pending_state[0] = s

    def on_line(raw):
        pending_line[0] = raw

    ble = buddy_ble.BuddyBLE(
        name_prefix="Status",
        on_line=on_line,
        on_state=on_state,
    )
    print("Status Buddy up as", ble.advertised_name)

    kb = MatrixKeyboard()
    time.sleep_ms(400)

    try:
        while True:
            new_state = pending_state[0]
            if new_state is not None:
                pending_state[0] = None
                label = "linked" if new_state in ("connected", "encrypted") else "waiting"
                _draw_conn(label)

            new_line = pending_line[0]
            if new_line is not None:
                pending_line[0] = None
                try:
                    payload = json.loads(new_line)
                    if isinstance(payload, dict):
                        state.update(payload)
                        _draw_body(state)
                except Exception as e:
                    # Malformed/partial JSON from a mid-reconnect bridge
                    # write shouldn't crash the app — just drop the line
                    # and wait for the next one.
                    print("status_buddy: bad line:", e)

            kb.tick()
            if _is_exit(kb.get_key()):
                return

            time.sleep_ms(40)
    finally:
        try:
            ble.deinit()
        except Exception as e:
            print("status_buddy: deinit warning:", e)
        try:
            M5.Lcd.fillScreen(_BLACK)
        except Exception as e:
            print("status_buddy: screen-clear warning:", e)
        time.sleep_ms(200)
        machine.reset()


run()
