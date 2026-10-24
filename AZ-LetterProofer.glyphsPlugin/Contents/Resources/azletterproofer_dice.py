"""3D glyph dice for the AZ Letter Proofer window.

A rounded dodecahedron (d12) rendered with SceneKit, one glyph per face,
tumbling. Faces are PBR materials: a solid body colour over the bundled
AmbientCG plastic roughness map (Resources/assets/). Each glyph is engraved
into the surface (diffuse) and glows from the pit (emission); the focus letter
glows brightest.

The mesh math is pure Python and unit-tested outside Glyphs.
"""

from __future__ import annotations

import contextlib
import functools
import math
import random
import struct
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import objc
from AppKit import (
    NSAffineTransform,
    NSAppearanceNameAqua,
    NSAppearanceNameDarkAqua,
    NSApplication,
    NSBezierPath,
    NSCIImageRep,
    NSColor,
    NSColorSpace,
    NSCompositingOperationDestinationIn,
    NSCompositingOperationMultiply,
    NSCompositingOperationSourceOver,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSGradient,
    NSImage,
    NSMakeRect,
    NSRectFill,
    NSString,
)
from Foundation import (
    NSData,
    NSTimer,
)

try:  # optional frameworks: without them only the dice is off
    import Quartz
    import SceneKit as SK
except ImportError:
    Quartz = SK = None

PHI = (1 + 5**0.5) / 2
SCALE = 1 / math.sqrt(3)  # circumradius -> 1
DICE_ROUNDING = 0.981  # clamp-sphere radius: rounds edges and corners (smaller = rounder)
NORMAL_BAND = 0.069  # radius band where face normals blend into radial (soft seam)
FACE_SUBDIVISIONS = 14  # pentagon subdivision rings

TEXTURE_SIZE = 128  # banner shows ~150 px; 1024 cost ~20 s per pass at open
TEXTURE_SCALE = 3.0  # roughness-map zoom (bigger features)
UV_SPREAD = 0.46  # pentagon circumradius in texture units (margin against bleed)
# Body colour; the colour well rewrites it (the diceColor pref, #9A9A9A by default).
SURFACE_ALBEDO: tuple[float, float, float] = (154 / 255, 154 / 255, 154 / 255)
# Card and canvas colours, as sRGB components.
CARD_BACKGROUND = (40 / 255, 40 / 255, 40 / 255)  # dark mode
CARD_BACKGROUND_LIGHT = (228 / 255, 228 / 255, 228 / 255)  # light mode, the suite's #E4E4E4
DICE_CANVAS_BACKGROUND = (30 / 255, 30 / 255, 30 / 255)  # a step darker: a recessed well
DICE_CANVAS_BACKGROUND_LIGHT = (1.0, 1.0, 1.0)
GLOW_COLOR = (0.092, 0.091, 0.206)  # plain letters, deep indigo
HIGHLIGHT_GLOW_COLOR = (0.135, 0.086, 0.483)  # focus letter, violet
AURA_NODE_NAME = "AZLetterProoferHighlightAura"
# Crack light: a dim omni above the focus face, spilling its glow onto the plastic.
CRACK_LUMENS = 10.0
CRACK_HEIGHT = 1.902  # height above the face; higher = broader
CRACK_REACH = 3.443  # attenuation end
CRACK_SOFTNESS = 0.382  # attenuation start, as a fraction of the reach
# Focus and idle glow (emission_image with organic=True).
GLOW_MOTTLE = 0.036  # contrast of the hot/cool patches
GLOW_MOTTLE_SCALE = 0.5  # patch size, as a fraction of the texture
GLOW_POOL = 0.319  # 1 = bright floor fading up the walls, 0 = flat fill
IDLE_GLOW_COLOR = (0.194, 0.0, 1.0)  # nothing to proof: every letter glows this
IDLE_BOOST = 12.238
# Letter-change morph: blur both silhouettes, cross-dissolve, re-threshold.
MORPH_FRAMES = 20  # ~MORPH_SECONDS * 60
MORPH_SECONDS = 0.32
MORPH_BLUR = 0.02  # blur radius, as a fraction of the texture
MORPH_SIZE = 64  # px: keeps the off-main build ~0.3 s; the soft glow hides it
# Feature morph: the other faces morph to the Feature's glyphs in waves.
FEATURE_MORPH_SECONDS = 0.558  # per face
FEATURE_MORPH_BATCH = 3  # faces per wave
FEATURE_MORPH_STAGGER = 0.499  # seconds between waves
FEATURE_MORPH_INWAVE = 0.076  # seconds between faces of one wave
ENGRAVE_WALL_STOP = 0.16  # where the dark top wall ends; lower = deeper pit
ENGRAVE_FLOOR_STOP = 0.84  # where the lit floor begins
ENGRAVE_FLOOR_COLOR = (0.26, 0.12, 0.025, 1.0)
ENGRAVE_WALL_COLOR = (0.006, 0.005, 0.004, 1.0)
HIGHLIGHT_BOOST = 30.0  # focus letter
NON_FOCUS_BOOST = 0.998  # other letters stay at the dim base intensity
EMISSION_INTENSITY = 1.249
LETTER_SCALE = 0.35  # glyph size, as a fraction of the face texture
ROUGHNESS_INTENSITY = 1.746  # <1 = glossier

