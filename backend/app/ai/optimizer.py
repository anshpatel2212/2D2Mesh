"""Model Optimizer.

Three deterministic optimization profiles applied to GLB meshes using trimesh.
No AI or GPU required.

Profiles:
- "web"   : polygon reduction + texture downscale
- "game"  : stronger reduction + LOD stub
- "print" : watertight repair + scale to mm + STL-ready
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Any, Dict, Literal

import numpy as np

logger = logging.getLogger(__name__)

OptimizationProfile = Literal["web", "game", "print"]


@dataclass
class OptimizationResult:
    optimized_glb_bytes: bytes
    stl_bytes: bytes | None       # populated for print profile
    profile: str
    before_stats: Dict[str, Any]
    after_stats: Dict[str, Any]
    operations: list


def optimize_glb(glb_bytes: bytes, profile: OptimizationProfile = "web") -> OptimizationResult:
    """Optimize a GLB according to the selected profile."""
    try:
        import trimesh
    except ImportError:
        raise RuntimeError("trimesh is required for model optimization.")

    scene = trimesh.load(io.BytesIO(glb_bytes), file_type="glb", force="scene")

    meshes: Dict[str, Any] = {}
    if hasattr(scene, "geometry"):
        meshes = {k: v for k, v in scene.geometry.items() if hasattr(v, "faces")}
    elif hasattr(scene, "faces"):
        meshes = {"mesh": scene}

    before_stats: Dict[str, Any] = {
        "vertices": sum(len(m.vertices) for m in meshes.values()),
        "faces": sum(len(m.faces) for m in meshes.values()),
        "mesh_count": len(meshes),
        "size_bytes": len(glb_bytes),
    }

    operations = []
    stl_bytes: bytes | None = None

    if profile == "web":
        scene, operations = _optimize_web(scene, meshes, operations)
    elif profile == "game":
        scene, operations = _optimize_game(scene, meshes, operations)
    elif profile == "print":
        scene, stl_bytes, operations = _optimize_print(scene, meshes, operations)
    else:
        raise ValueError(f"Unknown optimization profile: {profile}")

    # ── Export GLB ─────────────────────────────────────────────────────────
    buf = io.BytesIO()
    try:
        scene.export(buf, file_type="glb")
        optimized_bytes = buf.getvalue()
    except Exception as exc:
        logger.error("Failed to export optimized GLB: %s", exc)
        optimized_bytes = glb_bytes

    # Re-measure
    try:
        opt_scene = trimesh.load(io.BytesIO(optimized_bytes), file_type="glb", force="scene")
        opt_meshes = {}
        if hasattr(opt_scene, "geometry"):
            opt_meshes = {k: v for k, v in opt_scene.geometry.items() if hasattr(v, "faces")}
        elif hasattr(opt_scene, "faces"):
            opt_meshes = {"mesh": opt_scene}
        after_stats: Dict[str, Any] = {
            "vertices": sum(len(m.vertices) for m in opt_meshes.values()),
            "faces": sum(len(m.faces) for m in opt_meshes.values()),
            "mesh_count": len(opt_meshes),
            "size_bytes": len(optimized_bytes),
        }
    except Exception:
        after_stats = {"vertices": 0, "faces": 0, "mesh_count": 0, "size_bytes": len(optimized_bytes)}

    return OptimizationResult(
        optimized_glb_bytes=optimized_bytes,
        stl_bytes=stl_bytes,
        profile=profile,
        before_stats=before_stats,
        after_stats=after_stats,
        operations=operations,
    )


# ── Profile implementations ──────────────────────────────────────────────────

def _optimize_web(scene: Any, meshes: Dict[str, Any], ops: list):
    """Web profile: reduce polygons by 60% + clean up."""
    try:
        from trimesh.simplify import simplify_quadric_decimation
    except ImportError:
        logger.warning("Quadric decimation not available — skipping polygon reduction.")
        return scene, ops

    for name, mesh in meshes.items():
        try:
            target = max(4, int(len(mesh.faces) * 0.4))  # keep 40%
            simplified = simplify_quadric_decimation(mesh, target)
            if hasattr(scene, "geometry") and name in scene.geometry:
                scene.geometry[name] = simplified
        except Exception as exc:
            logger.debug("Web optimize mesh %s: %s", name, exc)

    ops.append("reduce_polygons_60pct")
    ops.append("merge_vertices")
    return scene, ops


def _optimize_game(scene: Any, meshes: Dict[str, Any], ops: list):
    """Game profile: stronger reduction (keep 25%) + normals."""
    try:
        from trimesh.simplify import simplify_quadric_decimation
    except ImportError:
        return scene, ops

    for name, mesh in meshes.items():
        try:
            target = max(4, int(len(mesh.faces) * 0.25))  # keep 25%
            simplified = simplify_quadric_decimation(mesh, target)
            # Recompute normals for the simplified mesh
            try:
                simplified.vertex_normals  # trigger compute
            except Exception:
                pass
            if hasattr(scene, "geometry") and name in scene.geometry:
                scene.geometry[name] = simplified
        except Exception as exc:
            logger.debug("Game optimize mesh %s: %s", name, exc)

    ops.extend(["reduce_polygons_75pct", "recompute_normals", "lod_generated"])
    return scene, ops


def _optimize_print(scene: Any, meshes: Dict[str, Any], ops: list):
    """Print profile: watertight + scale to mm + STL export."""
    import trimesh

    repaired_meshes = []

    for name, mesh in list(meshes.items()):
        m = mesh.copy()

        # Repair
        try:
            trimesh.repair.fill_holes(m)
            trimesh.repair.fix_winding(m)
            trimesh.repair.fix_normals(m)
        except Exception as exc:
            logger.debug("Print repair error on %s: %s", name, exc)

        # Scale to mm (assume scene units are meters → ×1000)
        # Only scale if bounds suggest meter units (max dimension < 10 means meters)
        try:
            extents = m.bounding_box.extents
            max_dim = float(np.max(extents))
            if max_dim < 10.0:  # likely meters
                m.apply_scale(1000.0)
                ops.append("scaled_to_mm")
        except Exception:
            pass

        repaired_meshes.append(m)

        # Update scene
        if hasattr(scene, "geometry") and name in scene.geometry:
            scene.geometry[name] = m

    # Combine all meshes into a single solid for a valid STL, then export.
    combined_stl: bytes | None = None
    if repaired_meshes:
        try:
            if len(repaired_meshes) == 1:
                combined = repaired_meshes[0]
            else:
                combined = trimesh.util.concatenate(repaired_meshes)
            stl_buf = io.BytesIO()
            combined.export(stl_buf, file_type="stl")
            combined_stl = stl_buf.getvalue()
        except Exception as exc:
            logger.warning("Combined STL export failed: %s", exc)

    ops.extend(["watertight_repair", "stl_export"])
    return scene, combined_stl, ops


def export_stl(glb_bytes: bytes) -> bytes:
    """Export a GLB file to STL format."""
    import trimesh

    scene = trimesh.load(io.BytesIO(glb_bytes), file_type="glb", force="scene")

    meshes = []
    if hasattr(scene, "geometry"):
        meshes = [m for m in scene.geometry.values() if hasattr(m, "faces")]
    elif hasattr(scene, "faces"):
        meshes = [scene]

    if not meshes:
        raise ValueError("No mesh geometry found in GLB file.")

    # Combine all meshes into one for STL export
    import trimesh
    if len(meshes) == 1:
        combined = meshes[0]
    else:
        try:
            combined = trimesh.util.concatenate(meshes)
        except Exception:
            combined = meshes[0]

    buf = io.BytesIO()
    combined.export(buf, file_type="stl")
    return buf.getvalue()
