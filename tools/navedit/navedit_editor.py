# -*- coding: utf-8 -*-
"""
Offhangar Standalone 3D Route & Low-Poly Mesh Editor (NavEdit)
Runs on port 8765 (completely separate from the Live 3D Bot Tracker on port 8768).
Loads maps3d/<map>/ (color.jpg, meta.json, terrain.bin, models.bin) when available,
or falls back to res/packages/gui.pkg + offhangar/maps_obb.json + navgraphs/<map>.json.
Saves edited 3D models/OBBs and 30-stage auto-traced routes directly to the mod and updates manifest.json!
"""
import hashlib
import json
import math
import os
import sys
import threading
import time

try:
    from BaseHTTPServer import HTTPServer, BaseHTTPRequestHandler
except ImportError:
    from http.server import HTTPServer, BaseHTTPRequestHandler

try:
    _UNICODE_TYPE = unicode
except NameError:
    _UNICODE_TYPE = str

HTTP_PORT = 8765
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_THIS_DIR))
import repo_env

repo_env.load_env()  # WOT_082_DIR -> bot_subengine finds res/packages for map textures
repo_env.install_mod_imports()
_OFFHANGAR_DIR = repo_env.MOD_DIR  # edits are saved into src/offhangar

from gui.mods.offhangar.ai import bot_subengine as _bsub

_MAPS_OBB_PATH = os.path.join(_OFFHANGAR_DIR, 'maps_obb.json')
_CURRENT_EDIT_MAP = '42_north_america'


def _save_edited_map_to_disk(payload):
    """Saves edited 3D low-poly models/OBBs and 30-stage auto-baked routes to maps_obb.json, navgraphs/<map>.json, and manifest.json."""
    try:
        mname = str(payload.get('map') or _CURRENT_EDIT_MAP or '').strip()
        if not mname:
            return False
        db = _bsub._load_maps_database()
        if mname in db and isinstance(payload.get('buildings'), list):
            db[mname]['obbs'] = payload['buildings']
            with open(_MAPS_OBB_PATH, 'w') as wf:
                json.dump(db, wf, separators=(',', ':'))
        ng_dir = os.path.join(_OFFHANGAR_DIR, 'navgraphs')
        ng_path = os.path.join(ng_dir, mname + '.json')
        if os.path.isfile(ng_path) and isinstance(payload.get('routes'), list):
            with open(ng_path, 'r') as rf:
                ng = json.load(rf)
            routes_dict = {'1': [], '2': []}
            for rt in payload['routes']:
                tm_s = str(rt.get('team', 1))
                wps = [[round(float(p[0]), 1), round(float(p[2]), 1), False] for p in (rt.get('pts') or []) if len(p) >= 3]
                if wps:
                    routes_dict.setdefault(tm_s, []).append({
                        'id': str(rt.get('id', 'route')),
                        'capacity': 4,
                        'waypoints': wps,
                    })
            if routes_dict['1'] or routes_dict['2']:
                ng['routes'] = routes_dict
            with open(ng_path, 'w') as wf:
                json.dump(ng, wf, separators=(',', ':'))
            man_path = os.path.join(ng_dir, 'manifest.json')
            if os.path.isfile(man_path):
                with open(man_path, 'r') as mf:
                    man = json.load(mf)
                raw_ng = open(ng_path, 'rb').read()
                for minfo in man.get('maps', []):
                    if isinstance(minfo, dict) and minfo.get('map') == mname:
                        minfo['sha256'] = hashlib.sha256(raw_ng).hexdigest()
                        minfo['bytes'] = len(raw_ng)
                with open(man_path, 'w') as mf:
                    json.dump(man, mf, indent=2, sort_keys=True)
        _bsub.set_active_map(mname)
        return True
    except Exception:
        return False


class NavEditHTTPHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global _CURRENT_EDIT_MAP
        try:
            if self.path == '/' or self.path.startswith('/edit') or self.path.startswith('/view'):
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                html = VIEWER_HTML.replace('__MAP_NAME__', _CURRENT_EDIT_MAP)
                if isinstance(html, _UNICODE_TYPE):
                    html = html.encode('utf-8')
                self.wfile.write(html)
            elif self.path.startswith('/maps_list'):
                db = _bsub._load_maps_database()
                all_m = sorted(db.keys())
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                out = json.dumps({'maps': all_m, 'current': _CURRENT_EDIT_MAP})
                if isinstance(out, _UNICODE_TYPE):
                    out = out.encode('utf-8')
                self.wfile.write(out)
            elif self.path.startswith('/map_geom'):
                if '?map=' in self.path:
                    req_m = self.path.split('?map=', 1)[1].split('&')[0]
                    if req_m:
                        _CURRENT_EDIT_MAP = req_m
                        _bsub.set_active_map(req_m)
                else:
                    _bsub.set_active_map(_CURRENT_EDIT_MAP)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(_bsub._ensure_radar_map_json())
            elif self.path.startswith('/map_tex'):
                req_m = _CURRENT_EDIT_MAP
                if '?map=' in self.path:
                    req_m = self.path.split('?map=', 1)[1].split('&')[0] or req_m
                mime, img_bytes = _bsub._load_map_texture_bytes(req_m)
                if img_bytes:
                    self.send_response(200)
                    self.send_header('Content-Type', mime)
                    self.send_header('Cache-Control', 'max-age=3600')
                    self.end_headers()
                    self.wfile.write(img_bytes)
                else:
                    self.send_response(404)
                    self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()
        except Exception:
            pass

    def do_POST(self):
        try:
            if self.path.startswith('/save_map'):
                length = int(self.headers.get('Content-Length', 0) or 0)
                body = self.rfile.read(length) if length > 0 else b'{}'
                if isinstance(body, bytes):
                    body = body.decode('utf-8')
                payload = json.loads(body)
                ok = _save_edited_map_to_disk(payload)
                self.send_response(200 if ok else 500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                resp = '{"ok":true}' if ok else '{"ok":false}'
                if isinstance(resp, _UNICODE_TYPE):
                    resp = resp.encode('utf-8')
                self.wfile.write(resp)
            else:
                self.send_response(404)
                self.end_headers()
        except Exception:
            pass

    def log_message(self, format, *args):
        pass


def _http_server_thread():
    server = HTTPServer(('127.0.0.1', HTTP_PORT), NavEditHTTPHandler)
    server.serve_forever()


VIEWER_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <title>Offhangar 3D NavEdit // Редактор Хитбоксов, 3D-Сетки и Авто-Обкат 30 Ботов</title>
    <style>
        * { box-sizing: border-box; }
        body { margin: 0; padding: 0; background: #070b10; color: #f1f5f9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; overflow: hidden; user-select: none; }
        #canvas { display: block; width: 100vw; height: 100vh; cursor: grab; }
        #canvas.editing { cursor: crosshair; }
        #ui { position: absolute; top: 10px; left: 10px; background: rgba(11, 16, 26, 0.95); border: 1px solid #263449; border-radius: 10px; padding: 12px 15px; width: 430px; box-shadow: 0 10px 36px rgba(0,0,0,0.75); backdrop-filter: blur(6px); z-index: 10; max-height: calc(100vh - 76px); overflow-y: auto; }
        h1 { margin: 0 0 6px 0; font-size: 13px; font-weight: 800; color: #38bdf8; letter-spacing: 0.5px; display: flex; align-items: center; gap: 8px; }
        .live-dot { width: 9px; height: 9px; background: #10b981; border-radius: 50%; box-shadow: 0 0 10px #10b981; }
        .row { font-size: 11.5px; color: #94a3b8; margin: 4px 0; display: flex; flex-wrap: wrap; gap: 10px; align-items: center; justify-content: space-between; }
        .val { color: #f8fafc; font-weight: 700; }
        select, input[type=number], input[type=text] { background: #141e30; color: #38bdf8; border: 1px solid #2d3f58; border-radius: 6px; padding: 4px 7px; font-size: 11.5px; font-weight: 600; }
        select { width: 100%; margin: 3px 0 6px 0; cursor: pointer; border-color: #38bdf8; }
        .legend { margin-top: 7px; display: flex; flex-wrap: wrap; gap: 5px 9px; font-size: 10.5px; padding-top: 7px; border-top: 1px solid #1e293b; }
        .tag { display: inline-flex; align-items: center; gap: 4px; color: #cbd5e1; }
        .dot { width: 9px; height: 9px; border-radius: 2px; }
        #toolbar { position: absolute; top: 10px; right: 10px; background: rgba(11, 16, 26, 0.95); border: 1px solid #263449; border-radius: 10px; padding: 8px 10px; display: flex; flex-wrap: wrap; gap: 5px; max-width: calc(100vw - 460px); justify-content: flex-end; box-shadow: 0 10px 36px rgba(0,0,0,0.75); z-index: 10; }
        button { background: #162032; color: #cbd5e1; border: 1px solid #2d3f58; border-radius: 6px; padding: 5px 9px; font-size: 11px; font-weight: 600; cursor: pointer; transition: all 0.12s ease; }
        button:hover { background: #1e2d44; color: #38bdf8; border-color: #38bdf8; }
        button.active { background: #0284c7; border-color: #38bdf8; color: #fff; }
        button.mode3d { background: #059669; border-color: #34d399; color: #fff; }
        button.warn { background: #b45309; border-color: #f59e0b; color: #fff; }
        button.magic { background: #7c3aed; border-color: #a78bfa; color: #fff; box-shadow: 0 0 12px rgba(124, 58, 237, 0.4); }
        button.savebtn { background: #0f766e; border-color: #2dd4bf; color: #fff; }
        .panel-box { margin-top: 8px; padding: 8px 10px; background: rgba(20, 30, 48, 0.85); border: 1px solid #2d3f58; border-radius: 8px; font-size: 11px; }
        .panel-title { font-weight: 800; color: #38bdf8; margin-bottom: 6px; display: flex; justify-content: space-between; align-items: center; }
        .ctrl-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 5px 8px; align-items: center; }
        .ctrl-grid label { color: #94a3b8; font-size: 10.5px; display: flex; justify-content: space-between; align-items: center; gap: 4px; }
        .ctrl-grid input[type=range] { width: 95px; accent-color: #38bdf8; }
        #bake-progress { display: none; margin-top: 8px; padding: 8px 10px; background: rgba(88, 28, 135, 0.35); border: 1px solid #a855f7; border-radius: 8px; }
        .pbar-bg { width: 100%; height: 7px; background: #1e293b; border-radius: 4px; overflow: hidden; margin-top: 5px; }
        .pbar-fill { height: 100%; background: linear-gradient(90deg, #38bdf8, #a855f7, #10b981); width: 0%; transition: width 0.08s linear; }
        #bottom-bar { position: absolute; bottom: 10px; left: 10px; right: 10px; background: rgba(11, 16, 26, 0.94); border: 1px solid #263449; border-radius: 10px; padding: 7px 12px; display: flex; align-items: center; gap: 10px; font-size: 11px; z-index: 10; }
        #bottom-bar input[type=range] { flex: 1; accent-color: #38bdf8; cursor: pointer; }
        #toast { position: fixed; top: 68px; left: 50%; transform: translateX(-50%); background: rgba(16, 185, 129, 0.95); color: #042f2e; font-weight: 800; padding: 8px 18px; border-radius: 8px; font-size: 12.5px; display: none; z-index: 100; box-shadow: 0 8px 24px rgba(0,0,0,0.6); }
    </style>
</head>
<body>
    <div id="toast">Сохранено!</div>
    <div id="ui">
        <h1><span class="live-dot"></span> OFFHANGAR 3D NAVEDIT // РЕДАКТОР И 30-ЭТАПНЫЙ АВТО-ОБКАТ</h1>
        <select id="map-select" onchange="switchMap(this.value)"></select>
        <div class="row">
            <span>Камера: <span id="cam-lbl" class="val" style="color:#34d399">3D Перспектива (Прямая)</span></span>
            <span>Ботов: <span id="bot-cnt" class="val" style="color:#10b981">30</span></span>
            <span>FPS: <span id="fps-lbl" class="val">60</span></span>
        </div>
        <div class="row">
            <span>Неразруш. (0): <span id="solid-cnt" class="val" style="color:#38bdf8">0</span></span>
            <span>Непреод. разр. (3): <span id="imp-destr-cnt" class="val" style="color:#f43f5e">0</span></span>
            <span>Преод. разр. (1): <span id="pass-destr-cnt" class="val" style="color:#f59e0b">0</span></span>
        </div>
        <div class="row">
            <span>Горочки/Подъёмы (2): <span id="rubble-cnt" class="val" style="color:#a3e635">0</span></span>
            <span>Окна и Позиции: <span id="win-cnt" class="val" style="color:#d946ef">0</span></span>
        </div>

        <!-- Инструменты Редактора Хитбоксов (OBB) и 3D-сетки -->
        <div class="panel-box">
            <div class="panel-title">
                <span>🛠️ РЕДАКТОР ХИТБОКСОВ (OBB) И СЕТКИ</span>
                <span id="edit-mode-badge" style="color:#fbbf24;font-size:10px">Режим: Просмотр</span>
            </div>
            <div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:6px">
                <button id="btn-tool-view" class="active" onclick="setEditTool('view')">👁️ Камера</button>
                <button id="btn-tool-obb" onclick="setEditTool('obb')">📦 Двигать/Править OBB</button>
                <button id="btn-tool-brush" onclick="setEditTool('brush')">🖌️ Кисть 3D-сетки</button>
                <button onclick="addNewOBB()">➕ Новый OBB</button>
                <button onclick="deleteSelectedOBB()" style="color:#f87171">🗑️ Удалить</button>
            </div>

            <!-- Панель выбранного хитбокса дома/вагона -->
            <div id="obb-editor-box" style="display:none;border-top:1px solid #2d3f58;padding-top:6px">
                <div style="color:#38bdf8;font-weight:700;margin-bottom:4px" id="sel-obb-title">Хитбокс #0</div>
                <div class="ctrl-grid">
                    <label>X: <input type="number" id="obb-x" step="0.5" style="width:68px" onchange="applyOBBInputs()"></label>
                    <label>Z: <input type="number" id="obb-z" step="0.5" style="width:68px" onchange="applyOBBInputs()"></label>
                    <label>Шир: <input type="range" id="obb-hw" min="0.5" max="28" step="0.2" oninput="applyOBBSliders()"><span id="lbl-hw">2.0</span></label>
                    <label>Длн: <input type="range" id="obb-hl" min="0.5" max="28" step="0.2" oninput="applyOBBSliders()"><span id="lbl-hl">4.0</span></label>
                    <label>Угол: <input type="range" id="obb-yaw" min="-3.14" max="3.14" step="0.03" oninput="applyOBBSliders()"><span id="lbl-yaw">0°</span></label>
                    <label>Выс: <input type="range" id="obb-h" min="1.0" max="25" step="0.5" oninput="applyOBBSliders()"><span id="lbl-h">6м</span></label>
                </div>
                <div style="margin-top:5px;display:flex;align-items:center;justify-content:space-between;gap:6px">
                    <span style="color:#94a3b8">Тип хитбокса:</span>
                    <select id="obb-flag" style="width:240px;margin:0" onchange="applyOBBSliders()">
                        <option value="0">🏢 0: Неразрушаемое здание / Скала</option>
                        <option value="3">🚧 3: Разрушаемое непреодолимое (Вагон/Надолб)</option>
                        <option value="2">⛰️ 2: Разрушенная горочка / Подъём / Мост</option>
                        <option value="1">🪵 1: Разрушаемое преодолимое (Дом/Забор)</option>
                    </select>
                </div>
                <div style="font-size:10px;color:#94a3b8;margin-top:4px">
                    💡 Тяни хитбокс мышью (ЛКМ) прямо по текстуре карты! Колесо + Shift крутит угол, пересчёт 3D-сетки идёт налету.
                </div>
            </div>

            <!-- Панель кисти 3D-сетки -->
            <div id="brush-editor-box" style="display:none;border-top:1px solid #2d3f58;padding-top:6px">
                <div style="display:flex;align-items:center;justify-content:space-between;gap:6px">
                    <span>Тип точки:</span>
                    <select id="brush-flag" style="width:210px;margin:0">
                        <option value="1">🟢 1: Проходимая дорога / грунт</option>
                        <option value="3">⛰️ 3: Разрушенная горочка (Завал)</option>
                        <option value="0">🔴 0: Сплошная стена / Непроходимо</option>
                        <option value="4">🔵 4: Глубокая вода</option>
                        <option value="5">🟠 5: Крутой обрыв / Скала</option>
                    </select>
                </div>
                <div style="display:flex;align-items:center;justify-content:space-between;margin-top:4px">
                    <span>Радиус кисти: <b id="lbl-brad">8м</b></span>
                    <input type="range" id="brush-rad" min="4" max="32" step="4" value="8" oninput="document.getElementById('lbl-brad').innerText=this.value+'м'">
                </div>
            </div>
        </div>

        <!-- 30-этапная авто-трассировка и обкат ботов -->
        <div class="panel-box">
            <div class="panel-title">
                <span>🤖 АВТО-ТРАССИРОВКА (30 ЭТАПОВ) И ОБКАТ БОТОВ</span>
            </div>
            <div style="display:flex;flex-wrap:wrap;gap:5px">
                <button class="magic" onclick="run30StageAutoBaker()">⚡ Авто-маршруты (30 этапов)</button>
                <button id="btn-sim-bots" class="mode3d" onclick="toggleBotSim()">🚜 Тест-обкат 30 ботов</button>
                <button onclick="resetSimBots()">🔄 Респаун ботов</button>
            </div>
            <div id="bake-progress">
                <div style="display:flex;justify-content:space-between;font-weight:700;color:#e9d5ff">
                    <span id="bake-stage-lbl">Этап 1 / 30: A* Разведка коридоров...</span>
                    <span id="bake-pct-lbl">0%</span>
                </div>
                <div class="pbar-bg"><div class="pbar-fill" id="bake-bar"></div></div>
            </div>
        </div>

        <!-- Сохранение и Текстура игры -->
        <div style="display:flex;flex-wrap:wrap;gap:5px;margin-top:8px">
            <button class="savebtn" onclick="saveMapToMod()">💾 Сохранить в мод</button>
            <button onclick="exportMapJSON()">📥 Скачать JSON</button>
            <button onclick="document.getElementById('tex-input').click()">🖼️ Своя текстура (.png)</button>
            <input type="file" id="tex-input" accept="image/*" style="display:none" onchange="loadCustomTexture(event)">
        </div>

        <div class="legend">
            <span class="tag"><span class="dot" style="background:#38bdf8"></span> <span id="leg-c0">Неразрушаемые здания</span></span>
            <span class="tag"><span class="dot" style="background:#f43f5e"></span> <span id="leg-c3">Непреодолимые разр.</span></span>
            <span class="tag"><span class="dot" style="background:#f59e0b"></span> <span id="leg-c1">Разрушаемые преодолимые</span></span>
            <span class="tag"><span class="dot" style="background:#a3e635"></span> <span id="leg-c2">Горочки и подъёмы</span></span>
            <span class="tag"><span class="dot" style="background:#d946ef"></span> Окна / Бойницы (по габаритам танка)</span>
        </div>
    </div>

    <div id="toolbar">
        <button id="btn-mode3d" class="active mode3d" onclick="toggle3D()">🎥 3D Перспектива</button>
        <button id="btn-tex" class="active" onclick="toggleOpt('tex')">🗺️ Текстура карты</button>
        <button id="btn-grid3d" class="active" onclick="toggleOpt('grid3d')">🟢 Точечная 3D-сетка</button>
        <button id="btn-buildings" class="active" onclick="toggleOpt('buildings')">🏢 Неразрушаемые (0)</button>
        <button id="btn-wagons" class="active" onclick="toggleOpt('wagons')">🚧 Непреодолимые разр. (3)</button>
        <button id="btn-destr" class="active" onclick="toggleOpt('destr')">🪵 Разрушаемые преод. (1)</button>
        <button id="btn-rubble" class="active" onclick="toggleOpt('rubble')">⛰️ Горочки / Подъёмы (2)</button>
        <button id="btn-pockets" class="active" onclick="toggleOpt('pockets')">🎯 Окна / Позиции</button>
        <button id="btn-routes" class="active" onclick="toggleOpt('routes')">🛣️ Маршруты (A*)</button>
        <button id="btn-whiskers" class="active" onclick="toggleOpt('whiskers')">📡 16 Лучей (360°)</button>
        <button id="btn-tracks" class="active" onclick="toggleOpt('tracks')">🚜 Боты / Реплей</button>
        <button onclick="resetCamera()">Сброс камеры</button>
    </div>

    <div id="bottom-bar">
        <button id="btn-play" class="active" onclick="togglePlay()">⏸ Пауза</button>
        <span>Скорость:</span>
        <button onclick="simSpeed=1" id="spd-1">1x</button>
        <button onclick="simSpeed=3" id="spd-3" class="active">3x</button>
        <button onclick="simSpeed=8" id="spd-8">8x</button>
        <span>Время: <b id="time-lbl">15.0s</b></span>
        <input id="time-slider" type="range" min="0" max="250" step="0.5" value="15" oninput="scrubTime(this.value)">
        <span style="color:#94a3b8">ЛКМ: Вращение 3D (или двигать OBB в режиме 📦) | ПКМ/Shift+ЛКМ: Панорама | Колесо: Зум</span>
    </div>

    <canvas id="canvas"></canvas>

    <script>
        const IS_STANDALONE_BUNDLE = (typeof BUNDLE !== 'undefined');
        const canvas = document.getElementById('canvas');
        const ctx = canvas.getContext('2d', { alpha: false });
        let width = window.innerWidth, height = window.innerHeight;

        let is3D = true;
        // Right-side-up 3D Orbit Camera
        let camYaw = 0.0, camPitch = 0.78, camDist = 640.0;
        let camTx = 0.0, camTy = 8.0, camTz = 0.0;
        let scale2D = 1.0, panX2D = 640, panY2D = 360;

        let editTool = 'view'; // 'view' | 'obb' | 'brush'
        let selectedOBBIndex = -1;
        let isDraggingOBB = false;
        let isBrushing = false;
        let dragMode = 0, lastMx = 0, lastMy = 0, clickMoved = false;

        let curMapKey = '42_north_america';
        let curData = null;
        let allObbs = [];
        let selectedVid = null;
        let simTime = 15.0, maxTime = 200.0, isPlaying = true, simSpeed = 3.0;
        let liveSimActive = false;
        let simBots = [];

        // Pre-rendered procedural + game texture canvas for ground underlay
        const texCanvas = document.createElement('canvas');
        texCanvas.width = 1024;
        texCanvas.height = 1024;
        const texCtx = texCanvas.getContext('2d');
        let customMapImg = null;
        let texReady = false;

        const MAP_FEATURES = {
            '01_karelia': { title: 'Карелия', c0: 'Скалы и валуны', c3: 'Тяжёлые укрепления / камни', c2: 'Подъёмы и каменные насыпи', c1: 'Разрушаемые заборы / деревья' },
            '02_malinovka': { title: 'Малиновка', c0: 'Каменные дома и церковь', c3: 'Тяжёлые деревенские постройки', c2: 'Разрушенные горочки / окопы', c1: 'Деревянные избы и плетни (Преодолимые)' },
            '03_campania': { title: 'Провинция', c0: 'Каменные здания и монастырь', c3: 'Тяжёлые каменные постройки', c2: 'Серпантины и завалы (Подъёмы)', c1: 'Виноградники, заборы и сараи (Преодолимые)' },
            '04_himmelsdorf': { title: 'Химмельсдорф', c0: 'Городские кварталы и замок', c3: 'Ж/Д Вагоны и составы (Непреодолимые)', c2: 'Разрушенные горочки и завалы (Подъёмы)', c1: 'Машины, ларьки и заборы (Преодолимые)' },
            '05_prohorovka': { title: 'Прохоровка', c0: 'Насыпь и вокзальные здания', c3: 'Ж/Д Вагоны на переезде (Непреодолимые)', c2: 'Переезды и холмы (Подъёмы)', c1: 'Деревянные избы и заборы (Преодолимые)' },
            '06_ensk': { title: 'Энск', c0: 'Кирпичные дома и заводы', c3: 'Ж/Д Вагоны и локомотивы (Непреодолимые)', c2: 'Разрушенные горочки и завалы', c1: 'Деревянные сараи и заборы (Преодолимые)' },
            '07_lakeville': { title: 'Ласвилль', c0: 'Каменные дома, кирха и скалы', c3: 'Тяжёлые городские постройки', c2: 'Завалы и подъёмы ущелья', c1: 'Деревянные пристройки и машины (Преодолимые)' },
            '08_ruinberg': { title: 'Руинберг', c0: 'Разрушенные кварталы и собор', c3: 'Тяжёлые руины и Ж/Д объекты', c2: 'Разрушенные горочки и проломы (Подъёмы)', c1: 'Деревянные постройки и заборы (Преодолимые)' },
            '10_hills': { title: 'Рудники', c0: 'Скалы, маяк и рудник', c3: 'Тяжёлые шахтные конструкции', c2: 'Центральный подъём и насыпи', c1: 'Деревянные дома и заборы (Преодолимые)' },
            '11_murovanka': { title: 'Мурованка', c0: 'Каменные дома и мельница', c3: 'Тяжёлые деревенские постройки', c2: 'Холмы и пологие подъёмы', c1: 'Деревянные избы и заборы (Преодолимые)' },
            '13_erlenberg': { title: 'Эрленберг', c0: 'Замок, каменные дома и опоры', c3: 'Тяжёлые городские постройки', c2: 'Каменные мосты и подъёмы', c1: 'Деревянные дома и заборы (Преодолимые)' },
            '14_siegfried_line': { title: 'Линия Зигфрида', c0: 'Бетонные ДОТы и кварталы', c3: 'Бетонные надолбы «Зубы дракона» (Непреодолимые)', c2: 'Воронки и разрушенные горочки (Подъёмы)', c1: 'Разрушаемые стены и заборы (Преодолимые)' },
            '15_komarin': { title: 'Комарин', c0: 'Каменные постройки и доты', c3: 'Надолбы и тяжёлые постройки', c2: 'Мосты и береговые подъёмы', c1: 'Деревянные избы и заборы (Преодолимые)' },
            '17_munchen': { title: 'Уайдпарк', c0: 'Городские дома и виадук', c3: 'Ж/Д Вагоны на насыпи (Непреодолимые)', c2: 'Ж/Д насыпь, арки и завалы (Подъёмы)', c1: 'Машины, заборы и будки (Преодолимые)' },
            '18_cliff': { title: 'Утёс', c0: 'Скалы и маяк', c3: 'Тяжёлые скальные уступы', c2: 'Горные тропы и подъёмы', c1: 'Деревянные постройки и заборы (Преодолимые)' },
            '19_monastery': { title: 'Монастырь', c0: 'Монастырь и каменные дома', c3: 'Тяжёлые каменные стены', c2: 'Террасы и подъёмы во двор', c1: 'Беседки, заборы и сараи (Преодолимые)' },
            '22_slough': { title: 'Топь', c0: 'Замки и скалы', c3: 'Тяжёлые каменные руины', c2: 'Мосты и болотные тропы', c1: 'Деревянные сараи и заборы (Преодолимые)' },
            '23_westfeld': { title: 'Вестфилд', c0: 'Каменные дома и опоры виадука', c3: 'Тяжёлые каменные постройки', c2: 'Разрушенные горочки и подъёмы', c1: 'Деревянные сараи и заборы (Преодолимые)' },
            '28_desert': { title: 'Песчаная река', c0: 'Скалы и глинобитные крепости', c3: 'Тяжёлые каменные постройки', c2: 'Барханы и песчаные подъёмы', c1: 'Глинобитные дувалы и навесы (Преодолимые)' },
            '29_el_hallouf': { title: 'Эль-Халлуф', c0: 'Скалы и каменные дома', c3: 'Тяжёлые каменные уступы', c2: 'Серпантины и подъёмы каньона', c1: 'Глинобитные постройки и заборы (Преодолимые)' },
            '31_airfield': { title: 'Аэродром', c0: 'Скалы и бункеры', c3: 'Ангары и тяжёлая техника (Непреодолимые)', c2: 'Холмы и взлётные подъёмы', c1: 'Палатки, заборы и пропсы (Преодолимые)' },
            '33_fjord': { title: 'Фьорды', c0: 'Скалы, доты и портовые дома', c3: 'Тяжёлые портовые постройки', c2: 'Горные перевалы и завалы (Подъёмы)', c1: 'Деревянные домики и заборы (Преодолимые)' },
            '34_redshire': { title: 'Редшир', c0: 'Замок и каменные дома', c3: 'Тяжёлые фермерские постройки', c2: 'Холмы, броды и мосты', c1: 'Деревянные изгороди и сараи (Преодолимые)' },
            '35_steppes': { title: 'Степи', c0: 'Скалы и насыпь', c3: 'Ж/Д Составы и валуны (Непреодолимые)', c2: 'Балки и степные подъёмы', c1: 'Заборы, телеги и сараи (Преодолимые)' },
            '36_fishing_bay': { title: 'Рыбацкая бухта', c0: 'Каменные дома и причал', c3: 'Тяжёлые городские постройки', c2: 'Холмы и центральные завалы', c1: 'Деревянные сараи, лодки и заборы (Преодолимые)' },
            '37_caucasus': { title: 'Перевал', c0: 'Скалы и опоры моста', c3: 'Тяжёлые скальные валуны', c2: 'Мост, ледник и горные подъёмы', c1: 'Деревянные постройки и заборы (Преодолимые)' },
            '38_mannerheim_line': { title: 'Заполярье', c0: 'Скалы и бетонные ДОТы', c3: 'Гранитные надолбы и укрепления', c2: 'Снежные перевалы и подъёмы', c1: 'Деревянные постройки и заборы (Преодолимые)' },
            '39_crimea': { title: 'Южный берег', c0: 'Скалы и каменные виллы', c3: 'Тяжёлые курортные постройки', c2: 'Серпантины, мосты и террасы', c1: 'Беседки, заборы и пляжные домики (Преодолимые)' },
            '42_north_america': { title: 'Порт', c0: 'Портовые склады и цеха', c3: 'Ж/Д Вагоны и составы (Непреодолимые)', c2: '3D Эстакады, мосты и завалы (Подъёмы)', c1: 'Машины, будки и заборы (Преодолимые)' },
            '44_north_america': { title: 'Лайв Окс', c0: 'Городские здания и вокзал', c3: 'Ж/Д Вагоны и мостовые опоры (Непреодолимые)', c2: 'Ж/Д мосты и болотные тропы', c1: 'Деревянные дома и заборы (Преодолимые)' },
            '45_north_america': { title: 'Хайвей', c0: 'Городские кварталы и заводы', c3: 'Ж/Д Вагоны и тяжёлые цеха (Непреодолимые)', c2: 'Мосты и подъёмы в город', c1: 'Фермы, заборы и машины (Преодолимые)' },
            '47_canada_a': { title: 'Тихий берег', c0: 'Скалы и здания вокзала', c3: 'Ж/Д Вагоны и тяжёлые дома (Непреодолимые)', c2: 'Ж/Д переезды и полуостров', c1: 'Деревянные коттеджи и заборы (Преодолимые)' },
            '51_asia': { title: 'Хребет Дракона', c0: 'Каменные храмы и скалы', c3: 'Тяжёлые каменные фундаменты (Непреодолимые)', c2: 'Рисовые террасы, мосты и подъёмы', c1: 'Деревянные хижины деревни и заборы (Преодолимые)' }
        };
        function getMapFeat(m) {
            return MAP_FEATURES[m] || { title: m, c0: 'Неразрушаемые здания / скалы', c3: 'Непреодолимые объекты', c2: 'Разрушенные горочки / подъёмы', c1: 'Разрушаемые преодолимые объекты' };
        }
        let opts = {
            tex: true,
            grid3d: true,
            rubble: true,
            wagons: true,
            buildings: true,
            routes: true,
            whiskers: true,
            tracks: true,
            destr: true,
            pockets: true
        };

        function showToast(msg) {
            const t = document.getElementById('toast');
            t.innerText = msg;
            t.style.display = 'block';
            setTimeout(() => { t.style.display = 'none'; }, 2600);
        }

        function isWagonShape(o) {
            const w = Math.min(o[2], o[3]);
            const l = Math.max(o[2], o[3]);
            const h = o[6] - o[5];
            return (w >= 1.3 && w <= 2.6 && l >= 2.1 && l <= 12.0 && h >= 2.3 && h <= 5.8);
        }

        function setEditTool(tool) {
            editTool = tool;
            ['view', 'obb', 'brush'].forEach(k => {
                const b = document.getElementById('btn-tool-' + k);
                if (b) b.className = (k === tool) ? 'active' : '';
            });
            document.getElementById('obb-editor-box').style.display = (tool === 'obb' && selectedOBBIndex >= 0) ? 'block' : 'none';
            document.getElementById('brush-editor-box').style.display = (tool === 'brush') ? 'block' : 'none';
            const badge = document.getElementById('edit-mode-badge');
            if (tool === 'view') {
                badge.innerText = 'Режим: Камера';
                badge.style.color = '#34d399';
                canvas.className = '';
            } else if (tool === 'obb') {
                badge.innerText = 'Режим: Правка OBB (Клик/Тяни)';
                badge.style.color = '#fbbf24';
                canvas.className = 'editing';
            } else {
                badge.innerText = 'Режим: Кисть 3D-сетки';
                badge.style.color = '#a855f7';
                canvas.className = 'editing';
            }
        }

        function bakeProceduralTexture() {
            if (!curData) return;
            texCtx.fillStyle = '#101824';
            texCtx.fillRect(0, 0, 1024, 1024);
            if (customMapImg && customMapImg.complete && customMapImg.naturalWidth > 0) {
                texCtx.drawImage(customMapImg, 0, 0, 1024, 1024);
                texReady = true;
                return;
            }
            const t = curData.terrain;
            if (!t || !t.heights_dm) return;
            const tw = t.w, th = t.h, hdm = t.heights_dm, fls = t.flags;
            let minH = 9999, maxH = -9999;
            for (let i = 0; i < hdm.length; i++) {
                const y = hdm[i] * 0.1;
                if (y < minH) minH = y;
                if (y > maxH) maxH = y;
            }
            const spanH = Math.max(4.0, maxH - minH);
            const cellW = 1024 / tw, cellH = 1024 / th;
            for (let gz = 0; gz < th; gz++) {
                const r0 = gz * tw;
                const py = (th - 1 - gz) * cellH;
                for (let gx = 0; gx < tw; gx++) {
                    const y = hdm[r0 + gx] * 0.1;
                    const hn = Math.max(0, Math.min(1, (y - minH) / spanH));
                    const fl = fls[r0 + gx];
                    // Hillshade from neighbor
                    const yR = (gx + 1 < tw) ? hdm[r0 + gx + 1] * 0.1 : y;
                    const yU = (gz + 1 < th) ? hdm[(gz + 1) * tw + gx] * 0.1 : y;
                    const shade = Math.max(0.55, Math.min(1.35, 1.0 + ((yR - y) - (yU - y)) * 0.12));
                    let r = (34 + hn * 55) * shade;
                    let g = (46 + hn * 52) * shade;
                    let b = (36 + hn * 38) * shade;
                    if (fl === 4) { r = 14; g = 65; b = 115; }
                    else if (fl === 5) { r = 95; g = 55; b = 38; }
                    else if (fl === 0) { r = 28; g = 34; b = 46; }
                    else if (fl === 3) { r = 74; g = 98; b = 42; }
                    texCtx.fillStyle = `rgb(${r|0},${g|0},${b|0})`;
                    texCtx.fillRect(gx * cellW, py, Math.ceil(cellW), Math.ceil(cellH));
                }
            }
            // Grid lines A1..K0
            texCtx.strokeStyle = 'rgba(255,255,255,0.12)';
            texCtx.lineWidth = 1.5;
            for (let i = 0; i <= 10; i++) {
                const p = i * 102.4;
                texCtx.beginPath(); texCtx.moveTo(p, 0); texCtx.lineTo(p, 1024); texCtx.stroke();
                texCtx.beginPath(); texCtx.moveTo(0, p); texCtx.lineTo(1024, p); texCtx.stroke();
            }
            texReady = true;
        }

        function loadCustomTexture(ev) {
            const file = ev.target.files[0];
            if (!file) return;
            const reader = new FileReader();
            reader.onload = e => {
                const img = new Image();
                img.onload = () => {
                    customMapImg = img;
                    bakeProceduralTexture();
                    showToast('Текстура карты загружена!');
                };
                img.src = e.target.result;
            };
            reader.readAsDataURL(file);
        }

        function updateCounts() {
            if (!curData) return;
            allObbs = curData.buildings || [];
            let c0 = 0, c3 = 0, c2 = 0, c1 = 0;
            for (let i = 0; i < allObbs.length; i++) {
                const fl = allObbs[i][7];
                if (fl === 0) c0++;
                else if (fl === 3) c3++;
                else if (fl === 2) c2++;
                else if (fl === 1) c1++;
            }
            const feat = getMapFeat(curMapKey);
            document.getElementById('leg-c0').innerText = feat.c0;
            document.getElementById('leg-c3').innerText = feat.c3;
            document.getElementById('leg-c2').innerText = feat.c2;
            document.getElementById('leg-c1').innerText = feat.c1;
            document.getElementById('solid-cnt').innerText = c0;
            document.getElementById('imp-destr-cnt').innerText = c3;
            document.getElementById('pass-destr-cnt').innerText = c1;
            document.getElementById('rubble-cnt').innerText = c2 + ' + ' + (curData.rubble_pts || []).length + ' тчк';
            document.getElementById('win-cnt').innerText = (curData.pockets || []).length;
        }

        function recomputeGridUnderOBB(obb) {
            if (!curData || !curData.terrain || !curData.terrain.flags) return;
            const t = curData.terrain;
            const tw = t.w, th = t.h, ox = t.ox, oz = t.oz, cs = t.cs;
            // Rebuild cells inside OBB bounding radius
            const cx = obb[0], cz = obb[1], hw = obb[2] + 0.5, hl = obb[3] + 0.5, yaw = obb[4], flag = obb[7];
            const rad = Math.hypot(hw, hl) + cs;
            const gx0 = Math.max(0, Math.floor((cx - rad - ox) / cs));
            const gx1 = Math.min(tw - 1, Math.ceil((cx + rad - ox) / cs));
            const gz0 = Math.max(0, Math.floor((cz - rad - oz) / cs));
            const gz1 = Math.min(th - 1, Math.ceil((cz + rad - oz) / cs));
            const c = Math.cos(yaw), s = Math.sin(yaw);
            for (let gz = gz0; gz <= gz1; gz++) {
                const wz = oz + gz * cs;
                for (let gx = gx0; gx <= gx1; gx++) {
                    const wx = ox + gx * cs;
                    const dx = wx - cx, dz = wz - cz;
                    const lx = dx * c - dz * s;
                    const lz = dx * s + dz * c;
                    const idx = gz * tw + gx;
                    if (Math.abs(lx) <= hw && Math.abs(lz) <= hl) {
                        if (flag === 0 || flag === 3) t.flags[idx] = 0;
                        else if (flag === 2) t.flags[idx] = 3;
                        else if (flag === 1 && t.flags[idx] === 0) t.flags[idx] = 1;
                    }
                }
            }
        }

        function selectOBB(idx) {
            selectedOBBIndex = idx;
            const box = document.getElementById('obb-editor-box');
            if (idx < 0 || idx >= allObbs.length) {
                box.style.display = 'none';
                return;
            }
            const o = allObbs[idx];
            box.style.display = 'block';
            const feat = getMapFeat(curMapKey);
            const clsName = (o[7]===0) ? feat.c0 : (o[7]===3 ? feat.c3 : (o[7]===2 ? feat.c2 : feat.c1));
            const winInfo = (o.length >= 10 && Array.isArray(o[9])) ? (o[9][3]===1 ? ` [Окно h=${(o[9][0]*0.1).toFixed(1)}м, шир=${(o[9][2]*0.1).toFixed(1)}м]` : ` [Подъём h=${(o[9][0]*0.1).toFixed(1)}м]`) : '';
            document.getElementById('sel-obb-title').innerText = `3D Модель #${idx}: ${clsName}${winInfo}`;
            document.getElementById('obb-x').value = o[0].toFixed(1);
            document.getElementById('obb-z').value = o[1].toFixed(1);
            document.getElementById('obb-hw').value = o[2];
            document.getElementById('obb-hl').value = o[3];
            document.getElementById('obb-yaw').value = o[4];
            const h = Math.max(1.0, o[6] - o[5]);
            document.getElementById('obb-h').value = h;
            document.getElementById('obb-flag').value = o[7];
            document.getElementById('lbl-hw').innerText = (o[2]*2).toFixed(1) + 'м';
            document.getElementById('lbl-hl').innerText = (o[3]*2).toFixed(1) + 'м';
            document.getElementById('lbl-yaw').innerText = Math.round(o[4] * 180 / Math.PI) + '°';
            document.getElementById('lbl-h').innerText = h.toFixed(1) + 'м';
        }

        function applyOBBInputs() {
            if (selectedOBBIndex < 0 || selectedOBBIndex >= allObbs.length) return;
            const o = allObbs[selectedOBBIndex];
            o[0] = parseFloat(document.getElementById('obb-x').value) || 0;
            o[1] = parseFloat(document.getElementById('obb-z').value) || 0;
            const gy = sampleY(o[0], o[1]);
            const h = Math.max(1.0, o[6] - o[5]);
            o[5] = gy - 0.3;
            o[6] = gy + h;
            recomputeGridUnderOBB(o);
            updateCounts();
        }

        function applyOBBSliders() {
            if (selectedOBBIndex < 0 || selectedOBBIndex >= allObbs.length) return;
            const o = allObbs[selectedOBBIndex];
            o[2] = parseFloat(document.getElementById('obb-hw').value);
            o[3] = parseFloat(document.getElementById('obb-hl').value);
            o[4] = parseFloat(document.getElementById('obb-yaw').value);
            const h = parseFloat(document.getElementById('obb-h').value);
            o[6] = o[5] + h;
            o[7] = parseInt(document.getElementById('obb-flag').value, 10);
            document.getElementById('lbl-hw').innerText = (o[2]*2).toFixed(1) + 'м';
            document.getElementById('lbl-hl').innerText = (o[3]*2).toFixed(1) + 'м';
            document.getElementById('lbl-yaw').innerText = Math.round(o[4] * 180 / Math.PI) + '°';
            document.getElementById('lbl-h').innerText = h.toFixed(1) + 'м';
            recomputeGridUnderOBB(o);
            updateCounts();
        }

        function addNewOBB() {
            if (!curData) return;
            setEditTool('obb');
            const gy = sampleY(camTx, camTz);
            const newObb = [
                Math.round(camTx * 10) / 10,
                Math.round(camTz * 10) / 10,
                4.0, 6.0, 0.0,
                Math.round((gy - 0.3) * 10) / 10,
                Math.round((gy + 7.5) * 10) / 10,
                0
            ];
            allObbs.push(newObb);
            recomputeGridUnderOBB(newObb);
            updateCounts();
            selectOBB(allObbs.length - 1);
            showToast('Добавлен новый 3D хитбокс в центре камеры!');
        }

        function deleteSelectedOBB() {
            if (selectedOBBIndex < 0 || selectedOBBIndex >= allObbs.length) return;
            const o = allObbs[selectedOBBIndex];
            o[7] = 1;
            recomputeGridUnderOBB(o);
            allObbs.splice(selectedOBBIndex, 1);
            selectedOBBIndex = -1;
            document.getElementById('obb-editor-box').style.display = 'none';
            updateCounts();
            showToast('Хитбокс удалён и ячейки разблокированы!');
        }

        // Ray-ground intersection for mouse picking/dragging in 3D and 2D
        let camEx = 0, camEy = 100, camEz = 0;
        let rxX = 1, rxY = 0, rxZ = 0, upX = 0, upY = 1, upZ = 0, fwX = 0, fwY = 0, fwZ = 1, fovScale = 900;

        function updateCam() {
            const cp = Math.cos(camPitch), sp = Math.sin(camPitch);
            const cy = Math.cos(camYaw), sy = Math.sin(camYaw);
            camEx = camTx - sy * cp * camDist;
            camEy = camTy + sp * camDist;
            camEz = camTz - cy * cp * camDist;
            fwX = sy * cp; fwY = -sp; fwZ = cy * cp;
            rxX = cy; rxY = 0; rxZ = -sy;
            // FIXED UP VECTOR: Forward x Right so upY = +cos(camPitch) > 0 (NEVER upside down!)
            upX = sy * sp;
            upY = cp;
            upZ = cy * sp;
            fovScale = Math.min(width, height) * 0.92;
        }

        function project3D(wx, wy, wz) {
            if (!is3D) return [panX2D + wx * scale2D, panY2D - wz * scale2D, 100.0];
            const dx = wx - camEx, dy = (wy || 0) - camEy, dz = wz - camEz;
            const cz = dx * fwX + dy * fwY + dz * fwZ;
            if (cz < 4.0) return null;
            const cx = dx * rxX + dy * rxY + dz * rxZ;
            const cy = dx * upX + dy * upY + dz * upZ;
            const invZ = fovScale / cz;
            return [width * 0.5 + cx * invZ, height * 0.5 - cy * invZ, cz];
        }

        function screenToWorldXZ(sx, sy, planeY) {
            if (!is3D) {
                return [(sx - panX2D) / scale2D, -(sy - panY2D) / scale2D];
            }
            const ndcX = (sx - width * 0.5) / fovScale;
            const ndcY = (height * 0.5 - sy) / fovScale;
            const dirX = fwX + ndcX * rxX + ndcY * upX;
            const dirY = fwY + ndcX * rxY + ndcY * upY;
            const dirZ = fwZ + ndcX * rxZ + ndcY * upZ;
            const py = (planeY !== undefined) ? planeY : camTy;
            if (Math.abs(dirY) < 1e-4) return [camTx, camTz];
            const t = (py - camEy) / dirY;
            if (t <= 0) return [camTx, camTz];
            return [camEx + dirX * t, camEz + dirZ * t];
        }

        function pickOBBAtScreen(sx, sy) {
            let bestIdx = -1, bestDist = 28.0;
            for (let i = 0; i < allObbs.length; i++) {
                const o = allObbs[i];
                if (!opts.destr && o[7] === 1) continue;
                if (!opts.rubble && o[7] === 2) continue;
                if (o[7] === 0 && !opts.buildings) continue;
                if (o[7] === 3 && !opts.wagons) continue;
                const midY = (o[5] + o[6]) * 0.5;
                const pr = project3D(o[0], midY, o[1]);
                if (!pr) continue;
                const d = Math.hypot(pr[0] - sx, pr[1] - sy);
                if (d < bestDist) {
                    bestDist = d;
                    bestIdx = i;
                }
            }
            if (bestIdx >= 0) return bestIdx;
            // Also check ground footprint hit
            const [wx, wz] = screenToWorldXZ(sx, sy, camTy);
            for (let i = allObbs.length - 1; i >= 0; i--) {
                const o = allObbs[i];
                if (!opts.destr && o[7] === 1) continue;
                const dx = wx - o[0], dz = wz - o[1];
                const c = Math.cos(o[4]), s = Math.sin(o[4]);
                const lx = dx * c - dz * s, lz = dx * s + dz * c;
                if (Math.abs(lx) <= o[2] + 1.0 && Math.abs(lz) <= o[3] + 1.0) {
                    return i;
                }
            }
            return -1;
        }

        function applyBrushAtScreen(sx, sy) {
            if (!curData || !curData.terrain || !curData.terrain.flags) return;
            const [wx, wz] = screenToWorldXZ(sx, sy, camTy);
            const t = curData.terrain;
            const rad = parseFloat(document.getElementById('brush-rad').value) || 8.0;
            const fl = parseInt(document.getElementById('brush-flag').value, 10);
            const gx0 = Math.max(0, Math.floor((wx - rad - t.ox) / t.cs));
            const gx1 = Math.min(t.w - 1, Math.ceil((wx + rad - t.ox) / t.cs));
            const gz0 = Math.max(0, Math.floor((wz - rad - t.oz) / t.cs));
            const gz1 = Math.min(t.h - 1, Math.ceil((wz + rad - t.oz) / t.cs));
            for (let gz = gz0; gz <= gz1; gz++) {
                const cz = t.oz + gz * t.cs;
                for (let gx = gx0; gx <= gx1; gx++) {
                    const cx = t.ox + gx * t.cs;
                    if ((cx - wx) * (cx - wx) + (cz - wz) * (cz - wz) <= rad * rad) {
                        t.flags[gz * t.w + gx] = fl;
                    }
                }
            }
        }

        // ====================================================================
        // 30-STAGE AUTONOMOUS BOT ROUTE BAKER & SMOOTHER
        // ====================================================================
        function isPointBlockedBySolidOBB(wx, wz, pad) {
            for (let i = 0; i < allObbs.length; i++) {
                const o = allObbs[i];
                if (o[7] !== 0 && o[7] !== 3) continue;
                const dx = wx - o[0], dz = wz - o[1];
                const r = o[2] + o[3] + pad;
                if (dx * dx + dz * dz > r * r) continue;
                const c = Math.cos(o[4]), s = Math.sin(o[4]);
                const lx = dx * c - dz * s, lz = dx * s + dz * c;
                if (Math.abs(lx) <= o[2] + pad && Math.abs(lz) <= o[3] + pad) {
                    const gy = sampleY(wx, wz);
                    if (gy >= o[5] - 1.6 && gy <= o[6] + 0.6) return true;
                }
            }
            return false;
        }

        function astarOnTerrainGrid(sx, sz, ex, ez, laneBiasX, laneBiasZ) {
            const t = curData.terrain;
            if (!t || !t.flags) return [[sx, sampleY(sx, sz), sz], [ex, sampleY(ex, ez), ez]];
            const tw = t.w, th = t.h, ox = t.ox, oz = t.oz, cs = t.cs;
            const clampX = x => Math.max(2, Math.min(tw - 3, Math.round((x - ox) / cs)));
            const clampZ = z => Math.max(2, Math.min(th - 3, Math.round((z - oz) / cs)));
            let gx0 = clampX(sx), gz0 = clampZ(sz);
            let gx1 = clampX(ex), gz1 = clampZ(ez);

            // Snap start/goal out of blocked cells
            const snapOpen = (cx, cz) => {
                for (let r = 0; r <= 12; r++) {
                    for (let dz = -r; dz <= r; dz++) {
                        for (let dx = -r; dx <= r; dx++) {
                            const nx = cx + dx, nz = cz + dz;
                            if (nx >= 1 && nx < tw - 1 && nz >= 1 && nz < th - 1) {
                                const fl = t.flags[nz * tw + nx];
                                const wx = ox + nx * cs, wz = oz + nz * cs;
                                if (fl !== 0 && fl !== 4 && fl !== 5 && !isPointBlockedBySolidOBB(wx, wz, 1.4)) {
                                    return [nx, nz];
                                }
                            }
                        }
                    }
                }
                return [cx, cz];
            };
            [gx0, gz0] = snapOpen(gx0, gz0);
            [gx1, gz1] = snapOpen(gx1, gz1);

            const N = tw * th;
            const gScore = new Float32Array(N);
            gScore.fill(1e9);
            const cameFrom = new Int32Array(N);
            cameFrom.fill(-1);
            const closed = new Uint8Array(N);

            const startIdx = gz0 * tw + gx0;
            const goalIdx = gz1 * tw + gx1;
            gScore[startIdx] = 0;
            const open = [[0, startIdx]];

            const dirs = [
                [1, 0, 1.0], [-1, 0, 1.0], [0, 1, 1.0], [0, -1, 1.0],
                [1, 1, 1.414], [1, -1, 1.414], [-1, 1, 1.414], [-1, -1, 1.414]
            ];

            let steps = 0;
            while (open.length > 0 && steps < 9000) {
                steps++;
                // Pop min f
                let bestI = 0;
                for (let i = 1; i < open.length; i++) {
                    if (open[i][0] < open[bestI][0]) bestI = i;
                }
                const curIdx = open[bestI][1];
                open[bestI] = open[open.length - 1];
                open.pop();

                if (curIdx === goalIdx) break;
                if (closed[curIdx]) continue;
                closed[curIdx] = 1;

                const cx = curIdx % tw, cz = (curIdx / tw) | 0;
                const curG = gScore[curIdx];
                const curY = t.heights_dm[curIdx] * 0.1;

                for (let d = 0; d < 8; d++) {
                    const nx = cx + dirs[d][0], nz = cz + dirs[d][1];
                    if (nx < 1 || nx >= tw - 1 || nz < 1 || nz >= th - 1) continue;
                    const nIdx = nz * tw + nx;
                    if (closed[nIdx]) continue;
                    const fl = t.flags[nIdx];
                    if (fl === 0 || fl === 4 || fl === 5) continue;
                    const wx = ox + nx * cs, wz = oz + nz * cs;
                    if (isPointBlockedBySolidOBB(wx, wz, 1.5)) continue;

                    const nY = t.heights_dm[nIdx] * 0.1;
                    const slope = Math.abs(nY - curY) / (cs * dirs[d][2]);
                    if (slope > 0.45) continue;

                    let pen = dirs[d][2] * cs;
                    if (fl === 2) pen *= 1.35; // slope cost
                    else if (fl === 3) pen *= 1.10; // climbable rubble hill
                    // Soft wall clearance penalty
                    if (isPointBlockedBySolidOBB(wx, wz, 4.2)) pen += 4.5;
                    // Corridor anchor attraction so the 4 routes spread across flanks
                    const biasDist = Math.hypot(wx - laneBiasX, wz - laneBiasZ);
                    pen += biasDist * 0.012;

                    const tentG = curG + pen;
                    if (tentG < gScore[nIdx]) {
                        gScore[nIdx] = tentG;
                        cameFrom[nIdx] = curIdx;
                        const h = Math.hypot(gx1 - nx, gz1 - nz) * cs;
                        open.push([tentG + h, nIdx]);
                    }
                }
            }

            const path = [];
            let cur = goalIdx;
            if (cameFrom[cur] === -1 && cur !== startIdx) {
                // Fallback to closest reached node
                let bestD = 1e9;
                for (let i = 0; i < N; i++) {
                    if (gScore[i] < 1e8) {
                        const ix = i % tw, iz = (i / tw) | 0;
                        const d = Math.hypot(ix - gx1, iz - gz1);
                        if (d < bestD) { bestD = d; cur = i; }
                    }
                }
            }
            while (cur !== -1) {
                const ix = cur % tw, iz = (cur / tw) | 0;
                const wx = ox + ix * cs, wz = oz + iz * cs;
                path.push([Math.round(wx * 10) / 10, Math.round(t.heights_dm[cur] * 0.1 * 10) / 10, Math.round(wz * 10) / 10]);
                if (cur === startIdx) break;
                cur = cameFrom[cur];
            }
            path.reverse();
            // Subsample every 2nd node (~16m spacing)
            const out = [];
            for (let i = 0; i < path.length; i += 2) out.push(path[i]);
            if (path.length > 1 && out[out.length - 1] !== path[path.length - 1]) out.push(path[path.length - 1]);
            return out;
        }

        function repelAndSmoothRoute(pts, iter) {
            if (!pts || pts.length < 3) return pts;
            const next = pts.map(p => [p[0], p[1], p[2]]);
            for (let i = 1; i < next.length - 1; i++) {
                let wx = next[i][0], wz = next[i][2];
                // 1. Repel from nearby solid OBBs (buildings & railway wagons) within 5.5m
                let rx = 0, rz = 0;
                for (let k = 0; k < allObbs.length; k++) {
                    const o = allObbs[k];
                    if (o[7] !== 0 && o[7] !== 3) continue;
                    const dx = wx - o[0], dz = wz - o[1];
                    const rad = o[2] + o[3] + 6.0;
                    if (dx * dx + dz * dz > rad * rad) continue;
                    const c = Math.cos(o[4]), s = Math.sin(o[4]);
                    const lx = dx * c - dz * s, lz = dx * s + dz * c;
                    const mx = o[2] + 3.6, mz = o[3] + 3.6;
                    if (Math.abs(lx) < mx && Math.abs(lz) < mz) {
                        const pushX = (lx >= 0 ? (mx - lx) : (-mx - lx));
                        const pushZ = (lz >= 0 ? (mz - lz) : (-mz - lz));
                        if (Math.abs(pushX) < Math.abs(pushZ)) {
                            rx += pushX * c * 0.45;
                            rz -= pushX * s * 0.45;
                        } else {
                            rx += pushZ * s * 0.45;
                            rz += pushZ * c * 0.45;
                        }
                    }
                }
                wx += rx;
                wz += rz;
                // 2. Gentle curvature smoothing
                const smX = (next[i - 1][0] + wx * 2.0 + next[i + 1][0]) * 0.25;
                const smZ = (next[i - 1][2] + wz * 2.0 + next[i + 1][2]) * 0.25;
                if (!isPointBlockedBySolidOBB(smX, smZ, 1.8)) {
                    wx = smX;
                    wz = smZ;
                }
                next[i][0] = Math.round(wx * 10) / 10;
                next[i][2] = Math.round(wz * 10) / 10;
                next[i][1] = Math.round(sampleY(wx, wz) * 10) / 10;
            }
            return next;
        }

        function run30StageAutoBaker() {
            if (!curData) return;
            opts.routes = true;
            document.getElementById('btn-routes').className = 'active';
            const progBox = document.getElementById('bake-progress');
            const stageLbl = document.getElementById('bake-stage-lbl');
            const pctLbl = document.getElementById('bake-pct-lbl');
            const bar = document.getElementById('bake-bar');
            progBox.style.display = 'block';

            // Determine Team 1 & Team 2 spawn centers from existing routes or map bounds
            const b = curData.bounds || [-500, -500, 500, 500];
            let t1Spawn = [b[0] + (b[2] - b[0]) * 0.22, b[1] + (b[3] - b[1]) * 0.18];
            let t2Spawn = [b[0] + (b[2] - b[0]) * 0.78, b[1] + (b[3] - b[1]) * 0.82];
            if (curData.routes && curData.routes.length >= 2) {
                const r1 = curData.routes.find(r => r.team === 1 && r.pts && r.pts.length > 0);
                const r2 = curData.routes.find(r => r.team === 2 && r.pts && r.pts.length > 0);
                if (r1) t1Spawn = [r1.pts[0][0], r1.pts[0][2]];
                if (r2) t2Spawn = [r2.pts[0][0], r2.pts[0][2]];
            }

            const cx = (b[0] + b[2]) * 0.5, cz = (b[1] + b[3]) * 0.5;
            const spanX = (b[2] - b[0]) * 0.34, spanZ = (b[3] - b[1]) * 0.34;
            const corridors = [
                { id: 'west_flank', bx: cx - spanX, bz: cz + spanZ * 0.2 },
                { id: 'center_left', bx: cx - spanX * 0.35, bz: cz },
                { id: 'center_right', bx: cx + spanX * 0.35, bz: cz },
                { id: 'east_flank', bx: cx + spanX, bz: cz - spanZ * 0.2 }
            ];

            let stage = 1;
            let workingRoutes = [];

            function stepStage() {
                const pct = Math.round((stage / 30) * 100);
                pctLbl.innerText = pct + '%';
                bar.style.width = pct + '%';

                if (stage <= 8) {
                    const cIdx = (stage - 1) % 4;
                    const tm = (stage <= 4) ? 1 : 2;
                    const cor = corridors[cIdx];
                    stageLbl.innerText = `Этап ${stage} / 30: 3D A* трассировка коридора ${cor.id} (Команда ${tm})...`;
                    const sp = (tm === 1) ? t1Spawn : t2Spawn;
                    const gl = (tm === 1) ? t2Spawn : t1Spawn;
                    const midPts1 = astarOnTerrainGrid(sp[0], sp[1], cor.bx, cor.bz, cor.bx, cor.bz);
                    const midPts2 = astarOnTerrainGrid(cor.bx, cor.bz, gl[0], gl[1], cor.bx, cor.bz);
                    const combined = midPts1.concat(midPts2.slice(1));
                    workingRoutes.push({ team: tm, id: cor.id, pts: combined });
                    curData.routes = workingRoutes.slice();
                } else if (stage <= 24) {
                    stageLbl.innerText = `Этап ${stage} / 30: Отталкивание от хитбоксов домов/вагонов и 16-лучевая калибровка...`;
                    for (let i = 0; i < workingRoutes.length; i++) {
                        workingRoutes[i].pts = repelAndSmoothRoute(workingRoutes[i].pts, stage);
                    }
                    curData.routes = workingRoutes;
                } else {
                    stageLbl.innerText = `Этап ${stage} / 30: Сглаживание радиусов поворота и запуск 30 ботов на проверку...`;
                    for (let i = 0; i < workingRoutes.length; i++) {
                        workingRoutes[i].pts = repelAndSmoothRoute(workingRoutes[i].pts, stage);
                    }
                    curData.routes = workingRoutes;
                    if (stage === 30) {
                        spawnLiveSimBots();
                        liveSimActive = true;
                        document.getElementById('btn-sim-bots').innerText = '🛑 Остановить обкат';
                        showToast('30 этапов авто-трассировки завершены! Боты вышли на тестовый обкат.');
                        setTimeout(() => { progBox.style.display = 'none'; }, 1800);
                        return;
                    }
                }
                stage++;
                setTimeout(stepStage, 55);
            }
            stepStage();
        }

        // ====================================================================
        // LIVE 30-BOT SANDBOX SIMULATION (WITH 16-RAY 360° 3D VISION)
        // ====================================================================
        function spawnLiveSimBots() {
            if (!curData) return;
            simBots = [];
            const routes = curData.routes || [];
            const t1Routes = routes.filter(r => r.team === 1 && r.pts && r.pts.length >= 2);
            const t2Routes = routes.filter(r => r.team === 2 && r.pts && r.pts.length >= 2);
            const classes = ['heavyTank', 'mediumTank', 'lightTank', 'AT-SPG'];

            for (let tm = 1; tm <= 2; tm++) {
                const rList = (tm === 1) ? t1Routes : t2Routes;
                for (let i = 0; i < 15; i++) {
                    const rt = (rList.length > 0) ? rList[i % rList.length] : null;
                    const p0 = (rt && rt.pts.length > 0) ? rt.pts[0] : [0, 0, 0];
                    const p1 = (rt && rt.pts.length > 1) ? rt.pts[1] : [0, 0, 10];
                    const offX = ((i % 3) - 1) * 5.5;
                    const offZ = (Math.floor(i / 3) - 2) * 5.5;
                    const sx = p0[0] + offX, sz = p0[2] + offZ;
                    const sy = sampleY(sx, sz);
                    const yaw = Math.atan2(p1[0] - p0[0], p1[2] - p0[2]);
                    const cls = classes[i % classes.length];
                    simBots.push({
                        id: (tm === 1 ? 1000 : 2000) + i,
                        team: tm,
                        name: (tm === 1 ? 'Ally_' : 'Enemy_') + cls.slice(0, 2).toUpperCase() + '_' + (i + 1),
                        class: cls,
                        x: sx, y: sy, z: sz, yaw: yaw,
                        speed: (cls === 'lightTank' ? 11.5 : (cls === 'mediumTank' ? 9.5 : 7.2)),
                        route: rt ? rt.pts : [],
                        wpIdx: 1,
                        rays16: new Array(16).fill(11.0),
                        trail: [[sx, sy, sz]]
                    });
                }
            }
        }

        function toggleBotSim() {
            liveSimActive = !liveSimActive;
            const btn = document.getElementById('btn-sim-bots');
            if (liveSimActive) {
                if (simBots.length === 0) spawnLiveSimBots();
                btn.innerText = '🛑 Остановить обкат';
                showToast('Запущен живой 3D-обкат 30 ботов!');
            } else {
                btn.innerText = '🚜 Тест-обкат 30 ботов';
            }
        }

        function resetSimBots() {
            spawnLiveSimBots();
            liveSimActive = true;
            document.getElementById('btn-sim-bots').innerText = '🛑 Остановить обкат';
            showToast('Боты перезапущены со спавнов по текущим маршрутам!');
        }

        function stepSimBots(dt) {
            if (!liveSimActive || simBots.length === 0) return;
            const stepDt = Math.min(0.05, dt) * simSpeed;
            for (let i = 0; i < simBots.length; i++) {
                const b = simBots[i];
                // Cast 16-ray 360° hull whiskers against edited OBBs & terrain
                let steerBias = 0.0;
                for (let ri = 0; ri < 16; ri++) {
                    const ang = b.yaw + ri * (Math.PI / 8.0);
                    const dx = Math.sin(ang), dz = Math.cos(ang);
                    let hitD = 12.0;
                    for (let d = 2.5; d <= 12.0; d += 2.2) {
                        const px = b.x + dx * d, pz = b.z + dz * d;
                        if (isPointBlockedBySolidOBB(px, pz, 0.8)) {
                            hitD = d;
                            break;
                        }
                    }
                    b.rays16[ri] = hitD;
                }
                if (b.rays16[0] < 8.5 || b.rays16[1] < 7.5 || b.rays16[15] < 7.5) {
                    const leftSpace = b.rays16[15] + b.rays16[14];
                    const rightSpace = b.rays16[1] + b.rays16[2];
                    steerBias = (leftSpace > rightSpace) ? -0.95 : 0.95;
                }

                if (b.route && b.wpIdx < b.route.length) {
                    const wp = b.route[b.wpIdx];
                    const dist = Math.hypot(wp[0] - b.x, wp[2] - b.z);
                    if (dist < 10.0 && b.wpIdx < b.route.length - 1) {
                        b.wpIdx++;
                    }
                    const goalYaw = Math.atan2(wp[0] - b.x, wp[2] - b.z) + steerBias;
                    let dYaw = (goalYaw - b.yaw) % (Math.PI * 2);
                    if (dYaw > Math.PI) dYaw -= Math.PI * 2;
                    if (dYaw < -Math.PI) dYaw += Math.PI * 2;
                    b.yaw += Math.max(-2.2 * stepDt, Math.min(2.2 * stepDt, dYaw * 3.0 * stepDt));
                    const effSpd = (b.rays16[0] < 4.5) ? b.speed * 0.35 : b.speed;
                    const nx = b.x + Math.sin(b.yaw) * effSpd * stepDt;
                    const nz = b.z + Math.cos(b.yaw) * effSpd * stepDt;
                    if (!isPointBlockedBySolidOBB(nx, nz, 0.6)) {
                        b.x = nx;
                        b.z = nz;
                        b.y = sampleY(nx, nz);
                    } else {
                        b.yaw += (steerBias >= 0 ? 1.2 : -1.2) * stepDt;
                    }
                    if (b.trail.length === 0 || Math.hypot(b.x - b.trail[b.trail.length - 1][0], b.z - b.trail[b.trail.length - 1][2]) > 4.0) {
                        b.trail.push([b.x, b.y, b.z]);
                        if (b.trail.length > 80) b.trail.shift();
                    }
                }
            }
        }

        // ====================================================================
        // SAVE & EXPORT
        // ====================================================================
        async function saveMapToMod() {
            if (!curData) return;
            const payload = {
                map: curMapKey,
                buildings: curData.buildings,
                terrain: curData.terrain,
                routes: curData.routes
            };
            if (!IS_STANDALONE_BUNDLE) {
                try {
                    const res = await fetch('/save_map', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    });
                    if (res.ok) {
                        showToast('Успешно сохранено в maps_obb.json и navgraphs/' + curMapKey + '.json!');
                        return;
                    }
                } catch (e) {}
            }
            exportMapJSON();
        }

        function exportMapJSON() {
            if (!curData) return;
            const out = {
                map: curMapKey,
                bounds: curData.bounds,
                obbs: curData.buildings,
                routes: curData.routes,
                terrain: curData.terrain
            };
            const blob = new Blob([JSON.stringify(out, null, 2)], { type: 'application/json' });
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = curMapKey + '_nav_edited.json';
            a.click();
            showToast('Скачан файл ' + curMapKey + '_nav_edited.json!');
        }

        async function switchMap(mkey) {
            curMapKey = mkey;
            selectedOBBIndex = -1;
            selectedVid = null;
            liveSimActive = false;
            simBots = [];
            document.getElementById('btn-sim-bots').innerText = '🚜 Тест-обкат 30 ботов';
            document.getElementById('obb-editor-box').style.display = 'none';

            if (IS_STANDALONE_BUNDLE && BUNDLE[mkey]) {
                curData = BUNDLE[mkey];
            } else {
                try {
                    const res = await fetch('/map_geom?map=' + encodeURIComponent(mkey));
                    curData = await res.json();
                } catch (e) {}
            }
            if (!curData) return;

            // Try loading real game minimap texture from server if running via bot_subengine.py
            customMapImg = null;
            if (!IS_STANDALONE_BUNDLE) {
                const img = new Image();
                img.onload = () => { customMapImg = img; bakeProceduralTexture(); };
                img.onerror = () => { bakeProceduralTexture(); };
                img.src = '/map_tex?map=' + encodeURIComponent(mkey);
            }
            updateCounts();
            bakeProceduralTexture();

            const trks = curData.replay_tracks || [];
            document.getElementById('bot-cnt').innerText = trks.length || 30;
            maxTime = 180.0;
            for (let i = 0; i < trks.length; i++) {
                const pts = trks[i].pts;
                if (pts && pts.length > 0) maxTime = Math.max(maxTime, pts[pts.length - 1][0]);
            }
            document.getElementById('time-slider').max = maxTime;
            simTime = Math.min(25.0, maxTime * 0.25);
            resetCamera();
        }

        function sampleY(wx, wz) {
            if (!curData || !curData.terrain || !curData.terrain.heights_dm) return 0.0;
            const t = curData.terrain;
            const gx = Math.floor((wx - t.ox) / t.cs + 0.5);
            const gz = Math.floor((wz - t.oz) / t.cs + 0.5);
            if (gx >= 0 && gx < t.w && gz >= 0 && gz < t.h) {
                return t.heights_dm[gz * t.w + gx] * 0.1;
            }
            return 0.0;
        }

        function resetCamera() {
            selectedVid = null;
            const b = (curData && curData.bounds) ? curData.bounds : [-500, -500, 500, 500];
            const wW = Math.max(100, b[2] - b[0]);
            const wH = Math.max(100, b[3] - b[1]);
            camTx = (b[0] + b[2]) * 0.5;
            camTz = (b[1] + b[3]) * 0.5;
            camTy = sampleY(camTx, camTz);
            camDist = Math.max(wW, wH) * 0.68;
            camYaw = 0.0; // North (+Z) at top of screen!
            camPitch = 0.80;
            scale2D = Math.min(width / wW, height / worldH_or(wH)) * 0.86;
            panX2D = width * 0.5 - camTx * scale2D;
            panY2D = height * 0.5 + camTz * scale2D;
        }
        function worldH_or(v) { return v; }

        function toggle3D() {
            is3D = !is3D;
            const btn = document.getElementById('btn-mode3d');
            const lbl = document.getElementById('cam-lbl');
            if (is3D) {
                btn.innerText = '🎥 3D Перспектива';
                btn.className = 'active mode3d';
                lbl.innerText = '3D Перспектива (Прямая)';
                lbl.style.color = '#34d399';
            } else {
                btn.innerText = '🗺️ 2D Сверху';
                btn.className = 'active';
                lbl.innerText = '2D Сверху (По текстуре)';
                lbl.style.color = '#38bdf8';
            }
        }

        function toggleOpt(k) {
            opts[k] = !opts[k];
            const b = document.getElementById('btn-' + k);
            if (b) b.className = opts[k] ? 'active' : '';
        }

        function togglePlay() {
            isPlaying = !isPlaying;
            const b = document.getElementById('btn-play');
            b.innerText = isPlaying ? '⏸ Пауза' : '▶ Пуск';
            b.className = isPlaying ? 'active' : '';
        }

        function scrubTime(val) {
            simTime = parseFloat(val);
            document.getElementById('time-lbl').innerText = simTime.toFixed(1) + 's';
        }

        function resize() {
            width = canvas.width = window.innerWidth;
            height = canvas.height = window.innerHeight;
        }
        window.addEventListener('resize', resize);
        resize();

        canvas.addEventListener('contextmenu', e => e.preventDefault());
        canvas.addEventListener('mousedown', e => {
            lastMx = e.clientX; lastMy = e.clientY; clickMoved = false;
            if (e.button === 2 || e.button === 1 || e.shiftKey) {
                dragMode = 2; // Pan camera
                return;
            }
            if (editTool === 'obb' && e.button === 0) {
                const picked = pickOBBAtScreen(e.clientX, e.clientY);
                if (picked >= 0) {
                    selectOBB(picked);
                    isDraggingOBB = true;
                    return;
                } else {
                    selectOBB(-1);
                }
            } else if (editTool === 'brush' && e.button === 0) {
                isBrushing = true;
                applyBrushAtScreen(e.clientX, e.clientY);
                return;
            }
            dragMode = 1;
        });

        window.addEventListener('mousemove', e => {
            const dx = e.clientX - lastMx, dy = e.clientY - lastMy;
            if (Math.abs(dx) + Math.abs(dy) > 3) clickMoved = true;
            lastMx = e.clientX; lastMy = e.clientY;

            if (isDraggingOBB && selectedOBBIndex >= 0) {
                const o = allObbs[selectedOBBIndex];
                const [wx, wz] = screenToWorldXZ(e.clientX, e.clientY, o[5]);
                o[0] = Math.round(wx * 10) / 10;
                o[1] = Math.round(wz * 10) / 10;
                const gy = sampleY(o[0], o[1]);
                const h = Math.max(1.0, o[6] - o[5]);
                o[5] = Math.round((gy - 0.3) * 10) / 10;
                o[6] = Math.round((gy + h) * 10) / 10;
                document.getElementById('obb-x').value = o[0];
                document.getElementById('obb-z').value = o[1];
                recomputeGridUnderOBB(o);
                return;
            }
            if (isBrushing) {
                applyBrushAtScreen(e.clientX, e.clientY);
                return;
            }
            if (!dragMode) return;
            if (is3D) {
                if (dragMode === 1) {
                    camYaw += dx * 0.0065;
                    camPitch = Math.max(0.18, Math.min(1.52, camPitch + dy * 0.0060));
                } else {
                    selectedVid = null;
                    const pf = camDist * 0.0014;
                    const cy = Math.cos(camYaw), sy = Math.sin(camYaw);
                    camTx -= (dx * cy + dy * sy) * pf;
                    camTz -= (-dx * sy + dy * cy) * pf;
                    camTy = sampleY(camTx, camTz);
                }
            } else {
                panX2D += dx; panY2D += dy;
            }
        });

        window.addEventListener('mouseup', e => {
            isDraggingOBB = false;
            isBrushing = false;
            if (dragMode === 1 && !clickMoved && e.button === 0 && editTool === 'view') {
                const picked = pickOBBAtScreen(e.clientX, e.clientY);
                if (picked >= 0) {
                    setEditTool('obb');
                    selectOBB(picked);
                }
            }
            dragMode = 0;
        });

        window.addEventListener('keydown', e => {
            if (e.key === 'Delete' && selectedOBBIndex >= 0) deleteSelectedOBB();
        });

        canvas.addEventListener('wheel', e => {
            e.preventDefault();
            if (e.shiftKey && selectedOBBIndex >= 0) {
                const o = allObbs[selectedOBBIndex];
                o[4] = Math.max(-3.14, Math.min(3.14, o[4] + (e.deltaY < 0 ? 0.06 : -0.06)));
                document.getElementById('obb-yaw').value = o[4];
                applyOBBSliders();
                return;
            }
            if (is3D) {
                camDist = Math.max(30.0, Math.min(2000.0, camDist * (e.deltaY < 0 ? 0.86 : 1.16)));
            } else {
                const f = e.deltaY < 0 ? 1.15 : 0.87;
                panX2D = e.clientX - (e.clientX - panX2D) * f;
                panY2D = e.clientY - (e.clientY - panY2D) * f;
                scale2D *= f;
            }
        });

        function sampleTrackPose(trk, t) {
            const pts = trk.pts;
            if (!pts || pts.length === 0) return null;
            if (t <= pts[0][0]) return { x: pts[0][1], y: pts[0][2], z: pts[0][3], yaw: pts[0][4] };
            const last = pts[pts.length - 1];
            if (t >= last[0]) return { x: last[1], y: last[2], z: last[3], yaw: last[4] };
            for (let i = 0; i < pts.length - 1; i++) {
                const p0 = pts[i], p1 = pts[i + 1];
                if (t >= p0[0] && t <= p1[0]) {
                    const f = (t - p0[0]) / Math.max(0.01, p1[0] - p0[0]);
                    let dyaw = (p1[4] - p0[4]) % (Math.PI * 2);
                    if (dyaw > Math.PI) dyaw -= Math.PI * 2;
                    if (dyaw < -Math.PI) dyaw += Math.PI * 2;
                    return {
                        x: p0[1] + (p1[1] - p0[1]) * f,
                        y: p0[2] + (p1[2] - p0[2]) * f,
                        z: p0[3] + (p1[3] - p0[3]) * f,
                        yaw: p0[4] + dyaw * f
                    };
                }
            }
            return { x: last[1], y: last[2], z: last[3], yaw: last[4] };
        }

        function drawLowPolyMesh3D(o, strokeCol, fillCol, lw, isSelected) {
            const cx = o[0], cz = o[1], hw = o[2], hl = o[3], yaw = o[4], minY = o[5], maxY = o[6], fl = o[7];
            const hGrid = (o.length >= 9 && Array.isArray(o[8])) ? o[8] : null;
            const winMeta = (o.length >= 10 && Array.isArray(o[9])) ? o[9] : null;
            if (is3D && hGrid && (hGrid.length === 25 || hGrid.length === 9)) {
                const N = (hGrid.length === 25) ? 5 : 3;
                const halfN = (N - 1) * 0.5;
                const c = Math.cos(yaw), s = Math.sin(yaw);
                const gridPts = [];
                for (let iz = 0; iz < N; iz++) {
                    const lz = ((iz - halfN) / halfN) * hl;
                    for (let ix = 0; ix < N; ix++) {
                        const lx = ((ix - halfN) / halfN) * hw;
                        const wx = cx + lx * c + lz * s, wz = cz - lx * s + lz * c;
                        const wy = minY + Math.max(0.2, hGrid[iz * N + ix] * 0.1);
                        const pr = project3D(wx, wy, wz);
                        if (!pr) return;
                        gridPts.push(pr);
                    }
                }
                const midIdx = (N === 5) ? 12 : 4;
                if (gridPts[midIdx][0] < -150 || gridPts[midIdx][0] > width + 150 || gridPts[midIdx][1] < -150 || gridPts[midIdx][1] > height + 150) return;
                ctx.lineWidth = isSelected ? 2.8 : (lw || 1.15);
                ctx.strokeStyle = isSelected ? '#fde047' : strokeCol;
                ctx.fillStyle = isSelected ? 'rgba(250, 204, 21, 0.35)' : fillCol;
                for (let iz = 0; iz < N - 1; iz++) {
                    for (let ix = 0; ix < N - 1; ix++) {
                        const i0 = iz * N + ix, i1 = i0 + 1, i2 = i0 + N + 1, i3 = i0 + N;
                        ctx.beginPath();
                        ctx.moveTo(gridPts[i0][0], gridPts[i0][1]);
                        ctx.lineTo(gridPts[i1][0], gridPts[i1][1]);
                        ctx.lineTo(gridPts[i2][0], gridPts[i2][1]);
                        ctx.lineTo(gridPts[i3][0], gridPts[i3][1]);
                        ctx.closePath();
                        ctx.fill();
                        ctx.stroke();
                    }
                }
                const cIndices = [0, N - 1, N * N - 1, N * (N - 1)];
                const cornerL = [[-hw, -hl], [hw, -hl], [hw, hl], [-hw, hl]];
                ctx.beginPath();
                for (let ci = 0; ci < 4; ci++) {
                    const lx = cornerL[ci][0], lz = cornerL[ci][1];
                    const pb = project3D(cx + lx * c + lz * s, minY, cz - lx * s + lz * c);
                    const pt = gridPts[cIndices[ci]];
                    if (pb && pt) { ctx.moveTo(pb[0], pb[1]); ctx.lineTo(pt[0], pt[1]); }
                }
                ctx.stroke();
                if (opts.pockets && winMeta && winMeta[3] === 1) {
                    const sillY = minY + winMeta[0] * 0.1;
                    const lintelY = minY + winMeta[1] * 0.1;
                    const wSpan = Math.min(hw * 0.55, Math.max(0.8, winMeta[2] * 0.05));
                    const w0 = project3D(cx - wSpan * c, sillY, cz + wSpan * s);
                    const w1 = project3D(cx + wSpan * c, sillY, cz - wSpan * s);
                    const w2 = project3D(cx + wSpan * c, lintelY, cz - wSpan * s);
                    const w3 = project3D(cx - wSpan * c, lintelY, cz + wSpan * s);
                    if (w0 && w1 && w2 && w3) {
                        ctx.strokeStyle = '#e879f9';
                        ctx.fillStyle = 'rgba(217, 70, 239, 0.30)';
                        ctx.lineWidth = 1.6;
                        ctx.beginPath();
                        ctx.moveTo(w0[0], w0[1]); ctx.lineTo(w1[0], w1[1]);
                        ctx.lineTo(w2[0], w2[1]); ctx.lineTo(w3[0], w3[1]);
                        ctx.closePath();
                        ctx.fill();
                        ctx.stroke();
                    }
                }
                if (isSelected) {
                    ctx.fillStyle = '#fde047';
                    for (let ci = 0; ci < 4; ci++) {
                        const pt = gridPts[cIndices[ci]];
                        ctx.fillRect(pt[0] - 4, pt[1] - 4, 8, 8);
                    }
                }
                return;
            }
            draw3DBox(cx, cz, hw, hl, yaw, minY, maxY, strokeCol, fillCol, lw, isSelected);
        }

        function draw3DBox(cx, cz, hw, hl, yaw, minY, maxY, strokeCol, fillCol, lw, isSelected) {
            const c = Math.cos(yaw), s = Math.sin(yaw);
            const corners = [[-hw, -hl], [hw, -hl], [hw, hl], [-hw, hl]];
            const bPts = [], tPts = [];
            for (let i = 0; i < 4; i++) {
                const lx = corners[i][0], lz = corners[i][1];
                const wx = cx + lx * c + lz * s;
                const wz = cz - lx * s + lz * c;
                const pb = project3D(wx, minY, wz);
                const pt = project3D(wx, maxY, wz);
                if (!pb || !pt) return;
                bPts.push(pb); tPts.push(pt);
            }
            if (bPts[0][0] < -150 && bPts[2][0] < -150) return;
            if (bPts[0][0] > width + 150 && bPts[2][0] > width + 150) return;
            if (bPts[0][1] < -150 && bPts[2][1] < -150) return;
            if (bPts[0][1] > height + 150 && bPts[2][1] > height + 150) return;

            ctx.lineWidth = isSelected ? 2.8 : (lw || 1.2);
            ctx.strokeStyle = isSelected ? '#fde047' : strokeCol;
            if (fillCol) {
                ctx.fillStyle = isSelected ? 'rgba(250, 204, 21, 0.35)' : fillCol;
                ctx.beginPath();
                ctx.moveTo(tPts[0][0], tPts[0][1]);
                for (let i = 1; i < 4; i++) ctx.lineTo(tPts[i][0], tPts[i][1]);
                ctx.closePath();
                ctx.fill();
            }
            ctx.beginPath();
            ctx.moveTo(tPts[0][0], tPts[0][1]);
            for (let i = 1; i < 4; i++) ctx.lineTo(tPts[i][0], tPts[i][1]);
            ctx.closePath();
            if (is3D) {
                ctx.moveTo(bPts[0][0], bPts[0][1]);
                for (let i = 1; i < 4; i++) ctx.lineTo(bPts[i][0], bPts[i][1]);
                ctx.closePath();
                for (let i = 0; i < 4; i++) {
                    ctx.moveTo(bPts[i][0], bPts[i][1]);
                    ctx.lineTo(tPts[i][0], tPts[i][1]);
                }
            }
            ctx.stroke();

            if (isSelected) {
                // Draw corner handles & orientation arrow
                ctx.fillStyle = '#fde047';
                for (let i = 0; i < 4; i++) {
                    ctx.fillRect(tPts[i][0] - 4, tPts[i][1] - 4, 8, 8);
                }
            }
        }

        let lastFrame = performance.now(), frames = 0, fpsAcc = 0;

        function render() {
            const now = performance.now();
            const dt = Math.min(0.1, (now - lastFrame) / 1000);
            lastFrame = now;
            frames++; fpsAcc += dt;
            if (fpsAcc >= 0.5) {
                document.getElementById('fps-lbl').innerText = Math.round(frames / fpsAcc);
                frames = 0; fpsAcc = 0;
            }

            if (isPlaying && !liveSimActive) {
                simTime += dt * simSpeed;
                if (simTime > maxTime) simTime = 5.0;
                document.getElementById('time-slider').value = simTime;
                document.getElementById('time-lbl').innerText = simTime.toFixed(1) + 's';
            }
            if (liveSimActive) {
                stepSimBots(dt);
            }

            updateCam();
            ctx.fillStyle = '#070b10';
            ctx.fillRect(0, 0, width, height);

            if (!curData) { requestAnimationFrame(render); return; }
            const b = curData.bounds || [-500, -500, 500, 500];

            // 0. In 2D mode, blit the full map texture directly underneath!
            if (opts.tex && texReady && !is3D) {
                const [tlX, tlY] = project3D(b[0], 0, b[3]);
                const [brX, brY] = project3D(b[2], 0, b[1]);
                ctx.globalAlpha = 0.72;
                ctx.drawImage(texCanvas, tlX, tlY, brX - tlX, brY - tlY);
                ctx.globalAlpha = 1.0;
            }

            // 1. Render 3D Point-by-Point Bot Vision Grid & Elevation Wireframe
            if (opts.grid3d && curData.terrain && curData.terrain.heights_dm) {
                const t = curData.terrain;
                const tw = t.w, th = t.h, ox = t.ox, oz = t.oz, cs = t.cs;
                const hdm = t.heights_dm, fls = t.flags;

                ctx.lineWidth = 0.75;
                ctx.strokeStyle = 'rgba(16, 185, 129, 0.16)';
                ctx.beginPath();
                const stride = (camDist > 520 && is3D) ? 2 : 1;
                for (let gz = 0; gz < th - stride; gz += stride) {
                    const wz = oz + gz * cs, wz2 = oz + (gz + stride) * cs;
                    const r0 = gz * tw, r1 = (gz + stride) * tw;
                    for (let gx = 0; gx < tw - stride; gx += stride) {
                        if (fls[r0 + gx] === 0) continue;
                        const wx = ox + gx * cs;
                        const p0 = project3D(wx, hdm[r0 + gx] * 0.1, wz);
                        if (!p0 || p0[0] < -40 || p0[0] > width + 40 || p0[1] < -40 || p0[1] > height + 40) continue;
                        const pR = project3D(wx + cs * stride, hdm[r0 + gx + stride] * 0.1, wz);
                        const pU = project3D(wx, hdm[r1 + gx] * 0.1, wz2);
                        if (pR) { ctx.moveTo(p0[0], p0[1]); ctx.lineTo(pR[0], pR[1]); }
                        if (pU) { ctx.moveTo(p0[0], p0[1]); ctx.lineTo(pU[0], pU[1]); }
                    }
                }
                ctx.stroke();

                const radSq = 250 * 250;
                for (let gz = 0; gz < th; gz++) {
                    const wz = oz + gz * cs, r0 = gz * tw;
                    for (let gx = 0; gx < tw; gx++) {
                        const wx = ox + gx * cs;
                        const fl = fls[r0 + gx];
                        const dSq = (wx - camTx) * (wx - camTx) + (wz - camTz) * (wz - camTz);
                        if ((fl === 1 || fl === 0) && dSq > radSq && ((gx + gz) & 1) !== 0) continue;
                        const pr = project3D(wx, hdm[r0 + gx] * 0.1, wz);
                        if (!pr || pr[0] < 0 || pr[0] > width || pr[1] < 0 || pr[1] > height) continue;
                        if (fl === 1) ctx.fillStyle = (dSq <= radSq) ? 'rgba(16, 185, 129, 0.62)' : 'rgba(16, 185, 129, 0.25)';
                        else if (fl === 2) ctx.fillStyle = 'rgba(56, 189, 248, 0.68)';
                        else if (fl === 3) ctx.fillStyle = '#a3e635';
                        else if (fl === 4) ctx.fillStyle = 'rgba(2, 132, 199, 0.78)';
                        else if (fl === 5) ctx.fillStyle = 'rgba(249, 115, 22, 0.85)';
                        else ctx.fillStyle = (dSq <= radSq * 0.5) ? 'rgba(244, 63, 94, 0.50)' : 'rgba(244, 63, 94, 0.16)';
                        const sz = (fl === 3 || fl === 5) ? 3.2 : (dSq <= radSq ? 2.4 : 1.5);
                        ctx.fillRect(pr[0] - sz * 0.5, pr[1] - sz * 0.5, sz, sz);
                    }
                }
            }

            // 2. Render Climbable Rubble Hills ('разрушенная горочка' & composite OBBs obb[7]==2)
            if (opts.rubble) {
                const rpts = curData.rubble_pts || [];
                ctx.fillStyle = '#a3e635';
                for (let i = 0; i < rpts.length; i++) {
                    const rp = rpts[i];
                    const pr = project3D(rp[0], rp[1], rp[2]);
                    if (pr && pr[0] >= 0 && pr[0] <= width && pr[1] >= 0 && pr[1] <= height) {
                        ctx.fillRect(pr[0] - 1.8, pr[1] - 1.8, 3.6, 3.6);
                    }
                }
            }

            // 3. Render Tactical Corridors / Auto-Baked Routes
            if (opts.routes && curData.routes) {
                for (let i = 0; i < curData.routes.length; i++) {
                    const rt = curData.routes[i];
                    const pts = rt.pts;
                    if (!pts || pts.length < 2) continue;
                    ctx.strokeStyle = (rt.team === 1) ? 'rgba(34, 197, 94, 0.88)' : 'rgba(239, 68, 68, 0.88)';
                    ctx.lineWidth = 2.5;
                    ctx.beginPath();
                    let st = false;
                    for (let j = 0; j < pts.length; j++) {
                        const pr = project3D(pts[j][0], pts[j][1] + 0.9, pts[j][2]);
                        if (!pr) continue;
                        if (!st) { ctx.moveTo(pr[0], pr[1]); st = true; }
                        else ctx.lineTo(pr[0], pr[1]);
                    }
                    ctx.stroke();
                }
            }

            // 4. Render All 4 Classes of 3D Low-Poly Models (0=Indestructible, 3=Impassable Destructible, 2=Climbable Rubble/Ramps, 1=Passable Destructible)
            for (let i = 0; i < allObbs.length; i++) {
                const o = allObbs[i];
                const isSel = (i === selectedOBBIndex);
                if (o[7] === 1) {
                    if (!opts.destr && !isSel) continue;
                    drawLowPolyMesh3D(o, 'rgba(245, 158, 11, 0.72)', 'rgba(180, 83, 9, 0.22)', 0.95, isSel);
                } else if (o[7] === 2) {
                    if (!opts.rubble && !isSel) continue;
                    drawLowPolyMesh3D(o, 'rgba(163, 230, 53, 0.82)', 'rgba(132, 204, 22, 0.25)', 1.15, isSel);
                } else if (o[7] === 3) {
                    if (!opts.wagons && !isSel) continue;
                    drawLowPolyMesh3D(o, 'rgba(244, 63, 94, 0.92)', 'rgba(159, 18, 57, 0.50)', 1.35, isSel);
                } else {
                    if (!opts.buildings && !isSel) continue;
                    drawLowPolyMesh3D(o, 'rgba(56, 189, 248, 0.82)', 'rgba(15, 23, 42, 0.68)', 1.2, isSel);
                }
            }

            // 4b. Render Tactical Firing Windows (kind=3), Climbable Ramps (kind=2), and Corner Positions (kind=1)
            if (opts.pockets && curData.pockets) {
                const pks = curData.pockets;
                for (let i = 0; i < pks.length; i++) {
                    const pk = pks[i];
                    const sx = pk[0], sy = pk[1], sz = pk[2], kind = pk[3];
                    const kx = (pk.length >= 7) ? pk[4] : sx;
                    const ky = (pk.length >= 7) ? pk[5] : sy;
                    const kz = (pk.length >= 7) ? pk[6] : sz;
                    const pS = project3D(sx, sy + 1.2, sz);
                    const pK = project3D(kx, ky + 1.4, kz);
                    if (!pS || pS[0] < 0 || pS[0] > width || pS[1] < 0 || pS[1] > height) continue;
                    if (kind === 3) {
                        ctx.fillStyle = '#d946ef';
                        ctx.fillRect(pS[0] - 3, pS[1] - 3, 6, 6);
                        if (pK) {
                            ctx.strokeStyle = 'rgba(217, 70, 239, 0.85)';
                            ctx.lineWidth = 1.5;
                            ctx.beginPath(); ctx.moveTo(pS[0], pS[1]); ctx.lineTo(pK[0], pK[1]); ctx.stroke();
                        }
                    } else if (kind === 2) {
                        ctx.fillStyle = '#a3e635';
                        ctx.fillRect(pS[0] - 2.5, pS[1] - 2.5, 5, 5);
                    } else {
                        ctx.fillStyle = 'rgba(56, 189, 248, 0.65)';
                        ctx.fillRect(pS[0] - 2, pS[1] - 2, 4, 4);
                    }
                }
            }

            // 5. Render Either Live 30-Bot Sandbox Sim OR Replay Trajectories
            if (opts.tracks) {
                if (liveSimActive && simBots.length > 0) {
                    for (let i = 0; i < simBots.length; i++) {
                        const b = simBots[i];
                        const isA = (b.team === 1);
                        const col = isA ? '#22c55e' : '#ef4444';
                        const fillCol = isA ? 'rgba(6, 78, 59, 0.88)' : 'rgba(127, 29, 29, 0.88)';

                        // Trail
                        if (b.trail.length > 1) {
                            ctx.strokeStyle = isA ? 'rgba(34, 197, 94, 0.45)' : 'rgba(239, 68, 68, 0.45)';
                            ctx.lineWidth = 1.5;
                            ctx.beginPath();
                            let st = false;
                            for (let j = 0; j < b.trail.length; j++) {
                                const pr = project3D(b.trail[j][0], b.trail[j][1] + 0.5, b.trail[j][2]);
                                if (!pr) continue;
                                if (!st) { ctx.moveTo(pr[0], pr[1]); st = true; }
                                else ctx.lineTo(pr[0], pr[1]);
                            }
                            ctx.stroke();
                        }

                        // 16-Ray 360° 3D Hull Whiskers
                        if (opts.whiskers) {
                            const p0 = project3D(b.x, b.y + 0.9, b.z);
                            if (p0) {
                                for (let ri = 0; ri < 16; ri++) {
                                    const rLen = b.rays16[ri];
                                    const ang = b.yaw + ri * (Math.PI / 8.0);
                                    const ex = b.x + Math.sin(ang) * rLen;
                                    const ez = b.z + Math.cos(ang) * rLen;
                                    const ey = sampleY(ex, ez) + 0.9;
                                    const p1 = project3D(ex, ey, ez);
                                    if (!p1) continue;
                                    ctx.strokeStyle = (rLen < 9.5) ? 'rgba(239, 68, 68, 0.85)' : 'rgba(56, 189, 248, 0.30)';
                                    ctx.lineWidth = (rLen < 9.5) ? 1.5 : 0.9;
                                    ctx.beginPath(); ctx.moveTo(p0[0], p0[1]); ctx.lineTo(p1[0], p1[1]); ctx.stroke();
                                }
                            }
                        }

                        draw3DBox(b.x, b.z, 1.7, 3.3, b.yaw, b.y, b.y + 1.6, col, fillCol, 1.8, false);
                        const lblPr = project3D(b.x, b.y + 3.4, b.z);
                        if (lblPr) {
                            ctx.font = '600 9.5px -apple-system, sans-serif';
                            ctx.fillStyle = '#f8fafc';
                            ctx.textAlign = 'center';
                            ctx.fillText(b.name, lblPr[0], lblPr[1]);
                        }
                    }
                } else if (curData.replay_tracks) {
                    const trks = curData.replay_tracks;
                    for (let i = 0; i < trks.length; i++) {
                        const trk = trks[i];
                        const pose = sampleTrackPose(trk, simTime);
                        if (!pose) continue;
                        const isP = trk.is_player;
                        const isA = trk.team === 1;
                        const col = isP ? '#eab308' : (isA ? '#22c55e' : '#ef4444');
                        const fillCol = isP ? 'rgba(120, 53, 15, 0.85)' : (isA ? 'rgba(6, 78, 59, 0.85)' : 'rgba(127, 29, 29, 0.85)');

                        ctx.strokeStyle = isP ? 'rgba(234, 179, 8, 0.65)' : (isA ? 'rgba(34, 197, 94, 0.35)' : 'rgba(239, 68, 68, 0.35)');
                        ctx.lineWidth = isP ? 2.2 : 1.2;
                        ctx.beginPath();
                        let st = false;
                        for (let j = 0; j < trk.pts.length; j++) {
                            const pt = trk.pts[j];
                            if (pt[0] > simTime) break;
                            const pr = project3D(pt[1], pt[2] + 0.6, pt[3]);
                            if (!pr) continue;
                            if (!st) { ctx.moveTo(pr[0], pr[1]); st = true; }
                            else ctx.lineTo(pr[0], pr[1]);
                        }
                        ctx.stroke();

                        draw3DBox(pose.x, pose.z, 1.7, 3.3, pose.yaw, pose.y, pose.y + 1.6, col, fillCol, 1.7, false);
                        const lblPr = project3D(pose.x, pose.y + 3.4, pose.z);
                        if (lblPr && lblPr[0] >= 0 && lblPr[0] <= width && lblPr[1] >= 0 && lblPr[1] <= height) {
                            ctx.font = '600 10px -apple-system, sans-serif';
                            ctx.fillStyle = '#f8fafc';
                            ctx.textAlign = 'center';
                            ctx.fillText(trk.name, lblPr[0], lblPr[1]);
                        }
                    }
                }
            }

            requestAnimationFrame(render);
        }

        async function initApp() {
            const sel = document.getElementById('map-select');
            if (IS_STANDALONE_BUNDLE) {
                const labels = {
                    '42_north_america': '42_north_america — Порт (1065 Ж/Д Вагонов + 3D Мост)',
                    '07_lakeville': '07_lakeville — Ласвилль (Озёрная тропа + Город)',
                    '04_himmelsdorf': '04_himmelsdorf — Химмельсдорф (Завалы/Горочки + Вокзал)',
                    '23_westfeld': '23_westfeld — Вестфилд (374 Завала/Горочки + Виадук)',
                    '33_fjord': '33_fjord — Фьорды (Скалы + Перевалы + Завалы)'
                };
                sel.innerHTML = '';
                Object.keys(BUNDLE).forEach(k => {
                    const opt = document.createElement('option');
                    opt.value = k;
                    opt.innerText = labels[k] || k;
                    sel.appendChild(opt);
                });
                await switchMap('42_north_america');
            } else {
                try {
                    const res = await fetch('/maps_list');
                    const data = await res.json();
                    sel.innerHTML = '';
                    (data.maps || []).forEach(k => {
                        const opt = document.createElement('option');
                        opt.value = k;
                        opt.innerText = k;
                        if (k === data.current) opt.selected = true;
                        sel.appendChild(opt);
                    });
                    await switchMap(data.current || '04_himmelsdorf');
                } catch (e) {}
            }
            requestAnimationFrame(render);
        }

        initApp();
    </script>
</body>
</html>
"""


if __name__ == '__main__':
    import webbrowser
    _bsub.set_active_map(_CURRENT_EDIT_MAP)
    print('Starting Offhangar 3D NavEdit & Tactical Radar on http://127.0.0.1:%d/ ...' % HTTP_PORT)
    try:
        webbrowser.open('http://127.0.0.1:%d/' % HTTP_PORT)
    except Exception:
        pass
    _http_server_thread()
