"""Hermes Buddy — type to chat with your local Hermes Agent.

Text-only sibling of push_to_claude.py: no mic/Whisper, just typed
prompts POSTed to hermes-agent's built-in OpenAI-compatible API
server (``gateway/platforms/api_server.py``), reached over the LAN
since Hermes runs locally on the host machine rather than behind a
public Cloudflare Worker.

State machine:
  TYPING     → Enter      → UPLOADING
  UPLOADING  → reply      → SHOWING
  UPLOADING  → error      → ERROR
  SHOWING    → any key    → TYPING
  ERROR      → any key    → TYPING
  any        → Q / ESC    → exit (machine.reset)

Session continuity: a random session id is generated once per app
launch and sent as ``X-Hermes-Session-Id`` on every request, so
follow-up questions keep context within one run of the app. Pressing
``N`` mints a fresh session id (the closest equivalent to
push_to_claude's server-side ``/reset`` — hermes-agent's API server
has no reset endpoint, but a new session id starts clean since
nothing has been said under it yet).
"""

import gc
import time

import M5
import machine
from hardware import MatrixKeyboard


# ---- DEPLOYMENT-SPECIFIC CONSTANTS ----------------------------------
# Loaded from buddy/device/apps/config.py at runtime — same file
# push_to_claude.py reads, just different keys. Copy config.example.py
# to config.py and fill in HERMES_BASE + HERMES_API_KEY. See
# worker/README.md's Hermes Buddy section (or the setup conversation
# that created this file) for how those get set on the Hermes side.
try:
    from . import config as _cfg  # type: ignore
except Exception:
    try:
        import config as _cfg  # type: ignore
    except Exception:
        _cfg = None

_HERMES_BASE = (getattr(_cfg, "HERMES_BASE", "") if _cfg else "").rstrip("/")
_HERMES_API_KEY = getattr(_cfg, "HERMES_API_KEY", "") if _cfg else ""
_CHAT_URL = _HERMES_BASE + "/chat/completions"
# ---------------------------------------------------------------------


_BLACK = 0x000000
_ORANGE = 0xCC785C
_CREAM = 0xF0EEE6
_DARK = 0x1F1F1F
_GRAY_MID = 0x777777
_RED = 0xFF0000

_LCD = M5.Lcd
_W = 240
_H = 135


# ---- UI HELPERS (same conventions as push_to_claude.py) -------------

def _set_font():
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu9)
    except Exception as e:
        print("hermes: setFont fallback:", e)


