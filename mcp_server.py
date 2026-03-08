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
