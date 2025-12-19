#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json, array, os, time, threading
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from flask import Flask, render_template, abort, request
from ola.ClientWrapper import ClientWrapper

# =======================
# Konfiguration / Pfade
# =======================
CFG_PATH   = Path(os.environ.get("SCENES_JSON", "scenes.json"))
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "")
BIND_HOST  = os.environ.get("BIND_HOST", "0.0.0.0")
BIND_PORT  = int(os.environ.get("BIND_PORT", "8080"))
DIMMER_STATE_PATH = Path(os.environ.get("DIMMER_STATE_PATH", "dimmer.json"))

LOGO_URL = "/static/logo.png"

# =======================
# State laden/speichern
# =======================
def load_state() -> Dict:
    try:
        data = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data.setdefault("universe", 1)
    data.setdefault("fixtures", [])
    data.setdefault("defaults", {"dimmer": 255, "strobe": 0})
    data.setdefault("buttons", [])
    return data

def save_state(state: Dict) -> None:
    CFG_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")

def load_cfg() -> Tuple[int, List[Dict], List[Dict], Dict[str,int]]:
    st = load_state()
    return int(st.get("universe", 1)), st.get("fixtures", []), st.get("buttons", []), (st.get("defaults", {}) or {})

def _load_dimmer_from_disk(default: int = 100) -> int:
    try:
        if DIMMER_STATE_PATH.exists():
            data = json.loads(DIMMER_STATE_PATH.read_text(encoding="utf-8"))
            return int(data.get("level", default))
    except Exception:
        pass
    return default

def _save_dimmer_to_disk(level: int) -> None:
    try:
        DIMMER_STATE_PATH.write_text(json.dumps({"level": int(level)}), encoding="utf-8")
    except Exception:
        pass

# =======================
# Globaler Zustand
# =======================
CURRENT_FRAME: Optional[List[int]] = None
CURRENT_BASE_FRAME: Optional[List[int]] = None
FRAME_LOCK = threading.Lock()
SEND_LOCK  = threading.Lock()

ANIM_THREAD: Optional[threading.Thread] = None
ANIM_STOP  = threading.Event()

GLOBAL_DIMMER_PERCENT = _load_dimmer_from_disk(100)
GLOBAL_DIMMER_LOCK = threading.Lock()

# =======================
# Global Dimmer Helpers
# =======================
def set_global_dimmer(pct: int):
    global GLOBAL_DIMMER_PERCENT
    with GLOBAL_DIMMER_LOCK:
        GLOBAL_DIMMER_PERCENT = max(0, min(100, int(pct)))
        _save_dimmer_to_disk(GLOBAL_DIMMER_PERCENT)

def get_global_dimmer() -> int:
    with GLOBAL_DIMMER_LOCK:
        return GLOBAL_DIMMER_PERCENT

def _fixture_span(f: Dict) -> int:
    if f.get("map"):
        return len(f["map"])
    mode = str(f.get("mode", "RGBW")).upper()
    if mode == "RGBWA":
        return 5
    elif mode == "RGBW":
        return 4
    else:  # RGB
        return 3

def _apply_global_dimmer(frame: List[int], fixtures: List[Dict]) -> List[int]:
    """Globaler Dimmer: DIM/A-Kanal setzen, falls vorhanden – sonst RGB(W) skalieren."""
    pct = get_global_dimmer()
    if pct == 100:
        return list(frame)
    out = list(frame)
    factor = pct / 100.0
    for f in fixtures:
        start = f["start_channel"] - 1
        span  = _fixture_span(f)
        m = [x.upper() for x in (f.get("map") or []) if isinstance(x, str)]
        
        # Prüfe auf DIM oder A (Amber als Dimmer-Kanal)
        dimmer_channels = [ch for ch in ["DIM", "A"] if ch in m]
        if dimmer_channels:
            for dim_ch in dimmer_channels:
                ch = start + m.index(dim_ch)
                if 0 <= ch < len(out):
                    out[ch] = int(round(255 * factor))
        else:
            # Skaliere RGB(W)
            color_channels = min(span, 4)
            for off in range(color_channels):
                i = start + off
                if 0 <= i < len(out):
                    out[i] = int(round(out[i] * factor))
    return out