# Lights. The directional key only shapes the diffuse; specular comes from the
# environment's softbox panels.
KEY_INTENSITY = 600.0
KEY_COLOR = (1.0, 1.0, 1.0)
FILL_INTENSITY = 400.0
FILL_COLOR = (0.55, 0.72, 1.0)  # cool blue
AMBIENT_WHITE = 0.337
KEY_ELEVATION = -1.571  # radians, negative looks down
KEY_AZIMUTH = -1.92
FILL_ELEVATION = -1.571
FILL_AZIMUTH = -2.038
# Environment softbox panels (specular catchlights), brightness 0..1.
ENV_BASE = 0.603  # surround
ENV_MAIN = 0.722  # main softbox, upper left
ENV_SIDE = 0.925  # side strip
ENV_BOUNCE = 1.0  # bounce card below
ENV_INTENSITY = 3.0
# Bloom and motion blur (camera post).
BLOOM_INTENSITY = 0.18
BLOOM_THRESHOLD = 0.15
BLOOM_RADIUS = 4.0
MOTION_BLUR = 1.0  # while two-finger spinning; 0 at rest
MOTION_BLUR_DECAY = 0.18  # seconds after the last scroll event
# Rectangular area light: its reflection is a soft rectangle, not a dot.
SOFTBOX_INTENSITY = 3000.0
SOFTBOX_WIDTH = 3.547
SOFTBOX_HEIGHT = 8.0
SOFTBOX_POSITION = (-2.6, 3.0, 4.2)  # upper-left front, aimed at the dice
SOFTBOX_COLOR = (1.0, 1.0, 1.0)

# Orthographic camera.
DICE_ORTHO_SCALE = 1.208  # bigger = smaller dice
CAMERA_DISTANCE = 5.0
CAMERA_TILT = -0.15  # gentle look-down so the upper faces show
ZOOM_RANGE = (0.5, 2.5)  # orthographic-scale clamp for Option-scroll / pinch
OPTION_FLAG = 1 << 19  # NSEventModifierFlagOption
DICE_ROTATE_SENSITIVITY = 0.012  # radians per two-finger scroll point
IDLE_TUMBLE_KEY = "AZLetterProoferIdleTumble"
FOCUS_FRONT_SECONDS = 0.45  # ease when a letter change turns face 0 to the front

ASSETS_DIR = Path(__file__).parent / "assets"

Vec3 = tuple[float, float, float]


# --- Vector helpers -----------------------------------------------------------


def _dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _add(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _scaled(v: Vec3, s: float) -> Vec3:
    return (v[0] * s, v[1] * s, v[2] * s)


def _cross(a: Vec3, b: Vec3) -> Vec3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _normalized(v: Vec3) -> Vec3:
    mag = math.sqrt(_dot(v, v))
    return (v[0] / mag, v[1] / mag, v[2] / mag)


def _blend(a: Vec3, b: Vec3, t: float) -> Vec3:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)


