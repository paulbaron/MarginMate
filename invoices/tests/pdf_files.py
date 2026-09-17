"""Tiny PDF files written by hand, for the tests that need a real file.

The project has no PDF writer, and a real invoice must never be committed:
these are a few hundred bytes of PDF syntax - a page of text in Helvetica,
optionally with a small image placed on it the way a logo is.
"""


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def write_pdf(path: str, lines: list[str], logo: bool = False, page_size=(595, 842)) -> str:
    """A one-page PDF printing `lines` from the top, 14 points apart; with
    `logo`, a 2x2 image placed small in the top-left corner - the only image
    on the page, as on a web shop's invoice."""
    width, height = page_size
    content = ["BT", "/F1 10 Tf", "14 TL", f"40 {height - 120} Td"]
    for line in lines:
        content.append(f"({_escape(line)}) Tj T*")
    content.append("ET")
    if logo:
        content.append(f"q 120 0 0 30 40 {height - 60} cm /Im1 Do Q")
    stream = "\n".join(content).encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] /Contents 4 0 R "
            f"/Resources << /Font << /F1 5 0 R >>{' /XObject << /Im1 6 0 R >>' if logo else ''} >> >>"
        ).encode("latin-1"),
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    if logo:
        pixels = bytes([200, 30, 30] * 4)
        objects.append(
            b"<< /Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceRGB "
            b"/BitsPerComponent 8 /Length " + str(len(pixels)).encode() + b" >>\nstream\n" + pixels + b"\nendstream"
        )

    output = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(output)
    output += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        output += f"{offset:010d} 00000 n \n".encode()
    output += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    with open(path, "wb") as handle:
        handle.write(bytes(output))
    return path