# =======================
# Frame Utilities
# =======================
def frame_len(fixtures: List[Dict]) -> int:
    end = 0
    for f in fixtures:
        end = max(end, f["start_channel"] + _fixture_span(f) - 1)
    return end

def ensure_len(buf: Optional[List[int]], n: int) -> List[int]:
    if buf is None: return [0]*n
    if len(buf) < n: return buf + [0]*(n - len(buf))
    if len(buf) > n: return buf[:n]
    return buf

def get_current_frame(target_len: int) -> List[int]:
    global CURRENT_FRAME
    with FRAME_LOCK:
        return ensure_len(CURRENT_FRAME, target_len)

def set_current_frame(frame: List[int]) -> None:
    global CURRENT_FRAME
    with FRAME_LOCK:
        CURRENT_FRAME = list(frame)

def get_base_frame(target_len: int) -> List[int]:
    global CURRENT_BASE_FRAME
    with FRAME_LOCK:
        return ensure_len(CURRENT_BASE_FRAME, target_len)

def set_base_frame(frame: List[int]) -> None:
    global CURRENT_BASE_FRAME
    with FRAME_LOCK:
        CURRENT_BASE_FRAME = list(frame)

def _resend_current_frame():
    try:
        universe, fixtures, _buttons, _defaults = load_cfg()
        base = get_base_frame(frame_len(fixtures))
        with SEND_LOCK:
            send_dmx(universe, base)
    except Exception:
        pass

# =======================
# DMX Frame Builder (mit A-Kanal Support)
# =======================
def _apply_fixture(buf: List[int], f: Dict, color: Dict[str,int], attrs: Dict[str,int], root_defaults: Dict[str,int]):
    """Schreibt Werte eines Fixtures gemäß 'map' oder fallback (RGB/RGBW)."""
    start = f["start_channel"] - 1
    m = [x.upper() for x in (f.get("map") or []) if isinstance(x, str)]
    dim_def = int(root_defaults.get("dimmer", 255))
    stro_def= int(root_defaults.get("strobe", 0))

    dim = int(attrs.get("dimmer", dim_def))
    stro= int(attrs.get("strobe", stro_def))
    r = int(color.get("r",0))
    g = int(color.get("g",0))
    b = int(color.get("b",0))
    w = int(color.get("w",0))
    a = int(color.get("a",0))  # NEU: Amber-Kanal

    if not m:
        # Fallback ohne map
        if start+0 < len(buf): buf[start+0] = r
        if start+1 < len(buf): buf[start+1] = g
        if start+2 < len(buf): buf[start+2] = b
        if str(f.get("mode","RGBW")).upper()=="RGBW" and start+3 < len(buf):
            buf[start+3] = w
        return

    for idx, role in enumerate(m):
        ch = start + idx
        if ch >= len(buf): break
        if role == "DIM":      buf[ch] = max(0, min(255, dim))
        elif role == "R":      buf[ch] = max(0, min(255, r))
        elif role == "G":      buf[ch] = max(0, min(255, g))
        elif role == "B":      buf[ch] = max(0, min(255, b))
        elif role == "W":      buf[ch] = max(0, min(255, w))
        elif role == "A":      buf[ch] = max(0, min(255, a))  # NEU: Amber
        elif role == "STROBE": buf[ch] = max(0, min(255, stro))

def build_dmx_frame(fixtures: List[Dict], scene: Dict, root_defaults: Dict[str,int]) -> List[int]:
    dmx = [0] * frame_len(fixtures)
    t = (scene.get("type") or "static").lower()
    if t == "blackout":
        return dmx

    gattrs = scene.get("attrs", {})
    pfattrs= (scene.get("per_fixture_attrs") or {})

    if t == "static":
        allc = scene.get("all", {})
        for f in fixtures:
            _apply_fixture(dmx, f, allc, {**gattrs, **pfattrs.get(f["name"], {})}, root_defaults)
        return dmx

    if t == "per-fixture":
        vals = scene.get("values", {})
        for f in fixtures:
            col = vals.get(f["name"], {})
            _apply_fixture(dmx, f, col, {**gattrs, **pfattrs.get(f["name"], {})}, root_defaults)
        return dmx

    return dmx