# Quaternions (x, y, z, w), only to compose two axis-angle rotations in
# _focus_front_rotation. SceneKit's setRotation_ takes (x, y, z, angle).
def _axisangle_to_quat(r: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    half = r[3] / 2.0
    s = math.sin(half)
    return (r[0] * s, r[1] * s, r[2] * s, math.cos(half))


def _quat_mul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def _quat_to_axisangle(q) -> tuple[float, float, float, float]:
    x, y, z, w = q
    w = max(-1.0, min(1.0, w))
    s = math.sqrt(1.0 - w * w)
    if s < 1e-9:  # ~no rotation: any axis, zero angle
        return (0.0, 1.0, 0.0, 0.0)
    return (x / s, y / s, z / s, 2.0 * math.acos(w))


# --- Dodecahedron ---------------------------------------------------------------


def _raw_vertices() -> list[Vec3]:
    """The 20 vertices of a regular dodecahedron (circumradius sqrt(3))."""
    inv = 1 / PHI
    verts: list[Vec3] = [(x, y, z) for x in (-1.0, 1.0) for y in (-1.0, 1.0) for z in (-1.0, 1.0)]
    for a in (-inv, inv):
        for b in (-PHI, PHI):
            verts.extend([(0.0, a, b), (a, b, 0.0), (b, 0.0, a)])
    return verts


def _face_directions() -> list[Vec3]:
    """The 12 face-center directions: cyclic permutations of (0, ±PHI, ±1)."""
    dirs: list[Vec3] = []
    for a in (-PHI, PHI):
        for b in (-1.0, 1.0):
            dirs.extend([(0.0, a, b), (a, b, 0.0), (b, 0.0, a)])
    return dirs


@dataclass(frozen=True)
class _Face:
    normal: Vec3
    indices: tuple[int, ...]  # 5 vertex indices, CCW outward
    center: Vec3
    u_axis: Vec3
    v_axis: Vec3


def _faces(verts: list[Vec3]) -> list[_Face]:
    """Twelve pentagon faces with vertices ordered CCW and a UV basis."""
    faces: list[_Face] = []
    for direction in _face_directions():
        normal = _normalized(direction)
        ring = sorted(range(len(verts)), key=lambda i, n=normal: -_dot(verts[i], n))[:5]

        up: Vec3 = (0.0, 1.0, 0.0) if abs(normal[1]) < 0.95 else (0.0, 0.0, 1.0)
        v_axis = _normalized(_sub(up, _scaled(normal, _dot(up, normal))))
        u_axis = _cross(v_axis, normal)  # (u, v, n) right-handed: u x v = n

        center = _scaled(
            (
                sum(verts[i][0] for i in ring),
                sum(verts[i][1] for i in ring),
                sum(verts[i][2] for i in ring),
            ),
            1 / 5,
        )

        def angle(index: int, c=center, ua=u_axis, va=v_axis) -> float:
            offset = _sub(verts[index], c)
            return math.atan2(_dot(offset, va), _dot(offset, ua))

        ring.sort(key=angle)
        winding = _cross(_sub(verts[ring[1]], verts[ring[0]]), _sub(verts[ring[2]], verts[ring[0]]))
        if _dot(winding, normal) < 0:
            ring.reverse()

        faces.append(_Face(normal, tuple(ring), center, u_axis, v_axis))
    return faces


@dataclass(frozen=True)
class RoundedMesh:
    """A rounded dodecahedron: 12 subdivided faces, one triangle group each."""

    positions: tuple[Vec3, ...]
    normals: tuple[Vec3, ...]
    uvs: tuple[tuple[float, float], ...]
    face_triangles: tuple[tuple[tuple[int, int, int], ...], ...]  # 12 groups


def _smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


@functools.cache
def rounded_dodecahedron_mesh() -> RoundedMesh:
    """The dodecahedron intersected with a sphere: a moulded, rounded die.

    Each pentagon is subdivided into rings; any vertex farther from the centre
    than DICE_ROUNDING is pulled onto that sphere, so face centres stay flat and
    edges and corners turn spherical. Adjacent faces clamp the same boundary
    points, so the surface is watertight; normals blend from the face normal to
    radial across NORMAL_BAND. Built once.
    """
    verts = _raw_vertices()
    positions: list[Vec3] = []
    normals: list[Vec3] = []
    uvs: list[tuple[float, float]] = []
    face_triangles: list[tuple[tuple[int, int, int], ...]] = []

    for face in _faces(verts):
        corners = [_scaled(verts[i], SCALE) for i in face.indices]
        center = _scaled(face.center, SCALE)
        # UV basis: the pentagon corners land UV_SPREAD from the centre.
        extent = max(
            max(abs(_dot(_sub(c, center), face.u_axis)), abs(_dot(_sub(c, center), face.v_axis)))
            for c in corners
        )

        def add_point(
            point: Vec3,
            center=center,
            extent=extent,
            face_normal=face.normal,
            u_axis=face.u_axis,
            v_axis=face.v_axis,
        ) -> int:
            radius = math.sqrt(_dot(point, point))
            final = _scaled(point, DICE_ROUNDING / radius) if radius > DICE_ROUNDING else point
            blend_t = _smoothstep((radius - (DICE_ROUNDING - NORMAL_BAND)) / NORMAL_BAND)
            normal = _normalized(_blend(face_normal, _normalized(final), blend_t))
            offset = _sub(point, center)
            u = 0.5 + UV_SPREAD * _dot(offset, u_axis) / extent
            v = 0.5 - UV_SPREAD * _dot(offset, v_axis) / extent
            positions.append(final)
            normals.append(normal)
            uvs.append((u, v))
            return len(positions) - 1

        # Ring r holds 5 * r points along the shrunken pentagon.
        rings: list[list[int]] = [[add_point(center)]]
        for ring_n in range(1, FACE_SUBDIVISIONS + 1):
            scale_factor = ring_n / FACE_SUBDIVISIONS
            ring: list[int] = []
            for sector in range(5):
                corner_a = corners[sector]
                corner_b = corners[(sector + 1) % 5]
                for step in range(ring_n):
                    boundary = _blend(corner_a, corner_b, step / ring_n)
                    ring.append(add_point(_blend(center, boundary, scale_factor)))
            rings.append(ring)

        triangles: list[tuple[int, int, int]] = []

        def add_triangle(a: int, b: int, c: int, triangles=triangles) -> None:
            edge1 = _sub(positions[b], positions[a])
            edge2 = _sub(positions[c], positions[a])
            outward = _add(_add(positions[a], positions[b]), positions[c])
            if _dot(_cross(edge1, edge2), outward) < 0:
                a, c = c, a
            triangles.append((a, b, c))

        for ring_n in range(1, FACE_SUBDIVISIONS + 1):
            inner, outer = rings[ring_n - 1], rings[ring_n]
            for sector in range(5):
                for step in range(ring_n):
                    out_a = outer[(sector * ring_n + step) % len(outer)]
                    out_b = outer[(sector * ring_n + step + 1) % len(outer)]
                    if ring_n == 1:
                        add_triangle(inner[0], out_a, out_b)
                        continue
                    in_a = inner[(sector * (ring_n - 1) + step) % len(inner)]
                    add_triangle(in_a, out_a, out_b)
                    if step < ring_n - 1:
                        in_b = inner[(sector * (ring_n - 1) + step + 1) % len(inner)]
                        add_triangle(in_a, out_b, in_b)

        face_triangles.append(tuple(triangles))

    return RoundedMesh(tuple(positions), tuple(normals), tuple(uvs), tuple(face_triangles))


# --- Face textures ------------------------------------------------------------


@functools.cache
def _load_map(filename: str) -> object | None:
    """A bundled assets/ image, or None if missing."""
    path = ASSETS_DIR / filename
    return NSImage.alloc().initWithContentsOfFile_(str(path)) if path.exists() else None


def make_background(size: int = TEXTURE_SIZE) -> object:
    """The glyph-less face: SURFACE_ALBEDO only. No painted sheen — it would
    turn with the face; every highlight comes from the scene lights."""
    image = NSImage.alloc().initWithSize_((size, size))
    image.lockFocus()
    NSColor.colorWithCalibratedRed_green_blue_alpha_(*SURFACE_ALBEDO, 1.0).set()
    NSRectFill(NSMakeRect(0, 0, size, size))
    image.unlockFocus()
    return image


# A "stamp" draws the glyph at a pixel offset in a given colour into the focused
# image, so the engraving and the glow share identical glyph geometry.


def text_stamp(text: str, size: int):
    font = NSFont.boldSystemFontOfSize_(size * LETTER_SCALE)
    string = NSString.stringWithString_(text)
    metrics = string.sizeWithAttributes_({NSFontAttributeName: font})
    origin_x = (size - metrics.width) / 2
    origin_y = (size - metrics.height) / 2

    def stamp(dx: float, dy: float, color: object) -> None:
        string.drawAtPoint_withAttributes_(
            (origin_x + dx, origin_y + dy),
            {NSFontAttributeName: font, NSForegroundColorAttributeName: color},
        )

    return stamp


def _bezier_transformed(
    path: object, bounds: tuple[float, float, float, float], upm: float, size: int
) -> object:
    """A copy of the glyph path scaled and centred into the size×size texture.

    One em-relative scale for every glyph, so cap-height and x-height letters
    keep their true sizes; the bbox only clamps a glyph that would spill out."""
    box_x, box_y, box_width, box_height = bounds
    em_scale = size * LETTER_SCALE * 1.6 / max(upm, 1)
    fit_cap = size * 0.92 / max(box_width, box_height, 1.0)
    glyph_scale = min(em_scale, fit_cap)
    transform = NSAffineTransform.transform()
    transform.translateXBy_yBy_(
        size / 2 - glyph_scale * (box_x + box_width / 2),
        size / 2 - glyph_scale * (box_y + box_height / 2),
    )
    transform.scaleXBy_yBy_(glyph_scale, glyph_scale)
    base = path.copy()
    base.transformUsingAffineTransform_(transform)
    return base


def bezier_stamp(path: object, bounds: tuple[float, float, float, float], upm: float, size: int):
    base = _bezier_transformed(path, bounds, upm, size)

    def stamp(dx: float, dy: float, color: object) -> None:
        color.set()
        shifted = base.copy()
        if dx or dy:
            offset = NSAffineTransform.transform()
            offset.translateXBy_yBy_(dx, dy)
            shifted.transformUsingAffineTransform_(offset)
        shifted.fill()

    return stamp


def _masked_gradient(
    stamp,
    size: int,
    bottom: tuple[float, float, float, float],
    top: tuple[float, float, float, float],
    stops: tuple[float, float, float, float],
) -> object:
    """The glyph filled with a bottom-to-top gradient, transparent elsewhere."""
    rect = NSMakeRect(0, 0, size, size)
    color = NSColor.colorWithCalibratedRed_green_blue_alpha_

    mask = NSImage.alloc().initWithSize_((size, size))
    mask.lockFocus()
    stamp(0.0, 0.0, NSColor.blackColor())
    mask.unlockFocus()

    image = NSImage.alloc().initWithSize_((size, size))
    image.lockFocus()
    NSGradient.alloc().initWithColors_atLocations_colorSpace_(
        [color(*bottom), color(*bottom), color(*top), color(*top)],
        stops,  # the extremes hold outside the glyph's span
        NSColorSpace.deviceRGBColorSpace(),
    ).drawInRect_angle_(rect, 90.0)
    mask.drawInRect_fromRect_operation_fraction_(
        rect, ((0, 0), (0, 0)), NSCompositingOperationDestinationIn, 1.0
    )
    image.unlockFocus()
    return image


def _organic_mottle(size: int, strength: float, scale: float) -> object:
    """A white field freckled with soft dark patches: multiplied onto the glow,
    it pools the light unevenly, like molten metal."""
    image = NSImage.alloc().initWithSize_((size, size))
    image.lockFocus()
    NSColor.whiteColor().set()
    NSRectFill(NSMakeRect(0, 0, size, size))
    feature = max(8.0, size * scale)
    count = max(6, int((size / feature) ** 2 * 2.2))
    space = NSColorSpace.deviceRGBColorSpace()
    for _ in range(count):
        radius = feature * random.uniform(0.5, 1.4)
        center = (random.uniform(0, size), random.uniform(0, size))
        dark = 1.0 - strength * random.uniform(0.25, 1.0)
        gradient = NSGradient.alloc().initWithColors_atLocations_colorSpace_(
            [
                NSColor.colorWithCalibratedWhite_alpha_(dark, 1.0),
                NSColor.colorWithCalibratedWhite_alpha_(1.0, 1.0),
            ],
            (0.0, 1.0),
            space,
        )
        gradient.drawFromCenter_radius_toCenter_radius_options_(center, 0.0, center, radius, 0)
    image.unlockFocus()
    return image


def emission_image(
    stamp,
    size: int = TEXTURE_SIZE,
    color: tuple[float, float, float] | None = None,
    organic: bool = False,
) -> object:
    """Glow mask for a stamp: the glyph filled with `color` on black.

    organic=True (focus and idle letters) pools the light toward the pit floor
    (GLOW_POOL) and mottles it (GLOW_MOTTLE); otherwise one flat tone."""
    image = NSImage.alloc().initWithSize_((size, size))
    image.lockFocus()
    NSColor.blackColor().set()
    NSRectFill(NSMakeRect(0, 0, size, size))
    floor = color or GLOW_COLOR
    top = floor
    if organic:
        dim_top = (floor[0] * 0.30, floor[1] * 0.22, floor[2] * 0.10)
        flat = 1.0 - max(0.0, min(1.0, GLOW_POOL))
        top = tuple(dim_top[i] + (floor[i] - dim_top[i]) * flat for i in range(3))
    glow = _masked_gradient(
        stamp,
        size,
        bottom=(floor[0], floor[1], floor[2], 1.0),
        top=(top[0], top[1], top[2], 1.0),
        stops=(0.0, 0.26, 0.70, 1.0),
    )
    glow.drawInRect_fromRect_operation_fraction_(
        NSMakeRect(0, 0, size, size),
        ((0, 0), (0, 0)),
        NSCompositingOperationSourceOver,
        1.0,
    )
    if organic and GLOW_MOTTLE > 0:
        mottle = _organic_mottle(size, GLOW_MOTTLE, GLOW_MOTTLE_SCALE)
        mottle.drawInRect_fromRect_operation_fraction_(
            NSMakeRect(0, 0, size, size),
            ((0, 0), (0, 0)),
            NSCompositingOperationMultiply,
            1.0,
        )
    image.unlockFocus()
    return image


def face_image(stamp, background: object, size: int = TEXTURE_SIZE) -> object:
    """Diffuse map: the glyph engraved into the background as a pit — a
    near-black top wall falling to a faintly lit floor, all inside the
    silhouette (no outline around the letter)."""
    image = background.copy()
    image.lockFocus()
    pit = _masked_gradient(
        stamp,
        size,
        bottom=ENGRAVE_FLOOR_COLOR,
        top=ENGRAVE_WALL_COLOR,
        stops=(0.0, ENGRAVE_WALL_STOP, ENGRAVE_FLOOR_STOP, 1.0),
    )
    pit.drawInRect_fromRect_operation_fraction_(
        NSMakeRect(0, 0, size, size),
        ((0, 0), (0, 0)),
        NSCompositingOperationSourceOver,
        1.0,
    )
    image.unlockFocus()
    return image


# --- Letter-change morph --------------------------------------------------------


def _stamp_silhouette(stamp, size: int) -> object:
    """White-on-black opaque rasterization of a stamp (morph input)."""
    image = NSImage.alloc().initWithSize_((size, size))
    image.lockFocus()
    NSColor.blackColor().set()
    NSRectFill(NSMakeRect(0, 0, size, size))
    stamp(0.0, 0.0, NSColor.whiteColor())
    image.unlockFocus()
    return image


def _image_stamp(mask_image: object, size: int):
    """A stamp backed by an alpha-mask image, so morph frames go through the
    same emission pipeline as real glyph stamps."""

    def stamp(dx: float, dy: float, color: object) -> None:
        rect = NSMakeRect(0, 0, size, size)
        tinted = NSImage.alloc().initWithSize_((size, size))
        tinted.lockFocus()
        color.set()
        NSRectFill(rect)
        mask_image.drawInRect_fromRect_operation_fraction_(
            rect, ((0, 0), (0, 0)), NSCompositingOperationDestinationIn, 1.0
        )
        tinted.unlockFocus()
        tinted.drawInRect_fromRect_operation_fraction_(
            NSMakeRect(dx, dy, size, size),
            ((0, 0), (0, 0)),
            NSCompositingOperationSourceOver,
            1.0,
        )

    return stamp


def morph_silhouettes(
    image_a: object, image_b: object, size: int, frames: int = MORPH_FRAMES
) -> list:
    """Intermediate silhouettes between two white-on-black letter images, as
    alpha masks: the contour glides between the two letterforms instead of
    fading."""

    def as_ci(image):
        return Quartz.CIImage.imageWithData_(image.TIFFRepresentation())

    def blurred(ci_image):
        blur = Quartz.CIFilter.filterWithName_("CIGaussianBlur")
        blur.setValue_forKey_(ci_image, "inputImage")
        blur.setValue_forKey_(size * MORPH_BLUR, "inputRadius")
        return blur.valueForKey_("outputImage")

    base_a = as_ci(image_a)
    soft_a = blurred(base_a)
    soft_b = blurred(as_ci(image_b))
    # Crop in the bitmap's own pixels: on Retina the silhouettes are baked at 2x.
    rect = base_a.extent()
    results = []
    for index in range(1, frames + 1):
        dissolve = Quartz.CIFilter.filterWithName_("CIDissolveTransition")
        dissolve.setValue_forKey_(soft_a, "inputImage")
        dissolve.setValue_forKey_(soft_b, "inputTargetImage")
        dissolve.setValue_forKey_(index / (frames + 1), "inputTime")
        threshold = Quartz.CIFilter.filterWithName_("CIColorThreshold")
        threshold.setValue_forKey_(dissolve.valueForKey_("outputImage"), "inputImage")
        threshold.setValue_forKey_(0.5, "inputThreshold")
        to_alpha = Quartz.CIFilter.filterWithName_("CIMaskToAlpha")
        to_alpha.setValue_forKey_(threshold.valueForKey_("outputImage"), "inputImage")
        output = to_alpha.valueForKey_("outputImage").imageByCroppingToRect_(rect)
        frame = NSImage.alloc().initWithSize_((size, size))
        frame.addRepresentation_(NSCIImageRep.imageRepWithCIImage_(output))
        results.append(frame)
    return results


def morph_frames(
    stamp_a,
    stamp_b,
    color: tuple[float, float, float],
    frames: int = MORPH_FRAMES,
    size: int = MORPH_SIZE,
    color_to: tuple[float, float, float] | None = None,
) -> list:
    """Glow-only morph frames (emission maps; the engraving swaps once the
    morph lands). color_to shifts the glow across the frames."""
    silhouettes = morph_silhouettes(
        _stamp_silhouette(stamp_a, size), _stamp_silhouette(stamp_b, size), size, frames
    )
    total = len(silhouettes)
    results = []
    for index, silhouette in enumerate(silhouettes):
        frame_color = (
            color if color_to is None else _blend(color, color_to, (index + 1) / (total + 1))
        )
        results.append(emission_image(_image_stamp(silhouette, size), size, color=frame_color))
    return results


# --- SceneKit assembly ------------------------------------------------------------


def _geometry_source(vectors: tuple, semantic: object, components: int, count: int) -> object:

    flat = [component for vector in vectors for component in vector]
    data = struct.pack(f"<{len(flat)}f", *flat)
    return SK.SCNGeometrySource.geometrySourceWithData_semantic_vectorCount_floatComponents_componentsPerVector_bytesPerComponent_dataOffset_dataStride_(  # noqa: E501
        NSData.dataWithBytes_length_(data, len(data)),
        semantic,
        count,
        True,
        components,
        4,
        0,
        components * 4,
    )


def _triangle_element(triangles: tuple, primitive: object) -> object:

    indices = [index for triangle in triangles for index in triangle]
    data = struct.pack(f"<{len(indices)}I", *indices)
    return (
        SK.SCNGeometryElement.geometryElementWithData_primitiveType_primitiveCount_bytesPerIndex_(  # noqa: E501
            NSData.dataWithBytes_length_(data, len(data)),
            primitive,
            len(triangles),
            4,
        )
    )


def _build_geometry(
    face_images: list[object],
    emission_images: list[object] | None = None,
    emission_boosts: list[float] | None = None,
) -> object:
    mesh = rounded_dodecahedron_mesh()
    count = len(mesh.positions)
    sources = [
        _geometry_source(mesh.positions, SK.SCNGeometrySourceSemanticVertex, 3, count),
        _geometry_source(mesh.normals, SK.SCNGeometrySourceSemanticNormal, 3, count),
        _geometry_source(mesh.uvs, SK.SCNGeometrySourceSemanticTexcoord, 2, count),
    ]
    elements = [
        _triangle_element(group, SK.SCNGeometryPrimitiveTypeTriangles)
        for group in mesh.face_triangles
    ]
    geometry = SK.SCNGeometry.geometryWithSources_elements_(sources, elements)
    geometry.setMaterials_(
        [
            _face_material(
                image,
                emission_images[index] if emission_images else None,
                emission_boosts[index] if emission_boosts and index < len(emission_boosts) else 1.0,
            )
            for index, image in enumerate(face_images[:12])
        ]
    )
    return geometry


def _face_material(
    image: object,
    emission: object | None = None,
    emission_boost: float = 1.0,
) -> object:
    material = SK.SCNMaterial.material()
    material.diffuse().setContents_(image)
    material.setDoubleSided_(False)
    material.setLightingModelName_(SK.SCNLightingModelPhysicallyBased)
    material.metalness().setContents_(0.0)
    roughness = _load_map("Roughness.jpg")
    if roughness is not None:
        material.roughness().setContents_(roughness)
        # Zoom the sampling by TEXTURE_SCALE around the map's centre.
        inverse = 1.0 / TEXTURE_SCALE
        shift = 0.5 * (1.0 - inverse)
        material.roughness().setContentsTransform_(
            SK.SCNMatrix4Translate(SK.SCNMatrix4MakeScale(inverse, inverse, 1.0), shift, shift, 0.0)
        )
        material.roughness().setWrapS_(SK.SCNWrapModeClamp)
        material.roughness().setWrapT_(SK.SCNWrapModeClamp)
    material.roughness().setIntensity_(ROUGHNESS_INTENSITY)
    if emission is not None:
        material.emission().setContents_(emission)
        material.emission().setIntensity_(EMISSION_INTENSITY * emission_boost)
    return material


def _environment_image(size: int = 256) -> object:
    """A studio: flat softbox panels on a mid-grey surround. The glossy surface
    reflects them as broad soft catchlights; roughness softens their edges."""
    image = NSImage.alloc().initWithSize_((size, size))
    image.lockFocus()
    NSColor.colorWithCalibratedWhite_alpha_(ENV_BASE, 1.0).set()
    NSRectFill(NSMakeRect(0, 0, size, size))
    for white, rect, radius in (
        (ENV_MAIN, (0.14, 0.58, 0.50, 0.30), 0.05),  # main softbox, upper left
        (ENV_SIDE, (0.74, 0.30, 0.16, 0.44), 0.04),  # side strip
        (ENV_BOUNCE, (0.24, 0.04, 0.50, 0.16), 0.04),  # bounce card below
    ):
        NSColor.colorWithCalibratedWhite_alpha_(white, 1.0).set()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(*(size * value for value in rect)), size * radius, size * radius
        ).fill()
    image.unlockFocus()
    return image


