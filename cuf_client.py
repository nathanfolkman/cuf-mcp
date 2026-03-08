"""
Reusable async CUF API client extracted from download_cuf.py.
"""

import base64
import json
import re
import uuid
from pathlib import Path

import httpx

GRAPHQL_URL = "https://www.saudecuf.pt/mycuf/graphql"
LOGIN_URL = "https://www.saudecuf.pt/mycuf/login"

_HEADERS = {
    "content-type": "application/json",
    "x-channel-type-id": "1",
    "origin": "https://www.saudecuf.pt",
    "referer": LOGIN_URL,
    "user-agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    ),
    "accept": "*/*",
}


def sanitize(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name).strip() or "unnamed"


class CufClient:
    def __init__(self, username: str, password: str, out_dir: Path):
        self.username = username
        self.password = password
        self.out_dir = out_dir
        self._client: httpx.AsyncClient | None = None
        self.token: str = ""
        self.patient_id: str = ""
        self.internal_code: str = ""
        self.user_code: str = ""
        self._cuf_pt_logged_in: bool = False

    async def __aenter__(self) -> "CufClient":
        self._client = httpx.AsyncClient(timeout=120)
        await self.authenticate()
        return self

    async def __aexit__(self, *args) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _gql(self, operation: str, query: str, variables: dict) -> dict:
        assert self._client is not None, "CufClient must be used as async context manager"
        headers = {
            **_HEADERS,
            "authorization": f"Bearer {self.token}" if self.token else "",
            "x-transaction-id": str(uuid.uuid4()),
        }
        resp = await self._client.post(
            GRAPHQL_URL,
            json={"operationName": operation, "variables": variables, "query": query},
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("data") is None and "errors" in data:
            raise RuntimeError(f"{operation} error: {data['errors']}")
        return data.get("data", {})

    async def authenticate(self) -> None:
        """Authenticate and store token + patient IDs."""
        data = await self._gql(
            "authenticationMutation",
            "mutation authenticationMutation($authenticationInput: AuthenticationInput!) {"
            "  authenticationMutation(authenticationInput: $authenticationInput) {"
            "    sessionId accessToken userLogin"
            "  }"
            "}",
            {"authenticationInput": {"username": self.username, "password": self.password, "channel": "web"}},
        )
        self.token = data["authenticationMutation"]["accessToken"]

        payload_b64 = self.token.split(".")[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.b64decode(payload_b64))
        self.patient_id = payload.get("patientId", "")
        self.internal_code = payload.get("internalPatientCode", "0006080719")
        self.user_code = payload.get("usercode", "")

    async def _gql_authed(self, operation: str, query: str, variables: dict) -> dict:
        """Execute GQL, re-authenticating once on auth errors."""
        try:
            return await self._gql(operation, query, variables)
        except (RuntimeError, httpx.HTTPStatusError) as e:
            if "401" in str(e) or "Unauthorized" in str(e) or "token" in str(e).lower():
                await self.authenticate()
                return await self._gql(operation, query, variables)
            raise

    # ── Clinical documents ──────────────────────────────────────────────────

    async def list_clinical_documents(self) -> list[dict]:
        data = await self._gql_authed(
            "getPatientClinicalDocuments",
            "query getPatientClinicalDocuments($data: ClinicalDocumentInput!) {"
            "  getPatientClinicalDocuments(data: $data) {"
            "    id patientId documentType documentDate fileName"
            "  }"
            "}",
            {"data": {"patientId": self.patient_id}},
        )
        return data.get("getPatientClinicalDocuments") or []

    async def get_clinical_document(self, doc_id: str) -> Path:
        """Download a clinical document PDF; returns path to saved file."""
        # First get listing to find doc metadata
        docs = await self.list_clinical_documents()
        doc = next((d for d in docs if d["id"] == doc_id), None)

        doc_type = sanitize((doc or {}).get("documentType") or "document")
        doc_date = ((doc or {}).get("documentDate") or "")[:10].replace(":", "-")
        filename = f"{doc_date}_{doc_type}.pdf"

        section_dir = self.out_dir / "documentos_clinicos"
        section_dir.mkdir(parents=True, exist_ok=True)
        save_path = section_dir / filename
        if save_path.exists():
            suffix = doc_id[-8:].replace("/", "_")
            save_path = section_dir / f"{doc_date}_{doc_type}_{suffix}.pdf"

        data = await self._gql_authed(
            "getPatientClinicalDocumentFile",
            "query getPatientClinicalDocumentFile($data: ClinicalDocumentFileInput!) {"
            "  getPatientClinicalDocumentFile(data: $data) {"
            "    fileName fileBase64 fileDate"
            "  }"
            "}",
            {"data": {"patientId": self.patient_id, "documentId": doc_id}},
        )
        file_obj = data.get("getPatientClinicalDocumentFile") or {}
        b64 = file_obj.get("fileBase64", "")
        if not b64:
            raise RuntimeError(f"No file data returned for document {doc_id}")

        pdf_bytes = base64.b64decode(b64)
        save_path.write_bytes(pdf_bytes)
        return save_path

    # ── Invoices ────────────────────────────────────────────────────────────

    async def list_invoices(self) -> list[dict]:
        data = await self._gql_authed(
            "getPendingInvoices",
            "query getPendingInvoices($data: PendingInvoicesInput!) {"
            "  getPendingInvoices(data: $data) {"
            "    invoicesList { id code paymentNumber date status value externalSiteCode }"
            "  }"
            "}",
            {
                "data": {
                    "client": [self.internal_code],
                    "skip": 1,
                    "take": 500,
                    "since": None,
                    "until": None,
                    "status": "balanced",
                    "date": "DESC",
                    "paymentNumber": "null",
                    "isMulticoreRequest": True,
                }
            },
        )
        return (data.get("getPendingInvoices") or {}).get("invoicesList") or []

    async def get_invoice(self, payment_number: str) -> Path:
        """Download an invoice PDF by payment number; returns path to saved file."""
        parts = payment_number.rsplit("/", 1)
        if len(parts) != 2:
            raise ValueError(f"Unexpected paymentNumber format: {payment_number!r}")
        doc_series, doc_number = parts[0].strip(), parts[1].strip()

        section_dir = self.out_dir / "faturas"
        section_dir.mkdir(parents=True, exist_ok=True)

        data = await self._gql_authed(
            "getInvoice",
            "query getInvoice($data: GetInvoiceInput!) {"
            "  getInvoice(data: $data) { message data name }"
            "}",
            {"data": {"docSeries": doc_series, "docNumber": doc_number, "patientId": self.patient_id}},
        )
        inv_obj = data.get("getInvoice") or {}
        b64 = inv_obj.get("data", "")
        if not b64:
            msg = inv_obj.get("message", "")
            raise RuntimeError(f"No PDF data for {payment_number}: {msg}")

        inv_date = ""
        # Try to get date from invoice listing
        try:
            invoices = await self.list_invoices()
            inv = next((i for i in invoices if i.get("paymentNumber") == payment_number), None)
            if inv:
                inv_date = (inv.get("date") or "")[:10]
        except Exception:
            pass

        pdf_bytes = base64.b64decode(b64)
        server_name = inv_obj.get("name", "")
        if server_name:
            filename = sanitize(server_name)
            if not filename.endswith(".pdf"):
                filename += ".pdf"
            if inv_date:
                filename = f"{inv_date}_{filename}"
        else:
            filename = f"{inv_date}_{sanitize(payment_number)}.pdf" if inv_date else f"{sanitize(payment_number)}.pdf"

        save_path = section_dir / filename
        save_path.write_bytes(pdf_bytes)
        return save_path

    # ── Appointments ────────────────────────────────────────────────────────

    async def list_appointments(self, date_from: str | None = None) -> list[dict]:
        """List appointment history. date_from defaults to today if None."""
        from datetime import date
        if date_from is None:
            date_from = date.today().isoformat()

        data = await self._gql_authed(
            "getAppointmentHistoryV2",
            """query getAppointmentHistoryV2($appointmentHistory: AppointmentHistoryV2Input!) {
  getAppointmentHistoryV2(data: $appointmentHistory) {
    appointmentHistory {
      id cardNumber code procedure procedureGroup duration creationDate
      scheduledDatetime site speciality staff client type status report
      endDatetime source startDatetime appointmentNumber comments
      medicalActName staffName siteName canCancel canReSchedule canReBook
    }
  }
}""",
            {
                "appointmentHistory": {
                    "appointmentType": None,
                    "client": [{"patientId": self.patient_id, "internalPatientCode": self.internal_code}],
                    "skip": 1,
                    "take": 500,
                    "dateFrom": date_from,
                    "dateTo": None,
                    "isMulticoreRequest": True,
                    "status": None,
                    "withCabinet": False,
                }
            },
        )
        return (data.get("getAppointmentHistoryV2") or {}).get("appointmentHistory") or []

    async def list_past_appointments(self) -> list[dict]:
        """List all past appointments (from 2000-01-01 to today)."""
        from datetime import date
        data = await self._gql_authed(
            "getAppointmentHistoryV2",
            """query getAppointmentHistoryV2($appointmentHistory: AppointmentHistoryV2Input!) {
  getAppointmentHistoryV2(data: $appointmentHistory) {
    appointmentHistory {
      id cardNumber code procedure procedureGroup duration creationDate
      scheduledDatetime site speciality staff client type status report
      endDatetime source startDatetime appointmentNumber comments
      medicalActName staffName siteName canCancel canReSchedule canReBook
    }
  }
}""",
            {
                "appointmentHistory": {
                    "appointmentType": None,
                    "client": [{"patientId": self.patient_id, "internalPatientCode": self.internal_code}],
                    "skip": 1,
                    "take": 500,
                    "dateFrom": "2000-01-01",
                    "dateTo": date.today().isoformat(),
                    "isMulticoreRequest": True,
                    "status": None,
                    "withCabinet": False,
                }
            },
        )
        return (data.get("getAppointmentHistoryV2") or {}).get("appointmentHistory") or []

    # ── Exam results ────────────────────────────────────────────────────────

    async def list_exam_results(self) -> list[dict]:
        data = await self._gql_authed(
            "getActiveExamResults",
            "query getActiveExamResults($isMultiCoreRequestInput: Boolean) {"
            "  getActiveExamResults(isMultiCoreRequestInput: $isMultiCoreRequestInput) {"
            "    code report client"
            "  }"
            "}",
            {"isMultiCoreRequestInput": True},
        )
        return data.get("getActiveExamResults") or []

    # ── Prescriptions (www.cuf.pt Drupal, separate auth) ────────────────────

    _CUF_PT_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    )
    _CUF_PT_BASE = "https://www.cuf.pt"

    async def _cuf_pt_login(self) -> None:
        """Login to www.cuf.pt (Drupal) and store session cookie on self._client.
        No-op if already logged in this session.
        """
        if self._cuf_pt_logged_in:
            return
        resp = await self._client.get(
            f"{self._CUF_PT_BASE}/mycuf/login",
            headers={"user-agent": self._CUF_PT_UA, "accept": "text/html"},
            follow_redirects=True,
        )
        m = re.search(r'name="form_build_id"\s+value="([^"]+)"', resp.text)
        if not m:
            raise RuntimeError("Could not find Drupal form_build_id on cuf.pt login page")
        form_build_id = m.group(1)
        resp2 = await self._client.post(
            f"{self._CUF_PT_BASE}/mycuf/login",
            data={
                "name": self.username,
                "pass": self.password,
                "op": "ENTRAR",
                "auth_type": "password",
                "id_token": "",
                "refresh_token": "",
                "firebase_uid": "",
                "form_build_id": form_build_id,
                "form_id": "mycuf_login_form",
            },
            headers={
                "user-agent": self._CUF_PT_UA,
                "content-type": "application/x-www-form-urlencoded",
                "origin": self._CUF_PT_BASE,
                "referer": f"{self._CUF_PT_BASE}/mycuf/login",
                "accept": "text/html",
            },
            follow_redirects=False,
        )
        if resp2.status_code not in (302, 303):
            raise RuntimeError(f"cuf.pt login failed: HTTP {resp2.status_code}")
        self._cuf_pt_logged_in = True

    async def list_prescriptions(self, years: list[int] | None = None) -> list[dict]:
        """List exam prescriptions from www.cuf.pt (Drupal portal).

        Scrapes HTML — prescriptions are server-side rendered, not via GraphQL.
        Returns list of dicts: date, patient, site, download_url, document_id.
        """
        from datetime import date as _date
        if years is None:
            current_year = _date.today().year
            years = list(range(current_year, current_year - 3, -1))

        await self._cuf_pt_login()

        results = []
        for year in years:
            resp = await self._client.get(
                f"{self._CUF_PT_BASE}/mycuf/documents/exam-prescriptions"
                f"?patient={self.patient_id}&year={year}",
                headers={
                    "user-agent": self._CUF_PT_UA,
                    "accept": "text/html",
                    "referer": f"{self._CUF_PT_BASE}/mycuf/home",
                },
                follow_redirects=True,
            )
            # Each prescription is a .card.mb-3 block; split on the opening tag
            for card in re.split(r'(?=<div class="card mb-3">)', resp.text)[1:]:
                date_m = re.search(r'class="date[^"]*"[^>]*>.*?(\d{4}-\d{2}-\d{2})', card, re.DOTALL)
                patient_m = re.search(r'class="[^"]*client-name[^"]*"[^>]*>.*?</i>([^<]+)', card, re.DOTALL)
                site_m = re.search(r'icon-map-dark[^<]+</i>([^<]+)', card, re.DOTALL)
                href_m = re.search(r'href="(/mycuf/download/report/[^"]+)"', card)
                if not href_m:
                    continue
                download_path = href_m.group(1)
                # Decode the base64 token to get document_id
                token = download_path.split("/")[-1]
                try:
                    pad = token + "=" * (4 - len(token) % 4)
                    token_data = json.loads(base64.b64decode(pad.replace("%3D", "=").replace("%2B", "+").replace("%2F", "/")))
                    doc_id = token_data.get("documentId", "")
                except Exception:
                    doc_id = ""
                results.append({
                    "date": date_m.group(1).strip() if date_m else "",
                    "patient": patient_m.group(1).strip() if patient_m else "",
                    "site": site_m.group(1).strip() if site_m else "",
                    "download_url": f"{self._CUF_PT_BASE}{download_path}",
                    "document_id": doc_id,
                })
        return results

    async def get_prescription(self, download_url: str) -> Path:
        """Download a prescription PDF by its download_url from list_prescriptions().

        Returns path to saved file.
        """
        await self._cuf_pt_login()

        section_dir = self.out_dir / "prescricoes"
        section_dir.mkdir(parents=True, exist_ok=True)

        resp = await self._client.get(
            download_url,
            headers={
                "user-agent": self._CUF_PT_UA,
                "accept": "application/pdf,*/*",
                "referer": f"{self._CUF_PT_BASE}/mycuf/documents/exam-prescriptions",
            },
            follow_redirects=True,
        )
        resp.raise_for_status()

        # Derive filename from Content-Disposition, or decode the URL token
        cd = resp.headers.get("content-disposition", "")
        fn_m = re.search(r'filename="?([^";]+)"?', cd)
        if fn_m:
            filename = sanitize(fn_m.group(1).strip())
            if not filename.endswith(".pdf"):
                filename += ".pdf"
        else:
            # Decode the base64 token to get documentId for a stable filename
            try:
                token = download_url.split("/")[-1]
                pad = token.replace("%3D", "=").replace("%2B", "+").replace("%2F", "/")
                pad += "=" * (4 - len(pad) % 4)
                token_data = json.loads(base64.b64decode(pad))
                doc_id = sanitize(token_data.get("documentId", token[:16]))
            except Exception:
                doc_id = sanitize(download_url.split("/")[-1][:20])
            filename = f"prescription_{doc_id}.pdf"

        save_path = section_dir / filename
        save_path.write_bytes(resp.content)
        return save_path

    # ── Notifications ───────────────────────────────────────────────────────

    async def get_notifications(self, include_read: bool = True, take: int = 50) -> list[dict]:
        data = await self._gql_authed(
            "getNotifications",
            """query getNotifications($notification: NotificationsInput!) {
  getNotifications(data: $notification) {
    id code name description order recipient date message
    entityType entityCode read variables
  }
}""",
            {
                "notification": {
                    "userCode": self.user_code,
                    "includeReaded": include_read,
                    "skip": 1,
                    "take": take,
                }
            },
        )
        return data.get("getNotifications") or []

    # ── Patient info ────────────────────────────────────────────────────────

    async def get_patient_info(self) -> dict:
        data = await self._gql_authed(
            "GetPatient",
            """query GetPatient($patientId: String!) {
  getPatient(patientId: $patientId) {
    fullName title
    contacts { contactType contactPurpose contactValue contactRank contactActive }
    identity {
      id patientId internalPatientCode fiscalNumber healthCareNumber nationality
    }
  }
}""",
            {"patientId": self.patient_id},
        )
        return data.get("getPatient") or {}
