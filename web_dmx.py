#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json, array, os, time, threading
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from flask import Flask, render_template_string, abort, request
from ola.ClientWrapper import ClientWrapper

# =======================
# Konfiguration / Pfade
# =======================
CFG_PATH   = Path(os.environ.get("SCENES_JSON", "scenes.json"))
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "")   # leer => kein Schutz
BIND_HOST  = os.environ.get("BIND_HOST", "0.0.0.0")
BIND_PORT  = int(os.environ.get("BIND_PORT", "8080"))
DIMMER_STATE_PATH = Path(os.environ.get("DIMMER_STATE_PATH", "dimmer.json"))

# Logo: bitte Datei hier ablegen: ./static/logo.png
LOGO_URL = "/static/logo.png"

# =======================
# State laden/speichern
# =======================
def load_state() -> Dict:
    """Liest scenes.json und ergänzt fehlende Felder."""
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
    """Convenience: Universe, Fixtures, Buttons, Defaults."""
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
CURRENT_FRAME: Optional[List[int]] = None         # zuletzt GESENDETER (gedimmter) Frame
CURRENT_BASE_FRAME: Optional[List[int]] = None    # ungedimmter Basis-Frame
FRAME_LOCK = threading.Lock()
SEND_LOCK  = threading.Lock()

ANIM_THREAD: Optional[threading.Thread] = None
ANIM_STOP  = threading.Event()

# Globaler Dimmer (0..100%) – wirkt auf alles (persistiert)
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
    return 4 if str(f.get("mode", "RGBW")).upper() == "RGBW" else 3

def _apply_global_dimmer(frame: List[int], fixtures: List[Dict]) -> List[int]:
    """Globaler Dimmer: DIM-Kanal setzen, falls vorhanden – sonst RGB(W) skalieren."""
    pct = get_global_dimmer()
    if pct == 100:
        return list(frame)
    out = list(frame)
    factor = pct / 100.0
    for f in fixtures:
        start = f["start_channel"] - 1
        span  = _fixture_span(f)
        m = [x.upper() for x in (f.get("map") or []) if isinstance(x, str)]
        if "DIM" in m:
            ch = start + m.index("DIM")
            if 0 <= ch < len(out):
                out[ch] = int(round(255 * factor))
        else:
            # Skaliere RGB(W)
            if span >= 4:
                for off in (0,1,2,3):
                    i = start + off
                    if 0 <= i < len(out):
                        out[i] = int(round(out[i] * factor))
            else:
                for off in (0,1,2):
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
    """Basis-Frame erneut mit aktuellem Dimmer senden (für Dimmeränderungen)."""
    try:
        universe, fixtures, _buttons, _defaults = load_cfg()
        base = get_base_frame(frame_len(fixtures))
        with SEND_LOCK:
            send_dmx(universe, base)
    except Exception:
        pass

# =======================
# DMX Frame Builder
# =======================
def _apply_fixture(buf: List[int], f: Dict, color: Dict[str,int], attrs: Dict[str,int], root_defaults: Dict[str,int]):
    """Schreibt Werte eines Fixtures gemäß 'map' oder fallback (RGB/RGBW)."""
    start = f["start_channel"] - 1
    m = [x.upper() for x in (f.get("map") or []) if isinstance(x, str)]
    dim_def = int(root_defaults.get("dimmer", 255))
    stro_def= int(root_defaults.get("strobe", 0))

    dim = int(attrs.get("dimmer", dim_def))
    stro= int(attrs.get("strobe",  stro_def))
    r = int(color.get("r",0)); g = int(color.get("g",0)); b = int(color.get("b",0)); w = int(color.get("w",0))

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
        elif role == "STROBE": buf[ch] = max(0, min(255, stro))
        else: pass