def _configure_camera(camera: object) -> None:
    """Orthographic camera with HDR and bloom. KVC pins the knobs without
    SceneKit's implicit animation ramping them in."""
    camera.setUsesOrthographicProjection_(True)
    camera.setOrthographicScale_(DICE_ORTHO_SCALE)
    camera.setZNear_(0.1)
    camera.setZFar_(50.0)
    for key, value in (
        ("wantsHDR", True),
        ("motionBlurIntensity", 0.0),  # only while spinning
        ("wantsExposureAdaptation", False),
        ("exposureAdaptationDuration", 0.0),
        ("exposureOffset", 0.0),
        ("bloomIntensity", BLOOM_INTENSITY),  # the halo on the glowing letters
        ("bloomThreshold", BLOOM_THRESHOLD),
        ("bloomBlurRadius", BLOOM_RADIUS),
    ):
        camera.setValue_forKey_(value, key)


def _light_node(kind: object, color: object, intensity: float | None = None) -> object:
    light = SK.SCNLight.light()
    light.setType_(kind)
    light.setColor_(color)
    if intensity is not None:
        light.setIntensity_(intensity)
    node = SK.SCNNode.node()
    node.setLight_(light)
    return node


def _idle_tumble_action() -> object:
    return SK.SCNAction.repeatActionForever_(
        SK.SCNAction.rotateByX_y_z_duration_(0.7 * math.pi, 2 * math.pi, 0.3 * math.pi, 24.0)
    )