def _draw_chrome(title="Hermes Buddy", hint="Enter send  N new  Q back"):
    _LCD.fillScreen(_BLACK)
    _LCD.fillRect(0, 0, _W, 20, _DARK)
    _LCD.fillRect(0, 20, _W, 1, _ORANGE)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_ORANGE, _DARK)
    _LCD.drawString(title, 6, 5)

    _LCD.fillRect(0, _H - 18, _W, 18, _DARK)
    _LCD.setTextColor(_GRAY_MID, _DARK)
    _LCD.drawString(hint, (_W - _LCD.textWidth(hint)) // 2, _H - 14)


def _wrap_lines(text, max_w_px, char_size=1):
    _LCD.setTextSize(char_size)
    words = (text or "").split()
    lines = []
    cur = ""
    for w in words:
        cand = w if not cur else cur + " " + w
        if _LCD.textWidth(cand) <= max_w_px:
            cur = cand
        else:
            if cur:
                lines.append(cur)
            cur = w
            while _LCD.textWidth(cur) > max_w_px and len(cur) > 1:
                cut = len(cur) - 1
                while cut > 1 and _LCD.textWidth(cur[:cut]) > max_w_px:
                    cut -= 1
                lines.append(cur[:cut])
                cur = cur[cut:]
    if cur:
        lines.append(cur)
    return lines


def _draw_typing(buf, cursor_on):
    _draw_chrome(hint="Enter send  N new  Q back")
    _LCD.setTextSize(1)
    _LCD.setTextColor(_GRAY_MID, _BLACK)
    _LCD.drawString("> ", 6, 28)
    _LCD.setTextColor(_CREAM, _BLACK)

    lines = _wrap_lines(buf or " ", _W - 24, 1) or [""]
    if len(lines) > 5:
        lines = lines[-5:]
    y = 28
    for line in lines:
        _LCD.fillRect(18, y, _W - 24, 12, _BLACK)
        _LCD.drawString(line, 18, y)
        y += 12

    last_line = lines[-1] if lines else ""
    cur_x = 18 + _LCD.textWidth(last_line)
    cur_y = y - 12
    if cursor_on:
        _LCD.fillRect(cur_x, cur_y + 1, 6, 10, _ORANGE)
    else:
        _LCD.fillRect(cur_x, cur_y + 1, 6, 10, _BLACK)


def _draw_uploading(stage="thinking", detail="asking Hermes"):
    _LCD.fillRect(0, 21, _W, _H - 21 - 18, _BLACK)
    _LCD.setTextSize(2)
    _LCD.setTextColor(_ORANGE, _BLACK)
    _LCD.drawString(stage, (_W - _LCD.textWidth(stage)) // 2, 50)
    if detail:
        _LCD.setTextSize(1)
        _LCD.setTextColor(_GRAY_MID, _BLACK)
        _LCD.drawString(detail, (_W - _LCD.textWidth(detail)) // 2, 80)


def _result_layout(prompt, response):
    _LCD.setTextSize(1)
    p_lines = _wrap_lines("you: " + (prompt or ""), _W - 12, 1)[:2]
    response_y = 24 + len(p_lines) * 12 + 10
    max_visible = max(1, (_H - 18 - response_y) // 12)
    r_lines = _wrap_lines(response or "(empty)", _W - 12, 1)
    return p_lines, r_lines, response_y, max_visible


def _draw_result(prompt, response, scroll=0):
    p_lines, r_lines, response_y, max_visible = _result_layout(prompt, response)
    can_scroll = len(r_lines) > max_visible
    hint = "any key: new msg  N new chat  Q back"
    if can_scroll:
        hint = "; . scroll  N new  Q back"
    _draw_chrome(hint=hint)

    _LCD.setTextSize(1)
    _LCD.setTextColor(_GRAY_MID, _BLACK)
    y = 24
    for line in p_lines:
        _LCD.drawString(line, 6, y)
        y += 12
    _LCD.fillRect(6, y + 2, _W - 12, 1, _DARK)

    _LCD.setTextColor(_CREAM, _BLACK)
    visible = r_lines[scroll:scroll + max_visible]
    y = response_y
    for line in visible:
        _LCD.drawString(line, 6, y)
        y += 12

    if can_scroll:
        if scroll > 0:
            _LCD.fillTriangle(
                _W - 8, response_y + 2, _W - 2, response_y + 2,
                _W - 5, response_y - 3, _ORANGE,
            )
        if scroll + max_visible < len(r_lines):
            bottom_y = response_y + (len(visible) - 1) * 12
            _LCD.fillTriangle(
                _W - 8, bottom_y + 6, _W - 2, bottom_y + 6,
                _W - 5, bottom_y + 11, _ORANGE,
            )


def _draw_error(msg):
    _draw_chrome(hint="any key: retry  Q/ESC back")
    _LCD.setTextSize(1)
    _LCD.setTextColor(_RED, _BLACK)
    _LCD.drawString("Error", 6, 28)
    _LCD.setTextColor(_CREAM, _BLACK)
    for i, line in enumerate(_wrap_lines(msg, _W - 12, 1)[:6]):
        _LCD.drawString(line, 6, 46 + i * 12)


# ---- KEY HELPERS (same as push_to_claude.py) -------------------------

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
    return isinstance(k, str) and k and k.lower() == "q"


def _is_new_chat(k):
    if k is None:
        return False
    if isinstance(k, int):
        if 0x20 <= k <= 0x7E:
            k = chr(k)
        else:
            return False
    return isinstance(k, str) and k.lower() == "n"


def _is_enter(k):
    if k is None:
        return False
    if isinstance(k, int) and k in (0x0A, 0x0D):
        return True
    return isinstance(k, str) and k in ("\r", "\n")


def _is_backspace(k):
    if k is None:
        return False
    if isinstance(k, int) and k in (0x08, 0x7F):
        return True
    return isinstance(k, str) and k in ("\b", "\x7f")


def _scroll_intent(k):
    if k is None:
        return None
    if isinstance(k, int):
        if 0x20 <= k <= 0x7E:
            k = chr(k)
        else:
            return None
    if not isinstance(k, str) or not k:
        return None
    ch = k.lower()
    if ch in (";", ","):
        return "up"
    if ch in (".", "/"):
        return "down"
    return None


def _printable_char(k):
    if k is None:
        return None
    if isinstance(k, int):
        if 0x20 <= k <= 0x7E:
            return chr(k)
        return None
    if isinstance(k, str) and k and 0x20 <= ord(k[0]) <= 0x7E:
        return k[0]
    return None


# ---- NETWORK ----------------------------------------------------------

def _ensure_wifi():
    try:
        import network
        sta = network.WLAN(network.STA_IF)
        if not sta.active():
            sta.active(True)
        if sta.isconnected():
            return True
        try:
            import wifi_event
            res = wifi_event.connect()
            return bool(res.get("ok"))
        except Exception as e:
            print("hermes: wifi_event err:", e)
            return False
    except Exception as e:
        print("hermes: ensure_wifi err:", e)
        return False


def _new_session_id():
    # No os.urandom dependency — ticks + a free-running counter is
    # plenty unique for a per-launch/per-"N" chat-session label; this
    # is a continuity key, not a security token.
    return "cardputer-{}".format(time.ticks_ms())


def _free_internal_ram():
    # Same rationale as push_to_claude.py: NimBLE holds internal RAM
    # the TLS/HTTP client wants during the request. This app talks
    # plain HTTP to a LAN host (no TLS handshake), so the pressure is
    # lower, but dropping BLE here is cheap and keeps headroom
    # consistent with the rest of the bundle.
    try:
        import bluetooth
        ble = bluetooth.BLE()
        if ble.active():
            ble.active(False)
    except Exception as e:
        print("hermes: ble teardown warn:", e)
    gc.collect()
    gc.collect()


def _post_chat(prompt, session_id):
    """POST to hermes-agent's OpenAI-compatible /chat/completions.
    Returns the assistant's reply text. Raises on any failure."""
    _free_internal_ram()
    import json as _json

    body = _json.dumps({
        "model": "hermes-agent",
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    gc.collect()

    import requests
    headers = {
        "content-type": "application/json",
        "authorization": "Bearer " + _HERMES_API_KEY,
        "x-hermes-session-id": session_id,
    }
    r = requests.post(_CHAT_URL, data=body, headers=headers, timeout=90)
    try:
        if r.status_code != 200:
            raise RuntimeError(
                "hermes {}: {}".format(r.status_code, r.text[:160]),
            )
        data = r.json()
        return data["choices"][0]["message"]["content"]
    finally:
        try:
            r.close()
        except Exception:
            pass


# ---- MAIN -------------------------------------------------------------

def run():
    _set_font()
    if not _HERMES_BASE or not _HERMES_API_KEY:
        _draw_error(
            "Not configured.\n"
            "Add HERMES_BASE +\nHERMES_API_KEY to\n"
            "apps/config.py."
        )
        kb = MatrixKeyboard()
        while True:
            kb.tick()
            if _is_exit(kb.get_key()):
                return
            time.sleep_ms(50)

    _ensure_wifi()
    kb = MatrixKeyboard()
    time.sleep_ms(400)

    state = "typing"
    text_buf = ""
    cursor_on = True
    last_blink_ms = time.ticks_ms()
    last_prompt = ""
    last_response = ""
    scroll = 0
    session_id = _new_session_id()
    _draw_typing(text_buf, cursor_on)

    try:
        while True:
            kb.tick()
            k = kb.get_key()

            if state != "typing" and _is_exit(k):
                return

            if state == "typing":
                if k is not None and isinstance(k, int) and k == 0x1B:
                    return
                elif _is_new_chat(k):
                    session_id = _new_session_id()
                    text_buf = ""
                    _draw_chrome()
                    _LCD.setTextSize(1)
                    _LCD.setTextColor(_ORANGE, _BLACK)
                    msg = "new chat started"
                    _LCD.drawString(msg, (_W - _LCD.textWidth(msg)) // 2, 60)
                    time.sleep_ms(700)
                    _draw_typing(text_buf, cursor_on)
                elif _is_enter(k):
                    if text_buf.strip():
                        prompt = text_buf.strip()
                        state = "uploading"
                        _draw_uploading()
                        try:
                            reply = _post_chat(prompt, session_id)
                            last_prompt = prompt
                            last_response = reply
                            scroll = 0
                            state = "showing"
                            _draw_result(last_prompt, last_response, scroll)
                        except Exception as e:
                            msg = str(e)[:200]
                            print("hermes: post err:", msg)
                            state = "error"
                            _draw_error(msg)
                        text_buf = ""
                        gc.collect()
                elif _is_backspace(k):
                    if text_buf:
                        text_buf = text_buf[:-1]
                        _draw_typing(text_buf, cursor_on)
                else:
                    ch = _printable_char(k)
                    if ch is not None and len(text_buf) < 240:
                        text_buf += ch
                        _draw_typing(text_buf, cursor_on)

                if state == "typing":
                    now = time.ticks_ms()
                    if time.ticks_diff(now, last_blink_ms) >= 500:
                        cursor_on = not cursor_on
                        last_blink_ms = now
                        _draw_typing(text_buf, cursor_on)

            elif state == "showing":
                if _is_new_chat(k):
                    session_id = _new_session_id()
                    state = "typing"
                    text_buf = ""
                    cursor_on = True
                    last_blink_ms = time.ticks_ms()
                    _draw_typing(text_buf, cursor_on)
                    continue
                intent = _scroll_intent(k)
                if intent is not None:
                    _, r_lines, _, max_visible = _result_layout(
                        last_prompt, last_response,
                    )
                    max_scroll = max(0, len(r_lines) - max_visible)
                    new_scroll = (
                        max(0, scroll - 1) if intent == "up"
                        else min(max_scroll, scroll + 1)
                    )
                    if new_scroll != scroll:
                        scroll = new_scroll
                        _draw_result(last_prompt, last_response, scroll)
                elif k is not None:
                    state = "typing"
                    text_buf = ""
                    cursor_on = True
                    last_blink_ms = time.ticks_ms()
                    _draw_typing(text_buf, cursor_on)

            elif state == "error":
                if _is_new_chat(k):
                    session_id = _new_session_id()
                    state = "typing"
                    text_buf = ""
                    cursor_on = True
                    last_blink_ms = time.ticks_ms()
                    _draw_typing(text_buf, cursor_on)
                elif k is not None:
                    state = "typing"
                    text_buf = ""
                    cursor_on = True
                    last_blink_ms = time.ticks_ms()
                    _draw_typing(text_buf, cursor_on)

            time.sleep_ms(40)
    finally:
        try:
            _LCD.fillScreen(_BLACK)
        except Exception:
            pass
        time.sleep_ms(200)
        machine.reset()


run()