def preview_rgb(scene: Dict) -> Tuple[int,int,int]:
    t = (scene.get("type") or "static").lower()
    cols = []
    if t == "blackout": return (0,0,0)
    if t == "static":
        c = scene.get("all", {})
        cols.append((c.get("r",0), c.get("g",0), c.get("b",0)))
    elif t == "per-fixture":
        for c in (scene.get("values", {}) or {}).values():
            cols.append((c.get("r",0), c.get("g",0), c.get("b",0)))
    elif t == "sequence":
        steps = scene.get("steps", [])
        if steps:
            c = (steps[0].get("all") or {})
            if c:
                cols.append((c.get("r",0), c.get("g",0), c.get("b",0)))
            else:
                vals = (steps[0].get("values") or {})
                if vals:
                    first = next(iter(vals.values()))
                    cols.append((first.get("r",0), first.get("g",0), first.get("b",0)))
    if not cols: return (0,0,0)
    n = len(cols)
    return (sum(r for r,_,_ in cols)//n,
            sum(g for _,g,_ in cols)//n,
            sum(b for *_,b in cols)//n)

# =======================
# Sequenzen
# =======================
def build_sequence_frames(fixtures: List[Dict], scene: Dict, root_defaults: Dict[str,int]):
    default_hold = int(scene.get("hold_ms", 0))
    default_xf   = int(scene.get("crossfade_ms", 0))
    frames = []
    for st in (scene.get("steps") or []):
        hold = int(st.get("hold_ms", default_hold))
        xfad = int(st.get("crossfade_ms", default_xf))
        if "all" in st:
            frame = build_dmx_frame(fixtures, {"type":"static","all":st["all"]}, root_defaults)
        elif "values" in st:
            frame = build_dmx_frame(fixtures, {"type":"per-fixture","values":st["values"]}, root_defaults)
        else:
            frame = [0] * frame_len(fixtures)
        frames.append((frame, hold, xfad))
    return frames

def run_sequence(universe: int, fixtures: List[Dict], scene: Dict, root_defaults: Dict[str,int]):
    seq = build_sequence_frames(fixtures, scene, root_defaults)
    if not seq: return
    L = max(len(f) for f,_,_ in seq)
    seq = [(ensure_len(f, L), hold, xf) for (f, hold, xf) in seq]

    with SEND_LOCK:
        send_dmx(universe, seq[0][0])

    idx = 0
    while not ANIM_STOP.is_set():
        cur_f, cur_hold, _cur_xf = seq[idx]
        if cur_hold > 0 and ANIM_STOP.wait(cur_hold/1000):
            break
        nxt = (idx + 1) % len(seq)
        nxt_f, _hold_nxt, xf = seq[nxt]
        xf = int(xf or 0)
        if xf > 0:
            steps = calculate_fade_steps(xf)
            delay = xf / 1000 / steps
            start = get_base_frame(len(nxt_f))
            for i in range(1, steps+1):
                if ANIM_STOP.is_set(): break
                t = i / steps
                mix = [int(a + (b - a) * t) for a, b in zip(start, nxt_f)]
                with SEND_LOCK:
                    send_dmx(universe, mix)
                if ANIM_STOP.wait(delay): break
        else:
            with SEND_LOCK:
                send_dmx(universe, nxt_f)
        idx = nxt

# =======================
# OPTIMIERTE FADE STEPS
# =======================
def calculate_fade_steps(fade_ms: int) -> int:
    """
    Berechnet optimale Anzahl Fade-Steps basierend auf Fade-Zeit.
    
    Richtlinien:
    - < 100ms: sehr schnell, wenige Steps (2-5)
    - 100-500ms: schnell, moderate Steps (5-15)
    - 500-2000ms: normal, gute Balance (15-40)
    - 2000-5000ms: langsam, mehr Steps (40-80)
    - > 5000ms: sehr langsam, viele Steps (80-150)
    
    Target: ~30-50 FPS für sichtbare Smoothness
    """
    if fade_ms < 100:
        # Sehr kurze Fades: 2-5 Steps
        return max(2, min(5, fade_ms // 20))
    elif fade_ms < 500:
        # Kurze Fades: 5-15 Steps (ca. 30-50 FPS)
        return max(5, min(15, fade_ms // 30))
    elif fade_ms < 2000:
        # Normale Fades: 15-40 Steps
        return max(15, min(40, fade_ms // 40))
    elif fade_ms < 5000:
        # Lange Fades: 40-80 Steps
        return max(40, min(80, fade_ms // 50))
    else:
        # Sehr lange Fades: 80-150 Steps
        return max(80, min(150, fade_ms // 60))

# =======================
# Senden (Base + Dimmer)
# =======================
def send_dmx(universe: int, dmx: List[int]) -> None:
    set_base_frame(dmx)

    try:
        _, fixtures, _, _defaults = load_cfg()
        to_send = _apply_global_dimmer(list(dmx), fixtures)
    except Exception:
        to_send = list(dmx)

    data = array.array('B', to_send)
    wrapper = ClientWrapper()
    client = wrapper.Client()
    client.SendDmx(universe, data, lambda status: wrapper.Stop())
    wrapper.Run()

    set_current_frame(to_send)

# =======================
# Animation Control
# =======================
def stop_animation():
    global ANIM_THREAD
    if ANIM_THREAD and ANIM_THREAD.is_alive():
        ANIM_STOP.set()
        ANIM_THREAD.join(timeout=2)
    ANIM_STOP.clear()
    ANIM_THREAD = None

# =======================
# Flask App
# =======================
app = Flask(__name__)

def check_token():
    if not AUTH_TOKEN: return True
    t = request.args.get("token") or request.headers.get("X-Auth-Token")
    return t == AUTH_TOKEN

@app.get("/")
def index():
    if not check_token(): abort(401)
    universe, fixtures, buttons, _defs = load_cfg()
    btns = []
    for b in sorted(buttons, key=lambda x: x["index"]):
        x = dict(b)
        x.setdefault("scene", {})
        btns.append(x)
    return render_template('control.html', universe=universe, fixtures=fixtures, buttons=btns, token=AUTH_TOKEN, logo_url=LOGO_URL)

# --- Trigger / Blackout ---
@app.post("/trigger/<int:index>")
def trigger(index: int):
    global ANIM_THREAD
    if not check_token(): abort(401)
    fade_ms = int(request.args.get("fade_ms", "0"))
    universe, fixtures, buttons, defs = load_cfg()
    b = next((x for x in buttons if x["index"] == index), None)
    if not b: abort(404, "Button nicht definiert")
    scene = b.get("scene", {"type":"blackout"})

    stop_animation()

    if (scene.get("type") or "").lower() == "sequence":
        if fade_ms > 0:
            seq_frames = build_sequence_frames(fixtures, scene, defs)
            if seq_frames:
                target = seq_frames[0][0]
                start = get_base_frame(len(target))
                steps = calculate_fade_steps(fade_ms)
                delay = fade_ms / 1000 / steps
                with SEND_LOCK:
                    for i in range(1, steps + 1):
                        t = i / steps
                        mix = [int(a + (b - a) * t) for a, b in zip(start, target)]
                        send_dmx(universe, mix)
                        time.sleep(delay)

        ANIM_THREAD = threading.Thread(target=run_sequence, args=(universe, fixtures, scene, defs), daemon=True)
        ANIM_THREAD.start()
        return ("", 204)

    target = build_dmx_frame(fixtures, scene, defs)
    if fade_ms <= 0:
        with SEND_LOCK:
            send_dmx(universe, target)
        return ("", 204)

    start = get_base_frame(len(target))
    steps = calculate_fade_steps(fade_ms)
    delay = fade_ms / 1000 / steps
    with SEND_LOCK:
        for i in range(1, steps + 1):
            t = i / steps
            mix = [int(a + (b - a) * t) for a, b in zip(start, target)]
            send_dmx(universe, mix)
            time.sleep(delay)
    return ("", 204)

@app.post("/blackout")
def blackout():
    if not check_token(): abort(401)
    stop_animation()
    universe, fixtures, _buttons, _defs = load_cfg()
    with SEND_LOCK:
        send_dmx(universe, [0]*frame_len(fixtures))
    return ("", 204)

# --- API: Global Dimmer ---
@app.post("/api/dim/step")
def api_dim_step():
    if not check_token(): abort(401)
    delta = int(request.args.get("delta", "10"))
    level = get_global_dimmer()
    set_global_dimmer(level + delta)
    _resend_current_frame()
    return str(get_global_dimmer())

@app.post("/api/dim/set")
def api_dim_set():
    if not check_token(): abort(401)
    pct = int(request.args.get("percent", request.json.get("percent", 100) if request.is_json else 100))
    set_global_dimmer(pct)
    _resend_current_frame()
    return {"level": get_global_dimmer()}

@app.get("/api/dim")
def api_dim_get():
    if not check_token(): abort(401)
    return {"level": get_global_dimmer()}

# --- API: State laden/speichern ---
@app.get("/api/state")
def api_state_get():
    if not check_token(): abort(401)
    return load_state()

@app.post("/api/state")
def api_state_set():
    if not check_token(): abort(401)
    data = request.get_json(force=True, silent=False)
    if not isinstance(data, dict):
        abort(400, "State muss ein JSON-Objekt sein")
    stop_animation()
    save_state(data)
    return {"ok": True}

# --- API: Szene testweise abspielen ---
@app.post("/api/test_scene")
def api_test_scene():
    global ANIM_THREAD
    if not check_token(): abort(401)
    payload = request.get_json(force=True, silent=False) or {}
    scene = payload.get("scene")
    fade_ms = int(payload.get("fade_ms", 0))
    duration_ms = int(payload.get("duration_ms", 3000))
    if not scene or not isinstance(scene, dict):
        abort(400, "scene fehlt oder ist ungültig")

    universe, fixtures, _buttons, defs = load_cfg()
    stop_animation()

    if (scene.get("type") or "").lower() == "sequence":
        def runner():
            try:
                run_sequence(universe, fixtures, scene, defs)
            except Exception:
                pass
        ANIM_THREAD = threading.Thread(target=runner, daemon=True)
        ANIM_THREAD.start()

        def stopper():
            time.sleep(max(0, duration_ms/1000))
            stop_animation()
        threading.Thread(target=stopper, daemon=True).start()
        return {"ok": True, "running": "sequence"}

    target = build_dmx_frame(fixtures, scene, defs)
    if fade_ms <= 0:
        with SEND_LOCK:
            send_dmx(universe, target)
    else:
        start = get_base_frame(len(target))
        steps = calculate_fade_steps(fade_ms)
        delay = fade_ms / 1000 / steps
        with SEND_LOCK:
            for i in range(1, steps + 1):
                t = i / steps
                mix = [int(a + (b - a) * t) for a, b in zip(start, target)]
                send_dmx(universe, mix)
                time.sleep(delay)
    return {"ok": True}

@app.get("/admin")
def admin_page():
    if not check_token(): abort(401)
    return render_template('admin.html', logo_url=LOGO_URL)

# =======================
# Main
# =======================
if __name__ == "__main__":
    if not CFG_PATH.exists():
        initial = {
            "universe": 1,
            "fixtures": [],
            "defaults": {"dimmer": 255, "strobe": 0},
            "buttons": []
        }
        save_state(initial)
    print(f"Starte auf {BIND_HOST}:{BIND_PORT} (Token gesetzt: {'ja' if AUTH_TOKEN else 'nein'})")
    print("Logo-Datei erwartet unter: ./static/logo.png")
    print("\nOptimierungen aktiviert:")
    print("  ✓ Amber (A) Kanal vollständig unterstützt")
    print("  ✓ Adaptive Fade-Steps für smoothe Übergänge")
    print("  ✓ <100ms: 2-5 Steps | 100-500ms: 5-15 Steps")
    print("  ✓ 500-2000ms: 15-40 Steps | >2s: bis 150 Steps")
    app.run(host=BIND_HOST, port=BIND_PORT)