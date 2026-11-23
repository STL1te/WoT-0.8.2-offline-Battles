# -*- coding: utf-8 -*-
"""
WoT Offhangar Native Bot Subengine & Live 360-Degree 3D Tactical Radar (Self-Contained)
Runs directly inside the World of Tanks client process with ZERO external dependencies.
Loads all 33 maps' 3D solid building/rock/fortification OBB geometry from offhangar/maps_obb.json
and 3D terrain elevation + OFFHNAV1 surface relief grids, computing sub-millisecond
16-sector 360-degree hull obstacle/slope/elevation vision (front, flanks, and rear),
continuous uphill/downhill/cross-slope grading, 3D gun-barrel/window line-of-sight rays,
and serving a 60 FPS HTML5 3D tactical radar on http://127.0.0.1:8768/
"""

import io
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

HTTP_PORT = 8768
_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_OFFHANGAR_DIR = os.path.abspath(os.path.join(_CURRENT_DIR, '..'))
_MAPS_OBB_PATH = os.path.join(_OFFHANGAR_DIR, 'maps_obb.json')

# 16-sector 360-degree unit circle offsets (every 22.5 deg = pi/8 rad from hull yaw):
# 0: 0 deg (Front), 1: +22.5 deg (FR inner), 2: +45 deg (FR diag), 3: +67.5 deg (FR wide),
# 4: +90 deg (Right beam), 5: +112.5 deg (RR wide), 6: +135 deg (RR diag), 7: +157.5 deg (RR inner),
# 8: 180 deg (Rear center), 9: -157.5 deg (RL inner), 10: -135 deg (RL diag), 11: -112.5 deg (RL wide),
# 12: -90 deg (Left beam), 13: -67.5 deg (FL wide), 14: -45 deg (FL diag), 15: -22.5 deg (FL inner)
_SECTOR_ANGLES = tuple(i * (math.pi / 8.0) for i in range(16))
_SECTOR_MAX_DIST = (
    13.0, 11.2, 9.8, 8.6,
    8.0,  8.2,  9.2, 9.8,
    10.2, 9.8,  9.2, 8.2,
    8.0,  8.6,  9.8, 11.2,
)


class SpatialOBBGrid(object):
    """High-speed 3D spatial hash grid for microsecond 360-degree ray-OBB and 3D terrain slope/elevation queries."""
    def __init__(self, cell_size=25.0):
        self.cell_size = float(cell_size)
        self.inv_cell = 1.0 / float(cell_size)
        self.solid_grid = {}
        self.whisker_grid = {}
        self.solid_obbs = []  # (cx, cz, hw, hl, cos_yaw, sin_yaw, rad, minY_pad, maxY_pad)
        self.dynamic_wreck_obbs = []  # [(cx, cz, hw, hl, cos_yaw, sin_yaw, rad, minY_pad, maxY_pad, None), ...]
        self.all_obbs = []
        self.bounds = [-500.0, -500.0, 500.0, 500.0]
        self.nav_ox = -500.0
        self.nav_oz = -500.0
        self.nav_cs = 4.0
        self.nav_inv_cs = 0.25
        self.nav_w = 0
        self.nav_h = 0
        self.nav_valid = None
        self.nav_hazards = None
        self.nav_near_haz = None
        self.nav_heights_mm = None
        self.nav_links = None
        self.mesh3d_ox = -600.0
        self.mesh3d_oz = -600.0
        self.mesh3d_step = 0.0
        self.mesh3d_inv_step = 0.0
        self.mesh3d_w = 0
        self.mesh3d_h = 0
        self.mesh3d_flags = None
        self.mesh3d_heights = None

    def set_geometry(self, bounds, obb_list):
        self.bounds = bounds or [-500.0, -500.0, 500.0, 500.0]
        self.solid_obbs = []
        self.solid_grid.clear()
        self.whisker_grid.clear()
        self.all_obbs = obb_list or []
        inv_cs = self.inv_cell
        for item in self.all_obbs:
            if len(item) >= 5:
                fl_cls = int(item[7]) if len(item) >= 8 else 0
                # Class 0 (Indestructible building/rock) and Class 3 (Impassable destructible/heavy: wagons, dragon teeth, heavy foundations) block hulls!
                # Class 1 (Passable destructible: wooden houses, sheds, fences, cars) and Class 2 (Climbable rubble hills/ramps/bridges) do NOT block hulls!
                if fl_cls in (1, 2):
                    continue
                x, z, hw, hl, yaw = float(item[0]), float(item[1]), float(item[2]), float(item[3]), float(item[4])
                minY = float(item[5]) if len(item) >= 6 else -100.0
                maxY = float(item[6]) if len(item) >= 7 else 100.0
                rad = math.hypot(hw, hl)
                c = math.cos(yaw)
                s = math.sin(yaw)
                h_grid = tuple(item[8]) if (len(item) >= 9 and isinstance(item[8], (list, tuple)) and len(item[8]) in (9, 25)) else None
                idx = len(self.solid_obbs)
                self.solid_obbs.append((x, z, hw, hl, c, s, rad, minY - 0.55, maxY + 0.55, h_grid))
                min_cx = int(math.floor((x - rad - 1.0) * inv_cs))
                max_cx = int(math.floor((x + rad + 1.0) * inv_cs))
                min_cz = int(math.floor((z - rad - 1.0) * inv_cs))
                max_cz = int(math.floor((z + rad + 1.0) * inv_cs))
                for cx in range(min_cx, max_cx + 1):
                    for cz in range(min_cz, max_cz + 1):
                        k = (cx, cz)
                        lst = self.solid_grid.get(k)
                        if lst is None:
                            self.solid_grid[k] = [idx]
                        else:
                            lst.append(idx)
                w_pad = rad + 14.5
                w_min_cx = int(math.floor((x - w_pad) * inv_cs))
                w_max_cx = int(math.floor((x + w_pad) * inv_cs))
                w_min_cz = int(math.floor((z - w_pad) * inv_cs))
                w_max_cz = int(math.floor((z + w_pad) * inv_cs))
                for cx in range(w_min_cx, w_max_cx + 1):
                    for cz in range(w_min_cz, w_max_cz + 1):
                        k = (cx, cz)
                        lst = self.whisker_grid.get(k)
                        if lst is None:
                            self.whisker_grid[k] = [idx]
                        else:
                            lst.append(idx)

    def set_navgraph(self, graph):
        if not isinstance(graph, dict):
            self.nav_w = 0
            self.nav_h = 0
            self.nav_valid = None
            self.nav_hazards = None
            self.nav_near_haz = None
            self.nav_heights_mm = None
            self.nav_links = None
            self.mesh3d_w = 0
            self.mesh3d_h = 0
            self.mesh3d_flags = None
            self.mesh3d_heights = None
            return
        try:
            orig = graph.get('origin') or (-500.0, -500.0)
            self.nav_ox = float(orig[0])
            self.nav_oz = float(orig[1])
            self.nav_cs = float(graph.get('cell_size', 4.0) or 4.0)
            self.nav_inv_cs = 1.0 / self.nav_cs
            self.nav_w = int(graph.get('width', 0) or 0)
            self.nav_h = int(graph.get('height', 0) or 0)
            self.nav_hazards = graph.get('hazards')
            heights_mm = graph.get('heights_mm')
            links = graph.get('links')
            self.nav_heights_mm = heights_mm
            self.nav_links = links
            mesh3d = graph.get('mesh3d')
            if isinstance(mesh3d, (tuple, list)) and len(mesh3d) == 7:
                self.mesh3d_ox = float(mesh3d[0])
                self.mesh3d_oz = float(mesh3d[1])
                self.mesh3d_step = max(0.5, float(mesh3d[2]))
                self.mesh3d_inv_step = 1.0 / self.mesh3d_step
                self.mesh3d_w = int(mesh3d[3])
                self.mesh3d_h = int(mesh3d[4])
                self.mesh3d_flags = mesh3d[5]
                self.mesh3d_heights = mesh3d[6]
            else:
                self.mesh3d_w = 0
                self.mesh3d_h = 0
                self.mesh3d_flags = None
                self.mesh3d_heights = None
            if self.nav_w > 0 and self.nav_h > 0 and self.nav_hazards and heights_mm and links:
                w = self.nav_w
                h = self.nav_h
                hz = self.nav_hazards
                val = bytearray(w * h)
                for idx in range(w * h):
                    if heights_mm[idx] is not None and not (hz[idx] & 3) and links[idx] != 0:
                        val[idx] = 1
                self.nav_valid = val
                near = bytearray(w * h)
                for z in range(h):
                    row = z * w
                    for x in range(w):
                        idx = row + x
                        if (hz[idx] & 1):
                            for dz in (-1, 0, 1):
                                nz = z + dz
                                if 0 <= nz < h:
                                    nrow = nz * w
                                    for dx in (-1, 0, 1):
                                        nx = x + dx
                                        if 0 <= nx < w:
                                            near[nrow + nx] = 1
                self.nav_near_haz = near
            else:
                self.nav_near_haz = None
        except Exception:
            self.nav_w = 0
            self.nav_near_haz = None

    def is_near_hazard(self, rx, rz):
        if not self.nav_near_haz or self.nav_w <= 0:
            return False
        cx = int(math.floor((rx - self.nav_ox) * self.nav_inv_cs + 0.5))
        cz = int(math.floor((rz - self.nav_oz) * self.nav_inv_cs + 0.5))
        if 0 <= cx < self.nav_w and 0 <= cz < self.nav_h:
            return bool(self.nav_near_haz[cz * self.nav_w + cx])
        return True

    def sample_elevation_3d(self, wx, wz, fallback_y=0.0):
        """Return (ground_y, surface_y, is_solid_or_fatal) at world (wx, wz) in O(1)."""
        gy = float(fallback_y)
        sy = gy
        fatal = False
        hm = None
        if self.nav_w > 0 and self.nav_heights_mm:
            cx = int(math.floor((wx - self.nav_ox) * self.nav_inv_cs + 0.5))
            cz = int(math.floor((wz - self.nav_oz) * self.nav_inv_cs + 0.5))
            if 0 <= cx < self.nav_w and 0 <= cz < self.nav_h:
                idx = cz * self.nav_w + cx
                hm = self.nav_heights_mm[idx]
                hz = int(self.nav_hazards[idx]) if self.nav_hazards else 0
                if (hz & 3) != 0 or not (self.nav_valid and self.nav_valid[idx]):
                    fatal = True
                if hm is not None:
                    gy = float(hm) * 0.001
                    sy = gy
            else:
                fatal = True
        if self.mesh3d_w > 0 and self.mesh3d_heights:
            mx = int((wx - self.mesh3d_ox) * self.mesh3d_inv_step)
            mz = int((wz - self.mesh3d_oz) * self.mesh3d_inv_step)
            if 0 <= mx < self.mesh3d_w and 0 <= mz < self.mesh3d_h:
                midx = mz * self.mesh3d_w + mx
                mfl = int(self.mesh3d_flags[midx]) if self.mesh3d_flags else 1
                if mfl:
                    mh = float(self.mesh3d_heights[midx])
                    if hm is None:
                        gy = mh
                        sy = mh
                        if (mfl & 2) != 0 and self.nav_w <= 0:
                            fatal = True
                    else:
                        # If cell is a valid drivable road/bridge/underpass in navgraph, ignore overhead bridge/archway roofs (mh - gy > 1.8m)!
                        if abs(mh - gy) <= 1.8:
                            sy = mh
                        if (mfl & 4) != 0 and (mfl & 26) == 0:
                            fatal = False
        return gy, sy, fatal

    def is_point_climbable_3d(self, wx, wz, ref_y=None):
        """Return True if (wx, wz) is a verified passable 3D cell or climbable rubble hill ('разрушенная горочка')
        and NOT a blocked solid building or railway wagon in nav_valid."""
        if self.nav_w > 0 and self.nav_valid:
            cx = int(math.floor((wx - self.nav_ox) * self.nav_inv_cs + 0.5))
            cz = int(math.floor((wz - self.nav_oz) * self.nav_inv_cs + 0.5))
            if not (0 <= cx < self.nav_w and 0 <= cz < self.nav_h):
                return False
            idx = cz * self.nav_w + cx
            if not self.nav_valid[idx]:
                return False
            if ref_y is not None and self.nav_heights_mm and self.nav_heights_mm[idx] is not None:
                gy = float(self.nav_heights_mm[idx]) * 0.001
                if abs(gy - float(ref_y)) > 2.15:
                    return False
            if self.mesh3d_w > 0 and self.mesh3d_flags:
                mx = int((wx - self.mesh3d_ox) * self.mesh3d_inv_step)
                mz = int((wz - self.mesh3d_oz) * self.mesh3d_inv_step)
                if 0 <= mx < self.mesh3d_w and 0 <= mz < self.mesh3d_h:
                    midx = mz * self.mesh3d_w + mx
                    mfl = int(self.mesh3d_flags[midx])
                    if (mfl & 4) != 0 and (mfl & 2) == 0:
                        return True
            return False
        return False

    def cast_hazard_ray(self, rx, rz, rdx, rdz, max_d):
        if not self.nav_hazards or not self.nav_valid or self.nav_w <= 0:
            return max_d, None
        w = self.nav_w
        h = self.nav_h
        ox = self.nav_ox
        oz = self.nav_oz
        inv_cs = self.nav_inv_cs
        hz = self.nav_hazards
        val = self.nav_valid
        d = 2.0
        while d <= max_d:
            px = rx + rdx * d
            pz = rz + rdz * d
            cx = int(math.floor((px - ox) * inv_cs + 0.5))
            cz = int(math.floor((pz - oz) * inv_cs + 0.5))
            if cx < 0 or cx >= w or cz < 0 or cz >= h:
                return d, (round(px, 2), round(pz, 2))
            idx = cz * w + cx
            if (hz[idx] & 3) or not val[idx]:
                return d, (round(px, 2), round(pz, 2))
            d += 2.0
        return max_d, None

    def local_solid_obbs(self, rx, ry, rz):
        """Return solid building/rock/fortification/wreck OBBs within 14.5m of (rx, rz) that overlap hull height + slope."""
        indices = self.whisker_grid.get((int(math.floor(rx * self.inv_cell)), int(math.floor(rz * self.inv_cell))))
        obbs = self.solid_obbs
        hy = ry + 0.9
        res = []
        if indices:
            for idx in indices:
                o = obbs[idx]
                if (hy + 1.85) < o[7] or (hy - 1.85) > o[8]:
                    continue
                dx = rx - o[0]
                dz = rz - o[1]
                lim = o[6] + 14.5
                if dx * dx + dz * dz <= lim * lim:
                    res.append(o)
        if self.dynamic_wreck_obbs:
            for w_obb in self.dynamic_wreck_obbs:
                if (hy + 2.2) < w_obb[7] or (hy - 2.2) > w_obb[8]:
                    continue
                dx = rx - w_obb[0]
                dz = rz - w_obb[1]
                if dx * dx + dz * dz <= 361.0:
                    res.append(w_obb)
        return res

    def _test_obb_list(self, indices, checked, rx, rz, rdx, rdz, best_d, ry, rdy):
        obbs = self.solid_obbs
        hit_pt = None
        for idx in indices:
            if checked is not None:
                if idx in checked:
                    continue
                checked.add(idx)
            cx_o, cz_o, hw, hl, c, s, rad, minY_pad, maxY_pad, h9 = obbs[idx]
            dx = rx - cx_o
            dz = rz - cz_o
            proj = -(dx * rdx + dz * rdz)
            if proj < -rad or proj > best_d + rad:
                continue
            perp_sq = (dx * dx + dz * dz) - proj * proj
            if perp_sq > rad * rad:
                continue
            lx = dx * c - dz * s
            lz = dx * s + dz * c
            ldx = rdx * c - rdz * s
            ldz = rdx * s + rdz * c
            if abs(ldx) < 1e-6:
                if abs(lx) > hw:
                    continue
                t1x, t2x = -1e9, 1e9
            else:
                inv_x = 1.0 / ldx
                t1x = (-hw - lx) * inv_x
                t2x = (hw - lx) * inv_x
                if t1x > t2x:
                    t1x, t2x = t2x, t1x
            if abs(ldz) < 1e-6:
                if abs(lz) > hl:
                    continue
                t1z, t2z = -1e9, 1e9
            else:
                inv_z = 1.0 / ldz
                t1z = (-hl - lz) * inv_z
                t2z = (hl - lz) * inv_z
                if t1z > t2z:
                    t1z, t2z = t2z, t1z
            tmin = t1x if t1x > t1z else t1z
            tmax = t2x if t2x < t2z else t2z
            if tmin <= tmax and tmax >= 0.0 and 0.0 <= tmin < best_d:
                hx_hit = rx + rdx * tmin
                hz_hit = rz + rdz * tmin
                if ry is not None:
                    hit_y = ry + rdy * tmin
                    if hit_y < minY_pad or hit_y > maxY_pad:
                        continue
                    if h9 is not None:
                        hlx = lx + ldx * tmin
                        hlz = lz + ldz * tmin
                        if len(h9) == 25:
                            ux5 = max(0, min(4, int((hlx / max(0.1, hw) + 1.0) * 2.5)))
                            uz5 = max(0, min(4, int((hlz / max(0.1, hl) + 1.0) * 2.5)))
                            loc_max_y = (minY_pad + 0.55) + float(h9[uz5 * 5 + ux5]) * 0.1 + 0.35
                        else:
                            ix9 = 0 if hlx < -hw * 0.33 else (2 if hlx > hw * 0.33 else 1)
                            iz9 = 0 if hlz < -hl * 0.33 else (2 if hlz > hl * 0.33 else 1)
                            loc_max_y = (minY_pad + 0.55) + float(h9[iz9 * 3 + ix9]) * 0.1 + 0.45
                        if hit_y > loc_max_y:
                            continue
                    # For hull drive rays (ry - ground ~ 0.9m), if the hit point is a climbable rubble ramp (HULL_DOWN_3D), do not block!
                    if self.is_point_climbable_3d(hx_hit, hz_hit, hit_y - 0.90):
                        continue
                best_d = tmin
                hit_pt = (round(hx_hit, 2), round(hz_hit, 2))
        return best_d, hit_pt

    def raycast(self, rx, rz, rdx, rdz, max_d, ry=None, rdy=0.0, solid_only=True):
        best_d = float(max_d)
        hit_pt = None
        inv_cs = self.inv_cell
        grid = self.solid_grid
        if not grid:
            return (max_d, None)

        checked = set()
        steps = int(max_d / 18.0) + 1
        step_len = max_d / float(steps)
        prev_cell = None
        for st in range(steps + 1):
            t_cur = st * step_len
            if t_cur > best_d + 18.0:
                break
            cx = int(math.floor((rx + rdx * t_cur) * inv_cs))
            cz = int(math.floor((rz + rdz * t_cur) * inv_cs))
            cell = (cx, cz)
            if cell == prev_cell:
                continue
            prev_cell = cell
            indices = grid.get(cell)
            if not indices:
                continue
            nd, npt = self._test_obb_list(indices, checked, rx, rz, rdx, rdz, best_d, ry, rdy)
            if npt is not None:
                best_d, hit_pt = nd, npt
        return (best_d, hit_pt) if hit_pt else (max_d, None)


