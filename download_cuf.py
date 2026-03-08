"""
Download all medical records from MyCUF portal (https://www.saudecuf.pt/mycuf).
Covers: clinical/imaging documents, invoices.
(Exam results and prescriptions are empty in this account's portal.)

Usage:
    uv run download_cuf.py [--out DIR]

Credentials are read from .env (CUF_USERNAME, CUF_PASSWORD) or prompted.

Strategy: pure httpx GraphQL API calls — no browser needed.
The WAF blocks headless Chromium but allows httpx with browser-like headers.
"""

import asyncio
import getpass
import os
from pathlib import Path

from dotenv import load_dotenv

from cuf_client import CufClient, sanitize

load_dotenv()


async def download_clinical_documents(client: CufClient) -> int:
    section_dir = client.out_dir / "documentos_clinicos"
    section_dir.mkdir(parents=True, exist_ok=True)

    print("\n[CUF] Fetching clinical documents list...")
    docs = await client.list_clinical_documents()
    print(f"[CUF] Found {len(docs)} clinical document(s).")

    downloaded = 0
    for doc in docs:
        doc_id = doc["id"]
        doc_type = sanitize(doc.get("documentType") or "document")
        doc_date = (doc.get("documentDate") or "")[:10].replace(":", "-")
        filename = f"{doc_date}_{doc_type}.pdf"

        save_path = section_dir / filename
        if save_path.exists():
            suffix = doc_id[-8:].replace("/", "_")
            save_path = section_dir / f"{doc_date}_{doc_type}_{suffix}.pdf"

        try:
            path = await client.get_clinical_document(doc_id)
            # get_clinical_document already saves; just rename if needed
            if path != save_path and not save_path.exists():
                path.rename(save_path)
                path = save_path
            downloaded += 1
            pdf_size = path.stat().st_size // 1024
            print(f"  [CUF] Saved: {path.name} ({pdf_size} KB)")
        except Exception as e:
            print(f"  [CUF] Failed {filename}: {e}")

    return downloaded


async def download_invoices(client: CufClient) -> int:
    section_dir = client.out_dir / "faturas"
    section_dir.mkdir(parents=True, exist_ok=True)

    print("\n[CUF] Fetching invoices list...")
    invoices = await client.list_invoices()
    print(f"[CUF] Found {len(invoices)} invoice(s).")

    downloaded = 0
    for inv in invoices:
        payment_number = inv.get("paymentNumber", "")
        inv_value = inv.get("value", "")

        parts = payment_number.rsplit("/", 1)
        if len(parts) != 2:
            print(f"  [CUF] Skipped (unexpected paymentNumber format): {payment_number!r}")
            continue

        try:
            path = await client.get_invoice(payment_number)
            downloaded += 1
            pdf_size = path.stat().st_size // 1024
            print(f"  [CUF] Saved: {path.name} ({pdf_size} KB, €{inv_value})")
        except Exception as e:
            print(f"  [CUF] Failed {payment_number}: {e}")

    return downloaded


async def run(username: str, password: str, out_dir: Path) -> None:
    async with CufClient(username, password, out_dir) as client:
        print(f"[CUF] Authenticated. patientId={client.patient_id}")
        total = 0
        total += await download_clinical_documents(client)
        total += await download_invoices(client)
        print(f"\n[CUF] Done. Total files downloaded: {total}")
        print(f"[CUF] Saved to: {out_dir.resolve()}")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Download MyCUF medical records")
    parser.add_argument("--out", default=None, help="Output directory (default: ./downloads/cuf)")
    args = parser.parse_args()

    username = os.getenv("CUF_USERNAME") or input("CUF username (email): ").strip()
    password = os.getenv("CUF_PASSWORD") or getpass.getpass("CUF password: ")

    base_out = Path(os.getenv("OUTPUT_DIR", "./downloads"))
    out_dir = Path(args.out) if args.out else base_out / "cuf"
    out_dir.mkdir(parents=True, exist_ok=True)

    asyncio.run(run(username, password, out_dir))


if __name__ == "__main__":
    main()
