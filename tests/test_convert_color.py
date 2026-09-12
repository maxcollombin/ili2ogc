"""`convert/color.py`'s LCh(ab) -> sRGB, checked by round-trip against an independent sRGB -> LCh(ab) reference."""

import math

from interlis.convert.color import lch_to_srgb

_D65_XN = 95.047
_D65_YN = 100.0
_D65_ZN = 108.883
_EPSILON = 216 / 24389
_KAPPA = 24389 / 27


def _srgb_to_lch(r: int, g: int, b: int) -> tuple[float, float, float]:
    """Independent inverse pipeline (sRGB -> linear -> XYZ -> Lab -> LCh), test-only - never used by production code."""

    def _linearize(channel: int) -> float:
        c = channel / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r_lin, g_lin, b_lin = _linearize(r), _linearize(g), _linearize(b)
    x = (0.4124 * r_lin + 0.3576 * g_lin + 0.1805 * b_lin) * 100
    y = (0.2126 * r_lin + 0.7152 * g_lin + 0.0722 * b_lin) * 100
    z = (0.0193 * r_lin + 0.1192 * g_lin + 0.9505 * b_lin) * 100

    def _f(t: float) -> float:
        return t ** (1 / 3) if t > _EPSILON else (_KAPPA * t + 16) / 116

    fx, fy, fz = _f(x / _D65_XN), _f(y / _D65_YN), _f(z / _D65_ZN)
    lum = 116 * fy - 16
    a = 500 * (fx - fy)
    b_lab = 200 * (fy - fz)
    c = math.hypot(a, b_lab)
    h = math.degrees(math.atan2(b_lab, a)) % 360
    return lum, c, h


def test_black_round_trips_exactly():
    lum, c, h = _srgb_to_lch(0, 0, 0)
    assert lch_to_srgb(lum, c, h) == (0, 0, 0)


def test_white_round_trips_exactly():
    lum, c, h = _srgb_to_lch(255, 255, 255)
    assert lch_to_srgb(lum, c, h) == (255, 255, 255)


def test_primary_colors_round_trip_within_rounding_tolerance():
    for rgb in [(255, 0, 0), (0, 255, 0), (0, 0, 255), (128, 128, 128), (255, 165, 0)]:
        lum, c, h = _srgb_to_lch(*rgb)
        recovered = lch_to_srgb(lum, c, h)
        assert all(abs(a - b) <= 1 for a, b in zip(rgb, recovered)), (rgb, recovered)
