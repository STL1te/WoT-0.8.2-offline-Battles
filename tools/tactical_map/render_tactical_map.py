# -*- coding: utf-8 -*-
"""Visual Tactical Map Renderer for Offline Battles 2.0 (v12.0 Sub-Engine With Eyes).

Renders high-resolution tactical overlays from navgraphs/<map>.json showing:
- 4.0m Terrain Hillshade (heights_mm)
- 2D Building / Obstacle Footprints (links == 0)
- Water & Cliff Hazards (hazards bitmask 1=deep water, 2=cliff, 4=shallow ford)
- Prebaked Corner-Peek & Hull-Down Pockets (tactical_pockets)
- Canonical 3-Wing Pre-Oriented Spawn Formations (Team 1 & Team 2)
- All 6-8 Multi-Corridor Routes per Team with Hold Clash-Zone Anchors
"""
import os
import sys
import json
import math
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

ROUTE_PALETTE = [
    "#00E5FF", "#FFEA00", "#FF4081", "#76FF03",
    "#FF9100", "#E040FB", "#1DE9B6", "#536DFE",
]

def render_map(json_path, out_png_path):
    with open(json_path, "r") as f:
        j = json.load(f)

    map_name = j.get("map", os.path.basename(json_path)[:-5])
    w = int(j["width"])
    h = int(j["height"])
    ox, oz = float(j["origin"][0]), float(j["origin"][1])
    cs = float(j["cell_size"])
    extent = [ox, ox + w * cs, oz, oz + h * cs]

    hm = np.array([float(v if (v is not None and v > -30000) else 0) / 1000.0 for v in j["heights_mm"]], dtype=np.float32).reshape((h, w))
    links = np.array(j["links"], dtype=np.int32).reshape((h, w))
    haz = np.array(j["hazards"], dtype=np.int32).reshape((h, w))

    # Compute terrain hillshade
    gy, gx = np.gradient(hm, cs, cs)
    slope = np.pi / 2.0 - np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    az = np.radians(315.0)
    alt = np.radians(45.0)
    shaded = np.sin(alt) * np.sin(slope) + np.cos(alt) * np.cos(slope) * np.cos(az - aspect)
    shaded = np.clip((shaded + 1.0) * 0.5, 0.0, 1.0)

    # Normalize elevation for subtle hypsometric tint
    h_min, h_max = float(np.percentile(hm, 2)), float(np.percentile(hm, 98))
    h_norm = np.clip((hm - h_min) / max(1.0, h_max - h_min), 0.0, 1.0)

    rgb = np.zeros((h, w, 3), dtype=np.float32)
    # Base terrain olive-sand to highland ochre
    rgb[..., 0] = (0.20 + 0.32 * h_norm) * (0.55 + 0.65 * shaded)
    rgb[..., 1] = (0.25 + 0.28 * h_norm) * (0.55 + 0.65 * shaded)
    rgb[..., 2] = (0.18 + 0.16 * h_norm) * (0.55 + 0.65 * shaded)

    # Impassable buildings / rocks (links == 0 and not water/cliff)
    bldg_mask = (links == 0) & ((haz & 3) == 0)
    rgb[bldg_mask] = [0.16, 0.18, 0.22]

    # Cliff hazards (haz & 2)
    cliff_mask = (haz & 2) != 0
    rgb[cliff_mask] = [0.42, 0.24, 0.16]

    # Shallow fordable water (haz & 4)
    shallow_mask = (haz & 4) != 0
    rgb[shallow_mask] = [0.15, 0.48, 0.68]

    # Deep water hazard (haz & 1)
    water_mask = (haz & 1) != 0
    rgb[water_mask] = [0.07, 0.24, 0.48]

    # Wall border highlight (links > 0 and links != 255)
    edge_mask = (links == 0) & ((haz & 1) == 0)
    fig, ax = plt.subplots(figsize=(12, 12), dpi=160)
    fig.patch.set_facecolor("#0E1117")
    ax.set_facecolor("#0E1117")

    ax.imshow(rgb, origin="lower", extent=extent, interpolation="bilinear")
    ax.contour(
        np.linspace(extent[0], extent[1], w),
        np.linspace(extent[2], extent[3], h),
        edge_mask.astype(np.float32),
        levels=[0.5],
        colors=["#546E7A"],
        linewidths=0.55,
        alpha=0.75,
    )

    # Plot WoT 10x10 tactical grid (A1..K0)
    bx0, bz0, bx1, bz1 = extent[0], extent[2], extent[1], extent[3]
    for gx_line in np.linspace(bx0, bx1, 11):
        ax.axvline(gx_line, color="#FFFFFF", alpha=0.12, lw=0.6, ls="--")
    for gz_line in np.linspace(bz0, bz1, 11):
        ax.axhline(gz_line, color="#FFFFFF", alpha=0.12, lw=0.6, ls="--")

    # Plot prebaked tactical pockets (building corners & hull-down crests)
    pockets = j.get("tactical_pockets") or []
    if pockets:
        c_x = [p[0] for p in pockets if len(p) >= 9 and p[8] == 1]
        c_z = [p[2] for p in pockets if len(p) >= 9 and p[8] == 1]
        h_x = [p[0] for p in pockets if len(p) >= 9 and p[8] == 2]
        h_z = [p[2] for p in pockets if len(p) >= 9 and p[8] == 2]
        if c_x:
            ax.scatter(c_x, c_z, s=10, c="#FFAB00", alpha=0.55, marker="s", zorder=3, label="Building Corner Peek Slots (%d)" % len(c_x))
        if h_x:
            ax.scatter(h_x, h_z, s=10, c="#80D8FF", alpha=0.55, marker="^", zorder=3, label="Terrain Hull-Down Pockets (%d)" % len(h_x))

    # Plot Team 1 routes (solid lines) & Team 2 routes (dashed subtle overlay)
    t1_routes = (j.get("routes") or {}).get("1") or []
    route_handles = []
    for r_i, r in enumerate(t1_routes):
        rid = r.get("id", "route_%d" % r_i)
        cap = r.get("capacity", 4)
        wps = r.get("waypoints") or []
        if not wps:
            continue
        col = ROUTE_PALETTE[r_i % len(ROUTE_PALETTE)]
        xs = [float(p[0]) for p in wps]
        zs = [float(p[1]) for p in wps]
        lw = 2.8 if rid != "lake_road" else 2.2
        ls = "-" if rid != "lake_road" else "-."
        ax.plot(xs, zs, color="#000000", lw=lw + 1.6, alpha=0.65, zorder=4)
        ax.plot(xs, zs, color=col, lw=lw, ls=ls, alpha=0.95, zorder=5)
        # Hold clash waypoints
        h_xs = [float(p[0]) for p in wps if len(p) > 2 and p[2]]
        h_zs = [float(p[1]) for p in wps if len(p) > 2 and p[2]]
        if h_xs:
            ax.scatter(h_xs, h_zs, s=42, facecolors="none", edgecolors=col, linewidths=1.5, zorder=6)
        # Label route at midpoint
        mid_p = wps[len(wps) // 2]
        ax.text(
            float(mid_p[0]), float(mid_p[1]),
            "%s (cap=%s)" % (rid, cap),
            color="#FFFFFF", fontsize=8.5, fontweight="bold",
            ha="center", va="center", zorder=8,
            bbox=dict(boxstyle="round,pad=0.22", facecolor="#11161D", edgecolor=col, alpha=0.88, lw=1.2)
        )
        route_handles.append(Line2D([0], [0], color=col, lw=2.8, ls=ls, label="Corridor: %s (cap=%s)" % (rid, cap)))

    # Plot 3-Wing Spawn Formations (Team 1 = Green, Team 2 = Red) with heading arrows
    formations = j.get("spawn_formations") or {}
    for tm_k, tm_col, tm_lbl in [("1", "#00E676", "Team 1 Spawn (15 slots)"), ("2", "#FF5252", "Team 2 Spawn (15 slots)")]:
        poses = formations.get(tm_k) or []
        if poses:
            s_x = [float(p[0]) for p in poses]
            s_z = [float(p[2]) for p in poses]
            s_u = [math.sin(float(p[3])) * 14.0 for p in poses]
            s_v = [math.cos(float(p[3])) * 14.0 for p in poses]
            ax.scatter(s_x, s_z, s=58, c=tm_col, edgecolors="#000000", linewidths=1.0, zorder=7)
            ax.quiver(s_x, s_z, s_u, s_v, angles="xy", scale_units="xy", scale=1.0, color=tm_col, width=0.0032, zorder=7)

    # Plot Bases
    bases = j.get("bases")
    if isinstance(bases, dict):
        b_list = [( "1", bases.get("1"), "#00E676"), ("2", bases.get("2"), "#FF5252")]
    elif isinstance(bases, list) and len(bases) >= 2:
        b_list = [("1", bases[0], "#00E676"), ("2", bases[1], "#FF5252")]
    else:
        b_list = []
    for b_id, b_pt, b_col in b_list:
        if b_pt and len(b_pt) >= 2:
            ax.scatter([float(b_pt[0])], [float(b_pt[1])], s=260, c=b_col, marker="*", edgecolors="#FFFFFF", linewidths=1.5, zorder=9)

    ax.set_xlim(extent[0], extent[1])
    ax.set_ylim(extent[2], extent[3])
    ax.set_title(
        "WoT 0.8.2 Offline Battles 2.0 (v12.0 Sub-Engine With Eyes) — %s\n%d Corridors/Team | %d Corner-Peek & Hull-Down Pockets | 4.0m Navgraph (%dx%d)" % (
            map_name, len(t1_routes), len(pockets), w, h),
        color="#ECEFF1", fontsize=12.5, fontweight="bold", pad=12
    )
    ax.tick_params(colors="#B0BEC5", labelsize=9)
    for sp in ax.spines.values():
        sp.set_edgecolor("#37474F")

    extra_handles = [
        Patch(facecolor="#292E38", edgecolor="#546E7A", label="Impassable Buildings / Rocks (links=0)"),
        Patch(facecolor="#123D7A", edgecolor="#29B6F6", label="Deep Water Hazard (hazards & 1)"),
        Patch(facecolor="#6B3D29", edgecolor="#FF8A65", label="Steep Cliff Hazard (hazards & 2)"),
        Line2D([0], [0], marker="s", color="w", markerfacecolor="#FFAB00", markersize=7, ls="None", label="Corner-Peek Slots (Peek-a-Boo on Reload)"),
        Line2D([0], [0], marker="^", color="w", markerfacecolor="#80D8FF", markersize=7, ls="None", label="Hull-Down Terrain Pockets"),
        Line2D([0], [0], marker="o", color="w", markerfacecolor="#00E676", markeredgecolor="#000", markersize=8, ls="None", label="Team 1 Canonical 3-Wing Spawns"),
        Line2D([0], [0], marker="o", color="w", markerfacecolor="#FF5252", markeredgecolor="#000", markersize=8, ls="None", label="Team 2 Canonical 3-Wing Spawns"),
    ]
    ax.legend(
        handles=route_handles + extra_handles,
        loc="upper right",
        fontsize=8.0,
        facecolor="#11161D",
        edgecolor="#455A64",
        labelcolor="#ECEFF1",
        framealpha=0.92,
    )
    plt.tight_layout()
    plt.savefig(out_png_path, dpi=160, facecolor=fig.get_facecolor())
    plt.close(fig)
    print("Rendered tactical map:", out_png_path)

if __name__ == "__main__":
    # python render_tactical_map.py [map ...]   -> build/tactical_maps/<map>_tactical_v12.png
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import repo_env
    nav_dir = os.path.join(repo_env.MOD_DIR, "navgraphs")
    out_dir = os.path.join(repo_env.ROOT, "build", "tactical_maps")
    os.makedirs(out_dir, exist_ok=True)
    for m in (sys.argv[1:] or ("04_himmelsdorf", "07_lakeville")):
        render_map(
            os.path.join(nav_dir, m + ".json"),
            os.path.join(out_dir, m + "_tactical_v12.png")
        )
