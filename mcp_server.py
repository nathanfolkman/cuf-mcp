"""
MCP server exposing CUF health portal as tools for Claude (and other MCP clients).

Run:
    uv run mcp dev mcp_server.py          # opens MCP inspector in browser
    uv run python mcp_server.py           # runs stdio server

Register with Claude Code (~/.claude/claude_desktop_config.json or mcp settings):
    {
      "mcpServers": {
        "cuf-health": {
          "command": "uv",
          "args": ["run", "--directory", "/home/nrf/medical-records-downloader", "python", "mcp_server.py"]
        }
      }
    }
"""

import logging
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

load_dotenv()

USERNAME = os.environ["CUF_USERNAME"]
PASSWORD = os.environ["CUF_PASSWORD"]
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "./downloads")) / "cuf"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

mcp = FastMCP("CUF Health Portal")

# Lazy-cached client (initialized on first tool call)
_client = None


async def _get_client():
    """Return a cached, authenticated CufClient (creates one if needed)."""
    global _client
    if _client is None:
        from cuf_client import CufClient
        c = CufClient(USERNAME, PASSWORD, OUTPUT_DIR)
        c._client = httpx.AsyncClient(timeout=120)
        await c.authenticate()
        _client = c
    return _client


# ── Clinical documents ──────────────────────────────────────────────────────

@mcp.tool()
async def list_clinical_documents() -> list[dict]:
    """List all clinical documents (imaging reports, cardiology, pathology, etc.).

    Returns a list of dicts with keys: id, patientId, documentType, documentDate, fileName.
    """
    c = await _get_client()
    return await c.list_clinical_documents()


@mcp.tool()
async def get_clinical_document(doc_id: str) -> str:
    """Download a specific clinical document PDF by its ID.

    Args:
        doc_id: The document ID from list_clinical_documents (e.g. "12345").

    Returns the file path where the PDF was saved.
    """
    c = await _get_client()
    path = await c.get_clinical_document(doc_id)
    return str(path)


# ── Invoices ────────────────────────────────────────────────────────────────

@mcp.tool()
async def list_invoices() -> list[dict]:
    """List all invoices from the CUF portal.

    Returns a list of dicts with keys: id, code, paymentNumber, date, status, value, externalSiteCode.
    """
    c = await _get_client()
    return await c.list_invoices()


@mcp.tool()
async def get_invoice(payment_number: str) -> str:
    """Download a specific invoice PDF by payment number.

    Args:
        payment_number: The paymentNumber from list_invoices (e.g. "CCF2026/45291" or "FT FSDR2025/170390").

    Returns the file path where the PDF was saved.
    """
    c = await _get_client()
    path = await c.get_invoice(payment_number)
    return str(path)


# ── Appointments ─────────────────────────────────────────────────────────────

@mcp.tool()
async def list_appointments(date_from: str | None = None) -> list[dict]:
    """List upcoming and recent appointments.

    Args:
        date_from: ISO date string (YYYY-MM-DD) to start from. Defaults to today.

    Returns a list of dicts with keys: id, scheduledDatetime, medicalActName, staffName,
    siteName, status, type, canCancel, canReSchedule, and more.
    """
    c = await _get_client()
    return await c.list_appointments(date_from=date_from)


@mcp.tool()
async def list_past_appointments() -> list[dict]:
    """List all past appointments (from 2000-01-01 to today).

    Returns a list of dicts with keys: id, scheduledDatetime, medicalActName, staffName,
    siteName, status, type, and more.
    """
    c = await _get_client()
    return await c.list_past_appointments()


# ── Exam results ─────────────────────────────────────────────────────────────

@mcp.tool()
async def list_exam_results() -> list[dict]:
    """List active lab/exam results available in the portal.

    Returns a list of dicts with keys: code, report, client.
    Note: This section may be empty if no results are currently available.
    """
    c = await _get_client()
    try:
        return await c.list_exam_results()
    except (RuntimeError, httpx.HTTPStatusError) as e:
        logger.error("list_exam_results failed: %s", e, exc_info=True)
        return [{"error": str(e)}]


# ── Prescriptions ─────────────────────────────────────────────────────────────