# Internal state singletons
_MAPS_DATABASE = None
_SPATIAL_GRID = SpatialOBBGrid(25.0)
_CURRENT_MAP_NAME = ''
_HTTP_SERVER_INSTANCE = None
_RADAR_MAP_JSON = '{"map":"Standby","bounds":[-500,-500,500,500],"buildings":[]}'
_RADAR_STATE_JSON = '{"map":"Standby","vehicles":[],"timestamp":0}'
_LAST_TICK_TIME = 0.0
_LAST_RADAR_POLL_TIME = 0.0
_LOS_CACHE = {}       # (vid, tid) -> (rx, rz, tx, tz, now, los_clear, los_hit_pt, los_window)
_BYPASS_MEMORY = {}   # vid -> (side_sign, until_t)


def _load_maps_database():
    global _MAPS_DATABASE
    if _MAPS_DATABASE is not None:
        return _MAPS_DATABASE
    if os.path.isfile(_MAPS_OBB_PATH):
        try:
            with open(_MAPS_OBB_PATH, 'r') as f:
                _MAPS_DATABASE = json.load(f)
        except Exception:
            _MAPS_DATABASE = {}
    else:
        _MAPS_DATABASE = {}
    return _MAPS_DATABASE


def set_active_map(raw_map_name):
    global _CURRENT_MAP_NAME, _RADAR_MAP_JSON
    if not raw_map_name:
        return
    short_name = str(raw_map_name).replace('\\', '/').rstrip('/').split('/')[-1].lower()
    if short_name == _CURRENT_MAP_NAME and len(_SPATIAL_GRID.all_obbs) > 0:
        return
    db = _load_maps_database()
    entry = db.get(short_name)
    if entry is None:
        for k in db:
            if k.endswith(short_name) or short_name.endswith(k):
                entry = db[k]
                short_name = k
                break
    bounds = entry.get('bounds', [-500.0, -500.0, 500.0, 500.0]) if entry else [-500.0, -500.0, 500.0, 500.0]
    obbs = entry.get('obbs', []) if entry else []
    _SPATIAL_GRID.set_geometry(bounds, obbs)
    _loaded_graph = None
    try:
        from gui.mods.offhangar.ai import prebaked_navigation as _pbn
        _loaded_graph = _pbn.load_graph(short_name)
        _SPATIAL_GRID.set_navgraph(_loaded_graph)
    except Exception:
        _SPATIAL_GRID.set_navgraph(None)
    _CURRENT_MAP_NAME = short_name
    _RADAR_MAP_JSON = b'{"map": "Standby", "bounds": [-500, -500, 500, 500], "buildings": []}'
    _LOS_CACHE.clear()
    _BYPASS_MEMORY.clear()
    _WHISKER_STATIC_CACHE.clear()


def _ensure_radar_map_json():
    global _RADAR_MAP_JSON
    short_name = _CURRENT_MAP_NAME or '04_himmelsdorf'
    if _RADAR_MAP_JSON and b'"Standby"' not in _RADAR_MAP_JSON:
        return _RADAR_MAP_JSON
    db = _load_maps_database()
    entry = db.get(short_name) or {}
    bounds = entry.get('bounds', [-500.0, -500.0, 500.0, 500.0])
    obbs = entry.get('obbs', [])
    from gui.mods.offhangar.ai import prebaked_navigation as _pbn
    _loaded_graph = _pbn.load_graph(short_name)
    try:
        terrain_payload = None
        rubble_pts = []
        pockets_out = []
        routes_out = []
        if _SPATIAL_GRID.nav_w > 0 and _SPATIAL_GRID.nav_h > 0 and _SPATIAL_GRID.nav_heights_mm:
            nw = _SPATIAL_GRID.nav_w
            nh = _SPATIAL_GRID.nav_h
            ncs = _SPATIAL_GRID.nav_cs
            nox = _SPATIAL_GRID.nav_ox
            noz = _SPATIAL_GRID.nav_oz
            nhm = _SPATIAL_GRID.nav_heights_mm
            nhz = _SPATIAL_GRID.nav_hazards
            nval = _SPATIAL_GRID.nav_valid
            # Export 8.0m 3D point-vision grid (tw x th) with exact height (decimeters) and bot vision state:
            # 0=solid_obstacle, 1=passable_flat, 2=climbable_slope, 3=rubble_hill_3d, 4=water, 5=cliff
            step_c = 2
            tw = (nw + step_c - 1) // step_c
            th = (nh + step_c - 1) // step_c
            t_heights = []
            t_flags = []
            for gz in range(0, nh, step_c):
                row_idx = gz * nw
                wz = noz + gz * ncs
                for gx in range(0, nw, step_c):
                    idx = row_idx + gx
                    wx = nox + gx * ncs
                    hm_v = nhm[idx]
                    y_m = float(hm_v) * 0.001 if (hm_v is not None and hm_v > -300000) else 0.0
                    hz_v = int(nhz[idx]) if nhz else 0
                    val_v = bool(nval[idx]) if nval else False
                    fl_code = 1
                    if (hz_v & 1) != 0:
                        fl_code = 4
                    elif (hz_v & 2) != 0:
                        fl_code = 5
                    elif not val_v:
                        fl_code = 0
                    else:
                        # Check local slope or 3D rubble flag
                        if _SPATIAL_GRID.mesh3d_w > 0 and _SPATIAL_GRID.mesh3d_flags:
                            mx = int((wx - _SPATIAL_GRID.mesh3d_ox) * _SPATIAL_GRID.mesh3d_inv_step)
                            mz = int((wz - _SPATIAL_GRID.mesh3d_oz) * _SPATIAL_GRID.mesh3d_inv_step)
                            if 0 <= mx < _SPATIAL_GRID.mesh3d_w and 0 <= mz < _SPATIAL_GRID.mesh3d_h:
                                mfl = int(_SPATIAL_GRID.mesh3d_flags[mz * _SPATIAL_GRID.mesh3d_w + mx])
                                if (mfl & 4) != 0:
                                    fl_code = 3
                        if fl_code == 1 and gx + 1 < nw and gz + 1 < nh:
                            hm_r = nhm[idx + 1]
                            hm_u = nhm[idx + nw]
                            if hm_r is not None and hm_u is not None:
                                gr = math.hypot(float(hm_r - (hm_v or 0)) * 0.001, float(hm_u - (hm_v or 0)) * 0.001) / ncs
                                if gr >= 0.14:
                                    fl_code = 2
                    t_heights.append(int(round(y_m * 10.0)))
                    t_flags.append(fl_code)
            terrain_payload = {
                'ox': round(nox, 1),
                'oz': round(noz, 1),
                'cs': round(ncs * step_c, 1),
                'w': tw,
                'h': th,
                'heights_dm': t_heights,
                'flags': t_flags,
            }
        # Collect high-resolution 3D climbable rubble hill points ('разрушенная горочка', HULL_DOWN_3D)
        if _SPATIAL_GRID.mesh3d_w > 0 and _SPATIAL_GRID.mesh3d_flags and _SPATIAL_GRID.mesh3d_heights:
            mw = _SPATIAL_GRID.mesh3d_w
            mh_g = _SPATIAL_GRID.mesh3d_h
            mox = _SPATIAL_GRID.mesh3d_ox
            moz = _SPATIAL_GRID.mesh3d_oz
            mst = _SPATIAL_GRID.mesh3d_step
            mfls = _SPATIAL_GRID.mesh3d_flags
            mhts = _SPATIAL_GRID.mesh3d_heights
            for mz in range(0, mh_g, 2):
                r_off = mz * mw
                wz = moz + mz * mst
                for mx in range(0, mw, 2):
                    mfl = int(mfls[r_off + mx])
                    if (mfl & 4) != 0 and (mfl & 26) == 0:
                        rubble_pts.append([round(mox + mx * mst, 1), round(float(mhts[r_off + mx]), 1), round(wz, 1)])
                        if len(rubble_pts) >= 2200:
                            break
                if len(rubble_pts) >= 2200:
                    break
        if isinstance(_loaded_graph, dict):
            raw_pockets = _loaded_graph.get('tactical_pockets') or []
            for pk in raw_pockets[:650]:
                if len(pk) >= 9:
                    _kp = int(pk[8])
                    _mgh = round(float(pk[9]), 2) if len(pk) >= 10 else 1.2
                    _mhw = round(float(pk[10]), 2) if len(pk) >= 11 else 4.0
                    _slh = round(float(pk[11]), 2) if len(pk) >= 12 else 0.0
                    pockets_out.append([
                        round(float(pk[0]), 1), round(float(pk[1]), 1), round(float(pk[2]), 1),
                        _kp,
                        round(float(pk[3]), 1), round(float(pk[4]), 1), round(float(pk[5]), 1),
                        _mgh, _mhw, _slh
                    ])
            raw_routes = _loaded_graph.get('routes') or {}
            for tm_k in ('1', '2', 1, 2):
                r_list = raw_routes.get(tm_k) or []
                for rt in r_list:
                    wps = rt.get('waypoints') or []
                    pts_3d = []
                    for wp in wps:
                        if len(wp) >= 2:
                            wx_r, wz_r = float(wp[0]), float(wp[1])
                            gy_r, _, _ = _SPATIAL_GRID.sample_elevation_3d(wx_r, wz_r, 0.0)
                            pts_3d.append([round(wx_r, 1), round(gy_r, 1), round(wz_r, 1)])
                    if pts_3d:
                        routes_out.append({
                            'team': int(tm_k),
                            'id': str(rt.get('id', 'route')),
                            'pts': pts_3d,
                        })
        raw_map_json = json.dumps({
            'map': short_name,
            'bounds': bounds,
            'buildings': obbs,
            'terrain': terrain_payload,
            'rubble_pts': rubble_pts,
            'pockets': pockets_out,
            'routes': routes_out,
        })
        if isinstance(raw_map_json, _UNICODE_TYPE):
            raw_map_json = raw_map_json.encode('utf-8')
        _RADAR_MAP_JSON = raw_map_json
    except Exception:
        pass
    # Both HTTP handlers write this straight to the socket; falling off the end
    # returned None on every rebuild, so the first /map_geom after a map switch failed.
    return _RADAR_MAP_JSON


