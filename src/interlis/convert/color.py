"""Convert an INTERLIS `StandardSymbology.Color` (CIE LCh(ab)) to sRGB.

`pycartosym` has no LCh notion at all (only `RGBColor`/`RGBColorNormalized`/
`WebColorName`), so this conversion has to happen before any INTERLIS
`Color` reaches it. Standard D65 LCh(ab) -> Lab -> XYZ -> linear sRGB ->
gamma-encoded sRGB pipeline
(Bruce Lindbloom's formulas, the de-facto reference for this conversion).
Out-of-gamut results are clamped to 0..255, the industry-standard
graceful degradation for a color space wider than sRGB - not a RULE #5
concern (colorimetric rounding, not structural data loss).
"""

import math

_D65_XN = 95.047
_D65_YN = 100.0
_D65_ZN = 108.883
_EPSILON = 216 / 24389
_KAPPA = 24389 / 27


def lch_to_srgb(lum: float, c: float, h_degrees: float) -> tuple[int, int, int]:
    """Convert CIE LCh(ab) (`lum` 0..100, `c` >=0, `h_degrees`) to an (r, g, b) triple, 0..255 each."""
    h_radians = math.radians(h_degrees)
    a = c * math.cos(h_radians)
    b = c * math.sin(h_radians)

    fy = (lum + 16) / 116
    fx = fy + a / 500
    fz = fy - b / 200
    xr = fx**3 if fx**3 > _EPSILON else (116 * fx - 16) / _KAPPA
    yr = ((lum + 16) / 116) ** 3 if lum > _KAPPA * _EPSILON else lum / _KAPPA
    zr = fz**3 if fz**3 > _EPSILON else (116 * fz - 16) / _KAPPA
    x, y, z = xr * _D65_XN / 100, yr * _D65_YN / 100, zr * _D65_ZN / 100

    r_lin = 3.2406 * x - 1.5372 * y - 0.4986 * z
    g_lin = -0.9689 * x + 1.8758 * y + 0.0415 * z
    b_lin = 0.0557 * x - 0.2040 * y + 1.0570 * z

    def _gamma_encode(channel: float) -> int:
        channel = 12.92 * channel if channel <= 0.0031308 else 1.055 * channel ** (1 / 2.4) - 0.055
        return round(min(1.0, max(0.0, channel)) * 255)

    return _gamma_encode(r_lin), _gamma_encode(g_lin), _gamma_encode(b_lin)
