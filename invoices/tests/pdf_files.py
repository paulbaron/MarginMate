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


def write_pdf_with_attachments(
    path: str,
    lines: list[str],
    attachments,
    nested_names: bool = False,
    compress: bool = True,
    via_af_only: bool = False,
    page_size=(595, 842),
) -> str:
    """A PDF/A-3 shaped file: a page of text carrying attached files.

    That is all a Factur-X invoice is - an ordinary-looking PDF whose catalog
    holds a `/Names /EmbeddedFiles` tree pointing at one `/Filespec` per
    attached file. Built here rather than committed because a real Factur-X
    invoice carries a supplier's IBAN and the bar's address, and because a
    test that needs a binary fixture it cannot build is a test nobody can fix.

    `attachments` is a sequence of (filename, payload bytes, relationship) -
    the relationship being `/AFRelationship`, "Data" for the invoice XML and
    "Supplement" or "Unspecified" for anything else a sender bolted on.
    `nested_names` puts the entries two `/Kids` levels down, which producers
    do as soon as a file carries several attachments. `via_af_only` leaves
    the name tree out and declares the files through `/AF` alone - PDF/A-3
    requires both, and files in the wild carry one or the other.
    """
    import zlib

    width, height = page_size
    content = ["BT", "/F1 10 Tf", "14 TL", f"40 {height - 120} Td"]
    for line in lines:
        content.append(f"({_escape(line)}) Tj T*")
    content.append("ET")
    stream = "\n".join(content).encode("latin-1")

    attachments = list(attachments)
    # Object numbers: 1 catalog, 2 pages, 3 page, 4 contents, 5 font, then a
    # (filespec, embedded stream) pair per attachment.
    first_attachment = 6
    entries = []
    for index, (name, _payload, _relationship) in enumerate(attachments):
        entries.append((name, first_attachment + 2 * index))
    names_array = " ".join(f"({_escape(name)}) {number} 0 R" for name, number in entries)
    if nested_names:
        limits = ""
        if entries:
            ordered = sorted(name for name, _ in entries)
            limits = f"/Limits [({_escape(ordered[0])}) ({_escape(ordered[-1])})] "
            names_array = " ".join(
                f"({_escape(name)}) {number} 0 R" for name, number in sorted(entries)
            )
        tree = (
            "<< /Kids [ << /Kids [ << " + limits + "/Names [" + names_array + "] >> ] >> ] >>"
        )
    else:
        tree = "<< /Names [" + names_array + "] >>"
    associated = " ".join(f"{number} 0 R" for _name, number in entries)

    named = "" if via_af_only else f"/Names << /EmbeddedFiles {tree} >> "
    objects = [
        (
            "<< /Type /Catalog /Pages 2 0 R " + named + f"/AF [{associated}] >>"
        ).encode("latin-1"),
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] /Contents 4 0 R "
            "/Resources << /Font << /F1 5 0 R >> >> >>"
        ).encode("latin-1"),
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    for index, (name, payload, relationship) in enumerate(attachments):
        embedded = first_attachment + 2 * index + 1
        objects.append(
            (
                f"<< /Type /Filespec /F ({_escape(name)}) /UF ({_escape(name)}) "
                f"/AFRelationship /{relationship} /Desc (Facture) "
                f"/EF << /F {embedded} 0 R >> >>"
            ).encode("latin-1")
        )
        body = zlib.compress(payload) if compress else payload
        filtered = b"/Filter /FlateDecode " if compress else b""
        objects.append(
            b"<< /Type /EmbeddedFile /Subtype /text#2Fxml "
            + filtered
            + b"/Params << /Size "
            + str(len(payload)).encode()
            + b" >> /Length "
            + str(len(body)).encode()
            + b" >>\nstream\n"
            + body
            + b"\nendstream"
        )

    output = bytearray(b"%PDF-1.7\n")
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


def truncate_file(path: str, keep: int) -> str:
    """The first `keep` bytes of a file - a download cut off part way."""
    with open(path, "rb") as handle:
        data = handle.read(keep)
    with open(path, "wb") as handle:
        handle.write(data)
    return path