def check_3d_sightline(start_pos, end_pos, barrel_height=1.45):
    """
    Fast 3D OBB sightline query for bot_ai.py (pockets, windows, arches, firing lanes).
    Returns True if a 3D ray at barrel_height (1.45m) or cupola height (1.85m) clears all solid 3D building OBBs.
    """
    try:
        if start_pos is None or end_pos is None or not _SPATIAL_GRID.solid_obbs:
            return True
        sx, sy, sz = float(start_pos[0]), float(start_pos[1]), float(start_pos[2])
        ex, ey, ez = float(end_pos[0]), float(end_pos[1]), float(end_pos[2])
        dx = ex - sx
        dz = ez - sz
        dist_2d = math.hypot(dx, dz)
        if dist_2d <= 2.0:
            return True
        nx = dx / dist_2d
        nz = dz / dist_2d
        slope_y = (ey - sy) / dist_2d
        _, hit_b = _SPATIAL_GRID.raycast(sx, sz, nx, nz, dist_2d - 1.2, ry=sy + float(barrel_height), rdy=slope_y, solid_only=True)
        if hit_b is None:
            return True
        _, hit_hi = _SPATIAL_GRID.raycast(sx, sz, nx, nz, dist_2d - 1.2, ry=sy + 1.85, rdy=slope_y, solid_only=True)
        return hit_hi is None
    except Exception:
        return True


def check_3d_sightline_detail(start_pos, end_pos, barrel_height=1.45):
    """
    Returns (los_clear, is_window_or_hulldown) in 3D:
    - los_clear: True if gun barrel (+1.45m) or cupola (+1.85m) ray clears all solid 3D OBBs.
    - is_window_or_hulldown: True if upper ray clears while lower hull ray (+0.68m) is shielded by a low wall/window sill!
    """
    try:
        if start_pos is None or end_pos is None or not _SPATIAL_GRID.solid_obbs:
            return (True, False)
        sx, sy, sz = float(start_pos[0]), float(start_pos[1]), float(start_pos[2])
        ex, ey, ez = float(end_pos[0]), float(end_pos[1]), float(end_pos[2])
        dx = ex - sx
        dz = ez - sz
        dist_2d = math.hypot(dx, dz)
        if dist_2d <= 2.0:
            return (True, False)
        nx = dx / dist_2d
        nz = dz / dist_2d
        slope_y = (ey - sy) / dist_2d
        _, hit_b = _SPATIAL_GRID.raycast(sx, sz, nx, nz, dist_2d - 1.2, ry=sy + float(barrel_height), rdy=slope_y, solid_only=True)
        if hit_b is None:
            _, hit_low = _SPATIAL_GRID.raycast(sx, sz, nx, nz, min(28.0, dist_2d - 1.2), ry=sy + 0.68, rdy=slope_y, solid_only=True)
            return (True, hit_low is not None)
        _, hit_hi = _SPATIAL_GRID.raycast(sx, sz, nx, nz, dist_2d - 1.2, ry=sy + 1.85, rdy=slope_y, solid_only=True)
        if hit_hi is None:
            return (True, True)
        return (False, False)
    except Exception:
        return (True, False)


def check_3d_drive_corridor(start_pos, end_pos, hull_pad=1.25):
    """
    Fast 3D hull driveability check for LocalDriver, dynamic LT/MT orbit arcs, and flanking plans:
    Verifies that centerline and lateral track shoulders (+-hull_pad) from start_pos to end_pos
    do NOT collide with solid 3D building/fortification/rock OBBs at hull height (+0.9m) and do NOT enter water or cliffs.
    """
    try:
        if start_pos is None or end_pos is None:
            return False
        sx, sy, sz = float(start_pos[0]), float(start_pos[1]), float(start_pos[2])
        ex, ey, ez = float(end_pos[0]), float(end_pos[1]), float(end_pos[2])
        dx = ex - sx
        dz = ez - sz
        dist_2d = math.hypot(dx, dz)
        if dist_2d <= 0.5:
            return not _SPATIAL_GRID.is_near_hazard(ex, ez)
        nx = dx / dist_2d
        nz = dz / dist_2d
        # Estimate end elevation from 3D terrain grid when end_pos[1] == start_pos[1]
        if abs(ey - sy) < 0.05 and _SPATIAL_GRID.nav_w > 0:
            ey_est, _, _ = _SPATIAL_GRID.sample_elevation_3d(ex, ez, ey)
            slope_y = (ey_est - sy) / dist_2d
        else:
            slope_y = (ey - sy) / dist_2d
        # 1. Check 3D water/cliff hazard ray along centerline
        _, haz_hit = _SPATIAL_GRID.cast_hazard_ray(sx, sz, nx, nz, dist_2d)
        if haz_hit is not None:
            return False
        # 2. Check 3D solid building/fortification OBBs along centerline and track shoulders (+-hull_pad)
        if _SPATIAL_GRID.solid_obbs:
            _, b_hit = _SPATIAL_GRID.raycast(sx, sz, nx, nz, dist_2d, ry=sy + 0.90, rdy=slope_y, solid_only=True)
            if b_hit is not None:
                return False
            if hull_pad > 0.2 and dist_2d <= 48.0:
                px, pz = -nz * float(hull_pad), nx * float(hull_pad)
                _, l_hit = _SPATIAL_GRID.raycast(sx + px, sz + pz, nx, nz, dist_2d, ry=sy + 0.90, rdy=slope_y, solid_only=True)
                if l_hit is not None:
                    return False
                _, r_hit = _SPATIAL_GRID.raycast(sx - px, sz - pz, nx, nz, dist_2d, ry=sy + 0.90, rdy=slope_y, solid_only=True)
                if r_hit is not None:
                    return False
        return True
    except Exception:
        return True


_WHISKER_STATIC_CACHE = {}  # vid -> (rx, rz, yaw, sec_dists, sec_hits, sec_hazs, sec_grades, sec_max_grades, sec_dy)
_FAST_OBB_CACHE = [None, None, ()]  # [ix, iz, obb_list]
_LAST_RADAR_JSON_TIME = 0.0
_SUB_TICK_COUNTER = 0


def fast_heading_obb_blocked(sx, sy, sz, rdx, rdz, max_d=8.0, hull_pad=0.9):
    """
    Ultra-fast O(1) local OBB heading check for LocalDriver._choose_yaw (called 15x per bot):
    Reuses the cached local OBB list for the bot's cell and tests (rdx, rdz) with zero allocations.
    """
    if not _SPATIAL_GRID.solid_obbs:
        return False
    ix = int(sx * 0.5)
    iz = int(sz * 0.5)
    if _FAST_OBB_CACHE[0] == ix and _FAST_OBB_CACHE[1] == iz:
        local_blds = _FAST_OBB_CACHE[2]
    else:
        local_blds = _SPATIAL_GRID.local_solid_obbs(sx, sy, sz)
        _FAST_OBB_CACHE[0] = ix
        _FAST_OBB_CACHE[1] = iz
        _FAST_OBB_CACHE[2] = local_blds
    if not local_blds:
        return False
    hy = sy + 0.90
    pad = float(hull_pad)
    for cx_o, cz_o, hw, hl, c, s, rad, minY_pad, maxY_pad, _h9 in local_blds:
        if hy < minY_pad or hy > maxY_pad:
            continue
        dx = sx - cx_o
        dz = sz - cz_o
        proj = -(dx * rdx + dz * rdz)
        if proj < -rad or proj > max_d + rad:
            continue
        if (dx * dx + dz * dz) - proj * proj > (rad + pad) * (rad + pad):
            continue
        lx = dx * c - dz * s
        lz = dz * c + dx * s
        ldx = rdx * c - rdz * s
        ldz = rdz * c + rdx * s
        hw_p = hw + pad
        hl_p = hl + pad
        if abs(ldx) < 1e-6:
            if abs(lx) > hw_p:
                continue
            t1x, t2x = -1e9, 1e9
        else:
            inv_x = 1.0 / ldx
            t1x = (-hw_p - lx) * inv_x
            t2x = (hw_p - lx) * inv_x
            if t1x > t2x:
                t1x, t2x = t2x, t1x
        if abs(ldz) < 1e-6:
            if abs(lz) > hl_p:
                continue
            t1z, t2z = -1e9, 1e9
        else:
            inv_z = 1.0 / ldz
            t1z = (-hl_p - lz) * inv_z
            t2z = (hl_p - lz) * inv_z
            if t1z > t2z:
                t1z, t2z = t2z, t1z
        tmin = t1x if t1x > t1z else t1z
        tmax = t2x if t2x < t2z else t2z
        if tmin <= tmax and tmax >= 0.0 and 0.0 <= tmin < max_d:
            if _h9 is not None and len(_h9) == 25:
                hlx = lx + ldx * max(0.0, tmin)
                hlz = lz + ldz * max(0.0, tmin)
                ux5 = max(0, min(4, int((hlx / max(0.1, hw) + 1.0) * 2.5)))
                uz5 = max(0, min(4, int((hlz / max(0.1, hl) + 1.0) * 2.5)))
                loc_max_y = (minY_pad + 0.55) + float(_h9[uz5 * 5 + ux5]) * 0.1 + 0.35
                if hy > loc_max_y:
                    continue
            hx_hit = sx + rdx * tmin
            hz_hit = sz + rdz * tmin
            if _SPATIAL_GRID.is_point_climbable_3d(hx_hit, hz_hit, sy):
                continue
            return True
    return False


def _cast_static_whisker(rx, rz, rdx, rdz, max_d, local_blds, check_haz=False, y0=0.0, fast_ring=False):
    """
    Static 3D world ray (terrain elevation/slope + hazards + solid 3D OBBs):
    Cached per bot until the hull moves >= 0.55m or rotates >= 0.045 rad.
    """
    best_d = float(max_d)
    hit_pt = None
    hit_haz = False

    prev_y = y0
    prev_d = 0.0
    max_local_grade = 0.0
    end_y = y0
    ring_steps = (4.2, float(max_d)) if (fast_ring or max_d < 9.0) else (3.5, 7.0, min(11.0, float(max_d)))
    for d_ring in ring_steps:
        if d_ring > best_d or d_ring <= prev_d + 0.2:
            continue
        px = rx + rdx * d_ring
        pz = rz + rdz * d_ring
        gy, sy, is_fatal = _SPATIAL_GRID.sample_elevation_3d(px, pz, prev_y)
        run = d_ring - prev_d
        loc_grade = (gy - prev_y) / run
        if abs(loc_grade) > abs(max_local_grade):
            max_local_grade = loc_grade
        step_relief = sy - gy
        climb_ok = _SPATIAL_GRID.is_point_climbable_3d(px, pz, prev_y)
        max_relief = 1.68 if climb_ok else 1.28
        if (is_fatal and not climb_ok) or step_relief >= max_relief or loc_grade > 0.44 or loc_grade < -0.40:
            if d_ring < best_d:
                best_d = d_ring
                hit_pt = (round(px, 2), round(pz, 2))
                hit_haz = True
            end_y = gy
            break
        prev_y = gy
        prev_d = d_ring
        end_y = gy

    slope_grade = (end_y - y0) / max(2.5, prev_d if prev_d > 0.5 else float(max_d))
    delta_y = end_y - y0

    if check_haz:
        hd, hpt = _SPATIAL_GRID.cast_hazard_ray(rx, rz, rdx, rdz, best_d)
        if hpt is not None and hd < best_d:
            best_d = hd
            hit_pt = hpt
            hit_haz = True

    if local_blds:
        for cx_o, cz_o, hw, hl, c, s, rad, minY_pad, maxY_pad, _h9 in local_blds:
            dx = rx - cx_o
            dz = rz - cz_o
            proj = -(dx * rdx + dz * rdz)
            if proj < -rad or proj > best_d + rad:
                continue
            if (dx * dx + dz * dz) - proj * proj > rad * rad:
                continue
            lx = dx * c - dz * s
            lz = dx * s + dz * c
            ldx = rdx * c - rdz * s
            ldz = rdx * s + rdz * c
            if abs(ldx) < 1e-6:
                if abs(lx) > hw:
                    continue
                t1x, t2x = -1e9, 1e9
            else:
                inv_x = 1.0 / ldx
                t1x = (-hw - lx) * inv_x
                t2x = (hw - lx) * inv_x
                if t1x > t2x:
                    t1x, t2x = t2x, t1x
            if abs(ldz) < 1e-6:
                if abs(lz) > hl:
                    continue
                t1z, t2z = -1e9, 1e9
            else:
                inv_z = 1.0 / ldz
                t1z = (-hl - lz) * inv_z
                t2z = (hl - lz) * inv_z
                if t1z > t2z:
                    t1z, t2z = t2z, t1z
            tmin = t1x if t1x > t1z else t1z
            tmax = t2x if t2x < t2z else t2z
            if tmin <= tmax and tmax >= 0.0 and 0.0 <= tmin < best_d:
                ray_hy = y0 + 0.90 + slope_grade * tmin
                if ray_hy < minY_pad or ray_hy > maxY_pad:
                    continue
                hx_hit = rx + rdx * tmin
                hz_hit = rz + rdz * tmin
                if _SPATIAL_GRID.is_point_climbable_3d(hx_hit, hz_hit, ray_hy - 0.90):
                    continue
                best_d = tmin
                hit_pt = (round(hx_hit, 2), round(hz_hit, 2))
                hit_haz = False

    return best_d, hit_pt, hit_haz, slope_grade, max_local_grade, delta_y


def _test_dynamic_vehs_whisker(rx, rz, rdx, rdz, best_d, hit_pt, hit_haz, local_vehs, ignore_vid):
    hit_veh = False
    for v_id, vx, vy, vz, hw, hl, c, s, rad in local_vehs:
        if v_id == ignore_vid:
            continue
        dx = rx - vx
        dz = rz - vz
        proj = -(dx * rdx + dz * rdz)
        if proj < 0.0 or proj > best_d + rad:
            continue
        if (dx * dx + dz * dz) - proj * proj > rad * rad:
            continue
        lx = dx * c - dz * s
        lz = dx * s + dz * c
        ldx = rdx * c - rdz * s
        ldz = rdx * s + rdz * c
        if abs(ldx) < 1e-6:
            if abs(lx) > hw:
                continue
            t1x, t2x = -1e9, 1e9
        else:
            inv_x = 1.0 / ldx
            t1x = (-hw - lx) * inv_x
            t2x = (hw - lx) * inv_x
            if t1x > t2x:
                t1x, t2x = t2x, t1x
        if abs(ldz) < 1e-6:
            if abs(lz) > hl:
                continue
            t1z, t2z = -1e9, 1e9
        else:
            inv_z = 1.0 / ldz
            t1z = (-hl - lz) * inv_z
            t2z = (hl - lz) * inv_z
            if t1z > t2z:
                t1z, t2z = t2z, t1z
        tmin = t1x if t1x > t1z else t1z
        tmax = t2x if t2x < t2z else t2z
        if tmin <= tmax and tmax >= 0.0 and 0.0 <= tmin < best_d:
            best_d = tmin
            hit_pt = (round(rx + rdx * tmin, 2), round(rz + rdz * tmin, 2))
            hit_veh = True
            hit_haz = False
    return best_d, hit_pt, hit_veh, hit_haz