def _pin_live_pose(dice_node: object) -> None:
    """Stop the tumble where the dice is. The tumble drives the presentation
    node only, so its transform is copied back first or the dice would snap."""
    for node in (dice_node, dice_node.parentNode()):
        presentation = node.presentationNode() if node is not None else None
        if presentation is not None:
            node.setTransform_(presentation.transform())
    dice_node.removeActionForKey_(IDLE_TUMBLE_KEY)


def stop_tumble(dice_node: object) -> None:
    """Freeze the idle tumble in place."""
    with contextlib.suppress(Exception):  # decorative
        _pin_live_pose(dice_node)


def start_tumble(dice_node: object) -> None:
    """Resume the idle tumble from the current pose (no-op if running)."""
    with contextlib.suppress(Exception):
        if dice_node.actionForKey_(IDLE_TUMBLE_KEY) is None:
            dice_node.runAction_forKey_(_idle_tumble_action(), IDLE_TUMBLE_KEY)


def face_front(dice_node: object, resume: Callable[[], bool] = lambda: True) -> None:
    """Glide face 0 (the focus letter) to the camera from the live pose, then
    resume the tumble if it was running and `resume()` still agrees."""
    with contextlib.suppress(Exception):  # decorative
        tumbling = dice_node.actionForKey_(IDLE_TUMBLE_KEY) is not None
        _pin_live_pose(dice_node)
        pivot = dice_node.parentNode()
        SK.SCNTransaction.begin()
        SK.SCNTransaction.setAnimationDuration_(FOCUS_FRONT_SECONDS)
        if tumbling:
            with contextlib.suppress(Exception):  # own guard: commit() must still run
                SK.SCNTransaction.setCompletionBlock_(
                    lambda: start_tumble(dice_node) if resume() else None
                )
        dice_node.setRotation_(_focus_front_rotation())
        if pivot is not None:
            pivot.setRotation_((0.0, 1.0, 0.0, 0.0))  # undo any scroll-spin
        SK.SCNTransaction.commit()


