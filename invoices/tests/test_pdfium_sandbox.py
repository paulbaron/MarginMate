"""PDFium draws in a process of its own (security review of g04, round 3).

What a PDF from outside can make PDFium allocate or compute is not bounded
by anything weighed beforehand: a tiling pattern, a Type3 glyph, a soft mask
of a few KB each took 700 MB to 1,3 GB and 20 s under PDFIUM_LOCK, an ink
annotation 53 s - in the one process serving every bar. `ocr.page_images`
now runs PDFium in a child (invoices/pdfium_worker.py) bounded by the
operating system (`pdfium_sandbox`: a Job Object on Windows, an rlimit
elsewhere, a wall clock everywhere), one at a time, and reads back its
pages.

Machine safety: every PDF here is a few hundred bytes, with the caps patched
down; the biggest page asked for is 70 MB of bitmap, refused by a 40 MB cap.
"""

import json
import os
import shutil
import tempfile
import threading
import time
from unittest import mock, skipUnless

import pypdfium2 as pdfium
from django.test import SimpleTestCase
from PIL import Image

from common import UNREADABLE_PDF, UnreadablePdf, error_for_page
from invoices import ocr, pdfium_sandbox, pdfium_worker
from returnables.tests.test_reading import DRAWN, pdf_with_streams


def never(what):
    return mock.Mock(side_effect=AssertionError(f"{what} dans le processus du serveur"))