def _classify_slope(grade, max_loc_grade, c_haz):
    if c_haz or max_loc_grade > 0.42 or max_loc_grade < -0.38:
        return 'impassable_cliff'
    if grade >= 0.25 or max_loc_grade >= 0.28:
        return 'steep_climb'
    if grade >= 0.13:
        return 'moderate_climb'
    if grade >= 0.055:
        return 'gentle_up'
    if grade <= -0.20 or max_loc_grade <= -0.24:
        return 'steep_descent'
    if grade <= -0.06:
        return 'gentle_down'
    return 'flat'


def tick_battle_subengine(mock_vehicles, player_vehicle_id, map_name):
    """
    Main 3D subengine tick: computes 16-sector 360-degree hull vision (front, sides, rear),
    continuous 3D terrain elevation/slope profiling, forward & reverse steering/throttle guidance,
    and 3D barrel/window line-of-sight rays with per-bot static ray caching and zero FPS spikes.
    """
    global _LAST_TICK_TIME, _RADAR_STATE_JSON, _LAST_RADAR_JSON_TIME, _SUB_TICK_COUNTER
    now = time.time()
    if map_name:
        set_active_map(map_name)
    if (now - _LAST_TICK_TIME) < 0.22:
        return
    _LAST_TICK_TIME = now
    _SUB_TICK_COUNTER += 1

    items = list(mock_vehicles.iteritems() if hasattr(mock_vehicles, 'iteritems') else mock_vehicles.items())
    poses = {}
    veh_obbs = []
    wreck_obbs_3d = []
    for vid, v in items:
        pos = getattr(v, 'position', None)
        if pos is None:
            continue
        px, py, pz = float(pos.x), float(pos.y), float(pos.z)
        is_alive = bool(getattr(v, 'isAlive', True) and (getattr(v, 'health', 0) or 0) > 0)
        vyaw = float(getattr(v, 'yaw', 0.0) or 0.0)
        cy, sy_v = math.cos(vyaw), math.sin(vyaw)
        if is_alive:
            poses[vid] = (px, py, pz)
            veh_obbs.append((vid, px, py, pz, 1.75, 3.45, cy, sy_v, 3.9))
        else:
            veh_obbs.append((vid, px, py, pz, 2.05, 3.75, cy, sy_v, 4.3))
            wreck_obbs_3d.append((px, pz, 1.95, 3.65, cy, sy_v, 4.15, py - 0.8, py + 2.6, None))
    if len(wreck_obbs_3d) != len(_SPATIAL_GRID.dynamic_wreck_obbs):
        _FAST_OBB_CACHE[0] = None
        _WHISKER_STATIC_CACHE.clear()
    _SPATIAL_GRID.dynamic_wreck_obbs = wreck_obbs_3d

    radar_active = ((now - _LAST_RADAR_POLL_TIME) <= 3.0) and ((now - _LAST_RADAR_JSON_TIME) >= 0.16)
    radar_vehicles = [] if radar_active else None
    p_pose = poses.get(player_vehicle_id)
    tick_parity = _SUB_TICK_COUNTER & 1

    for vid, v in items:
        pos = getattr(v, 'position', None)
        if pos is None:
            continue
        rx = float(pos.x)
        ry = float(getattr(pos, 'y', 0.0) or 0.0)
        rz = float(pos.z)
        yaw = float(getattr(v, 'yaw', 0.0) or 0.0)
        speed = float(getattr(v, '_veh_velocity', 0.0) or 0.0)
        is_alive = bool(getattr(v, 'isAlive', True) and (getattr(v, 'health', 0) or 0) > 0)
        tid = getattr(v, '_ai_target_id', None)
        ai_ord = getattr(v, '_offh_last_order_dict', None)
        if not isinstance(ai_ord, dict):
            oc = getattr(v, '_offh_ai_order_cache', None)
            if isinstance(oc, tuple) and len(oc) >= 3 and isinstance(oc[2], dict):
                ai_ord = oc[2]
        c_mode = str(ai_ord.get('combat_mode', 'route')) if isinstance(ai_ord, dict) else 'route'
        ignore_vid = ai_ord.get('wreck_cover_id') if isinstance(ai_ord, dict) else None
        if c_mode in ('ram_brawl', 'clinch', 'cliff_dive_ram') and tid is not None:
            ignore_vid = tid

        # Stagger distant (>130m from player) non-engaged bots across 2 slices (halves raycasting load!)
        prev_guid = getattr(v, '_subengine_guidance', None)
        if is_alive and prev_guid is not None and vid != player_vehicle_id and tid is None:
            if p_pose is not None and ((rx - p_pose[0]) ** 2 + (rz - p_pose[2]) ** 2) > 16900.0:
                if (int(vid) & 1) != tick_parity and not prev_guid.get('wall_warning'):
                    continue

        steer_bias = 0.0
        throttle_cap = 1.0
        rear_steer_bias = 0.0
        rear_throttle_cap = 1.0
        rear_clear_dist = 10.2
        wall_warning = False
        c_dist, c_hit, c_veh = 13.0, None, False
        l_dist, l_hit = 11.2, None
        r_dist, r_hit = 11.2, None
        fl_dist, fl_hit = 9.8, None
        fr_dist, fr_hit = 9.8, None
        b_dist, b_hit = 10.2, None
        rl_dist, rl_hit = 9.2, None
        rr_dist, rr_hit = 9.2, None
        slope_ahead = 0.0
        cross_slope = 0.0
        elev_ahead_m = 0.0
        slope_status = 'flat'
        best_360_yaw = yaw
        rays_16 = None
        los_clear = True
        los_hit_pt = None
        los_window = False

        if is_alive:
            local_vehs = [
                vo for vo in veh_obbs
                if vo[0] != vid and abs(vo[2] - ry) <= 3.2 and (vo[1] - rx) ** 2 + (vo[3] - rz) ** 2 <= 289.0
            ]
            # Check static 360-degree whisker cache (reused when hull moved < 1.1m and rotated < 0.095 rad)
            w_cached = _WHISKER_STATIC_CACHE.get(vid)
            if (w_cached is not None and
                    (rx - w_cached[0]) ** 2 + (rz - w_cached[1]) ** 2 < 1.21 and
                    abs(math.atan2(math.sin(yaw - w_cached[2]), math.cos(yaw - w_cached[2]))) < 0.095):
                st_dists, st_hits, st_hazs, sec_grades, sec_max_grades, sec_dy = w_cached[3], w_cached[4], w_cached[5], w_cached[6], w_cached[7], w_cached[8]
            else:
                near_haz = _SPATIAL_GRID.is_near_hazard(rx, rz)
                local_blds = _SPATIAL_GRID.local_solid_obbs(rx, ry, rz)
                st_dists = [0.0] * 16
                st_hits = [None] * 16
                st_hazs = [False] * 16
                sec_grades = [0.0] * 16
                sec_max_grades = [0.0] * 16
                sec_dy = [0.0] * 16
                y0_veh, _, _ = _SPATIAL_GRID.sample_elevation_3d(rx, rz, ry)
                for s_idx in range(16):
                    s_yaw = yaw + _SECTOR_ANGLES[s_idx]
                    s_max_d = _SECTOR_MAX_DIST[s_idx]
                    sd, spt, shaz, sgr, smgr, sdy = _cast_static_whisker(
                        rx, rz, math.sin(s_yaw), math.cos(s_yaw), s_max_d,
                        local_blds, near_haz, y0=y0_veh, fast_ring=(s_idx & 1) != 0)
                    st_dists[s_idx] = sd
                    st_hits[s_idx] = spt
                    st_hazs[s_idx] = shaz
                    sec_grades[s_idx] = sgr
                    sec_max_grades[s_idx] = smgr
                    sec_dy[s_idx] = sdy
                _WHISKER_STATIC_CACHE[vid] = (rx, rz, yaw, st_dists, st_hits, st_hazs, sec_grades, sec_max_grades, sec_dy)

            if local_vehs:
                sec_dists = [0.0] * 16
                sec_hits = [None] * 16
                sec_vehs = [False] * 16
                sec_hazs = [False] * 16
                for s_idx in range(16):
                    s_yaw = yaw + _SECTOR_ANGLES[s_idx]
                    sd, spt, sveh, shaz = _test_dynamic_vehs_whisker(
                        rx, rz, math.sin(s_yaw), math.cos(s_yaw),
                        st_dists[s_idx], st_hits[s_idx], st_hazs[s_idx],
                        local_vehs, ignore_vid)
                    sec_dists[s_idx] = sd
                    sec_hits[s_idx] = spt
                    sec_vehs[s_idx] = sveh
                    sec_hazs[s_idx] = shaz
            else:
                sec_dists = st_dists
                sec_hits = st_hits
                sec_vehs = [False] * 16
                sec_hazs = st_hazs

            if radar_active:
                rays_16 = []
                for s_idx in range(16):
                    shaz = sec_hazs[s_idx]
                    spt = sec_hits[s_idx]
                    sgr = sec_grades[s_idx]
                    r_flag = 2 if shaz else (1 if spt is not None else (3 if sgr >= 0.16 else 0))
                    rays_16.append([round(sec_dists[s_idx], 1), round(sgr, 2), r_flag])

            c_dist, c_hit, c_veh, c_haz = sec_dists[0], sec_hits[0], sec_vehs[0], sec_hazs[0]
            r_dist, r_hit, _, r_haz = sec_dists[1], sec_hits[1], sec_vehs[1], sec_hazs[1]
            fr_dist, fr_hit, _, fr_haz = sec_dists[2], sec_hits[2], sec_vehs[2], sec_hazs[2]
            l_dist, l_hit, _, l_haz = sec_dists[15], sec_hits[15], sec_vehs[15], sec_hazs[15]
            fl_dist, fl_hit, _, fl_haz = sec_dists[14], sec_hits[14], sec_vehs[14], sec_hazs[14]

            b_dist, b_hit, _, b_haz = sec_dists[8], sec_hits[8], sec_vehs[8], sec_hazs[8]
            rr_dist, rr_hit, _, rr_haz = min(sec_dists[7], sec_dists[6]), (sec_hits[7] or sec_hits[6]), False, (sec_hazs[7] or sec_hazs[6])
            rl_dist, rl_hit, _, rl_haz = min(sec_dists[9], sec_dists[10]), (sec_hits[9] or sec_hits[10]), False, (sec_hazs[9] or sec_hazs[10])
            rear_clear_dist = min(b_dist, sec_dists[7], sec_dists[9])

            slope_ahead = sec_grades[0]
            elev_ahead_m = sec_dy[0]
            cross_slope = (sec_dy[4] - sec_dy[12]) / 14.0
            slope_status = _classify_slope(slope_ahead, sec_max_grades[0], c_haz)

            def _eff_space(idx_s, w_s):
                d_s = sec_dists[idx_s]
                hz_s = sec_hazs[idx_s]
                gr_s = max(0.0, sec_grades[idx_s])
                slope_factor = max(0.35, 1.0 - max(0.0, gr_s - 0.12) * 2.2)
                return d_s * w_s * (0.25 if hz_s else slope_factor)

            left_space = _eff_space(15, 1.0) + _eff_space(14, 0.75) + _eff_space(13, 0.45) + _eff_space(12, 0.25)
            right_space = _eff_space(1, 1.0) + _eff_space(2, 0.75) + _eff_space(3, 0.45) + _eff_space(4, 0.25)

            if l_hit is not None and l_dist < 8.0 and left_space < right_space and not (r_haz and r_dist < 8.5):
                steer_bias += (1.0 - (l_dist / 8.0)) * (0.95 if l_haz else 0.80)
                wall_warning = True
            elif r_hit is not None and r_dist < 8.0 and right_space < left_space and not (l_haz and l_dist < 8.5):
                steer_bias -= (1.0 - (r_dist / 8.0)) * (0.95 if r_haz else 0.80)
                wall_warning = True
            elif fl_hit is not None and fl_dist < 6.2 and left_space < right_space:
                steer_bias += (1.0 - (fl_dist / 6.2)) * 0.55
                wall_warning = True
            elif fr_hit is not None and fr_dist < 6.2 and right_space < left_space:
                steer_bias -= (1.0 - (fr_dist / 6.2)) * 0.55
                wall_warning = True

            if not wall_warning and slope_ahead >= 0.16 and abs(steer_bias) < 0.15:
                l_diag_grade = min(sec_grades[15], sec_grades[14])
                r_diag_grade = min(sec_grades[1], sec_grades[2])
                if l_diag_grade < slope_ahead - 0.04 and l_dist > 8.5 and not l_haz and l_diag_grade <= r_diag_grade:
                    steer_bias = -0.36
                elif r_diag_grade < slope_ahead - 0.04 and r_dist > 8.5 and not r_haz:
                    steer_bias = 0.36

            if c_hit is not None and c_dist < (11.0 if c_haz else 9.0):
                wall_warning = True
                mem = _BYPASS_MEMORY.get(vid)
                if mem is not None and now < mem[1] and not c_haz:
                    side_sign = mem[0]
                else:
                    side_sign = -1.0 if left_space >= right_space else 1.0
                    _BYPASS_MEMORY[vid] = (side_sign, now + (1.4 if c_haz else 2.2))
                if c_haz:
                    steer_bias = side_sign * max(0.78, min(1.0, (11.0 - c_dist) / 6.5))
                    throttle_cap = 0.55 if c_dist > 5.0 else 0.25
                elif c_veh:
                    steer_bias = side_sign * max(0.65, min(1.0, (9.5 - c_dist) / 6.0))
                    throttle_cap = 0.85 if c_dist > 3.2 else 0.50
                elif c_dist < 6.2:
                    throttle_cap = max(0.32, c_dist / 6.2)
                    if abs(steer_bias) < 0.40:
                        steer_bias = side_sign * 0.90

            steer_bias = max(-1.0, min(1.0, steer_bias))

            rear_left_space = _eff_space(9, 1.0) + _eff_space(10, 0.80) + _eff_space(11, 0.45)
            rear_right_space = _eff_space(7, 1.0) + _eff_space(6, 0.80) + _eff_space(5, 0.45)
            if rear_clear_dist < 6.5:
                rear_throttle_cap = 0.0 if rear_clear_dist < 2.2 else max(0.35, min(1.0, rear_clear_dist / 6.5))
                rear_steer_bias = -0.85 if rear_left_space >= rear_right_space else 0.85
            elif abs(rear_left_space - rear_right_space) > 2.5:
                rear_steer_bias = -0.35 if rear_left_space > rear_right_space else 0.35

            best_360_score = -1e9
            for s_idx in range(16):
                s_ang = _SECTOR_ANGLES[s_idx]
                if s_ang > math.pi:
                    s_ang -= 2.0 * math.pi
                d_norm = sec_dists[s_idx] / _SECTOR_MAX_DIST[s_idx]
                pen_haz = -2.5 if sec_hazs[s_idx] else (-0.8 if sec_hits[s_idx] is not None else 0.0)
                pen_slope = -max(0.0, abs(sec_grades[s_idx]) - 0.12) * 2.5
                align_bonus = -abs(s_ang) * 0.32
                sc_360 = d_norm * 1.5 + pen_haz + pen_slope + align_bonus
                if sc_360 > best_360_score:
                    best_360_score = sc_360
                    best_360_yaw = yaw + s_ang

            if tid is not None and tid in poses:
                tx, ty, tz = poses[tid]
                l_key = (vid, tid)
                l_cached = _LOS_CACHE.get(l_key)
                if (l_cached is not None and (now - l_cached[4]) < 0.38 and
                        (rx - l_cached[0]) ** 2 + (rz - l_cached[1]) ** 2 < 1.96 and
                        (tx - l_cached[2]) ** 2 + (tz - l_cached[3]) ** 2 < 1.96):
                    los_clear, los_hit_pt, los_window = l_cached[5], l_cached[6], l_cached[7]
                else:
                    tdx = tx - rx
                    tdy = ty - ry
                    tdz = tz - rz
                    dist_tgt = math.hypot(tdx, tdz)
                    if dist_tgt > 1.5:
                        nx = tdx / dist_tgt
                        nz = tdz / dist_tgt
                        slope_y = tdy / dist_tgt
                        _, l_pt_b = _SPATIAL_GRID.raycast(rx, rz, nx, nz, dist_tgt - 1.5, ry=ry + 1.45, rdy=slope_y, solid_only=True)
                        if l_pt_b is None:
                            los_clear = True
                            los_hit_pt = None
                            if dist_tgt <= 165.0:
                                _, l_pt_low = _SPATIAL_GRID.raycast(rx, rz, nx, nz, min(28.0, dist_tgt - 1.5), ry=ry + 0.70, rdy=slope_y, solid_only=True)
                                los_window = (l_pt_low is not None)
                        elif dist_tgt <= 165.0:
                            _, l_pt_hi = _SPATIAL_GRID.raycast(rx, rz, nx, nz, dist_tgt - 1.5, ry=ry + 1.85, rdy=slope_y, solid_only=True)
                            if l_pt_hi is None:
                                los_clear = True
                                los_hit_pt = None
                                los_window = True
                            else:
                                los_clear = False
                                los_hit_pt = l_pt_b
                        else:
                            los_clear = False
                            los_hit_pt = l_pt_b
                    _LOS_CACHE[l_key] = (rx, rz, tx, tz, now, los_clear, los_hit_pt, los_window)

            v._subengine_guidance = {
                'steer_bias': round(steer_bias, 2),
                'throttle_cap': round(throttle_cap, 2),
                'rear_steer_bias': round(rear_steer_bias, 2),
                'rear_throttle_cap': round(rear_throttle_cap, 2),
                'rear_clear_dist': round(rear_clear_dist, 1),
                'wall_warning': wall_warning,
                'los_clear': los_clear,
                'los_window': los_window,
                'wall_dist': round(c_dist, 1),
                'slope_ahead': round(slope_ahead, 3),
                'cross_slope': round(cross_slope, 3),
                'elev_ahead_m': round(elev_ahead_m, 2),
                'slope_status': slope_status,
                'best_360_yaw': round(best_360_yaw, 3),
            }

        if not radar_active:
            continue
        _td = getattr(v, 'typeDescriptor', None)
        _tags = getattr(getattr(_td, 'type', None), 'tags', ()) or ()
        _cls = 'Tank'
        for _ct in ('heavyTank', 'mediumTank', 'AT-SPG', 'lightTank', 'SPG'):
            if _ct in _tags:
                _cls = _ct
                break

        _plan_pts = []
        _route_id = ''
        _tac_reason = ''
        _act_desc = ''
        _live_3d = getattr(v, '_offh_live_3d_path', None)
        if isinstance(ai_ord, dict):
            _route_id = str(ai_ord.get('route_id', '') or '')
            _tac_reason = str(ai_ord.get('tactic_reason', '') or '')
            _act_desc = str(ai_ord.get('action_desc', '') or '')
            _raw_pw = ai_ord.get('plan_waypoints')
            # For dynamic orbit / wolfpack encirclement, show the multi-step 3D orbit arc;
            # otherwise prefer the bot's actual live 3D A* trajectory (_offh_live_3d_path) around terrain/obstacles!
            def _append_wp_3d(pt_list, wp_item):
                if isinstance(wp_item, (list, tuple)):
                    if len(wp_item) >= 3:
                        wx_p, wy_p, wz_p = float(wp_item[0]), float(wp_item[1]), float(wp_item[2])
                        if abs(wy_p) < 0.01:
                            wy_p, _, _ = _SPATIAL_GRID.sample_elevation_3d(wx_p, wz_p, ry)
                        pt_list.append([round(wx_p, 1), round(wz_p, 1), round(wy_p, 1)])
                    elif len(wp_item) == 2:
                        wx_p, wz_p = float(wp_item[0]), float(wp_item[1])
                        wy_p, _, _ = _SPATIAL_GRID.sample_elevation_3d(wx_p, wz_p, ry)
                        pt_list.append([round(wx_p, 1), round(wz_p, 1), round(wy_p, 1)])
            if c_mode in ('circle_of_death', 'wolfpack_encircle') and isinstance(_raw_pw, (list, tuple)) and _raw_pw:
                for _wp_p in _raw_pw[:6]:
                    _append_wp_3d(_plan_pts, _wp_p)
            elif isinstance(_live_3d, (list, tuple)) and _live_3d:
                for _wp_p in _live_3d[:8]:
                    _append_wp_3d(_plan_pts, _wp_p)
            elif isinstance(_raw_pw, (list, tuple)) and _raw_pw:
                for _wp_p in _raw_pw[:6]:
                    _append_wp_3d(_plan_pts, _wp_p)
            elif ai_ord.get('move_position') is not None:
                _append_wp_3d(_plan_pts, ai_ord.get('move_position'))

        radar_vehicles.append({
            'id': vid,
            'team': int(getattr(v, '_bot_team', getattr(v, 'team', 0))),
            'is_player': (vid == player_vehicle_id),
            'name': getattr(v, 'name', '') or getattr(getattr(_td, 'type', None), 'shortUserString', 'Bot'),
            'class': _cls,
            'mode': c_mode,
            'route_id': _route_id,
            'tactic_reason': _tac_reason,
            'action_desc': _act_desc,
            'plan_pts': _plan_pts,
            'x': round(rx, 2),
            'y': round(ry, 2),
            'z': round(rz, 2),
            'yaw': round(yaw, 3),
            'turret_yaw': round(float(getattr(v, '_turret_yaw', getattr(v, 'turret_yaw', 0.0)) or 0.0), 3),
            'gun_pitch': round(float(getattr(v, '_gun_pitch', getattr(v, 'gun_pitch', 0.0)) or 0.0), 3),
            'speed': round(speed, 1),
            'target_id': tid,
            'hp': int(getattr(v, 'health', 0) or 0),
            'max_hp': int(getattr(v, 'maxHealth', 100) or 100),
            'is_alive': is_alive,
            'c_dist': round(c_dist, 1), 'c_hit': c_hit is not None,
            'l_dist': round(l_dist, 1), 'l_hit': l_hit is not None,
            'r_dist': round(r_dist, 1), 'r_hit': r_hit is not None,
            'fl_dist': round(fl_dist, 1), 'fl_hit': fl_hit is not None,
            'fr_dist': round(fr_dist, 1), 'fr_hit': fr_hit is not None,
            'b_dist': round(b_dist, 1), 'b_hit': b_hit is not None,
            'rl_dist': round(rl_dist, 1), 'rl_hit': rl_hit is not None,
            'rr_dist': round(rr_dist, 1), 'rr_hit': rr_hit is not None,
            'rays_16': rays_16,
            'slope_ahead': round(slope_ahead, 3),
            'cross_slope': round(cross_slope, 3),
            'elev_ahead_m': round(elev_ahead_m, 2),
            'slope_status': slope_status,
            'steer_bias': round(steer_bias, 2),
            'throttle_cap': round(throttle_cap, 2),
            'rear_clear_dist': round(rear_clear_dist, 1),
            'wall_warning': wall_warning,
            'los_clear': los_clear,
            'los_window': los_window,
            'los_hit_pt': los_hit_pt,
        })

    if radar_active:
        try:
            s_json = json.dumps({
                'map': _CURRENT_MAP_NAME or 'Standby',
                'vehicles': radar_vehicles,
                'timestamp': round(now, 3),
            })
            if isinstance(s_json, _UNICODE_TYPE):
                s_json = s_json.encode('utf-8')
            _RADAR_STATE_JSON = s_json
            _LAST_RADAR_JSON_TIME = now
        except Exception:
            pass
    _LAST_TICK_TIME = now


