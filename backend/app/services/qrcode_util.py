"""Server-side QR rendering (0034), replacing the frontend's client-side
`qrcode` npm package.

The payloads this draws (a badge code, a sticker code) are already whatever
sensitivity they were before this existed — a badge code is still returned
exactly once, in the same response it always was (DECISIONS.md §CC2); this
module only moves *where* the QR image gets drawn from, not what is exposed.
"""

import base64
from io import BytesIO

import qrcode
from qrcode.constants import ERROR_CORRECT_H


def to_data_uri(data: str, *, box_size: int = 6, border: int = 1) -> str:
    """A `data:image/png;base64,...` URI an <img> tag can use directly.

    High error correction throughout: badges live in a pocket for a year and
    stickers get scuffed in transit, matching the levels the frontend used to
    pass to qrcode.toCanvas.
    """
    img = qrcode.make(
        data,
        box_size=box_size,
        border=border,
        error_correction=ERROR_CORRECT_H,
    )
    buf = BytesIO()
    img.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"
