"""Server-side QR rendering (0034), replacing the frontend's client-side
`qrcode` npm package.

Uses `segno` rather than the Python `qrcode` package: both produce
spec-compliant QR codes with identical scan accuracy for the same error
correction level (accuracy is a property of the QR standard and the physical
print, not the generator), but segno has zero dependencies and writes PNG
natively, where `qrcode`'s PNG output needs the `[pil]` extra (Pillow) — a
much heavier import for the same result on a small Render instance.

The payloads this draws (a badge code, a sticker code) are already whatever
sensitivity they were before this existed — a badge code is still returned
exactly once, in the same response it always was (DECISIONS.md §CC2); this
module only moves *where* the QR image gets drawn from, not what is exposed.
"""

import base64
from io import BytesIO

import segno


def to_data_uri(data: str, *, scale: int = 6, border: int = 1) -> str:
    """A `data:image/png;base64,...` URI an <img> tag can use directly.

    High error correction throughout: badges live in a pocket for a year and
    stickers get scuffed in transit, matching the level the frontend used to
    pass to qrcode.toCanvas.
    """
    qr = segno.make(data, error="h")
    buf = BytesIO()
    qr.save(buf, kind="png", scale=scale, border=border, dark="black", light="white")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def to_svg_data_uri(data: str, *, scale: int = 8, border: int = 4) -> str:
    """A `data:image/svg+xml;base64,...` URI — vector, not raster.

    For a QR whose on-screen display size isn't known in advance (a badge
    shown on whatever phone happens to be presenting it, then blown up again
    for the full-screen scan view), a PNG is rasterized once at one small
    pixel size and the browser upscales it with interpolation to fill a
    bigger box, which softens the sharp module edges a scanning camera
    depends on. An SVG `<img>` re-renders from its vector path at whatever
    size the CSS asks for, so it stays crisp however large it's shown.
    """
    qr = segno.make(data, error="h")
    buf = BytesIO()
    qr.save(buf, kind="svg", scale=scale, border=border, dark="black", light="white")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"
