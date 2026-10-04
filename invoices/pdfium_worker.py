"""PDFium in a process of its own: everything `ocr.page_images` asks of a PDF.

PDFium is native code, and what a PDF from outside can make it allocate or
compute is not bounded by anything the server can weigh beforehand (security
review, round 3: a tiling pattern, a Type3 glyph, a soft mask, an ink
annotation with no appearance - each a few KB, each hundreds of MB and tens
of seconds under PDFIUM_LOCK). So it never runs in the one process serving
every bar: `ocr.page_images` starts this script for each document, bounded
by the operating system (`pdfium_sandbox`: a Job Object's memory cap on
Windows, an rlimit elsewhere, and a wall clock everywhere), and reads back
the pages it wrote.

One JSON line each way, in this order:

1. this process says its pid - the venv's python.exe is a launcher, so the
   interpreter is not the process the server started;
2. it waits for the job (the PDF, the folder, the caps), which the server
   sends only once that pid is in its Job Object: nothing of the file is
   read before;
3. it writes pages 1..n into the folder, losslessly - the server gets the
   very pixels PDFium gave: uncompressed (PPM) while they weigh
   RENDER_RAW_BYTES together, PNG after -, and says {"pages": n}, or
   {"refused": the
   French sentence of a DocumentTooBig}, {"memory": true}, or
   {"unreadable": what PDFium said}.

Imports nothing of Django: pypdfium2, Pillow and `invoices.ocr`'s caps and
checks (that module imports the standard library only).
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: The caps of `invoices.ocr` the server sends with the job, read there at
#: the call (a test patches them down).
LIMITS = (
    "MAX_PAGES",
    "RENDER_DPI",
    "RENDER_MAX_PIXELS",
    "MIN_RENDER_SCALE",
    "IMAGE_MAX_PIXELS",
    "RENDER_OUTPUT_BYTES",
    "RENDER_RAW_BYTES",
)
#: A PNG's zlib level, past RENDER_RAW_BYTES: 1 writes an A4 page at 300 dpi
#: in 0,13 s and 1,3 MB, where 6 takes 0,19 s for 1 MB.
PNG_LEVEL = 1


def plan_pdf(document, pdfium_raw, ocr) -> list:
    """(page, the one image to take as it is or None, the render scale) for
    every page - every page weighed before the first is drawn."""
    if len(document) > ocr.MAX_PAGES:
        raise ocr.DocumentTooBig(ocr.TOO_MANY_PAGES.format(pages=len(document), limit=ocr.MAX_PAGES))
    plan = []
    for number, page in enumerate(document, start=1):
        width, height = page.get_size()
        images = list(page.get_objects(filter=(pdfium_raw.FPDF_PAGEOBJ_IMAGE,)))
        for image in images:
            ocr._check_pixels(*image.get_px_size(), number)
        if len(images) == 1 and ocr.covers_page(images[0].get_bounds(), width, height):
            plan.append((page, images[0], None))
            continue
        scale = ocr.render_scale(width, height)
        if scale is None:
            raise ocr.DocumentTooBig(
                ocr.PAGE_TOO_LARGE.format(number=number, width=ocr._centimetres(width), height=ocr._centimetres(height))
            )
        plan.append((page, None, scale))
    return plan


def render(path: str, folder: str, limits: dict) -> dict:
    """Write every page of the PDF at `path` into `folder` (files 1, 2...),
    each as `page_images` gives it: the embedded photo of a page that is one,
    else the page rendered."""
    sys.path.insert(0, ROOT)
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_raw

    from invoices import ocr

    for name in LIMITS:
        setattr(ocr, name, limits[name])
    try:
        document = pdfium.PdfDocument(path)
    except pdfium.PdfiumError as error:
        return {"unreadable": str(error)}
    try:
        written = 0
        plan = plan_pdf(document, pdfium_raw, ocr)
        for number, (page, image, scale) in enumerate(plan, start=1):
            bitmap = image.get_bitmap() if image is not None else page.render(scale=scale)
            picture = bitmap.to_pil().convert("RGB")
            del bitmap
            target = os.path.join(folder, str(number))
            if written + picture.width * picture.height * 3 <= ocr.RENDER_RAW_BYTES:
                picture.save(target, "PPM")
            else:
                picture.save(target, "PNG", compress_level=PNG_LEVEL)
            del picture
            written += os.path.getsize(target)
            if written > ocr.RENDER_OUTPUT_BYTES:
                raise ocr.DocumentTooBig(ocr.TOO_MUCH_DRAWN.format(weight=ocr._megabytes(ocr.RENDER_OUTPUT_BYTES)))
        return {"pages": len(plan)}
    except ocr.DocumentTooBig as refusal:
        return {"refused": str(refusal)}
    except pdfium.PdfiumError as error:
        return {"unreadable": str(error)}
    finally:
        document.close()


def _cap_memory(memory: int) -> None:
    """Elsewhere than Windows, the cap the Job Object sets there: what the
    process may allocate (RLIMIT_DATA, else its address space)."""
    try:
        import resource
    except ImportError:
        return
    for name in ("RLIMIT_DATA", "RLIMIT_AS"):
        limit = getattr(resource, name, None)
        if limit is not None:
            resource.setrlimit(limit, (memory, memory))
            return


def main() -> int:
    channel = sys.stdout
    # Nothing but this protocol on the server's pipe.
    sys.stdout = sys.stderr

    def say(message: dict) -> None:
        channel.write(json.dumps(message) + "\n")
        channel.flush()

    say({"pid": os.getpid()})
    line = sys.stdin.readline()
    if not line:
        return 1
    job = json.loads(line)
    try:
        if os.name != "nt":
            _cap_memory(job["memory"])
        result = render(job["path"], job["folder"], job["limits"])
    except MemoryError:
        result = {"memory": True}
    except Exception as error:  # noqa: BLE001 - said to the server, which says « PDF illisible »
        result = {"unreadable": f"{type(error).__name__}: {error}"}
    say(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
