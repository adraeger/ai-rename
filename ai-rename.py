#!/usr/bin/env python3
"""
Automatically rename PDF files using Ollama.
Extracts text, detects date and content via LLM, renames the file.
Scanned PDFs are automatically detected and processed via macOS Vision OCR.

Usage: ai-rename.py <file1.pdf> [file2.pdf ...]
"""

import sys
import os
import json
import subprocess
import urllib.request
import urllib.error
import tempfile
import re
import logging
from datetime import datetime

# Extend PATH for Quick Actions / Services (Homebrew, Swiftly)
for p in ["/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.swiftly/bin")]:
    if p not in os.environ.get("PATH", ""):
        os.environ["PATH"] = p + ":" + os.environ.get("PATH", "")

# Configuration
OLLAMA_MODEL = "qwen3.5:9b"
OLLAMA_API = "http://localhost:11434/api/chat"
MAX_TEXT_CHARS = 4000
SCAN_WORD_THRESHOLD = 0.3
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
SWIFT_BINARY = os.path.join(SCRIPT_DIR, "pdf-text-extract")
OCR_BINARY = os.path.join(SCRIPT_DIR, "pdf-ocr")
LOG_FILE = os.path.join(SCRIPT_DIR, "ai-rename.log")

# Logging (max 1 MB, keeps 1 old backup)
from logging.handlers import RotatingFileHandler
_handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=1)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logging.getLogger().addHandler(_handler)
logging.getLogger().setLevel(logging.DEBUG)


def notify(title, message):
    """Show macOS notification."""
    safe_msg = message.replace("\\", "\\\\").replace('"', '\\"')
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(
        ["osascript", "-e", f'display notification "{safe_msg}" with title "{safe_title}"'],
        capture_output=True,
    )


