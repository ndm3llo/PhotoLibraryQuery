import io

from PIL import Image


def generate_thumbnail(contents: bytes, size=(400, 400)) -> bytes:
    img = Image.open(io.BytesIO(contents))
    # Convert to RGB if it's RGBA (for PNGs) to ensure JPEG compatibility
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    
    img.thumbnail(size)
    thumb_io = io.BytesIO()
    img.save(thumb_io, format="JPEG", quality=85)
    return thumb_io.getvalue()