def _focus_front_rotation() -> tuple[float, float, float, float]:
    """Axis-angle turning face 0 straight at the camera, glyph upright."""
    n0 = _normalized(_face_directions()[0])
    v_cam = _normalized((0.0, -CAMERA_DISTANCE * math.tan(CAMERA_TILT), CAMERA_DISTANCE))
    axis = _normalized(_cross(n0, v_cam))
    base = (axis[0], axis[1], axis[2], math.acos(max(-1.0, min(1.0, _dot(n0, v_cam)))))
    # The shortest arc leaves the glyph upside down: a half turn about v_cam
    # flips it upright and keeps the face on the camera.
    flip = (v_cam[0], v_cam[1], v_cam[2], math.pi)
    return _quat_to_axisangle(_quat_mul(_axisangle_to_quat(flip), _axisangle_to_quat(base)))


def canvas_background(appearance: object | None = None) -> object:
    """The canvas colour behind the dice for `appearance` (None = the app's).
    SceneKit flattens colours, so the view re-applies this on theme changes."""
    if appearance is None:
        appearance = NSApplication.sharedApplication().effectiveAppearance()
    names = [NSAppearanceNameAqua, NSAppearanceNameDarkAqua]
    dark = appearance.bestMatchFromAppearancesWithNames_(names) == NSAppearanceNameDarkAqua
    rgb = DICE_CANVAS_BACKGROUND if dark else DICE_CANVAS_BACKGROUND_LIGHT
    return NSColor.colorWithSRGBRed_green_blue_alpha_(*rgb, 1.0)