def compile_swift_binary(source, output_path, frameworks):
    """Compile Swift source to binary (one-time, result is cached)."""
    if os.path.isfile(output_path):
        return True

    with tempfile.NamedTemporaryFile(mode="w", suffix=".swift", delete=False) as f:
        f.write(source)
        src = f.name

    try:
        fw_args = []
        for fw in frameworks:
            fw_args += ["-framework", fw]
        r = subprocess.run(
            ["swiftc", "-O"] + fw_args + [src, "-o", output_path],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode == 0:
            return True
        r = subprocess.run(
            ["swiftc", "-O", src, "-o", output_path],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode == 0:
            return True
        sys.stderr.write(f"Swift compilation failed: {r.stderr[:300]}\n")
        return False
    finally:
        os.unlink(src)


def compile_swift_extractor():
    """Compile Swift binary for PDF text extraction (one-time)."""
    return compile_swift_binary("""\
import PDFKit
import Foundation
guard CommandLine.arguments.count > 1 else {
    fputs("Usage: pdf-text-extract <file.pdf>\\n", stderr)
    exit(1)
}
let url = URL(fileURLWithPath: CommandLine.arguments[1])
guard let doc = PDFDocument(url: url) else {
    fputs("Cannot open PDF\\n", stderr)
    exit(1)
}
print(doc.string ?? "")
""", SWIFT_BINARY, ["PDFKit", "Quartz"])


def compile_ocr_binary():
    """Compile Swift binary for Vision OCR (one-time)."""
    return compile_swift_binary("""\
import Vision
import CoreGraphics
import Foundation

guard CommandLine.arguments.count > 1 else {
    fputs("Usage: pdf-ocr <file.pdf>\\n", stderr)
    exit(1)
}

let url = URL(fileURLWithPath: CommandLine.arguments[1]) as CFURL
guard let doc = CGPDFDocument(url) else {
    fputs("Cannot open PDF\\n", stderr)
    exit(1)
}

for pageNum in 1...doc.numberOfPages {
    guard let page = doc.page(at: pageNum) else { continue }

    let box = page.getBoxRect(.mediaBox)
    let scale: CGFloat = 3.0
    let w = Int(box.width * scale)
    let h = Int(box.height * scale)

    guard let cs = CGColorSpace(name: CGColorSpace.sRGB),
          let ctx = CGContext(data: nil, width: w, height: h,
                             bitsPerComponent: 8, bytesPerRow: 0,
                             space: cs,
                             bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue)
    else { continue }

    ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
    ctx.scaleBy(x: scale, y: scale)
    ctx.drawPDFPage(page)

    guard let image = ctx.makeImage() else { continue }

    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["de-DE", "en-US"]
    request.usesLanguageCorrection = true
    request.minimumTextHeight = 0.0

    let handler = VNImageRequestHandler(cgImage: image)
    do {
        try handler.perform([request])
    } catch {
        fputs("OCR error page \\(pageNum): \\(error)\\n", stderr)
        continue
    }

    guard let results = request.results else { continue }
    for obs in results {
        if let text = obs.topCandidates(1).first?.string {
            print(text)
        }
    }
}
""", OCR_BINARY, ["Vision", "CoreGraphics"])


def is_scanned_pdf(pdf_path):
    """Check via pdffonts whether the PDF is a scan (no embedded fonts)."""
    try:
        r = subprocess.run(
            ["pdffonts", pdf_path],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            lines = [l for l in r.stdout.strip().splitlines() if l.strip()]
            has_fonts = len(lines) > 2
            logging.debug(f"pdffonts: {len(lines)-2} font(s) -> {'digital' if has_fonts else 'scan'}")
            return not has_fonts
    except FileNotFoundError:
        logging.warning("pdffonts not found")
    return False


def is_scan_garbage(text):
    """Check whether pdftotext output is scan noise (no real words)."""
    if not text.strip():
        return True
    words = text.split()
    if not words:
        return True
    real_words = sum(1 for w in words if len(re.sub(r'[^a-zA-ZäöüÄÖÜß]', '', w)) >= 3)
    ratio = real_words / len(words)
    logging.debug(f"Text quality: {real_words}/{len(words)} real words ({ratio:.0%})")
    return ratio < SCAN_WORD_THRESHOLD


def extract_text(pdf_path):
    """Extract text from PDF (pdftotext -> Swift/PDFKit fallback)."""
    logging.info(f"Extracting text: {pdf_path}")

    try:
        r = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0 and r.stdout.strip():
            logging.info(f"pdftotext OK, {len(r.stdout)} chars")
            return r.stdout.strip()
        logging.warning(f"pdftotext failed (rc={r.returncode}): {r.stderr[:200]}")
    except FileNotFoundError:
        logging.warning("pdftotext not found")

    if compile_swift_extractor():
        r = subprocess.run(
            [SWIFT_BINARY, pdf_path],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0 and r.stdout.strip():
            logging.info(f"Swift extractor OK, {len(r.stdout)} chars")
            return r.stdout.strip()
        logging.warning(f"Swift extractor failed: {r.stderr[:200]}")

    logging.error(f"No text extraction possible: {pdf_path}")
    return ""


def ocr_native(pdf_path):
    """OCR via macOS Vision framework (VNRecognizeTextRequest)."""
    if not compile_ocr_binary():
        raise RuntimeError("Failed to compile Vision OCR binary")

    logging.info(f"Starting Vision OCR: {pdf_path}")
    r = subprocess.run(
        [OCR_BINARY, pdf_path],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(f"Vision OCR failed: {r.stderr[:200]}")

    text = r.stdout.strip()
    logging.info(f"Vision OCR OK, {len(text)} chars")
    return text


def query_ollama(text_pdftotext, text_ocr, filename, num_predict=256):
    """Query Ollama for date and title (chat API, thinking disabled)."""

    if text_pdftotext and text_ocr:
        text_block = f"""Es liegen zwei Textversionen des Dokuments vor. Nutze beide zum Abgleich.
Bei Widersprüchen bevorzuge die vollständigere/klarere Version.

=== VERSION A (digital extrahiert) ===
{text_pdftotext[:MAX_TEXT_CHARS]}

=== VERSION B (optische Zeichenerkennung) ===
{text_ocr[:MAX_TEXT_CHARS]}"""
    else:
        text_block = (text_pdftotext or text_ocr or "")[:MAX_TEXT_CHARS]

    prompt = f"""Analysiere diesen Dokumenttext (Rechnung, Arztrechnung, Schreiben o.ä.).

Aufgabe:
1. Finde das Dokumentdatum (Rechnungsdatum, Ausstellungsdatum) → Format YYYY-MM-DD
2. Vergib einen kurzen deutschen Titel (2-4 Wörter)

Titel-Beispiele: "Beitragsrechnung DKV", "Arztrechnung Dr. Müller", "Stromrechnung EnBW", "Zahnarztrechnung", "KFZ-Versicherung HUK"

Der aktuelle Dateiname (Feld "Dateiname:" unten) dient als zusätzlicher Kontext:
- Thematische Hinweise (Absender, Dokumenttyp, Vertragsnummer) nutzen, wenn der Text mehrdeutig ist oder der Absender darin fehlt. Der Dateiname hat aber niedrigere Priorität als der Dokumenttext.
- Enthält er ein Datum (z.B. "07042022", "07.04.2022", "2022-04-07", "220407", "Rechnung_2022-04-07"), korrekt nach YYYY-MM-DD konvertieren — aber NUR verwenden, wenn im Dokumenttext kein Datum steht.
- Generische Teile wie "scan_001", "IMG_1234", "Dokument", "unbenannt" ignorieren.
- Datum im Dateinamen niemals erfinden oder aus unvollständigen Fragmenten (nur Jahr, nur Monat) herleiten.

Antwort NUR als JSON, ohne Erklärungen:
{{"date": "YYYY-MM-DD", "title": "Kurzer Titel"}}

Dateiname: {filename}

{text_block}"""

    logging.info(f"Sending {len(prompt)} chars to Ollama ({OLLAMA_MODEL})")

    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "think": False,
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": num_predict},
    }).encode()

    req = urllib.request.Request(
        OLLAMA_API, data=payload,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        raise ConnectionError(
            f"Ollama not reachable ({OLLAMA_API}). Is the server running? Error: {e}"
        )

    response = data.get("message", {}).get("content", "")
    logging.info(f"Ollama response ({len(response)} chars): {response[:200]}")

    match = re.search(r"\{[^{}]*\}", response)
    if match:
        result = json.loads(match.group())
        if "date" in result and "title" in result:
            return result

    raise ValueError(f"No valid JSON response: {response[:200]}")


def valid_date(d):
    """Validate date format YYYY-MM-DD."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", d)
    if not m:
        return False
    y, mo, da = int(m[1]), int(m[2]), int(m[3])
    return 1900 <= y <= 2100 and 1 <= mo <= 12 and 1 <= da <= 31


def set_file_dates(path, date_str):
    """Set modification and creation date of the file to the document date.
    date_str format: YYYY-MM-DD. Time is set to 12:00 local to avoid timezone
    edge cases shifting the calendar day. Creation date uses macOS SetFile
    (ships with Xcode Command Line Tools)."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=12)
    except ValueError:
        logging.warning(f"set_file_dates: invalid date '{date_str}'")
        return

    ts = dt.timestamp()
    try:
        os.utime(path, (ts, ts))
        logging.debug(f"mtime set to {date_str} 12:00")
    except OSError as e:
        logging.warning(f"os.utime failed: {e}")

    setfile_date = dt.strftime("%m/%d/%Y %H:%M:%S")
    try:
        r = subprocess.run(
            ["SetFile", "-d", setfile_date, path],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            logging.debug(f"creation date set to {date_str} 12:00")
        else:
            logging.warning(f"SetFile -d failed: {r.stderr[:200]}")
    except FileNotFoundError:
        logging.warning("SetFile not found (Xcode Command Line Tools required for creation date)")
    except subprocess.TimeoutExpired:
        logging.warning("SetFile timed out")


def safe_rename(src, date, title):
    """Rename file. Adds suffix (2), (3)... on conflicts.
    If the file already has the target name, returns the source path unchanged."""
    directory = os.path.dirname(src)
    ext = os.path.splitext(src)[1]

    title = re.sub(r'[/\\:*?"<>|\n\r]', "", title).strip()
    title = re.sub(r"\s+", " ", title)

    base = f"{date} {title}"
    new_path = os.path.join(directory, f"{base}{ext}")

    # Target path points to the source file itself (same inode) → nothing to do
    if os.path.exists(new_path) and os.path.samefile(src, new_path):
        return src

    n = 2
    while os.path.exists(new_path):
        new_path = os.path.join(directory, f"{base} ({n}){ext}")
        n += 1

    os.rename(src, new_path)
    return new_path


def process(filepath):
    """Process a single file: extract text -> LLM -> rename."""
    name = os.path.basename(filepath)
    text_pdftotext = ""
    text_ocr = ""

    # Step 1: pdftotext (fast, accurate for digital PDFs)
    text_pdftotext = extract_text(filepath)
    if text_pdftotext and is_scan_garbage(text_pdftotext):
        logging.info("pdftotext output is scan noise, discarding")
        text_pdftotext = ""

    # Step 2: Always run Vision OCR for cross-referencing
    try:
        text_ocr = ocr_native(filepath)
    except Exception as e:
        logging.error(f"Vision OCR failed: {e}")

    if not text_pdftotext and not text_ocr:
        notify("Error", f"No text extractable: {name}")
        return None

    # Step 3: LLM extraction with retry on JSON failure
    try:
        result = query_ollama(text_pdftotext, text_ocr, name)
    except ValueError:
        logging.info("Retrying with higher num_predict (512)")
        result = query_ollama(text_pdftotext, text_ocr, name, num_predict=512)

    date, title = result["date"], result["title"]

    if not valid_date(date):
        msg = f"Invalid date '{date}' for: {name}"
        notify("Error", msg)
        return None

    new_path = safe_rename(filepath, date, title)
    set_file_dates(new_path, date)
    new_name = os.path.basename(new_path)
    if new_name == name:
        notify("Bereits korrekt", name)
    else:
        notify("Umbenannt", f"{name} -> {new_name}")
    return new_path


def main():
    if len(sys.argv) < 2:
        print("Usage: ai-rename.py <file1.pdf> [file2.pdf ...]")
        sys.exit(1)

    ok, fail = 0, 0
    for path in sys.argv[1:]:
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            print(f"Not found: {path}")
            fail += 1
            continue
        try:
            new = process(path)
            if new:
                old_name = os.path.basename(path)
                new_name = os.path.basename(new)
                if old_name == new_name:
                    msg = f"OK  {old_name} (bereits korrekt benannt)"
                else:
                    msg = f"OK  {old_name} -> {new_name}"
                print(msg)
                logging.info(msg)
                ok += 1
            else:
                print(f"ERR {os.path.basename(path)}: processing failed")
                fail += 1
        except Exception as e:
            print(f"ERR {os.path.basename(path)}: {e}")
            logging.error(f"{os.path.basename(path)}: {e}", exc_info=True)
            notify("Error", str(e)[:100])
            fail += 1

    if ok + fail > 1:
        print(f"\n{ok} OK, {fail} errors")


if __name__ == "__main__":
    main()