class SandboxTests(SimpleTestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.made = []
        mkdtemp = tempfile.mkdtemp

        def recorded(*args, **kwargs):
            self.made.append(mkdtemp(*args, **kwargs))
            return self.made[-1]

        patch = mock.patch("tempfile.mkdtemp", recorded)
        patch.start()
        self.addCleanup(patch.stop)

    def write(self, name: str, content: bytes) -> str:
        path = os.path.join(self.folder, name)
        with open(path, "wb") as handle:
            handle.write(content)
        return path

    def drawn(self) -> str:
        """A page of text and strokes: no photo, so it is rendered."""
        return self.write("dessin.pdf", pdf_with_streams([([], DRAWN + b"10 10 m 200 300 l 4 w S\n")]))

    def scan(self) -> str:
        path = os.path.join(self.folder, "scan.pdf")
        Image.new("RGB", (40, 20), "white").save(path, "PDF")
        return path

    def blank(self, size: float) -> str:
        document = pdfium.PdfDocument.new()
        document.new_page(size, size)
        path = os.path.join(self.folder, "page.pdf")
        document.save(path)
        document.close()
        return path

    def assertNothingLeft(self):
        self.assertTrue(self.made)
        for folder in self.made:
            self.assertFalse(os.path.exists(folder), folder)

    def in_process(self, path) -> list:
        """What PDFium in this process gives - how page_images drew pages
        before (the test's own call, not the server's)."""
        import pypdfium2.raw as pdfium_raw

        document = pdfium.PdfDocument(path)
        try:
            return [
                (image.get_bitmap() if image is not None else page.render(scale=scale)).to_pil().convert("RGB")
                for page, image, scale in pdfium_worker.plan_pdf(document, pdfium_raw, ocr)
            ]
        finally:
            document.close()

    def test_pdfium_never_runs_in_the_server_process(self):
        with (
            mock.patch("pypdfium2.PdfDocument", never("PDFium")),
            mock.patch("pypdfium2.PdfPage.render", never("Un rendu")),
        ):
            pages = [list(ocr.page_images(path)) for path in (self.drawn(), self.scan())]
        self.assertEqual([[image.size for image in images] for images in pages], [[(2480, 3509)], [(40, 20)]])
        self.assertNothingLeft()

    def test_the_pages_are_pdfium_s_own_pixels(self):
        """Uncompressed within RENDER_RAW_BYTES, PNG past it: the same pixels."""
        written, read = [], ocr._drawn_page

        def recorded(path):
            with Image.open(path) as picture:
                written.append(picture.format)
            return read(path)

        for path in (self.drawn(), self.scan()):
            direct = self.in_process(path)
            for raw, kind in ((ocr.RENDER_RAW_BYTES, "PPM"), (0, "PNG")):
                with (
                    self.subTest(path=os.path.basename(path), kind=kind),
                    mock.patch.object(ocr, "RENDER_RAW_BYTES", raw),
                    mock.patch.object(ocr, "_drawn_page", recorded),
                ):
                    written.clear()
                    boxed = list(ocr.page_images(path))
                    self.assertEqual(written, [kind])
                    self.assertEqual([image.mode for image in boxed], ["RGB"])
                    self.assertEqual([image.size for image in boxed], [image.size for image in direct])
                    self.assertEqual([image.tobytes() for image in boxed], [image.tobytes() for image in direct])

    def test_past_the_uncompressed_bytes_the_pages_are_compressed(self):
        """A 30-page document is no 750 MB of pages on the disk."""
        path = self.blank(200)
        document = pdfium.PdfDocument(path)
        document.new_page(200, 200)
        document.new_page(200, 200)
        document.save(os.path.join(self.folder, "trois.pdf"))
        document.close()
        written, read = [], ocr._drawn_page

        def recorded(path):
            with Image.open(path) as picture:
                written.append(picture.format)
            return read(path)

        # A page of 200 pt is 833 px square at 300 dpi: 2 MB uncompressed.
        with (
            mock.patch.object(ocr, "RENDER_RAW_BYTES", 5 * 1024 * 1024),
            mock.patch.object(ocr, "_drawn_page", recorded),
        ):
            pages = list(ocr.page_images(os.path.join(self.folder, "trois.pdf")))
        self.assertEqual(written, ["PPM", "PPM", "PNG"])
        self.assertEqual({image.tobytes() for image in pages}, {pages[0].tobytes()})

    def test_past_its_memory_it_is_too_heavy_to_draw(self):
        """A page of 1 000 pt is 70 MB of bitmap at 300 dpi: past a 40 MB cap,
        Windows refuses the child the memory, and says so."""
        path = self.blank(1000)
        with mock.patch.object(ocr, "RENDER_MEMORY", 40 * 1024 * 1024):
            with self.assertRaises(ocr.DocumentTooBig) as refused:
                list(ocr.page_images(path))
        self.assertEqual(
            str(refused.exception), "Document trop lourd à afficher : plus de 40 Mo de mémoire pour dessiner ses pages."
        )
        self.assertNothingLeft()
        self.assertEqual(ocr.RENDER_MEMORY, 1536 * 1024 * 1024)

    def test_past_its_time_it_is_too_heavy_to_draw_and_killed(self):
        started = time.monotonic()
        with mock.patch.object(ocr, "RENDER_SECONDS", 0.05):
            with self.assertRaises(ocr.DocumentTooBig) as refused:
                list(ocr.page_images(self.drawn()))
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(
            str(refused.exception), "Document trop lourd à afficher : plus de 0.05 secondes pour dessiner ses pages."
        )
        self.assertNothingLeft()
        self.assertEqual(ocr.RENDER_SECONDS, 60)

    def test_past_what_its_pages_may_weigh_it_is_too_heavy_to_draw(self):
        with mock.patch.object(ocr, "RENDER_OUTPUT_BYTES", 1024):
            with self.assertRaises(ocr.DocumentTooBig) as refused:
                list(ocr.page_images(self.drawn()))
        self.assertEqual(
            str(refused.exception), "Document trop lourd à afficher : ses pages dessinées pèsent plus de 0 Mo."
        )
        self.assertNothingLeft()

    def test_the_folder_goes_whatever_happens(self):
        list(ocr.page_images(self.drawn()))
        with mock.patch.object(ocr, "MAX_PAGES", 0), self.assertRaises(ocr.DocumentTooBig):
            list(ocr.page_images(self.drawn()))
        refusal = pdfium_sandbox.Outcome()
        refusal.result = {"unreadable": "Failed to load document (PDFium: Data format error)."}
        with mock.patch.object(pdfium_sandbox, "run", return_value=refusal), self.assertRaises(UnreadablePdf):
            list(ocr.page_images(self.drawn()))
        self.assertEqual(len(self.made), 3)
        self.assertNothingLeft()

    def test_the_folders_are_made_under_the_server_s_own_parent(self):
        with mock.patch.object(tempfile, "tempdir", self.folder):
            list(ocr.page_images(self.drawn()))
        self.assertEqual(len(self.made), 1)
        self.assertEqual(os.path.dirname(self.made[0]), os.path.join(self.folder, "marginmate-pdfium"))
        self.assertTrue(os.path.basename(self.made[0]).startswith("pdfium-"))
        self.assertNothingLeft()

    def test_what_a_server_killed_mid_render_left_is_swept_at_start_up(self):
        """Its `finally` never ran (a crash, a power cut): a folder of up to
        RENDER_OUTPUT_BYTES stayed in TEMP for good. The next start removes
        the pdfium-* folders of the parent older than an hour - and nothing
        else, in the parent or beside it."""
        from django.apps import apps

        parent = os.path.join(self.folder, "marginmate-pdfium")
        old = time.time() - 2 * 3600
        folders = {name: os.path.join(parent, name) for name in ("pdfium-ancien", "pdfium-recent", "autre-ancien")}
        folders["dehors"] = os.path.join(self.folder, "pdfium-dehors")
        for name, folder in folders.items():
            os.makedirs(folder)
            with open(os.path.join(folder, "1"), "wb") as handle:
                handle.write(b"P6 1 1 255 ...")
            if name != "pdfium-recent":
                os.utime(folder, (old, old))
        with mock.patch.object(tempfile, "tempdir", self.folder):
            apps.get_app_config("invoices").ready()
        self.assertEqual(sorted(os.listdir(parent)), ["autre-ancien", "pdfium-recent"])
        self.assertTrue(os.path.exists(os.path.join(folders["dehors"], "1")))
        # No parent yet (nothing ever drawn): nothing to sweep.
        with mock.patch.object(tempfile, "tempdir", os.path.join(self.folder, "vide")):
            apps.get_app_config("invoices").ready()

    def test_what_pdfium_cannot_open_is_said_as_before(self):
        """PDFium's own error was « PDF illisible », by kind, its words in
        the server's log: so is what its process says it could not open,
        or a process gone without a word."""
        refusal = pdfium_sandbox.Outcome()
        refusal.result = {"unreadable": "Failed to load document (PDFium: Data format error)."}
        crash = pdfium_sandbox.Outcome()
        crash.returncode = 3221225477
        for outcome in (refusal, crash):
            with self.subTest(result=outcome.result), mock.patch.object(pdfium_sandbox, "run", return_value=outcome):
                with self.assertRaises(UnreadablePdf) as refused:
                    list(ocr.page_images(self.drawn()))
                self.assertEqual(error_for_page(refused.exception), UNREADABLE_PDF)
        self.assertEqual(error_for_page(pdfium.PdfiumError("Failed to load document")), UNREADABLE_PDF)

    def test_a_refusal_keeps_its_french_words_and_no_more(self):
        refusal = pdfium_sandbox.Outcome()
        refusal.result = {"refused": "Page trop grande pour être lue." + " x" * 1_000}
        with (
            mock.patch.object(pdfium_sandbox, "run", return_value=refusal),
            self.assertRaises(ocr.DocumentTooBig) as said,
        ):
            list(ocr.page_images(self.drawn()))
        self.assertTrue(str(said.exception).startswith("Page trop grande pour être lue."))
        self.assertEqual(len(str(said.exception)), ocr.SAID_CHARS)

    def test_one_process_at_a_time_for_the_whole_server(self):
        running, most = [0], [0]
        run = pdfium_sandbox.run

        def counted(*args, **kwargs):
            running[0] += 1
            most[0] = max(most[0], running[0])
            try:
                return run(*args, **kwargs)
            finally:
                running[0] -= 1

        path, errors = self.drawn(), []

        def read():
            try:
                list(ocr.page_images(path))
            except Exception as error:  # noqa: BLE001 - reported below
                errors.append(error)

        with mock.patch.object(pdfium_sandbox, "run", counted):
            threads = [threading.Thread(target=read) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(most[0], 1)

    def test_a_document_waits_its_turn_a_bounded_time(self):
        """Another document may hold PDFIUM_LOCK for RENDER_SECONDS, and a
        folder's thread queue one after another: past PDFIUM_WAIT_SECONDS,
        refused - « réessayez », said on the file's line like a refusal."""
        from invoices.receipt_batches import READING_REFUSALS

        held, release = threading.Event(), threading.Event()

        def hold():
            with ocr.PDFIUM_LOCK:
                held.set()
                release.wait(10)

        holder = threading.Thread(target=hold)
        holder.start()
        self.addCleanup(holder.join)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(10))
        with (
            mock.patch.object(ocr, "PDFIUM_WAIT_SECONDS", 0.05),
            mock.patch.object(pdfium_sandbox, "run", never("PDFium")),
            self.assertRaises(ocr.DocumentTooBig) as refused,
        ):
            list(ocr.page_images(self.drawn()))
        said = "Un autre document est en cours d'affichage : réessayez dans un instant."
        self.assertEqual(str(refused.exception), said)
        self.assertEqual(error_for_page(refused.exception, said=READING_REFUSALS), said)
        self.assertNothingLeft()
        self.assertEqual(ocr.PDFIUM_WAIT_SECONDS, 2 * ocr.RENDER_SECONDS)

    @skipUnless(os.name == "nt", "a Job Object is Windows'")
    def test_the_child_is_in_its_job_before_it_is_given_the_pdf(self):
        """The child reads nothing of the file before the server has put it
        in the Job Object: it waits for its job on stdin, which the server
        writes once the job holds it."""
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
        events, take, popen = [], pdfium_sandbox._Job.take, pdfium_sandbox.subprocess.Popen

        def taken(job, pid):
            held = take(job, pid)
            process, inside = kernel32.OpenProcess(0x1000, False, pid), wintypes.BOOL()
            kernel32.IsProcessInJob(process, job.handle, ctypes.byref(inside))
            kernel32.CloseHandle(process)
            events.append(("job", held, bool(inside)))
            return held

        class Watched:
            def __init__(self, pipe):
                self.pipe = pipe

            def write(self, data):
                events.append(("pdf", json.loads(data)["path"]))
                return self.pipe.write(data)

            def __getattr__(self, name):
                return getattr(self.pipe, name)

        def started(*args, **kwargs):
            process = popen(*args, **kwargs)
            process.stdin = Watched(process.stdin)
            return process

        path = self.drawn()
        with (
            mock.patch.object(pdfium_sandbox._Job, "take", taken),
            mock.patch.object(pdfium_sandbox.subprocess, "Popen", started),
        ):
            list(ocr.page_images(path))
        self.assertEqual(events, [("job", True, True), ("pdf", os.path.abspath(path))])

    def test_without_a_job_object_the_clock_still_bounds_it(self):
        """Never PDFium in the server instead: a warning, and the clock."""
        with (
            mock.patch.object(pdfium_sandbox._Job, "_make", side_effect=OSError("refusé")),
            self.assertLogs("invoices.pdfium_sandbox", "WARNING") as logged,
        ):
            (image,) = list(ocr.page_images(self.drawn()))
            with mock.patch.object(ocr, "RENDER_SECONDS", 0.05), self.assertRaises(ocr.DocumentTooBig):
                list(ocr.page_images(self.drawn()))
        self.assertEqual(image.size, (2480, 3509))
        self.assertIn("borné que par le temps", "\n".join(logged.output))

    def test_the_child_s_environment_holds_no_secret(self):
        seen = {}
        popen = pdfium_sandbox.subprocess.Popen

        def recorded(*args, **kwargs):
            seen.update(kwargs["env"])
            return popen(*args, **kwargs)

        with (
            mock.patch.dict(os.environ, {"SECRET_KEY": "secret", "OPENAI_API_KEY": "cle"}),
            mock.patch.object(pdfium_sandbox.subprocess, "Popen", recorded),
        ):
            list(ocr.page_images(self.drawn()))
        self.assertTrue(seen)
        self.assertLessEqual(set(seen), set(pdfium_sandbox.ENVIRONMENT))