def make_dice_scene(
    face_images: list[object],
    emission_images: list[object] | None = None,
    emission_boosts: list[float] | None = None,
) -> tuple[object, object, object]:
    """Returns (scene, dice_node, camera_node); the dice tumbles forever."""
    scene = SK.SCNScene.scene()
    root = scene.rootNode()
    scene.background().setContents_(canvas_background())
    scene.lightingEnvironment().setContents_(_environment_image())
    scene.lightingEnvironment().setIntensity_(ENV_INTENSITY)

    dice = SK.SCNNode.nodeWithGeometry_(
        _build_geometry(face_images, emission_images, emission_boosts)
    )
    dice.setRotation_(_focus_front_rotation())  # opens focus side out
    # The tumble spins the dice; two-finger scroll turns this pivot, so the two
    # rotations compose instead of fighting over one transform.
    pivot = SK.SCNNode.node()
    pivot.setName_("AZLetterProoferDicePivot")
    pivot.addChildNode_(dice)
    root.addChildNode_(pivot)
    dice.runAction_forKey_(_idle_tumble_action(), IDLE_TUMBLE_KEY)

    camera_node = SK.SCNNode.node()
    camera = SK.SCNCamera.camera()
    _configure_camera(camera)
    camera_node.setCamera_(camera)
    # Raised so the tilted look-at ray lands on the origin: the dice stays centred.
    camera_node.setPosition_((0.0, -CAMERA_DISTANCE * math.tan(CAMERA_TILT), CAMERA_DISTANCE))
    camera_node.setEulerAngles_((CAMERA_TILT, 0.0, 0.0))
    root.addChildNode_(camera_node)

    rgb = NSColor.colorWithCalibratedRed_green_blue_alpha_
    key = _light_node(SK.SCNLightTypeDirectional, rgb(*KEY_COLOR, 1.0), KEY_INTENSITY)
    key.setEulerAngles_((KEY_ELEVATION, KEY_AZIMUTH, 0.0))
    fill = _light_node(SK.SCNLightTypeDirectional, rgb(*FILL_COLOR, 1.0), FILL_INTENSITY)
    fill.setEulerAngles_((FILL_ELEVATION, FILL_AZIMUTH, 0.0))
    ambient = _light_node(
        SK.SCNLightTypeAmbient, NSColor.colorWithCalibratedWhite_alpha_(AMBIENT_WHITE, 1.0)
    )
    softbox = _light_node(SK.SCNLightTypeArea, rgb(*SOFTBOX_COLOR, 1.0), SOFTBOX_INTENSITY)
    softbox.light().setAreaType_(SK.SCNLightAreaTypeRectangle)
    softbox.light().setAreaExtents_((SOFTBOX_WIDTH, SOFTBOX_HEIGHT, 0.0))
    softbox.setPosition_(SOFTBOX_POSITION)
    softbox.setConstraints_([SK.SCNLookAtConstraint.lookAtConstraintWithTarget_(pivot)])
    for node in (key, fill, ambient, softbox):
        root.addChildNode_(node)
    return scene, dice, camera_node


_DICE_VIEW_CLASS = None