@mcp.tool()
async def list_prescriptions(years: list[int] | None = None) -> list[dict]:
    """List exam prescriptions from the CUF portal (www.cuf.pt).

    Scrapes the Drupal-rendered HTML page — prescriptions use a separate auth system
    from the GraphQL API. Returns list of dicts with: date, patient, site,
    download_url, document_id.

    Args:
        years: List of years to fetch (default: current year and 2 prior years).
    """
    c = await _get_client()
    return await c.list_prescriptions(years=years)


@mcp.tool()
async def get_prescription(download_url: str) -> str:
    """Download a prescription PDF by its download_url from list_prescriptions().

    Args:
        download_url: The download_url field from list_prescriptions() output.

    Returns the file path where the PDF was saved.
    """
    c = await _get_client()
    path = await c.get_prescription(download_url)
    return str(path)


# ── Prescription parsing ──────────────────────────────────────────────────────

def _build_prompt(text: str) -> str:
    return f"""You are a medical data extraction assistant. Extract the key fields from the following Portuguese prescription text and return ONLY valid JSON (no markdown, no explanation).

Use this exact schema:
{{
  "patient": "string",
  "date": "YYYY-MM-DD",
  "doctor": "string",
  "specialty": "string",
  "medications": [
    {{
      "name": "string",
      "dci": "string",
      "strength": "string",
      "form": "string",
      "quantity": "string",
      "posology": "string",
      "duration": "string"
    }}
  ],
  "prescription_number": "string",
  "notes": "string"
}}

Use null for missing fields. The text may be in Portuguese — handle it correctly.

PRESCRIPTION TEXT:
{text}"""


async def _parse_prescription_vision(file_path: str, model: str) -> dict:
    import fitz  # pymupdf
    import ollama, json, re, base64

    doc = fitz.open(file_path)
    images = []
    for page in doc:
        pix = page.get_pixmap(dpi=150)
        images.append(base64.b64encode(pix.tobytes("png")).decode())
    doc.close()

    if not images:
        return {"error": "Could not render PDF pages as images"}

    prompt = _build_prompt("[image-based prescription — extract from the image]")
    response = ollama.chat(
        model=model,
        messages=[{
            "role": "user",
            "content": prompt,
            "images": images,
        }],
    )
    raw = response["message"]["content"]
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if not match:
        return {"error": "No JSON in model response", "raw": raw}
    return json.loads(match.group())


@mcp.tool()
async def parse_prescription(file_path: str, model: str = "llama3.2") -> dict:
    """Parse a downloaded prescription PDF with a local Ollama model.

    Extracts text from the PDF and sends it to a local Ollama model for structured
    extraction. Falls back to vision-mode (image rendering) for image-based PDFs.

    Args:
        file_path: Path to the prescription PDF (from get_prescription).
        model: Ollama model ID to use (default: "llama3.2"; use a vision model like
               "llava" if the PDF is image-based).

    Returns structured dict with: patient, date, doctor, specialty, medications
    (list with name/dci/strength/form/quantity/posology/duration), prescription_number, notes.
    """
    import pdfplumber, ollama, json, re

    text = ""
    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            text += page.extract_text() or ""

    if len(text.strip()) < 50:
        return await _parse_prescription_vision(file_path, model)

    prompt = _build_prompt(text)
    response = ollama.chat(model=model, messages=[{"role": "user", "content": prompt}])
    raw = response["message"]["content"]

    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if not match:
        return {"error": "No JSON in model response", "raw": raw}
    return json.loads(match.group())


# ── Notifications ─────────────────────────────────────────────────────────────

@mcp.tool()
async def get_notifications(include_read: bool = True) -> list[dict]:
    """Get patient portal notifications.

    Args:
        include_read: Whether to include already-read notifications (default: True).

    Returns a list of dicts with keys: id, name, description, date, message, read, entityType, entityCode.
    """
    c = await _get_client()
    return await c.get_notifications(include_read=include_read)


# ── Patient info ──────────────────────────────────────────────────────────────

@mcp.tool()
async def get_patient_info() -> dict:
    """Get basic patient profile information (name, contacts, identity numbers).

    Returns a dict with keys: fullName, title, contacts, identity.
    """
    c = await _get_client()
    return await c.get_patient_info()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    mcp.run()


if __name__ == "__main__":
    main()
