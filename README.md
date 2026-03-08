# CUF Health Portal MCP Server

An MCP (Model Context Protocol) server that exposes the [CUF](https://www.saudecuf.pt) health portal as tools for Claude and other MCP clients.

## Tools

| Tool | Description |
|------|-------------|
| `get_patient_info` | Patient profile (name, contacts, NIF, SNS number) |
| `list_appointments` | Upcoming appointments |
| `list_past_appointments` | Full appointment history |
| `list_clinical_documents` | Imaging reports, lab reports, clinical files, etc. |
| `get_clinical_document` | Download a clinical document PDF by ID |
| `list_exam_results` | Active lab/exam results from the portal |
| `list_invoices` | All invoices |
| `get_invoice` | Download an invoice PDF by payment number |
| `list_prescriptions` | Exam prescriptions (scrapes www.cuf.pt) |
| `get_prescription` | Download a prescription PDF by download URL |

Downloaded PDFs are saved to `./downloads/cuf/` by default (configurable via `OUTPUT_DIR` env var).

## Setup

### Requirements

- [uv](https://docs.astral.sh/uv/)
- A CUF portal account (saudecuf.pt)

### Install

```bash
git clone <repo>
cd cuf-mcp
uv sync
```

### Configure credentials

Create a `.env` file:

```env
CUF_USERNAME=your.email@example.com
CUF_PASSWORD=yourpassword
OUTPUT_DIR=./downloads   # optional
```

### Register with Claude Code

Add to `~/.claude/claude_desktop_config.json` (or your MCP settings):

```json
{
  "mcpServers": {
    "cuf-health": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/cuf-mcp", "python", "mcp_server.py"]
    }
  }
}
```

Then run `/mcp` in Claude Code to connect.

### Run standalone (MCP Inspector)

```bash
uv run mcp dev mcp_server.py
```

### Bulk download all records

```bash
uv run python download_cuf.py
```

Downloads all clinical documents and invoices to `./downloads/cuf/`.

## Architecture

### Authentication

The portal uses two separate auth systems:

- **`saudecuf.pt` GraphQL API** — JWT bearer token obtained via `authenticationMutation`. The JWT contains `patientId`, `usercode`, and `internalPatientCode`. Headers require `x-channel-type-id: 1` and a per-request `x-transaction-id` UUID.

- **`cuf.pt` Drupal (prescriptions only)** — Separate Drupal session cookie (`SSESS*`) obtained by POSTing to `https://www.cuf.pt/mycuf/login`. Prescriptions are server-rendered HTML, not available via GraphQL.

### Key files

- `mcp_server.py` — FastMCP server; defines all 10 tools; lazy-initializes a shared authenticated `CufClient`
- `cuf_client.py` — Async `CufClient` class; wraps all GraphQL queries and the prescription scraper
- `download_cuf.py` — Standalone bulk downloader script

### WAF note

The portal runs behind a Barracuda WAF that blocks headless Chromium. Plain `httpx` with browser-like headers works fine.

## Notes

- `list_exam_results` currently returns an empty list — the `getActiveExamResults` GraphQL endpoint requires a separate auth scope not present in the standard JWT.
- Prescriptions use `internalPatientCode` as the client identifier, not `patientId`.
- Invoice `paymentNumber` format: `"CCF2026/45291"` → `docSeries="CCF2026"`, `docNumber="45291"`.