def _dice_view_class():
    """An SCNView subclass: two-finger scroll spins the dice, Option-scroll or
    Option-pinch zooms, a two-finger rotate is eaten. Clicks do nothing."""
    global _DICE_VIEW_CLASS
    if _DICE_VIEW_CLASS is not None:
        return _DICE_VIEW_CLASS

    # ObjC class names are process-wide: reuse the class on a module re-import.
    try:
        _DICE_VIEW_CLASS = objc.lookUpClass("AZLetterProoferDiceView")
        return _DICE_VIEW_CLASS
    except objc.error:
        pass

    class AZLetterProoferDiceView(SK.SCNView):
        def viewDidChangeEffectiveAppearance(self):
            with contextlib.suppress(Exception):
                objc.super(AZLetterProoferDiceView, self).viewDidChangeEffectiveAppearance()
            with contextlib.suppress(Exception):
                scene = self.scene()
                if scene is not None:
                    scene.background().setContents_(canvas_background(self.effectiveAppearance()))

        @objc.python_method
        def _set_motion_blur(self, value):
            point_of_view = self.pointOfView()
            camera = point_of_view.camera() if point_of_view is not None else None
            if camera is not None:
                camera.setValue_forKey_(value, "motionBlurIntensity")

        @objc.python_method
        def _touch_motion_blur(self):
            """Blur on while the fingers move; a restartable timer drops it."""
            self._set_motion_blur(MOTION_BLUR)
            timer = getattr(self, "blurDecayTimer", None)
            if timer is not None:
                timer.invalidate()
            self.blurDecayTimer = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
                MOTION_BLUR_DECAY, False, lambda _timer: self._set_motion_blur(0.0)
            )

        @objc.python_method
        def _zoom(self, delta):
            camera = self.pointOfView().camera() if self.pointOfView() else None
            if camera is None:
                return
            scale = float(camera.orthographicScale()) * (1.0 - delta * 0.02)
            camera.setOrthographicScale_(max(ZOOM_RANGE[0], min(ZOOM_RANGE[1], scale)))

        def scrollWheel_(self, event):
            try:
                flags = int(event.modifierFlags())
                delta_x = float(event.scrollingDeltaX())
                delta_y = float(event.scrollingDeltaY())
            except Exception:
                return
            if flags & OPTION_FLAG:
                self._zoom(delta_y)
                return
            # Rotate the pivot in WORLD space (transform * rotation, row vectors),
            # so the spin follows the fingers whatever the tumble's pose.
            dice = getattr(self, "diceNode", None)
            pivot = dice.parentNode() if dice is not None else None
            if pivot is not None:
                try:
                    self._touch_motion_blur()
                    transform = pivot.transform()
                    transform = SK.SCNMatrix4Mult(
                        transform,
                        SK.SCNMatrix4MakeRotation(delta_x * DICE_ROTATE_SENSITIVITY, 0.0, 1.0, 0.0),
                    )
                    transform = SK.SCNMatrix4Mult(
                        transform,
                        SK.SCNMatrix4MakeRotation(delta_y * DICE_ROTATE_SENSITIVITY, 1.0, 0.0, 0.0),
                    )
                    pivot.setTransform_(transform)
                    return
                except Exception:
                    pass
            objc.super(AZLetterProoferDiceView, self).scrollWheel_(event)

        def magnifyWithEvent_(self, event):
            try:
                flags = int(event.modifierFlags())
                magnification = float(event.magnification())
            except Exception:
                return
            if flags & OPTION_FLAG:
                self._zoom(magnification * 100.0)

        def rotateWithEvent_(self, event):
            pass  # eat two-finger rotate

    _DICE_VIEW_CLASS = AZLetterProoferDiceView
    return _DICE_VIEW_CLASS


def make_dice_view(
    width: float,
    height: float,
    face_images: list[object],
    emission_images: list[object] | None = None,
    emission_boosts: list[float] | None = None,
) -> tuple[object, object]:
    """SCNView with the tumbling dice; returns (view, dice_node)."""

    scene, dice, _camera = make_dice_scene(face_images, emission_images, emission_boosts)
    view = _dice_view_class().alloc().initWithFrame_options_(NSMakeRect(0, 0, width, height), None)
    view.diceNode = dice  # scrollWheel_ spins its parent pivot
    view.setScene_(scene)
    view.setPreferredFramesPerSecond_(60)
    view.setAntialiasingMode_(SK.SCNAntialiasingModeNone)
    view.setAllowsCameraControl_(False)  # no click-drag orbit
    return view, dice


def update_face(
    dice_node: object,
    index: int,
    image: object | None = None,
    emission: object | None = None,
    boost: float = 1.0,
    fade: float = 0.0,
) -> None:
    """Swap one face's diffuse and/or emission in place."""
    materials = dice_node.geometry().materials()
    if not 0 <= index < min(12, len(materials)):
        return
    if fade > 0:
        SK.SCNTransaction.begin()
        SK.SCNTransaction.setAnimationDuration_(fade)
    material = materials[index]
    if image is not None:
        material.diffuse().setContents_(image)
    if emission is not None:
        material.emission().setContents_(emission)
        material.emission().setIntensity_(EMISSION_INTENSITY * boost)
    if fade > 0:
        SK.SCNTransaction.commit()


@functools.cache
def _focus_anchor() -> tuple[Vec3, Vec3]:
    """(point just above face 0's dome, face 0's outward normal)."""
    normal = _normalized(_face_directions()[0])
    distance = max(_dot(position, normal) for position in rounded_dodecahedron_mesh().positions)
    return _scaled(normal, distance + 0.02), normal


def set_highlight_aura(dice_node: object, idle: bool = False) -> None:
    """Focus: face 0 glows at HIGHLIGHT_BOOST, its crack light spilling onto
    the plastic. Idle: every letter glows at IDLE_BOOST, no crack light
    (twelve washed the die out)."""
    existing = dice_node.childNodeWithName_recursively_(AURA_NODE_NAME, False)
    if existing is not None:
        existing.removeFromParentNode()
    materials = dice_node.geometry().materials()[:12]
    if idle:
        for material in materials:
            material.emission().setIntensity_(EMISSION_INTENSITY * IDLE_BOOST)
    elif materials:
        position, normal = _focus_anchor()
        crack = _light_node(
            SK.SCNLightTypeOmni,
            NSColor.colorWithCalibratedRed_green_blue_alpha_(*HIGHLIGHT_GLOW_COLOR, 1.0),
            CRACK_LUMENS,
        )
        crack.light().setAttenuationStartDistance_(CRACK_SOFTNESS * CRACK_REACH)
        crack.light().setAttenuationEndDistance_(CRACK_REACH)
        crack.setPosition_(_scaled(normal, CRACK_HEIGHT))
        aura = SK.SCNNode.node()
        aura.setName_(AURA_NODE_NAME)
        aura.setPosition_(position)
        aura.addChildNode_(crack)
        dice_node.addChildNode_(aura)
        materials[0].emission().setIntensity_(EMISSION_INTENSITY * HIGHLIGHT_BOOST)