def build_dmx_frame(fixtures: List[Dict], scene: Dict, root_defaults: Dict[str,int]) -> List[int]:
    """Baut Frame für static, per-fixture. (sequence nutzt diese Funktion je Step)"""
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
    """Fallback/Legacy (nicht mehr nötig für Webcontroller, der jetzt Canvas nutzt)."""
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
    """Gibt Liste von (frame, hold_ms, crossfade_ms) zurück."""
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
            steps = max(2, min(120, xf // 30))
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
# Senden (Base + Dimmer)
# =======================
def send_dmx(universe: int, dmx: List[int]) -> None:
    """
    Erwartet UNGEDIMMTE Kanalwerte (Basis-Frame).
    - Speichert den Basis-Frame
    - Wendet Global-Dimmer an
    - Sendet
    - Speichert den gesendeten (gedimmten) Frame
    """
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

# =======================
# Öffentliches UI (Webcontroller)
# - gleiche Farben/Akzente wie Admin
# - Logo rechts oben
# - Szenen-Swatches werden per Canvas mit der "StreamDeck"-Logik gerendert
# =======================
HTML = """
<!doctype html>
<meta charset="utf-8"/>
<title>DMX Web Controller</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root { color-scheme: light; }
body { font-family: system-ui, sans-serif; margin: 0; padding: 20px; background:#fff; color:#222; }

.header-bar{
  display:flex; justify-content:space-between; align-items:center;
  margin-bottom:18px; padding-bottom:14px;
  border-bottom:2px solid #a53792;
}
.header-left{ display:flex; flex-direction:column; gap:4px; }
.header-bar h1{ margin:0; color:#a53792; font-size: 22px; }
.small { opacity:0.75; font-size:0.95em; }
.logo { height:64px; width:auto; display:block; object-fit:contain; }

.card { border:1px solid #ddd; border-radius:12px; padding:14px; background:#f5f5f5; margin-bottom:14px; }
.row { display:flex; gap:10px; align-items:center; flex-wrap: wrap; }

input, button { background:#fff; color:#222; border:1px solid #ccc; border-radius:10px; padding:8px 10px; }
button { cursor:pointer; transition: all .2s; }
button:hover{ background:#f0f0f0; transform: translateY(-1px); box-shadow: 0 2px 4px rgba(0,0,0,.1); }

.btn-primary { background:#a53792; color:#fff; border-color:#a53792; }
.btn-primary:hover { background:#8e2d7d; }

.grid { display:grid; grid-template-columns: repeat(auto-fill,minmax(200px,1fr)); gap:14px; }

.scene {
  border:1px solid #ddd; border-radius:14px; overflow:hidden;
  background:#fff; text-decoration:none; color:inherit;
  box-shadow: 0 6px 18px rgba(0,0,0,.06);
  transition: transform .15s, box-shadow .15s;
}
.scene:hover{ transform: translateY(-2px); box-shadow: 0 10px 22px rgba(0,0,0,.10); }
.scene .swatch{ height:110px; background:#eee; }
.scene .meta{ padding:12px; display:flex; align-items:center; justify-content:space-between; gap:10px; }
.scene .label{ font-weight:700; }
.badge{ padding:3px 10px; border-radius:999px; border:1px solid #a53792; color:#a53792; font-weight:600; font-size:.85em; }

#dim-panel {
  position: fixed; right: 20px; bottom: 20px;
  background:#fff; border:1px solid #ddd; border-radius:14px;
  padding:12px; box-shadow: 0 10px 28px rgba(0,0,0,.12);
  z-index: 1000; min-width: 280px;
}
#dim-panel h3 { margin: 0 0 8px 0; font-size: 14px; opacity:.9; color:#a53792; }
#dim-panel .pill { padding:4px 10px; border:1px solid #a53792; border-radius:999px; background:#fff; color:#a53792; font-weight:600; }
#dim-slider { width: 100%; accent-color: #a53792; }
.dim-btn { padding:6px 10px; border-radius:10px; border:1px solid #ccc; background:#fff; cursor:pointer;}
.dim-btn:hover { background:#f0f0f0; }
</style>

<div class="header-bar">
  <div class="header-left">
    <h1>DMX Web Controller</h1>
    <div class="small">Universe {{universe}} · {{fixtures|length}} Fixtures · {{buttons|length}} Szenen</div>
  </div>
  <img src="{{logo_url}}" class="logo" alt="Logo">
</div>

<div class="card">
  <div class="row" style="justify-content:space-between">
    <form method="post" action="{{ url_for('blackout') }}{% if token %}?token={{token}}{% endif %}">
      <button class="btn-primary">Blackout</button>
    </form>
    <div class="row">
      <label class="small">Fade (ms):</label>
      <input type="number" id="fade_ms" value="0" min="0" step="50" style="width:120px">
    </div>
  </div>
</div>

<div class="grid">
  {% for b in buttons %}
    <a class="scene" href="#" onclick="trigger({{b.index}});return false;">
      <div class="swatch" data-scene='{{ (b.scene or {})|tojson }}'></div>
      <div class="meta">
        <div class="label">{{ b.label }}</div>
        <div class="badge">#{{ b.index }}</div>
      </div>
    </a>
  {% endfor %}
</div>

<div id="dim-panel">
  <h3>Helligkeit</h3>
  <div class="row" style="justify-content:space-between">
    <span class="pill">Aktuell: <b id="dimval">--%</b></span>
    <div class="row">
      <button class="dim-btn" onclick="dimStep(-10)">−10%</button>
      <button class="dim-btn" onclick="dimStep(-5)">−5%</button>
      <button class="dim-btn" onclick="dimStep(5)">+5%</button>
      <button class="dim-btn" onclick="dimStep(10)">+10%</button>
    </div>
  </div>
  <div style="margin:10px 0">
    <input id="dim-slider" type="range" min="0" max="100" value="100" oninput="dimSet(this.value)">
  </div>
  <div class="row">
    <button class="dim-btn" onclick="dimPreset(0)">0%</button>
    <button class="dim-btn" onclick="dimPreset(50)">50%</button>
    <button class="dim-btn" onclick="dimPreset(100)">100%</button>
  </div>
</div>

<script>
function trigger(idx){
  const fade = document.getElementById('fade_ms').value || "0";
  let url = "/trigger/"+idx+"?fade_ms="+encodeURIComponent(fade);
  {% if token %} url += "&token={{token}}"; {% endif %}
  fetch(url, {method:"POST"});
}

function dimApi(path){
  let url = path;
  {% if token %} url += (path.includes('?') ? '&' : '?') + "token={{token}}"; {% endif %}
  return fetch(url, {method: path.startsWith('/api/dim') && (path.includes('/set') || path.includes('/step')) ? 'POST' : 'GET'});
}

async function updateDim(){
  const r = await dimApi('/api/dim');
  const j = await r.json();
  const lvl = j.level ?? 100;
  document.getElementById('dimval').textContent = lvl + '%';
  document.getElementById('dim-slider').value = lvl;
}

async function dimStep(delta){
  await dimApi('/api/dim/step?delta='+delta);
  updateDim();
}
async function dimSet(val){
  const pct = parseInt(val||'0');
  await dimApi('/api/dim/set?percent='+pct);
  updateDim();
}
function dimPreset(p){ dimSet(p); }

// ====== Scene preview: gleiche Logik wie StreamDeck-Generator ======
function collectColors(scene){
  const colors = [];
  const t = (scene?.type || 'blackout').toLowerCase();

  if (t === 'blackout'){
    colors.push({r:0,g:0,b:0});
  } else if (t === 'static'){
    const c = scene.all || {r:0,g:0,b:0};
    colors.push({r:c.r||0, g:c.g||0, b:c.b||0});
  } else if (t === 'per-fixture'){
    const vals = scene.values || {};
    Object.values(vals).forEach(c => colors.push({r:c.r||0, g:c.g||0, b:c.b||0}));
  } else if (t === 'sequence'){
    const steps = scene.steps || [];
    steps.forEach(st => {
      if (st.all){
        colors.push({r:st.all.r||0, g:st.all.g||0, b:st.all.b||0});
      } else if (st.values){
        Object.values(st.values).forEach(c => colors.push({r:c.r||0, g:c.g||0, b:c.b||0}));
      }
    });
  }

  if (colors.length === 0) colors.push({r:0,g:0,b:0});
  return colors;
}

function renderSceneSwatch(el){
  const scene = JSON.parse(el.dataset.scene || '{}');
  const colors = collectColors(scene);

  const W = el.clientWidth || 400;
  const H = el.clientHeight || 110;

  const canvas = document.createElement('canvas');
  canvas.width = Math.max(2, Math.floor(W));
  canvas.height = Math.max(2, Math.floor(H));
  const ctx = canvas.getContext('2d');

  if (colors.length === 1){
    ctx.fillStyle = `rgb(${colors[0].r},${colors[0].g},${colors[0].b})`;
    ctx.fillRect(0,0,canvas.width,canvas.height);
  } else if (colors.length === 2){
    ctx.fillStyle = `rgb(${colors[0].r},${colors[0].g},${colors[0].b})`;
    ctx.fillRect(0,0,canvas.width/2,canvas.height);
    ctx.fillStyle = `rgb(${colors[1].r},${colors[1].g},${colors[1].b})`;
    ctx.fillRect(canvas.width/2,0,canvas.width/2,canvas.height);
  } else if (colors.length <= 4){
    const gridSize = Math.ceil(Math.sqrt(colors.length));
    const cellW = canvas.width / gridSize;
    const cellH = canvas.height / gridSize;
    colors.forEach((c,i)=>{
      const x = (i % gridSize) * cellW;
      const y = Math.floor(i / gridSize) * cellH;
      ctx.fillStyle = `rgb(${c.r},${c.g},${c.b})`;
      ctx.fillRect(x,y,cellW,cellH);
    });
  } else {
    const grad = ctx.createLinearGradient(0,0,0,canvas.height);
    colors.forEach((c,i)=>{
      grad.addColorStop(i/(colors.length-1), `rgb(${c.r},${c.g},${c.b})`);
    });
    ctx.fillStyle = grad;
    ctx.fillRect(0,0,canvas.width,canvas.height);
  }

  // Optional: leichter glossy overlay
  const g = ctx.createLinearGradient(0,0,0,canvas.height);
  g.addColorStop(0, 'rgba(255,255,255,0.25)');
  g.addColorStop(0.6, 'rgba(255,255,255,0.05)');
  g.addColorStop(1, 'rgba(0,0,0,0.12)');
  ctx.fillStyle = g;
  ctx.fillRect(0,0,canvas.width,canvas.height);

  el.style.backgroundImage = `url(${canvas.toDataURL('image/png')})`;
  el.style.backgroundSize = 'cover';
  el.style.backgroundPosition = 'center';
}

function renderAllSwatches(){
  document.querySelectorAll('.swatch[data-scene]').forEach(renderSceneSwatch);
}

window.addEventListener('resize', () => {
  // bei Resize neu rendern, damit Canvas zur Kachel passt
  renderAllSwatches();
});

// init
updateDim();
renderAllSwatches();
</script>
"""

@app.get("/")
def index():
    if not check_token(): abort(401)
    universe, fixtures, buttons, _defs = load_cfg()
    btns = []
    for b in sorted(buttons, key=lambda x: x["index"]):
        x = dict(b)
        x.setdefault("scene", {})
        btns.append(x)
    return render_template_string(HTML, universe=universe, fixtures=fixtures, buttons=btns, token=AUTH_TOKEN, logo_url=LOGO_URL)

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
                steps = max(2, min(60, fade_ms // 30))
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
    steps = max(2, min(60, fade_ms // 30))
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

# --- API: Szene testweise abspielen (ohne Speichern) ---
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
        steps = max(2, min(60, fade_ms // 30))
        delay = fade_ms / 1000 / steps
        with SEND_LOCK:
            for i in range(1, steps + 1):
                t = i / steps
                mix = [int(a + (b - a) * t) for a, b in zip(start, target)]
                send_dmx(universe, mix)
                time.sleep(delay)
    return {"ok": True}

# =======================
# Admin UI (angepasst)
# - Logo rechts oben (static/logo.png)
# - Index nicht editierbar
# - Companion URI kopierbar (voller Link inkl. Token)
# - Sequence-Step Karten hell (fix)
# =======================
ADMIN_HTML = r"""
<!doctype html>
<meta charset="utf-8"/>
<title>DMX Admin</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root { color-scheme: light; }
  body { font-family: system-ui, sans-serif; margin: 0; padding: 20px; background:#fff; color:#222; }
  .header-bar { display:flex; justify-content:space-between; align-items:center; margin-bottom:24px; padding-bottom:16px; border-bottom:2px solid #a53792; }
  .header-bar h1 { margin:0; color:#a53792; }
  .logo { height:64px; width:auto; object-fit:contain; display:block; }
  .row { display:flex; gap:10px; align-items:center; flex-wrap: wrap; }
  .card { border:1px solid #ddd; border-radius:12px; padding:16px; background:#f5f5f5; margin-bottom:16px; }
  .scene-card { border-left: 4px solid #a53792; }
  input, select, button { background:#fff; color:#222; border:1px solid #ccc; border-radius:8px; padding:8px 10px; }
  button { cursor:pointer; transition: all 0.2s; }
  .btn { padding:8px 12px; border-radius:10px; border:1px solid #ccc; background:#fff;}
  .btn:hover { background:#f0f0f0; transform: translateY(-1px); box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
  .btn-primary { background:#a53792; color:#fff; border-color:#a53792; }
  .btn-primary:hover { background:#8e2d7d; }
  .btn-success { background:#a6ce39; color:#fff; border-color:#a6ce39; }
  .btn-success:hover { background:#8fb52e; }
  .btn-info { background:#00aeef; color:#fff; border-color:#00aeef; }
  .btn-info:hover { background:#0092cc; }
  .btn-warning { background:#ffc000; color:#222; border-color:#ffc000; }
  .btn-warning:hover { background:#e6ad00; }
  .scenes-list { display:flex; flex-direction:column; gap:16px; }
  .tiny { font-size:.9em; opacity:.7; }
  .pill { padding:4px 12px; border:1px solid #a53792; border-radius:999px; background:#fff; color:#a53792; font-weight:500; }
  .section-title { font-weight:700; font-size:1.3rem; margin:24px 0 16px 0; color:#a53792; }
  .danger { background:#dc3545; color:#fff; border-color:#dc3545; }
  .danger:hover { background:#c82333; }

  .companion-uri { background:#f8f9fa; padding:8px 12px; border:1px solid #ccc; border-radius:6px; font-family:ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; font-size:0.9em; display:inline-flex; align-items:center; gap:8px; margin-top:8px; }
  .copy-btn { padding:4px 10px; font-size:0.85em; }
  .fixture-grid { display:grid; grid-template-columns: repeat(auto-fill,minmax(350px,1fr)); gap:12px; }

  /* FIX: Sequence-Step Hintergrund hell */
  .step-card{ background:#fff !important; border:1px solid #ddd; }

  .index-pill{
    display:inline-flex; align-items:center; gap:6px;
    padding:6px 10px; border:1px solid #ccc; border-radius:10px; background:#fff; font-weight:700;
  }
</style>

<div class="header-bar">
  <h1>DMX Controller Admin</h1>
  <img src="{{logo_url}}" class="logo" alt="Logo">
</div>

<div class="card">
  <div class="row">
    <span class="pill">Global Dimmer: <b id="dim-val">100%</b></span>
    <button class="btn btn-warning" onclick="dimStep(-10)">−10%</button>
    <button class="btn btn-warning" onclick="dimStep(10)">+10%</button>
    <input id="dim-set" type="number" style="width:90px" min="0" max="100" value="100">
    <button class="btn btn-primary" onclick="dimSet()">Set Dimmer</button>
  </div>
</div>

<div class="card">
  <div class="row">
    <label>Universe <input id="universe" type="number" min="1" value="1" style="width:100px"></label>
    <label>Default Dimmer <input id="def-dimmer" type="number" min="0" max="255" value="255" style="width:120px"></label>
    <label>Default Strobe <input id="def-strobe" type="number" min="0" max="255" value="0" style="width:120px"></label>
    <button class="btn btn-success" onclick="saveState()">💾 Save Settings</button>
    <button class="btn btn-info" onclick="reloadState()">⤳ Reload</button>
    <span class="tiny" id="status"></span>
  </div>
</div>

<div class="section-title">Fixtures</div>
<div id="fixtures" class="fixture-grid"></div>
<button class="btn btn-primary" onclick="addFixture()">+ Add Fixture</button>

<div class="section-title">Scenes</div>
<div id="buttons" class="scenes-list"></div>
<button class="btn btn-primary" onclick="addButton()">+ Add Scene</button>

<template id="tpl-fixture">
  <div class="card">
    <div class="row">
      <label>Name <input class="fx-name" style="width:160px"></label>
      <label>Start Ch <input class="fx-start" type="number" min="1" style="width:90px"></label>
      <label>Mode
        <select class="fx-mode">
          <option>RGB</option><option selected>RGBW</option>
        </select>
      </label>
      <label>Map (opt) <input class="fx-map" placeholder="z.B. DIM,R,G,B,W" style="width:220px"></label>
      <button class="btn danger" onclick="delFixture(this)">Delete</button>
    </div>
  </div>
</template>

<template id="tpl-button">
  <div class="card scene-card">
    <div class="row" style="justify-content:space-between; margin-bottom:12px;">
      <div class="row">
        <span class="index-pill">Index: <span class="bt-index-text">0</span></span>
        <label>Label <input class="bt-label" style="width:220px"></label>
        <label>Type
          <select class="bt-type">
            <option>static</option>
            <option>per-fixture</option>
            <option>sequence</option>
            <option>blackout</option>
          </select>
        </label>
      </div>
      <div class="row">
        <button class="btn btn-info" onclick="triggerScene(this)">▶ Test Scene</button>
        <button class="btn btn-success" onclick="downloadStreamDeckImage(this)">⬇ Download PNG</button>
        <button class="btn danger" onclick="delButton(this)">Delete</button>
      </div>
    </div>

    <div class="companion-uri">
      <strong>Companion URI:</strong>
      <code class="uri-text">/trigger/0?fade_ms=2000</code>
      <button class="btn copy-btn" onclick="copyURI(this)">📋 Copy</button>
    </div>

    <div class="bt-body"></div>
  </div>
</template>

<script>
let STATE = { universe:1, fixtures:[], defaults:{dimmer:255,strobe:0}, buttons:[] };

function el(id){ return document.getElementById(id); }
function status(msg){ el('status').textContent = msg; setTimeout(()=>el('status').textContent="", 2500); }

function hexToRgb(hex){ const v=hex.replace('#',''); const n=parseInt(v,16); return {r:(n>>16)&255,g:(n>>8)&255,b:n&255}; }
function rgbToHex(r,g,b){ return '#'+[r,g,b].map(x=>x.toString(16).padStart(2,'0')).join(''); }

function colorRow(title, obj, onChange){
  const r=obj.r|0, g=obj.g|0, b=obj.b|0, w=obj.w|0;
  const wrap=document.createElement('div'); wrap.className='row'; wrap.style.marginTop='8px';
  const hex=rgbToHex(r,g,b);

  wrap.innerHTML=`
    <span style="width:120px">${title}</span>
    <input type="color" value="${hex}" class="pick">
    <span class="tiny">R:${r} G:${g} B:${b}</span>
    <span style="margin-left:15px">Weiß:</span>
    <input type="range" min="0" max="255" value="${w}" class="wslider" style="width:120px">
    <span class="tiny">${w}</span>`;

  wrap.querySelector('.pick').addEventListener('input',(e)=>{
    const {r,g,b}=hexToRgb(e.target.value);
    const newW = parseInt(wrap.querySelector('.wslider').value);
    onChange({r,g,b,w:newW});
    wrap.querySelector('.tiny:last-child').textContent = newW;
  });
  wrap.querySelector('.wslider').addEventListener('input',(e)=>{
    const newW=parseInt(e.target.value);
    const {r,g,b}=hexToRgb(wrap.querySelector('.pick').value);
    onChange({r,g,b,w:newW});
    wrap.querySelector('.tiny:last-child').textContent = newW;
  });
  return wrap;
}

// StreamDeck Image Generator (72x72 px)
function generateStreamDeckImage(button){
  const canvas = document.createElement('canvas');
  canvas.width = 72;
  canvas.height = 72;
  const ctx = canvas.getContext('2d');

  const scene = button.scene || {type:'blackout'};
  const colors = [];

  const t = (scene.type || 'blackout').toLowerCase();
  if (t === 'blackout'){
    colors.push({r:0,g:0,b:0});
  } else if (t === 'static'){
    const c = scene.all || {r:0,g:0,b:0};
    colors.push({r:c.r||0, g:c.g||0, b:c.b||0});
  } else if (t === 'per-fixture'){
    const vals = scene.values || {};
    Object.values(vals).forEach(c => colors.push({r:c.r||0, g:c.g||0, b:c.b||0}));
  } else if (t === 'sequence'){
    const steps = scene.steps || [];
    steps.forEach(st => {
      if (st.all){
        colors.push({r:st.all.r||0, g:st.all.g||0, b:st.all.b||0});
      } else if (st.values){
        Object.values(st.values).forEach(c => colors.push({r:c.r||0, g:c.g||0, b:c.b||0}));
      }
    });
  }

  if (colors.length === 0) colors.push({r:0,g:0,b:0});

  if (colors.length === 1){
    ctx.fillStyle = `rgb(${colors[0].r},${colors[0].g},${colors[0].b})`;
    ctx.fillRect(0, 0, 72, 72);
  } else if (colors.length === 2){
    ctx.fillStyle = `rgb(${colors[0].r},${colors[0].g},${colors[0].b})`;
    ctx.fillRect(0, 0, 36, 72);
    ctx.fillStyle = `rgb(${colors[1].r},${colors[1].g},${colors[1].b})`;
    ctx.fillRect(36, 0, 36, 72);
  } else if (colors.length <= 4){
    const gridSize = Math.ceil(Math.sqrt(colors.length));
    const cellW = 72 / gridSize;
    const cellH = 72 / gridSize;
    colors.forEach((c, i) => {
      const x = (i % gridSize) * cellW;
      const y = Math.floor(i / gridSize) * cellH;
      ctx.fillStyle = `rgb(${c.r},${c.g},${c.b})`;
      ctx.fillRect(x, y, cellW, cellH);
    });
  } else {
    const grad = ctx.createLinearGradient(0, 0, 0, 72);
    colors.forEach((c, i) => grad.addColorStop(i / (colors.length - 1), `rgb(${c.r},${c.g},${c.b})`));
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, 72, 72);
  }

  ctx.fillStyle = '#fff';
  ctx.font = 'bold 10px sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.shadowColor = 'rgba(0,0,0,0.8)';
  ctx.shadowBlur = 3;
  ctx.fillText(button.label || 'Scene', 36, 36);

  return canvas;
}

function downloadStreamDeckImage(btnEl){
  const idx = parseInt(btnEl.closest('.card').dataset.idx);
  const button = STATE.buttons[idx];
  const canvas = generateStreamDeckImage(button);

  canvas.toBlob(blob => {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `streamdeck_${button.index}_${(button.label||'scene').replace(/\\s+/g,'_')}.png`;
    a.click();
    URL.revokeObjectURL(url);
    status('StreamDeck Bild heruntergeladen');
  });
}

// Companion URI (kopiert voller Link inkl Token)
function buildCompanionUrl(path){
  const u = new URL(path, window.location.origin);
  const token = new URLSearchParams(window.location.search).get('token');
  if (token) u.searchParams.set('token', token);
  return u.toString();
}

function copyURI(btnEl){
  const codeEl = btnEl.parentElement.querySelector('.uri-text');
  const relative = codeEl.textContent.trim();
  const full = buildCompanionUrl(relative);

  const setCopied = () => {
    const orig = btnEl.textContent;
    btnEl.textContent = '✓ Copied!';
    setTimeout(()=>btnEl.textContent = orig, 1500);
  };

  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(full).then(setCopied).catch(() => copyToClipboardFallback(full, btnEl));
  } else {
    copyToClipboardFallback(full, btnEl);
  }
}

function copyToClipboardFallback(text, btnEl){
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.select();
  try {
    document.execCommand('copy');
    if (btnEl) {
      const orig = btnEl.textContent;
      btnEl.textContent = '✓ Copied!';
      setTimeout(()=>btnEl.textContent = orig, 1500);
    }
  } catch (err) {
    alert('Kopieren fehlgeschlagen. URI: ' + text);
  }
  document.body.removeChild(textarea);
}

// ===== Fixtures =====
function renderFixtures(){
  const host=el('fixtures'); host.innerHTML='';
  const tpl=el('tpl-fixture');
  STATE.fixtures.forEach((f,idx)=>{
    const node=tpl.content.firstElementChild.cloneNode(true);
    node.dataset.idx=idx;
    node.querySelector('.fx-name').value=f.name||'';
    node.querySelector('.fx-start').value=f.start_channel||1;
    node.querySelector('.fx-mode').value=f.mode||'RGBW';
    node.querySelector('.fx-map').value=(f.map||[]).join(',');
    node.querySelector('.fx-name').oninput = e=>f.name=e.target.value;
    node.querySelector('.fx-start').oninput= e=>f.start_channel=parseInt(e.target.value||'1');
    node.querySelector('.fx-mode').onchange= e=>f.mode=e.target.value;
    node.querySelector('.fx-map').oninput  = e=>f.map=e.target.value.split(',').map(s=>s.trim()).filter(Boolean);
    host.appendChild(node);
  });
}
function addFixture(){ STATE.fixtures.push({name:'New', start_channel:1, mode:'RGBW', map:[]}); renderFixtures(); }
function delFixture(btn){
  const idx=parseInt(btn.closest('.card').dataset.idx);
  STATE.fixtures.splice(idx,1); renderFixtures();
}

// ===== Scenes / Buttons =====
function nextFreeIndex(){
  const used = new Set((STATE.buttons||[]).map(b => parseInt(b.index)));
  let i = 0;
  while (used.has(i)) i++;
  return i;
}

function renderButtons(){
  const host=el('buttons'); host.innerHTML='';
  const tpl=el('tpl-button');

  STATE.buttons.sort((a,b)=>a.index-b.index).forEach((b,idx)=>{
    const node=tpl.content.firstElementChild.cloneNode(true);
    node.dataset.idx=idx;

    node.querySelector('.bt-index-text').textContent = String(b.index);
    node.querySelector('.bt-label').value=b.label||'';
    node.querySelector('.uri-text').textContent = `/trigger/${b.index}?fade_ms=2000`;

    const typeSel=node.querySelector('.bt-type');
    typeSel.value=b.scene?.type||'static';

    node.querySelector('.bt-label').oninput=e=>b.label=e.target.value;

    typeSel.onchange=e=>{
      const t=e.target.value;
      if (t==='blackout'){ b.scene={type:'blackout'}; }
      else if (t==='static'){ b.scene={type:'static', all:{r:255,g:190,b:120,w:0}}; }
      else if (t==='per-fixture'){ b.scene={type:'per-fixture', values:{}}; }
      else if (t==='sequence'){
        b.scene={ type:'sequence', hold_ms:200, crossfade_ms:400,
          steps:[ {all:{r:255,g:0,b:0,w:0}}, {all:{r:0,g:0,b:255,w:0}} ] };
      }
      renderButtons();
    };

    const body=node.querySelector('.bt-body');
    const s=b.scene||{type:'static'};

    if (s.type==='static'){
      s.all = s.all || {r:255,g:190,b:120,w:0};
      body.appendChild(colorRow('All', s.all, (c)=>{ s.all=c; }));
    }
    else if (s.type==='per-fixture'){
      s.values = s.values || {};
      STATE.fixtures.forEach(f=>{
        s.values[f.name]=s.values[f.name]||{r:0,g:0,b:0,w:0};
        body.appendChild(colorRow(f.name, s.values[f.name], (c)=>{ s.values[f.name]=c; }));
      });
    }
    else if (s.type==='sequence'){
      const ctrl=document.createElement('div'); ctrl.className='row';
      ctrl.innerHTML=`<label>hold_ms <input class="hold" type="number" value="${s.hold_ms??0}" style="width:100px"></label>
                      <label>crossfade_ms <input class="xf" type="number" value="${s.crossfade_ms??0}" style="width:120px"></label>
                      <button class="btn btn-primary" onclick="addSeqStep(${idx})">+ Add Step</button>`;
      body.appendChild(ctrl);
      ctrl.querySelector('.hold').oninput=e=>s.hold_ms=parseInt(e.target.value||'0');
      ctrl.querySelector('.xf').oninput  =e=>s.crossfade_ms=parseInt(e.target.value||'0');

      (s.steps||[]).forEach((st, si)=>{
        body.appendChild(renderSeqStep(b, s, st, si));
      });
    }

    host.appendChild(node);
  });
}

function renderSeqStep(button, scene, step, si){
  const box=document.createElement('div');
  box.className='card step-card';

  const head=document.createElement('div'); head.className='row';

  const typ = step.values ? 'per-fixture' : 'all';
  const holdVal = step.hold_ms ?? '';
  const xfVal   = step.crossfade_ms ?? '';

  head.innerHTML = `
    <b>Step ${si+1}</b>
    <span class="tiny">Typ</span>
    <select class="s-type" style="width:220px">
      <option value="all"${typ==='all'?' selected':''}>all (für alle gleich)</option>
      <option value="per-fixture"${typ==='per-fixture'?' selected':''}>per-fixture (pro Leuchte)</option>
    </select>
    <span class="tiny">hold_ms</span><input class="s-hold" type="number" value="${holdVal}" style="width:90px" placeholder="global">
    <span class="tiny">crossfade_ms</span><input class="s-xf" type="number" value="${xfVal}" style="width:110px" placeholder="global">
    <button class="btn danger" onclick="delStep(${STATE.buttons.indexOf(button)},${si})">Delete</button>
  `;
  box.appendChild(head);

  const body=document.createElement('div'); body.className='row'; body.style.display='block';
  box.appendChild(body);

  function renderBody(){
    body.innerHTML='';
    if (step.values){
      step.values = step.values || {};
      STATE.fixtures.forEach(f=>{
        step.values[f.name] = step.values[f.name] || {r:0,g:0,b:0,w:0};
        body.appendChild(colorRow(f.name, step.values[f.name], (c)=>{ step.values[f.name]=c; }));
      });
    } else {
      step.all = step.all || {r:0,g:0,b:0,w:0};
      body.appendChild(colorRow('All', step.all, (c)=>{ step.all=c; }));
    }
  }
  renderBody();

  head.querySelector('.s-type').onchange = (e)=>{
    const t=e.target.value;
    if (t==='all'){
      const first = (step.values && Object.values(step.values)[0]) || {r:0,g:0,b:0,w:0};
      step.all = step.all || {...first};
      delete step.values;
    }else{
      step.values = step.values || {};
      if (step.all){
        STATE.fixtures.forEach(f=>{
          step.values[f.name] = step.values[f.name] || {...step.all};
        });
      }else{
        STATE.fixtures.forEach(f=>{
          step.values[f.name] = step.values[f.name] || {r:0,g:0,b:0,w:0};
        });
      }
      delete step.all;
    }
    renderBody();
  };

  head.querySelector('.s-hold').oninput = e=>{
    const v=e.target.value;
    step.hold_ms = (v===''? null : parseInt(v));
  };
  head.querySelector('.s-xf').oninput = e=>{
    const v=e.target.value;
    step.crossfade_ms = (v===''? null : parseInt(v));
  };

  return box;
}

function addSeqStep(buttonIdx){
  const b=STATE.buttons[buttonIdx];
  b.scene.steps=b.scene.steps||[];
  b.scene.steps.push({ all:{r:0,g:0,b:0,w:0} });
  renderButtons();
}

function delStep(buttonIdx, stepIdx){
  const b=STATE.buttons[buttonIdx];
  b.scene.steps.splice(stepIdx,1);
  renderButtons();
}

// ===== State I/O =====
async function reloadState(){
  const r=await fetch('/api/state'); const j=await r.json();
  STATE=j;
  el('universe').value=STATE.universe??1;
  el('def-dimmer').value=STATE.defaults?.dimmer ?? 255;
  el('def-strobe').value=STATE.defaults?.strobe ?? 0;
  renderFixtures(); renderButtons();
  const d=await (await fetch('/api/dim')).json();
  el('dim-val').textContent=(d.level ?? 100)+'%';
  el('dim-set').value=d.level ?? 100;
}
async function saveState(){
  STATE.universe=parseInt(el('universe').value||'1');
  STATE.defaults={dimmer:parseInt(el('def-dimmer').value||'255'), strobe:parseInt(el('def-strobe').value||'0')};
  const resp=await fetch('/api/state',{method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(STATE)});
  status(resp.ok?'Gespeichert':'Fehler beim Speichern');
}

// ===== Dimmer UI =====
async function dimStep(delta){
  const r=await fetch('/api/dim/step?delta='+delta,{method:'POST'}); const text=await r.text();
  const lvl = parseInt(text);
  el('dim-val').textContent=lvl+'%'; el('dim-set').value=lvl;
}
async function dimSet(){
  const pct=parseInt(el('dim-set').value||'100');
  const r=await fetch('/api/dim/set?percent='+pct,{method:'POST'}); const j=await r.json();
  el('dim-val').textContent=j.level+'%';
}

async function triggerScene(btnEl){
  const idx=parseInt(btnEl.closest('.card').dataset.idx);
  const scene=STATE.buttons[idx].scene;
  await fetch('/api/test_scene', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ scene, fade_ms:0, duration_ms:3000 })});
  status('Test Szene abgespielt');
}

function addButton(){
  const nextIdx = nextFreeIndex();
  STATE.buttons.push({index:nextIdx, label:'New Scene', scene:{type:'static', all:{r:255,g:190,b:120,w:0}}});
  renderButtons();
}
function delButton(btn){
  const idx=parseInt(btn.closest('.card').dataset.idx);
  STATE.buttons.splice(idx,1);
  renderButtons();
}

reloadState();
</script>
"""

@app.get("/admin")
def admin_page():
    if not check_token(): abort(401)
    return render_template_string(ADMIN_HTML, logo_url=LOGO_URL)

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
    app.run(host=BIND_HOST, port=BIND_PORT)