_MAP_TEX_CACHE = {}


def _decode_dxt_to_bmp(dds_bytes, max_dim=512):
    """Pure-Python DXT1/DXT3/DXT5 .dds decoder -> 24-bit uncompressed BMP bytes (zero external deps, works in Python 2.7 & 3.x)."""
    try:
        import struct as _st
        if not dds_bytes or len(dds_bytes) < 128 or dds_bytes[:4] != b'DDS ':
            return None
        h_dds = _st.unpack_from('<I', dds_bytes, 12)[0]
        w_dds = _st.unpack_from('<I', dds_bytes, 16)[0]
        fourcc = dds_bytes[84:88]
        if w_dds <= 0 or h_dds <= 0 or fourcc not in (b'DXT1', b'DXT3', b'DXT5'):
            return None
        bsize = 8 if fourcc == b'DXT1' else 16
        bw = max(1, (w_dds + 3) // 4)
        bh = max(1, (h_dds + 3) // 4)
        step = 4
        while (w_dds // step) > 256 and step < 16:
            step *= 2
        out_w = max(1, w_dds // step)
        out_h = max(1, h_dds // step)
        row_pad = (4 - (out_w * 3) % 4) % 4
        stride = out_w * 3 + row_pad
        pix = bytearray(stride * out_h)
        off0 = 128
        for oy in range(out_h):
            sy = oy * step
            by = sy // 4
            py_in = sy & 3
            dst_row = (out_h - 1 - oy) * stride
            for ox in range(out_w):
                sx = ox * step
                bx = sx // 4
                px_in = sx & 3
                boff = off0 + (by * bw + bx) * bsize
                if boff + bsize > len(dds_bytes):
                    continue
                c_off = boff if bsize == 8 else boff + 8
                c0, c1, bits = _st.unpack_from('<HHI', dds_bytes, c_off)
                r0 = ((c0 >> 11) & 31) * 255 // 31
                g0 = ((c0 >> 5) & 63) * 255 // 63
                b0 = (c0 & 31) * 255 // 31
                r1 = ((c1 >> 11) & 31) * 255 // 31
                g1 = ((c1 >> 5) & 63) * 255 // 63
                b1 = (c1 & 31) * 255 // 31
                code = (bits >> (2 * (py_in * 4 + px_in))) & 3
                if code == 0:
                    r, g, b = r0, g0, b0
                elif code == 1:
                    r, g, b = r1, g1, b1
                elif c0 > c1 or bsize == 16:
                    if code == 2:
                        r = (2 * r0 + r1) // 3; g = (2 * g0 + g1) // 3; b = (2 * b0 + b1) // 3
                    else:
                        r = (r0 + 2 * r1) // 3; g = (g0 + 2 * g1) // 3; b = (b0 + 2 * b1) // 3
                else:
                    if code == 2:
                        r = (r0 + r1) // 2; g = (g0 + g1) // 2; b = (b0 + b1) // 2
                    else:
                        r, g, b = 0, 0, 0
                di = dst_row + ox * 3
                pix[di] = b
                pix[di + 1] = g
                pix[di + 2] = r
        fsize = 54 + len(pix)
        hdr = _st.pack('<2sIHHI IIIHHIIIIII', b'BM', fsize, 0, 0, 54, 40, out_w, out_h, 1, 24, 0, len(pix), 2835, 2835, 0, 0)
        return bytes(hdr) + bytes(pix)
    except Exception:
        return None


def _load_map_texture_bytes(req_m):
    """Returns (content_type, bytes) for map texture from maps3d/<map>/color.jpg or res/packages/<map>.pkg / gui.pkg."""
    if req_m in _MAP_TEX_CACHE:
        return _MAP_TEX_CACHE[req_m]
    try:
        # 1. Check maps3d/<map>/color.jpg in common locations relative to mod & game root
        cands = []
        d = _OFFHANGAR_DIR
        for _ in range(7):
            cands.append(os.path.join(d, 'maps3d', req_m, 'color.jpg'))
            cands.append(os.path.join(d, 'tools', 'navedit', 'maps3d', req_m, 'color.jpg'))
            cands.append(os.path.join(d, 'navedit', 'maps3d', req_m, 'color.jpg'))
            d = os.path.dirname(d)
        for p in cands:
            if os.path.isfile(p):
                raw = open(p, 'rb').read()
                res = ('image/jpeg', raw)
                _MAP_TEX_CACHE[req_m] = res
                return res
        # 2. Check res/packages/gui.pkg or <map>.pkg for .png / .dds minimap
        import zipfile as _zf
        d = _OFFHANGAR_DIR
        pkg_dirs = []
        # Host-side tools (NavEdit) run outside the client and point here via .env.
        if os.environ.get('WOT_082_DIR'):
            pkg_dirs.append(os.path.join(os.environ['WOT_082_DIR'], 'res', 'packages'))
        for _ in range(7):
            pkg_dirs.append(os.path.join(d, 'res', 'packages'))
            d = os.path.dirname(d)
        for pdir in pkg_dirs:
            if not os.path.isdir(pdir):
                continue
            gui_pkg = os.path.join(pdir, 'gui.pkg')
            if os.path.isfile(gui_pkg):
                with _zf.ZipFile(gui_pkg, 'r') as zf:
                    nl = zf.namelist()
                    for ext, mime in (('.png', 'image/png'), ('.dds', 'image/bmp')):
                        ap = 'gui/maps/icons/map/%s%s' % (req_m, ext)
                        if ap in nl:
                            raw = zf.read(ap)
                            if ext == '.dds':
                                bmp = _decode_dxt_to_bmp(raw, 512)
                                if bmp:
                                    res = ('image/bmp', bmp)
                                    _MAP_TEX_CACHE[req_m] = res
                                    return res
                            else:
                                res = (mime, raw)
                                _MAP_TEX_CACHE[req_m] = res
                                return res
            mpkg = os.path.join(pdir, '%s.pkg' % req_m)
            if os.path.isfile(mpkg):
                with _zf.ZipFile(mpkg, 'r') as zf:
                    for n in zf.namelist():
                        nl_low = n.lower()
                        if nl_low.endswith('mmap.dds') or (nl_low.endswith('color_tex.dds') and 'landscape' in nl_low):
                            bmp = _decode_dxt_to_bmp(zf.read(n), 512)
                            if bmp:
                                res = ('image/bmp', bmp)
                                _MAP_TEX_CACHE[req_m] = res
                                return res
    except Exception:
        pass
    return (None, None)


class SubengineHTTPHandler(BaseHTTPRequestHandler):
    """Zero-lock-contention internal HTTP server serving the Live 3D Bot Tracker (pure observation, no editor clutter)."""
    def do_GET(self):
        global _LAST_RADAR_POLL_TIME
        _LAST_RADAR_POLL_TIME = time.time()
        try:
            if self.path == '/' or self.path.startswith('/view'):
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                cur_m = _CURRENT_MAP_NAME or '04_himmelsdorf'
                html = VIEWER_HTML.replace('__MAP_NAME__', cur_m)
                if isinstance(html, _UNICODE_TYPE):
                    html = html.encode('utf-8')
                self.wfile.write(html)
            elif self.path.startswith('/maps_list'):
                db = _load_maps_database()
                all_m = sorted(db.keys())
                cur_m = _CURRENT_MAP_NAME or ('04_himmelsdorf' if '04_himmelsdorf' in db else (all_m[0] if all_m else '04_himmelsdorf'))
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                out = json.dumps({'maps': all_m, 'current': cur_m})
                if isinstance(out, _UNICODE_TYPE):
                    out = out.encode('utf-8')
                self.wfile.write(out)
            elif self.path.startswith('/map_geom'):
                if '?map=' in self.path:
                    req_m = self.path.split('?map=', 1)[1].split('&')[0]
                    if req_m and not _CURRENT_MAP_NAME:
                        set_active_map(req_m)
                elif not _CURRENT_MAP_NAME:
                    set_active_map('04_himmelsdorf')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(_ensure_radar_map_json())
            elif self.path.startswith('/map_tex'):
                req_m = _CURRENT_MAP_NAME or '04_himmelsdorf'
                if '?map=' in self.path:
                    req_m = self.path.split('?map=', 1)[1].split('&')[0] or req_m
                mime, img_bytes = _load_map_texture_bytes(req_m)
                if img_bytes:
                    self.send_response(200)
                    self.send_header('Content-Type', mime)
                    self.send_header('Cache-Control', 'max-age=3600')
                    self.end_headers()
                    self.wfile.write(img_bytes)
                else:
                    self.send_response(404)
                    self.end_headers()
            elif self.path.startswith('/state'):
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(_RADAR_STATE_JSON)
            else:
                self.send_response(404)
                self.end_headers()
        except Exception:
            pass

    def log_message(self, format, *args):
        pass


def _http_server_thread():
    global _HTTP_SERVER_INSTANCE
    try:
        server = HTTPServer(('127.0.0.1', HTTP_PORT), SubengineHTTPHandler)
        _HTTP_SERVER_INSTANCE = server
        server.serve_forever()
    except Exception:
        pass


def ensure_http_radar_running():
    global _HTTP_SERVER_INSTANCE
    if _HTTP_SERVER_INSTANCE is None:
        t = threading.Thread(target=_http_server_thread, name="SubengineRadarHTTP")
        if hasattr(t, 'daemon'):
            t.daemon = True
        elif hasattr(t, 'setDaemon'):
            t.setDaemon(True)
        t.start()


def start_battle_subengine(map_name):
    """Called automatically when battle begins."""
    set_active_map(map_name)
    ensure_http_radar_running()


def stop_battle_subengine():
    """Called when battle ends."""
    global _RADAR_STATE_JSON
    _RADAR_STATE_JSON = '{"map":"Standby","vehicles":[],"timestamp":0}'


VIEWER_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <title>Offhangar 3D Bot Tracker // Навигатор и Трекинг Ботов в Реальном Времени</title>
    <style>
        * { box-sizing: border-box; }
        body { margin: 0; padding: 0; background: #070b10; color: #f1f5f9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; overflow: hidden; user-select: none; }
        #canvas { display: block; width: 100vw; height: 100vh; cursor: grab; }
        #canvas:active { cursor: grabbing; }
        #ui { position: absolute; top: 12px; left: 12px; background: rgba(11, 16, 26, 0.94); border: 1px solid #263449; border-radius: 10px; padding: 12px 16px; width: 440px; box-shadow: 0 10px 36px rgba(0,0,0,0.75); backdrop-filter: blur(6px); z-index: 10; }
        h1 { margin: 0 0 8px 0; font-size: 13.5px; font-weight: 800; color: #38bdf8; letter-spacing: 0.5px; display: flex; align-items: center; gap: 8px; }
        .live-dot { width: 9px; height: 9px; background: #10b981; border-radius: 50%; box-shadow: 0 0 10px #10b981; animation: pulse 1.5s infinite; }
        @keyframes pulse { 0% { opacity: 0.4; } 50% { opacity: 1; } 100% { opacity: 0.4; } }
        .row { font-size: 11.5px; color: #94a3b8; margin: 4px 0; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; justify-content: space-between; }
        .val { color: #f8fafc; font-weight: 700; }
        .legend { margin-top: 8px; display: flex; flex-wrap: wrap; gap: 6px 10px; font-size: 11px; padding-top: 8px; border-top: 1px solid #1e293b; }
        .tag { display: inline-flex; align-items: center; gap: 5px; color: #cbd5e1; }
        .dot { width: 9px; height: 9px; border-radius: 2px; }
        #toolbar { position: absolute; top: 12px; right: 12px; background: rgba(11, 16, 26, 0.94); border: 1px solid #263449; border-radius: 10px; padding: 8px 10px; display: flex; flex-wrap: wrap; gap: 5px; max-width: calc(100vw - 470px); justify-content: flex-end; z-index: 10; box-shadow: 0 10px 36px rgba(0,0,0,0.75); }
        button { background: #162032; color: #cbd5e1; border: 1px solid #2d3f58; border-radius: 6px; padding: 6px 10px; font-size: 11px; font-weight: 600; cursor: pointer; transition: all 0.12s ease; }
        button:hover { background: #1e2d44; color: #38bdf8; border-color: #38bdf8; }
        button.active { background: #0284c7; border-color: #38bdf8; color: #fff; }
        button.mode3d { background: #059669; border-color: #34d399; color: #fff; }
        #bot-inspector { position: absolute; bottom: 14px; left: 12px; background: rgba(11, 16, 26, 0.95); border: 1px solid #263449; border-radius: 10px; padding: 12px 16px; font-size: 12px; min-width: 320px; display: none; z-index: 10; box-shadow: 0 10px 36px rgba(0,0,0,0.75); }
        #help { position: absolute; bottom: 14px; right: 12px; background: rgba(11, 16, 26, 0.85); border: 1px solid #263449; border-radius: 8px; padding: 6px 12px; font-size: 11px; color: #94a3b8; z-index: 10; }
    </style>
</head>
<body>
    <div id="ui">
        <h1><span class="live-dot"></span> OFFHANGAR // 3D ТРЕКЕР БОТОВ (LIVE)</h1>
        <div class="row">
            <span>Карта: <span id="map-name" class="val" style="color:#38bdf8">__MAP_NAME__</span></span>
            <span>Ботов в бою: <span id="bot-count" class="val" style="color:#10b981">0</span></span>
            <span>FPS: <span id="fps-val" class="val">60</span></span>
        </div>
        <div class="row">
            <span>Неразруш.: <span id="bld-count" class="val" style="color:#38bdf8">0</span></span>
            <span>Непреод. разр.: <span id="imp-destr-count" class="val" style="color:#f43f5e">0</span></span>
            <span>Преод. разр.: <span id="pass-destr-count" class="val" style="color:#f59e0b">0</span></span>
        </div>
        <div class="row">
            <span>Подъёмы/Горочки: <span id="rubble-count" class="val" style="color:#a3e635">0</span></span>
            <span>Окна и Позиции: <span id="win-count" class="val" style="color:#d946ef">0</span></span>
            <span>Вид: <span id="cam-mode-lbl" class="val" style="color:#34d399">3D Перспектива</span></span>
        </div>
        <div class="legend">
            <span class="tag"><span class="dot" style="background:#22c55e"></span> Союзники</span>
            <span class="tag"><span class="dot" style="background:#ef4444"></span> Враги</span>
            <span class="tag"><span class="dot" style="background:#eab308"></span> Игрок</span>
            <span class="tag"><span class="dot" style="background:#38bdf8"></span> <span id="leg-c0">Неразрушаемые здания</span></span>
            <span class="tag"><span class="dot" style="background:#f43f5e"></span> <span id="leg-c3">Непреодолимые разр.</span></span>
            <span class="tag"><span class="dot" style="background:#f59e0b"></span> <span id="leg-c1">Разрушаемые преодолимые</span></span>
            <span class="tag"><span class="dot" style="background:#a3e635"></span> <span id="leg-c2">Горочки и подъёмы</span></span>
            <span class="tag"><span class="dot" style="background:#d946ef"></span> Окна / Бойницы (по габаритам танка)</span>
        </div>
    </div>

    <div id="toolbar">
        <button id="btn-mode3d" class="active mode3d" onclick="toggle3D()">🎥 3D / 2D Вид</button>
        <button id="btn-tex" class="active" onclick="toggleOption('tex')">🗺️ Подложка</button>
        <button id="btn-grid3d" class="active" onclick="toggleOption('grid3d')">🟢 3D Рельеф</button>
        <button id="btn-buildings" class="active" onclick="toggleOption('buildings')">🏢 Неразрушаемые (0)</button>
        <button id="btn-impDestr" class="active" onclick="toggleOption('impDestr')">🚧 Непреодолимые разр. (3)</button>
        <button id="btn-destr" class="active" onclick="toggleOption('destr')">🪵 Разрушаемые преод. (1)</button>
        <button id="btn-rubble" class="active" onclick="toggleOption('rubble')">⛰️ Горочки / Подъёмы (2)</button>
        <button id="btn-pockets" class="active" onclick="toggleOption('pockets')">🎯 Окна / Позиции</button>
        <button id="btn-plans" class="active" onclick="toggleOption('plans')">🛣️ Пути (A*)</button>
        <button id="btn-whiskers" class="active" onclick="toggleOption('whiskers')">📡 16 Лучей</button>
        <button id="btn-aim" class="active" onclick="toggleOption('aim')">🔫 Прострел (LOS)</button>
        <button onclick="resetView()">Сброс камеры</button>
    </div>

    <div id="bot-inspector">
        <div style="font-weight:800;color:#38bdf8;margin-bottom:6px;display:flex;justify-content:space-between">
            <span id="insp-name">Танк</span>
            <span style="font-size:10.5px;color:#94a3b8;cursor:pointer" onclick="selectedVehicleId=null;document.getElementById('bot-inspector').style.display='none'">[✕]</span>
        </div>
        <div id="insp-details" style="color:#cbd5e1;line-height:1.55"></div>
    </div>

    <div id="help">ЛКМ: Вращение 3D / Клик по танку — слежение | ПКМ или Shift+ЛКМ: Панорама | Колесо: Зум</div>
    <canvas id="canvas"></canvas>

    <script>
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
        const canvas = document.getElementById('canvas');
        const ctx = canvas.getContext('2d', { alpha: false });
        let width = window.innerWidth, height = window.innerHeight;

        let is3D = true;
        let camYaw = 0.0, camPitch = 0.80, camDist = 640.0;
        let camTx = 0.0, camTy = 8.0, camTz = 0.0;
        let scale2D = 1.0, panX2D = 640, panY2D = 360;
        let dragMode = 0, lastMx = 0, lastMy = 0, clickMoved = false;

        let loadedMapName = '';
        let bounds = [-500, -500, 500, 500];
        let allObbs = [];
        let terrainData = null;
        let rubblePts = [];
        let pocketsData = [];
        let routesData = [];
        let vehicles = [];
        let vehMap = new Map();
        let interpPose = new Map();
        let selectedVehicleId = null;

        const texCanvas = document.createElement('canvas');
        texCanvas.width = 1024;
        texCanvas.height = 1024;
        const texCtx = texCanvas.getContext('2d');
        let mapImg = null;
        let texReady = false;

        let opts = {
            tex: true, grid3d: true, plans: true, whiskers: true, aim: true,
            buildings: true, impDestr: true, destr: true, rubble: true, pockets: true
        };

        function toggleOption(k) {
            opts[k] = !opts[k];
            const b = document.getElementById('btn-' + k);
            if (b) b.className = opts[k] ? 'active' : '';
        }

        function toggle3D() {
            is3D = !is3D;
            const b = document.getElementById('btn-mode3d');
            const l = document.getElementById('cam-mode-lbl');
            if (is3D) {
                b.className = 'active mode3d';
                l.innerText = '3D Перспектива';
                l.style.color = '#34d399';
            } else {
                b.className = 'active';
                l.innerText = '2D Тактический';
                l.style.color = '#38bdf8';
            }
        }

        function sampleY(wx, wz) {
            if (!terrainData || !terrainData.heights_dm) return 0.0;
            const gx = Math.floor((wx - terrainData.ox) / terrainData.cs + 0.5);
            const gz = Math.floor((wz - terrainData.oz) / terrainData.cs + 0.5);
            if (gx >= 0 && gx < terrainData.w && gz >= 0 && gz < terrainData.h) {
                return terrainData.heights_dm[gz * terrainData.w + gx] * 0.1;
            }
            return 0.0;
        }

        function resetView() {
            selectedVehicleId = null;
            document.getElementById('bot-inspector').style.display = 'none';
            const wW = Math.max(100, bounds[2] - bounds[0]);
            const wH = Math.max(100, bounds[3] - bounds[1]);
            camTx = (bounds[0] + bounds[2]) * 0.5;
            camTz = (bounds[1] + bounds[3]) * 0.5;
            camTy = sampleY(camTx, camTz);
            camDist = Math.max(wW, wH) * 0.68;
            camYaw = 0.0;
            camPitch = 0.80;
            scale2D = Math.min(width / wW, height / wH) * 0.88;
            panX2D = width * 0.5 - camTx * scale2D;
            panY2D = height * 0.5 + camTz * scale2D;
        }

        function resize() {
            width = canvas.width = window.innerWidth;
            height = canvas.height = window.innerHeight;
        }
        window.addEventListener('resize', resize);
        resize();

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
            upX = sy * sp; upY = cp; upZ = cy * sp;
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

        canvas.addEventListener('contextmenu', e => e.preventDefault());
        canvas.addEventListener('mousedown', e => {
            lastMx = e.clientX; lastMy = e.clientY; clickMoved = false;
            dragMode = (e.button === 2 || e.button === 1 || e.shiftKey) ? 2 : 1;
        });
        window.addEventListener('mousemove', e => {
            if (!dragMode) return;
            const dx = e.clientX - lastMx, dy = e.clientY - lastMy;
            if (Math.abs(dx) + Math.abs(dy) > 3) clickMoved = true;
            lastMx = e.clientX; lastMy = e.clientY;
            if (is3D) {
                if (dragMode === 1) {
                    camYaw += dx * 0.0065;
                    camPitch = Math.max(0.18, Math.min(1.52, camPitch + dy * 0.0060));
                } else {
                    selectedVehicleId = null;
                    const pf = camDist * 0.0014;
                    const cy = Math.cos(camYaw), sy = Math.sin(camYaw);
                    camTx -= (dx * cy + dy * sy) * pf;
                    camTz -= (-dx * sy + dy * cy) * pf;
                    camTy = sampleY(camTx, camTz);
                }
            } else {
                selectedVehicleId = null;
                panX2D += dx; panY2D += dy;
            }
        });
        window.addEventListener('mouseup', e => {
            if (dragMode === 1 && !clickMoved && e.button === 0) {
                let bestV = null, bestD = 26.0;
                for (let i = 0; i < vehicles.length; i++) {
                    const v = vehicles[i];
                    const p = interpPose.get(v.id) || v;
                    const pr = project3D(p.x, (v.y || sampleY(p.x, p.z)) + 1.5, p.z);
                    if (!pr) continue;
                    const d = Math.hypot(pr[0] - e.clientX, pr[1] - e.clientY);
                    if (d < bestD) { bestD = d; bestV = v; }
                }
                selectedVehicleId = bestV ? bestV.id : null;
                updateInspector(bestV);
            }
            dragMode = 0;
        });
        canvas.addEventListener('wheel', e => {
            e.preventDefault();
            if (is3D) {
                camDist = Math.max(30.0, Math.min(2000.0, camDist * (e.deltaY < 0 ? 0.86 : 1.16)));
            } else {
                const f = e.deltaY < 0 ? 1.15 : 0.87;
                panX2D = e.clientX - (e.clientX - panX2D) * f;
                panY2D = e.clientY - (e.clientY - panY2D) * f;
                scale2D *= f;
            }
        });

        function bakeUnderlayTexture() {
            texCtx.fillStyle = '#0c131d';
            texCtx.fillRect(0, 0, 1024, 1024);
            if (mapImg && mapImg.complete && mapImg.naturalWidth > 0) {
                texCtx.drawImage(mapImg, 0, 0, 1024, 1024);
                texReady = true;
                return;
            }
            if (!terrainData || !terrainData.heights_dm) return;
            const tw = terrainData.w, th = terrainData.h, hdm = terrainData.heights_dm, fls = terrainData.flags;
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
                    const yR = (gx + 1 < tw) ? hdm[r0 + gx + 1] * 0.1 : y;
                    const yU = (gz + 1 < th) ? hdm[(gz + 1) * tw + gx] * 0.1 : y;
                    const shade = Math.max(0.55, Math.min(1.35, 1.0 + ((yR - y) - (yU - y)) * 0.12));
                    let r = (34 + hn * 55) * shade, g = (46 + hn * 52) * shade, b = (36 + hn * 38) * shade;
                    if (fl === 4) { r = 14; g = 65; b = 115; }
                    else if (fl === 5) { r = 95; g = 55; b = 38; }
                    else if (fl === 0) { r = 28; g = 34; b = 46; }
                    else if (fl === 3) { r = 74; g = 98; b = 42; }
                    texCtx.fillStyle = `rgb(${r|0},${g|0},${b|0})`;
                    texCtx.fillRect(gx * cellW, py, Math.ceil(cellW), Math.ceil(cellH));
                }
            }
            texReady = true;
        }

        // Draw sculpted Low-Poly 3D Model with LOD, early frustum culling, and batched canvas strokes
        function drawLowPolyModel(o) {
            const cx = o[0], cz = o[1], hw = o[2], hl = o[3], yaw = o[4], minY = o[5], maxY = o[6], fl = o[7];
            const hGrid = (o.length >= 9 && Array.isArray(o[8])) ? o[8] : null;
            const winMeta = (o.length >= 10 && Array.isArray(o[9])) ? o[9] : null;
            const c = Math.cos(yaw), s = Math.sin(yaw);

            // Fast Bounding Sphere Frustum Culling: skip projection completely if off-screen
            const pMid = project3D(cx, (minY + maxY) * 0.5, cz);
            const bRadius = Math.max(hw, hl) * 2.2 + 50.0;
            if (pMid && (pMid[0] < -bRadius || pMid[0] > width + bRadius || pMid[1] < -bRadius || pMid[1] > height + bRadius)) return;

            let strokeCol = 'rgba(56, 189, 248, 0.82)';
            let fillCol = 'rgba(15, 23, 42, 0.70)';
            if (fl === 3) {
                strokeCol = 'rgba(244, 63, 94, 0.92)';
                fillCol = 'rgba(159, 18, 57, 0.50)';
            } else if (fl === 2) {
                strokeCol = 'rgba(163, 230, 53, 0.82)';
                fillCol = 'rgba(132, 204, 22, 0.25)';
            } else if (fl === 1) {
                strokeCol = 'rgba(245, 158, 11, 0.70)';
                fillCol = 'rgba(180, 83, 9, 0.22)';
            }

            // LOD: When camera is zoomed out (camDist > 180), render clean 4-corner 3D box (1 quad instead of 16 sub-quads)
            if (is3D && hGrid && (hGrid.length === 25 || hGrid.length === 9) && camDist <= 180) {
                const N = (hGrid.length === 25) ? 5 : 3;
                const halfN = (N - 1) * 0.5;
                const gridPts = [];
                for (let iz = 0; iz < N; iz++) {
                    const lz = ((iz - halfN) / halfN) * hl;
                    for (let ix = 0; ix < N; ix++) {
                        const lx = ((ix - halfN) / halfN) * hw;
                        const wx = cx + lx * c + lz * s;
                        const wz = cz - lx * s + lz * c;
                        const wy = minY + Math.max(0.2, hGrid[iz * N + ix] * 0.1);
                        const pr = project3D(wx, wy, wz);
                        if (!pr) return;
                        gridPts.push(pr);
                    }
                }

                // Batch fill sub-quads
                ctx.fillStyle = fillCol;
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
                    }
                }

                // Batch outline stroke (single stroke call per building instead of 16)
                ctx.strokeStyle = strokeCol;
                ctx.lineWidth = (fl === 1) ? 0.9 : 1.1;
                ctx.beginPath();
                for (let ix = 0; ix < N - 1; ix++) {
                    ctx.moveTo(gridPts[ix][0], gridPts[ix][1]); ctx.lineTo(gridPts[ix + 1][0], gridPts[ix + 1][1]);
                    const b0 = (N - 1) * N + ix;
                    ctx.moveTo(gridPts[b0][0], gridPts[b0][1]); ctx.lineTo(gridPts[b0 + 1][0], gridPts[b0 + 1][1]);
                }
                for (let iz = 0; iz < N - 1; iz++) {
                    const l0 = iz * N, l1 = (iz + 1) * N;
                    ctx.moveTo(gridPts[l0][0], gridPts[l0][1]); ctx.lineTo(gridPts[l1][0], gridPts[l1][1]);
                    const r0 = iz * N + (N - 1), r1 = (iz + 1) * N + (N - 1);
                    ctx.moveTo(gridPts[r0][0], gridPts[r0][1]); ctx.lineTo(gridPts[r1][0], gridPts[r1][1]);
                }

                // Corner vertical pillars
                const cIndices = [0, N - 1, N * N - 1, N * (N - 1)];
                const cornerL = [[-hw, -hl], [hw, -hl], [hw, hl], [-hw, hl]];
                for (let ci = 0; ci < 4; ci++) {
                    const lx = cornerL[ci][0], lz = cornerL[ci][1];
                    const pb = project3D(cx + lx * c + lz * s, minY, cz - lx * s + lz * c);
                    const pt = gridPts[cIndices[ci]];
                    if (pb && pt) { ctx.moveTo(pb[0], pb[1]); ctx.lineTo(pt[0], pt[1]); }
                }
                ctx.stroke();

                // Highlight 3D firing window / embrasure aperture if present (winMeta[3] === 1)
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
                        ctx.fillStyle = 'rgba(217, 70, 239, 0.28)';
                        ctx.lineWidth = 1.5;
                        ctx.beginPath();
                        ctx.moveTo(w0[0], w0[1]); ctx.lineTo(w1[0], w1[1]);
                        ctx.lineTo(w2[0], w2[1]); ctx.lineTo(w3[0], w3[1]);
                        ctx.closePath();
                        ctx.fill();
                        ctx.stroke();
                    }
                }
                return;
            }

            const corners = [[-hw, -hl], [hw, -hl], [hw, hl], [-hw, hl]];
            const bPts = [], tPts = [];
            for (let i = 0; i < 4; i++) {
                const lx = corners[i][0], lz = corners[i][1];
                const wx = cx + lx * c + lz * s, wz = cz - lx * s + lz * c;
                const pb = project3D(wx, minY, wz);
                const pt = project3D(wx, maxY, wz);
                if (!pb || !pt) return;
                bPts.push(pb); tPts.push(pt);
            }
            if (bPts[0][0] < -150 && bPts[2][0] < -150) return;
            if (bPts[0][0] > width + 150 && bPts[2][0] > width + 150) return;

            ctx.fillStyle = fillCol;
            ctx.strokeStyle = strokeCol;
            ctx.lineWidth = (fl === 1) ? 0.9 : 1.15;
            ctx.beginPath();
            ctx.moveTo(tPts[0][0], tPts[0][1]);
            for (let i = 1; i < 4; i++) ctx.lineTo(tPts[i][0], tPts[i][1]);
            ctx.closePath();
            ctx.fill();
            if (is3D) {
                for (let i = 0; i < 4; i++) {
                    ctx.moveTo(bPts[i][0], bPts[i][1]);
                    ctx.lineTo(tPts[i][0], tPts[i][1]);
                }
            }
            ctx.stroke();
        }

        function updateInspector(v) {
            const panel = document.getElementById('bot-inspector');
            if (!v) { panel.style.display = 'none'; return; }
            panel.style.display = 'block';
            document.getElementById('insp-name').innerText = (v.name || 'Bot') + ' [ID: ' + v.id + ']';
            const pitchDeg = ((v.gun_pitch || 0) * 180 / Math.PI).toFixed(1);
            const slopeDeg = (Math.atan(v.slope_ahead || 0) * 180 / Math.PI).toFixed(1);
            const sightStatus = !v.target_id ? 'Поиск цели' : (v.los_clear ? (v.los_window ? '<span style="color:#d946ef">Прострел через окно/бойницу</span>' : '<span style="color:#00e5ff">Прямая видимость (LOS)</span>') : '<span style="color:#f97316">Перекрыто 3D-препятствием</span>');
            document.getElementById('insp-details').innerHTML =
                'Команда: ' + (v.team === 1 ? '<span style="color:#22c55e">Союзники (1)</span>' : '<span style="color:#ef4444">Противник (2)</span>') + ' | Класс: <b>' + (v.class || 'Tank') + '</b><br>' +
                'Режим ИИ: <span style="color:#38bdf8;font-weight:700">' + (v.mode || 'route') + '</span>' + (v.route_id ? ' | Фланг: <span style="color:#fbbf24">' + v.route_id + '</span>' : '') + '<br>' +
                (v.tactic_reason ? 'Решение: <span style="color:#e879f9">' + v.tactic_reason + '</span><br>' : '') +
                (v.action_desc ? 'Манёвр: <span style="color:#4ade80">' + v.action_desc + '</span><br>' : '') +
                'HP: <b>' + (v.hp || 0) + ' / ' + (v.max_hp || 0) + '</b> (' + Math.round((v.hp / (v.max_hp || 1)) * 100) + '%)<br>' +
                '3D Коорд: (' + v.x.toFixed(1) + ', <b>' + (v.y || 0).toFixed(1) + 'м</b>, ' + v.z.toFixed(1) + ')<br>' +
                'Рельеф по курсу: <b>' + (v.slope_status || 'flat') + '</b> (' + slopeDeg + '°, dY=' + (v.elev_ahead_m || 0).toFixed(1) + 'м)<br>' +
                'Скорость: ' + (v.speed || 0).toFixed(1) + ' м/с | Ствол: ' + pitchDeg + '°<br>' +
                'Прицел: ' + sightStatus;
        }

        function lerpAngle(a, b, t) {
            let d = (b - a) % (Math.PI * 2);
            if (d > Math.PI) d -= Math.PI * 2;
            if (d < -Math.PI) d += Math.PI * 2;
            return a + d * t;
        }

        let lastFrameTime = performance.now(), frameCount = 0, fpsTimer = 0;

        function render() {
            const now = performance.now();
            const dt = Math.min(0.1, (now - lastFrameTime) / 1000);
            lastFrameTime = now;
            frameCount++; fpsTimer += dt;
            if (fpsTimer >= 0.5) {
                document.getElementById('fps-val').innerText = Math.round(frameCount / fpsTimer);
                frameCount = 0; fpsTimer = 0;
            }

            const blend = Math.min(1.0, dt * 11.0);
            for (let i = 0; i < vehicles.length; i++) {
                const v = vehicles[i];
                let p = interpPose.get(v.id);
                if (!p || Math.hypot(v.x - p.x, v.z - p.z) > 35.0) {
                    p = { x: v.x, y: v.y || sampleY(v.x, v.z), z: v.z, yaw: v.yaw || 0, tyaw: v.turret_yaw || 0 };
                    interpPose.set(v.id, p);
                } else {
                    p.x += (v.x - p.x) * blend;
                    p.z += (v.z - p.z) * blend;
                    p.y += ((v.y || sampleY(p.x, p.z)) - p.y) * blend;
                    p.yaw = lerpAngle(p.yaw, v.yaw || 0, blend);
                    p.tyaw = lerpAngle(p.tyaw, v.turret_yaw || 0, blend);
                }
            }

            if (selectedVehicleId !== null) {
                const sp = interpPose.get(selectedVehicleId);
                if (sp) {
                    if (is3D) {
                        camTx += (sp.x - camTx) * 0.15;
                        camTz += (sp.z - camTz) * 0.15;
                        camTy += (sp.y - camTy) * 0.15;
                    } else {
                        panX2D += ((width * 0.5 - sp.x * scale2D) - panX2D) * 0.15;
                        panY2D += ((height * 0.5 + sp.z * scale2D) - panY2D) * 0.15;
                    }
                }
            }

            updateCam();
            ctx.fillStyle = '#070b10';
            ctx.fillRect(0, 0, width, height);

            if (opts.tex && texReady && !is3D) {
                const [tlX, tlY] = project3D(bounds[0], 0, bounds[3]);
                const [brX, brY] = project3D(bounds[2], 0, bounds[1]);
                ctx.globalAlpha = 0.78;
                ctx.drawImage(texCanvas, tlX, tlY, brX - tlX, brY - tlY);
                ctx.globalAlpha = 1.0;
            }

            // 1. 3D Terrain Elevation Wireframe
            if (opts.grid3d && terrainData && terrainData.heights_dm) {
                const tw = terrainData.w, th = terrainData.h, ox = terrainData.ox, oz = terrainData.oz, cs = terrainData.cs;
                const hdm = terrainData.heights_dm, fls = terrainData.flags;
                ctx.lineWidth = 0.75;
                ctx.strokeStyle = 'rgba(16, 185, 129, 0.15)';
                ctx.beginPath();
                const stride = (camDist > 480 && is3D) ? 2 : 1;
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
            }

            // 2. Prebaked Tactical Corridors (faint background reference)
            if (opts.plans && routesData.length > 0) {
                for (let i = 0; i < routesData.length; i++) {
                    const rt = routesData[i];
                    const pts = rt.pts;
                    if (!pts || pts.length < 2) continue;
                    ctx.strokeStyle = (rt.team === 1) ? 'rgba(34, 197, 94, 0.24)' : 'rgba(239, 68, 68, 0.24)';
                    ctx.lineWidth = 1.4;
                    ctx.beginPath();
                    let st = false;
                    for (let j = 0; j < pts.length; j++) {
                        const pr = project3D(pts[j][0], pts[j][1] + 0.6, pts[j][2]);
                        if (!pr) continue;
                        if (!st) { ctx.moveTo(pr[0], pr[1]); st = true; }
                        else ctx.lineTo(pr[0], pr[1]);
                    }
                    ctx.stroke();
                }
            }

            // 3. All 4 Classes of 3D Low-Poly Models (0=Indestructible, 3=Impassable Destructible, 2=Climbable Rubble/Ramps, 1=Passable Destructible)
            for (let i = 0; i < allObbs.length; i++) {
                const o = allObbs[i];
                const fl = o[7];
                if (fl === 0 && !opts.buildings) continue;
                if (fl === 3 && !opts.impDestr) continue;
                if (fl === 2 && !opts.rubble) continue;
                if (fl === 1 && !opts.destr) continue;
                drawLowPolyModel(o);
            }

            // 3b. Render Tactical Firing Positions, Climbable Ramps, and Window Embrasures (sized to tank hull/gun!)
            if (opts.pockets && pocketsData.length > 0) {
                for (let i = 0; i < pocketsData.length; i++) {
                    const pk = pocketsData[i];
                    const sx = pk[0], sy = pk[1], sz = pk[2], kind = pk[3];
                    const kx = (pk.length >= 7) ? pk[4] : sx;
                    const ky = (pk.length >= 7) ? pk[5] : sy;
                    const kz = (pk.length >= 7) ? pk[6] : sz;
                    const pS = project3D(sx, sy + 1.2, sz);
                    const pK = project3D(kx, ky + 1.4, kz);
                    if (!pS || pS[0] < 0 || pS[0] > width || pS[1] < 0 || pS[1] > height) continue;
                    if (kind === 3) {
                        // Window / Wall Breach Firing Position (magenta diamond + peek line)
                        ctx.fillStyle = '#d946ef';
                        ctx.fillRect(pS[0] - 3, pS[1] - 3, 6, 6);
                        if (pK) {
                            ctx.strokeStyle = 'rgba(217, 70, 239, 0.80)';
                            ctx.lineWidth = 1.4;
                            ctx.beginPath(); ctx.moveTo(pS[0], pS[1]); ctx.lineTo(pK[0], pK[1]); ctx.stroke();
                        }
                    } else if (kind === 2) {
                        // Climbable Rubble / Ramp Position (lime marker)
                        ctx.fillStyle = '#a3e635';
                        ctx.fillRect(pS[0] - 2.5, pS[1] - 2.5, 5, 5);
                    } else {
                        // Corner Peek Position (cyan dot)
                        ctx.fillStyle = 'rgba(56, 189, 248, 0.65)';
                        ctx.fillRect(pS[0] - 2, pS[1] - 2, 4, 4);
                    }
                }
            }

            // 4. Live Vehicles, 16-Ray 360° Hull Whiskers, Live A* Paths, and 3D Sightlines
            for (let i = 0; i < vehicles.length; i++) {
                const v = vehicles[i];
                if (v.is_alive === false) continue;
                const p = interpPose.get(v.id) || v;
                const vy = (p.y !== undefined) ? p.y : sampleY(p.x, p.z);
                const c0 = project3D(p.x, vy + 1.0, p.z);
                if (!c0) continue;

                const isP = v.is_player;
                const isA = (v.team === 1);
                const col = isP ? '#eab308' : (isA ? '#22c55e' : '#ef4444');

                // Live A* Path (plan_pts)
                if (opts.plans && v.plan_pts && v.plan_pts.length > 0) {
                    ctx.strokeStyle = isP ? '#fde047' : (isA ? 'rgba(52, 211, 153, 0.90)' : 'rgba(251, 113, 133, 0.90)');
                    ctx.lineWidth = 2.1;
                    ctx.beginPath();
                    ctx.moveTo(c0[0], c0[1]);
                    for (let pi = 0; pi < v.plan_pts.length; pi++) {
                        const wp = v.plan_pts[pi];
                        const wy = (wp.length >= 3) ? wp[2] : sampleY(wp[0], wp[1]);
                        const pr = project3D(wp[0], wy + 0.8, wp[1]);
                        if (pr) ctx.lineTo(pr[0], pr[1]);
                    }
                    ctx.stroke();
                }

                // 16-Ray 360° Hull Vision Fan
                if (opts.whiskers && v.rays_16) {
                    for (let ri = 0; ri < 16; ri++) {
                        const rItem = v.rays_16[ri];
                        const rLen = rItem[0];
                        const rCode = rItem[2];
                        const ang = (p.yaw || 0) + ri * (Math.PI / 8.0);
                        const ex = p.x + Math.sin(ang) * rLen;
                        const ez = p.z + Math.cos(ang) * rLen;
                        const ey = sampleY(ex, ez) + 0.9;
                        const p1 = project3D(ex, ey, ez);
                        if (!p1) continue;
                        ctx.strokeStyle = (rCode === 1 || rCode === 2) ? 'rgba(239, 68, 68, 0.88)' : (rCode === 3 ? 'rgba(249, 115, 22, 0.78)' : 'rgba(56, 189, 248, 0.28)');
                        ctx.lineWidth = (rCode !== 0) ? 1.5 : 0.9;
                        ctx.beginPath(); ctx.moveTo(c0[0], c0[1]); ctx.lineTo(p1[0], p1[1]); ctx.stroke();
                    }
                }

                // 3D Line of Sight (LOS) to Target
                if (opts.aim && v.target_id) {
                    const tgt = vehMap.get(v.target_id);
                    if (tgt && tgt.is_alive !== false) {
                        const tp = interpPose.get(tgt.id) || tgt;
                        const ty = (tp.y !== undefined) ? tp.y : sampleY(tp.x, tp.z);
                        const tPr = project3D(tp.x, ty + 1.45, tp.z);
                        if (tPr) {
                            ctx.strokeStyle = v.los_clear ? (v.los_window ? '#d946ef' : '#00e5ff') : 'rgba(249, 115, 22, 0.65)';
                            ctx.lineWidth = v.los_clear ? 1.9 : 1.2;
                            ctx.beginPath(); ctx.moveTo(c0[0], c0[1]); ctx.lineTo(tPr[0], tPr[1]); ctx.stroke();
                        }
                    }
                }

                // 3D Tank Hull + Turret Gun Barrel
                drawLowPolyModel([p.x, p.z, 1.7, 3.3, p.yaw || 0, vy, vy + 1.65, isP ? 2 : 0]);
                const aimYaw = (p.yaw || 0) + (p.tyaw || 0);
                const gunEnd = project3D(p.x + Math.sin(aimYaw) * 5.2, vy + 1.85, p.z + Math.cos(aimYaw) * 5.2);
                const gunStart = project3D(p.x, vy + 1.85, p.z);
                if (gunStart && gunEnd) {
                    ctx.strokeStyle = col;
                    ctx.lineWidth = 2.4;
                    ctx.beginPath(); ctx.moveTo(gunStart[0], gunStart[1]); ctx.lineTo(gunEnd[0], gunEnd[1]); ctx.stroke();
                }

                const lblPr = project3D(p.x, vy + 3.6, p.z);
                if (lblPr) {
                    ctx.font = '600 10.5px -apple-system, sans-serif';
                    ctx.fillStyle = col;
                    ctx.textAlign = 'center';
                    ctx.fillText((v.name || 'Bot') + ' [' + (v.mode || 'route') + ']', lblPr[0], lblPr[1]);
                }
            }

            requestAnimationFrame(render);
        }

        async function loadMapGeom(mapName) {
            try {
                const res = await fetch('/map_geom?map=' + encodeURIComponent(mapName));
                const data = await res.json();
                loadedMapName = data.map || mapName;
                bounds = data.bounds || [-500, -500, 500, 500];
                allObbs = data.buildings || [];
                terrainData = data.terrain || null;
                rubblePts = data.rubble_pts || [];
                pocketsData = data.pockets || [];
                routesData = data.routes || [];

                const feat = getMapFeat(loadedMapName);
                document.getElementById('map-name').innerText = loadedMapName + ' (' + feat.title + ')';
                document.getElementById('leg-c0').innerText = feat.c0;
                document.getElementById('leg-c3').innerText = feat.c3;
                document.getElementById('leg-c2').innerText = feat.c2;
                document.getElementById('leg-c1').innerText = feat.c1;

                document.getElementById('bld-count').innerText = allObbs.filter(o => o[7] === 0).length;
                document.getElementById('imp-destr-count').innerText = allObbs.filter(o => o[7] === 3).length;
                document.getElementById('pass-destr-count').innerText = allObbs.filter(o => o[7] === 1).length;
                document.getElementById('rubble-count').innerText = allObbs.filter(o => o[7] === 2).length + rubblePts.length;
                document.getElementById('win-count').innerText = pocketsData.length;

                mapImg = null;
                const img = new Image();
                img.onload = () => { mapImg = img; bakeUnderlayTexture(); };
                img.onerror = () => { bakeUnderlayTexture(); };
                img.src = '/map_tex?map=' + encodeURIComponent(loadedMapName);
                bakeUnderlayTexture();
                resetView();
            } catch (e) {}
        }

        async function pollState() {
            try {
                const res = await fetch('/state');
                const data = await res.json();
                if (data.map && data.map !== 'Standby' && data.map !== loadedMapName) {
                    await loadMapGeom(data.map);
                }
                vehicles = data.vehicles || [];
                vehMap.clear();
                for (let i = 0; i < vehicles.length; i++) vehMap.set(vehicles[i].id, vehicles[i]);
                document.getElementById('bot-count').innerText = vehicles.length;
                if (selectedVehicleId !== null && vehMap.has(selectedVehicleId)) {
                    updateInspector(vehMap.get(selectedVehicleId));
                }
            } catch (e) {}
            setTimeout(pollState, 90);
        }

        loadMapGeom('__MAP_NAME__').then(() => {
            requestAnimationFrame(render);
            pollState();
        });
    </script>
</body>
</html>
"""


_SUBENGINE_CHAT_OUTBOX = []


def queue_chat(vid, text='', common=False, event='?', target=None):
    """Queue an outgoing bot chat message or tactical radio callout from the subengine."""
    global _SUBENGINE_CHAT_OUTBOX
    try:
        _SUBENGINE_CHAT_OUTBOX.append({
            'vid': int(vid),
            'text': str(text) if text else '',
            'common': bool(common),
            'event': str(event),
            'target': target
        })
    except Exception:
        pass


def poll_chat():
    """Poll and flush all pending outgoing chat messages to the client."""
    global _SUBENGINE_CHAT_OUTBOX
    if not _SUBENGINE_CHAT_OUTBOX:
        return ()
    try:
        out = list(_SUBENGINE_CHAT_OUTBOX)
        _SUBENGINE_CHAT_OUTBOX = []
        return out
    except Exception:
        return ()


def clear_caches():
    """Flush subengine collision, LOS, and raycast caches between battles."""
    global _LOS_CACHE, _WHISKER_STATIC_CACHE, _FAST_OBB_CACHE, _MAP_TEX_CACHE, _SUBENGINE_CHAT_OUTBOX
    try:
        _SUBENGINE_CHAT_OUTBOX = []
    except Exception:
        pass
    try:
        if isinstance(_LOS_CACHE, dict):
            _LOS_CACHE.clear()
        if isinstance(_WHISKER_STATIC_CACHE, dict):
            _WHISKER_STATIC_CACHE.clear()
        if isinstance(_MAP_TEX_CACHE, dict):
            _MAP_TEX_CACHE.clear()
        _FAST_OBB_CACHE[0] = None
        _FAST_OBB_CACHE[1] = None
        _FAST_OBB_CACHE[2] = ()
    except Exception:
        pass
