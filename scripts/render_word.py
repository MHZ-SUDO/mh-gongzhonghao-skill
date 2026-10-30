"""Render DOCX in an owned, hidden, read-only Microsoft Word instance.

Windows only. Requires pywin32, lxml and PyMuPDF. The output directory must
be new or empty. Produces a QA PDF, page-N.png and meta.json; a successful
render is not a visual review. Never quits or kills a preexisting Word.
"""
from __future__ import annotations

import argparse
import csv
import gc
import hashlib
import io
import json
from pathlib import Path
import platform
import subprocess
import sys
import time
import traceback
from zipfile import ZipFile


def _word_pids() -> set[int]:
    output = subprocess.check_output(
        ["tasklist", "/FI", "IMAGENAME eq WINWORD.EXE", "/FO", "CSV", "/NH"],
        text=True, encoding="utf-8", errors="replace", timeout=10,
    )
    return {int(row[1]) for row in csv.reader(io.StringIO(output))
            if len(row) > 1 and row[0].lower() == "winword.exe"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def render_docx(input_docx, output_dir, dpi: int = 180) -> dict:
    """Export without saving source. Fail if process ownership is ambiguous."""
    if platform.system() != "Windows":
        raise RuntimeError("This renderer needs Windows and Microsoft Word COM")
    try:
        import fitz
        from lxml import etree
        import pythoncom
        import win32com.client
        import win32process
    except ImportError as exc:
        raise RuntimeError("Missing dependency: use the active Python with pywin32, lxml and PyMuPDF") from exc
    source = Path(input_docx).resolve(strict=True)
    output = Path(output_dir).resolve()
    if source.suffix.lower() != ".docx":
        raise ValueError("input must be a .docx file")
    if not isinstance(dpi, int) or dpi < 150:
        raise ValueError("dpi must be an integer of at least 150")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("Use a new or empty output directory; old page files must not contaminate QA")
    output.mkdir(parents=True, exist_ok=True)
    pdf = output / (source.stem + ".pdf")
    original_hash = _sha256(source)
    with ZipFile(source) as archive:
        if archive.testzip() is not None:
            raise ValueError("DOCX archive integrity check failed")
        root = etree.fromstring(archive.read("word/document.xml"))
    namespaces = {"m": "http://schemas.openxmlformats.org/officeDocument/2006/math"}
    meta = {
        "success": False,
        "renderer": "Microsoft Word COM DispatchEx",
        "source_docx": str(source),
        "source_sha256_before": original_hash,
        "read_only": True,
        "dpi": dpi,
        "pdf_path": str(pdf),
        "xml_math_counts": {name: len(root.xpath(f"//m:{name}", namespaces=namespaces))
                            for name in ("oMath", "oMathPara", "sSub", "sSup", "f", "nary", "eqArr")},
        "visual_review": "not_performed_by_renderer",
    }
    application = document = None
    owns_application = False
    pythoncom.CoInitialize()
    try:
        existing = _word_pids()
        meta["preexisting_word_pids"] = sorted(existing)
        application = win32com.client.DispatchEx("Word.Application")
        spawned = _word_pids() - existing
        deadline = time.monotonic() + 3
        while not spawned and time.monotonic() < deadline:
            time.sleep(.15)
            spawned = _word_pids() - existing
        meta["dispatch_ex_new_word_pids"] = sorted(spawned)
        if len(spawned) != 1:
            raise RuntimeError("Cannot identify exactly one new Word process; refusing to change or quit any preexisting instance")
        pid = next(iter(spawned))
        meta["word_instance_pid"] = pid
        owns_application = True
        application.Visible = False
        application.DisplayAlerts = 0
        application.AutomationSecurity = 3
        document = application.Documents.Open(
            FileName=str(source), ConfirmConversions=False, ReadOnly=True,
            AddToRecentFiles=False, Visible=False, OpenAndRepair=False,
            NoEncodingDialog=True,
        )
        try:
            window_pid = win32process.GetWindowThreadProcessId(document.Windows.Item(1).Hwnd)[1]
            meta["document_window_pid"] = window_pid
            if window_pid != pid:
                owns_application = False
                raise RuntimeError("Document window does not belong to new process; refusing to close or quit it")
        except AttributeError:
            # Some Word versions omit HWND. Unique DispatchEx-spawned PID is
            # the remaining ownership evidence; do not fall back to GetObject.
            meta["document_window_pid"] = "not exposed by this Word build"
        document.Repaginate()
        meta["word_omaths_count"] = int(document.OMaths.Count)
        meta["word_page_count"] = int(document.ComputeStatistics(2))
        meta["word_version"] = str(application.Version)
        document.ExportAsFixedFormat(
            OutputFileName=str(pdf), ExportFormat=17, OpenAfterExport=False,
            OptimizeFor=0, Range=0, Item=0, IncludeDocProps=False,
            KeepIRM=False, CreateBookmarks=0, DocStructureTags=True,
            BitmapMissingFonts=True, UseISO19005_1=False,
        )
        if not pdf.is_file() or not pdf.stat().st_size:
            raise RuntimeError("Word did not produce a nonempty PDF")
    except Exception:
        meta["error"] = traceback.format_exc()
        raise
    finally:
        if document is not None and owns_application:
            try:
                document.Close(SaveChanges=0)
                meta["document_closed_without_saving"] = True
            except Exception as exc:
                meta["document_close_error"] = str(exc)
        document = None
        if application is not None and owns_application:
            try:
                application.Quit(SaveChanges=0)
                meta["own_instance_quit_requested"] = True
            except Exception as exc:
                meta["word_quit_error"] = str(exc)
        application = None
        gc.collect()
        pythoncom.CoUninitialize()
        meta["source_sha256_after"] = _sha256(source)
        meta["source_sha256_unchanged"] = meta["source_sha256_after"] == original_hash
        remaining = _word_pids()
        deadline = time.monotonic() + 8
        while (owns_application and meta.get("own_instance_quit_requested")
               and meta.get("word_instance_pid") in remaining
               and time.monotonic() < deadline):
            time.sleep(.15)
            remaining = _word_pids()
        meta["word_pids_after"] = sorted(remaining)
        meta["own_instance_exit_observed"] = (
            owns_application and meta.get("word_instance_pid") not in remaining
        )
        (output / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        if not meta["source_sha256_unchanged"]:
            raise RuntimeError("The source DOCX changed during rendering")
        if not (meta.get("document_closed_without_saving") and meta.get("own_instance_exit_observed")):
            raise RuntimeError("Owned Word instance did not close cleanly; inspect meta.json, do not kill all Word processes")
        pages = []
        with fitz.open(pdf) as rendered:
            meta["pdf_page_count"] = len(rendered)
            for index, page in enumerate(rendered, 1):
                destination = output / f"page-{index}.png"
                page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), alpha=False).save(destination)
                pages.append(str(destination))
        meta["page_pngs"] = pages
        meta["word_pdf_page_counts_match"] = meta["word_page_count"] == meta["pdf_page_count"]
        if not meta["word_pdf_page_counts_match"]:
            raise RuntimeError("Word and PDF page counts differ")
        meta["success"] = True
        return meta
    except Exception:
        meta["error"] = traceback.format_exc()
        raise
    finally:
        (output / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_docx", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--dpi", type=int, default=180)
    args = parser.parse_args()
    print(json.dumps(render_docx(args.input_docx, args.output_dir, args.dpi), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
