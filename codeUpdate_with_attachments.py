#!/usr/bin/env python3
"""
End-to-end utility to:
- Extract a PO number from a vendor PDF invoice
- Look up the corresponding Purchase Order in Business Central
- Calculate remaining-to-invoice quantities/amounts per PO line
- Create a related Purchase Invoice in Business Central with those lines
- Post the Purchase Invoice
- Upload the PDF invoice as an attachment to the posted Purchase Invoice

Configuration via environment variables (preferred) or CLI flags:
  Required (one of):
    - BC_COMPANY_ID: GUID of the company in Business Central
    - or BC_COMPANY_NAME: Name of the company; script will resolve the ID

  Required:
    - BC_TENANT_ID: Azure AD tenant ID
    - BC_CLIENT_ID: Azure AD application client ID
    - BC_CLIENT_SECRET: Azure AD application client secret

  Optional:
    - BC_ENVIRONMENT: Business Central environment name (e.g. "Production", "Sandbox"). Default: Production
    - BC_BASE_URL: Base API URL. Default: https://api.businesscentral.dynamics.com

CLI:
  python codeUpdate_with_attachments.py --pdf /path/to/invoice.pdf [--po-regex "(?i)PO\s*#?\s*[:\-]?\s*([A-Z0-9\-]+)"] [--vendor-invoice-number INV123]

This script requires appropriate permissions for the application in Business Central, including read/write for purchase orders/invoices and attachments, and the ability to execute the post action.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import requests


DEFAULT_PO_REGEX = r"(?i)PO\s*(?:NUMBER|No\.|#)?\s*[:\-]?\s*([A-Z0-9\-]+)"


class BusinessCentralClient:
    """Minimal client for Business Central API v2.0 using OAuth2 client credentials."""

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        environment: str = "Production",
        base_url: str = "https://api.businesscentral.dynamics.com",
        session: Optional[requests.Session] = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.environment = environment
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self._token: Optional[str] = None

    def _get_token(self) -> str:
        if self._token:
            return self._token
        token_url = f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": "https://api.businesscentral.dynamics.com/.default",
        }
        resp = self.session.post(token_url, data=data, timeout=30)
        self._raise_for_status(resp, "Failed to acquire OAuth token")
        token = resp.json().get("access_token")
        if not token:
            raise RuntimeError("OAuth token response missing access_token")
        self._token = token
        return token

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._get_token()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if extra:
            headers.update(extra)
        return headers

    def _api_root(self) -> str:
        return f"{self.base_url}/v2.0/{self.tenant_id}/{self.environment}/api/v2.0"

    @staticmethod
    def _raise_for_status(resp: requests.Response, message: str) -> None:
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            detail = ""
            try:
                detail = json.dumps(resp.json(), indent=2)
            except Exception:
                detail = resp.text
            raise RuntimeError(f"{message}: {exc}\nResponse: {detail}")

    def get_companies(self) -> List[Dict[str, Any]]:
        url = f"{self._api_root()}/companies"
        resp = self.session.get(url, headers=self._headers(), timeout=30)
        self._raise_for_status(resp, "Failed to list companies")
        return resp.json().get("value", [])

    def resolve_company_id(self, company_id: Optional[str], company_name: Optional[str]) -> str:
        if company_id:
            return company_id
        if not company_name:
            raise ValueError("Either company_id or company_name must be provided")
        companies = self.get_companies()
        for comp in companies:
            if comp.get("name") == company_name:
                return comp["id"]
        raise RuntimeError(f"Company named '{company_name}' not found")

    def get_purchase_order_by_number(self, company_id: str, po_number: str) -> Optional[Dict[str, Any]]:
        url = f"{self._api_root()}/companies({company_id})/purchaseOrders"
        params = {"$filter": f"number eq '{po_number}'"}
        resp = self.session.get(url, headers=self._headers(), params=params, timeout=30)
        self._raise_for_status(resp, "Failed to query purchase orders")
        items = resp.json().get("value", [])
        return items[0] if items else None

    def get_purchase_order_lines(self, company_id: str, purchase_order_id: str) -> List[Dict[str, Any]]:
        url = f"{self._api_root()}/companies({company_id})/purchaseOrders({purchase_order_id})/purchaseOrderLines"
        resp = self.session.get(url, headers=self._headers(), timeout=30)
        self._raise_for_status(resp, "Failed to fetch purchase order lines")
        return resp.json().get("value", [])

    def create_purchase_invoice(self, company_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self._api_root()}/companies({company_id})/purchaseInvoices"
        resp = self.session.post(url, headers=self._headers(), data=json.dumps(payload), timeout=30)
        self._raise_for_status(resp, "Failed to create purchase invoice")
        return resp.json()

    def create_purchase_invoice_line(self, company_id: str, invoice_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self._api_root()}/companies({company_id})/purchaseInvoices({invoice_id})/purchaseInvoiceLines"
        resp = self.session.post(url, headers=self._headers(), data=json.dumps(payload), timeout=30)
        self._raise_for_status(resp, "Failed to create purchase invoice line")
        return resp.json()

    def post_purchase_invoice(self, company_id: str, invoice_id: str) -> None:
        url = f"{self._api_root()}/companies({company_id})/purchaseInvoices({invoice_id})/Microsoft.NAV.post"
        # POST with empty body
        resp = self.session.post(url, headers=self._headers(), timeout=60)
        self._raise_for_status(resp, "Failed to post purchase invoice")

    def attach_pdf_to_invoice(self, company_id: str, invoice_id: str, pdf_path: str) -> Dict[str, Any]:
        url = f"{self._api_root()}/companies({company_id})/purchaseInvoices({invoice_id})/attachments"
        with open(pdf_path, "rb") as f:
            content_b64 = base64.b64encode(f.read()).decode("utf-8")
        payload = {
            "fileName": os.path.basename(pdf_path),
            "content": content_b64,
            # Some BC versions infer mimeType from file name; include if supported by your API version
            # "mimeType": "application/pdf",
        }
        resp = self.session.post(url, headers=self._headers(), data=json.dumps(payload), timeout=60)
        self._raise_for_status(resp, "Failed to attach PDF to invoice")
        return resp.json()


def extract_po_number_from_pdf(pdf_path: str, regex_pattern: str) -> Optional[str]:
    try:
        # Lazy import to keep startup fast if PDF not used
        import PyPDF2  # type: ignore
    except Exception as exc:
        raise RuntimeError(
            "PyPDF2 is required for PDF parsing. Install dependencies from requirements.txt"
        ) from exc

    pattern = re.compile(regex_pattern)
    with open(pdf_path, "rb") as f:
        reader = PyPDF2.PdfReader(f)
        for page in reader.pages:
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            match = pattern.search(text)
            if match:
                return match.group(1).strip()
    return None


def compute_open_to_invoice_lines(po_lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Given PO lines, compute remaining quantities/amounts to invoice and map to invoice line payloads.

    For Item lines, use quantityReceived - quantityInvoiced.
    For G/L Account lines, use lineAmount - invoicedAmount if available, else include as-is once.
    Other line types (Comment, etc.) are ignored.
    """
    invoice_lines: List[Dict[str, Any]] = []

    for line in po_lines:
        line_type = (line.get("lineType") or "").strip()
        description = line.get("description")
        direct_unit_cost = line.get("directUnitCost")
        # For Item lines
        if line_type.lower() == "item":
            qty_received = float(line.get("quantityReceived") or 0)
            qty_invoiced = float(line.get("quantityInvoiced") or 0)
            open_qty = max(qty_received - qty_invoiced, 0.0)
            if open_qty <= 0:
                continue
            payload = {
                "lineType": "Item",
                "itemId": line.get("itemId"),
                "description": description,
                "quantity": open_qty,
                # Direct unit cost can be None for some lines; only include if present
            }
            if direct_unit_cost is not None:
                payload["directUnitCost"] = direct_unit_cost
            invoice_lines.append(payload)
        elif line_type.lower() in {"account", "glaccount", "g/l account"}:
            # Not all APIs expose invoicedAmount; include full amount one time if not invoiced
            line_amount = float(line.get("lineAmount") or 0)
            invoiced_amount = float(line.get("invoicedAmount") or 0)
            open_amount = max(line_amount - invoiced_amount, 0.0)
            if open_amount <= 0:
                continue
            payload = {
                "lineType": "Account",
                "accountId": line.get("accountId"),
                "description": description,
                "lineAmount": open_amount,
            }
            invoice_lines.append(payload)
        else:
            # Skip comments/charges/other line types by default
            continue

    return invoice_lines


def now_date_str() -> str:
    return dt.date.today().isoformat()


def ensure_at_least_one_line(lines: List[Dict[str, Any]]) -> None:
    if not lines:
        raise RuntimeError("No remaining PO lines to invoice. Nothing to create.")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Create and post BC purchase invoice from a PDF invoice's PO number, and attach PDF.")
    parser.add_argument("--pdf", required=True, help="Path to vendor PDF invoice")
    parser.add_argument("--po-regex", default=DEFAULT_PO_REGEX, help="Regex to extract PO number from PDF text; first capture group used as number")
    parser.add_argument("--vendor-invoice-number", default=None, help="Vendor invoice number to set on the purchase invoice; defaults to PDF file name")
    parser.add_argument("--company-id", default=os.getenv("BC_COMPANY_ID"), help="Business Central company ID (GUID)")
    parser.add_argument("--company-name", default=os.getenv("BC_COMPANY_NAME"), help="Business Central company name (used if company-id not provided)")
    parser.add_argument("--tenant-id", default=os.getenv("BC_TENANT_ID"), help="Azure AD tenant ID")
    parser.add_argument("--client-id", default=os.getenv("BC_CLIENT_ID"), help="Azure AD application (client) ID")
    parser.add_argument("--client-secret", default=os.getenv("BC_CLIENT_SECRET"), help="Azure AD client secret")
    parser.add_argument("--environment", default=os.getenv("BC_ENVIRONMENT", "Production"), help="Business Central environment name")
    parser.add_argument("--base-url", default=os.getenv("BC_BASE_URL", "https://api.businesscentral.dynamics.com"), help="Base URL for BC API")

    args = parser.parse_args(argv)

    if not os.path.isfile(args.pdf):
        print(f"PDF not found: {args.pdf}", file=sys.stderr)
        return 2

    required_env = {
        "tenant_id": args.tenant_id,
        "client_id": args.client_id,
        "client_secret": args.client_secret,
    }
    missing = [k for k, v in required_env.items() if not v]
    if missing:
        print(f"Missing required auth values: {', '.join(missing)}", file=sys.stderr)
        return 2

    client = BusinessCentralClient(
        tenant_id=args.tenant_id,
        client_id=args.client_id,
        client_secret=args.client_secret,
        environment=args.environment,
        base_url=args.base_url,
    )

    try:
        company_id = client.resolve_company_id(args.company_id, args.company_name)
    except Exception as exc:
        print(f"Failed to resolve company: {exc}", file=sys.stderr)
        return 2

    print("Extracting PO number from PDF...")
    po_number = extract_po_number_from_pdf(args.pdf, args.po_regex)
    if not po_number:
        print("Could not find a PO number in the PDF using provided regex.", file=sys.stderr)
        return 3
    print(f"Found PO number: {po_number}")

    print("Fetching purchase order...")
    po = client.get_purchase_order_by_number(company_id, po_number)
    if not po:
        print(f"Purchase Order '{po_number}' not found in Business Central.", file=sys.stderr)
        return 4

    po_id = po["id"]
    vendor_id = po.get("vendorId")
    if not vendor_id:
        print("PO vendorId is missing; cannot create invoice.", file=sys.stderr)
        return 4

    print("Fetching purchase order lines...")
    po_lines = client.get_purchase_order_lines(company_id, po_id)
    invoice_lines = compute_open_to_invoice_lines(po_lines)
    try:
        ensure_at_least_one_line(invoice_lines)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 5

    vendor_invoice_number = args.vendor_invoice_number or os.path.basename(args.pdf)

    invoice_payload = {
        "vendorId": vendor_id,
        "invoiceDate": now_date_str(),
        "postingDate": now_date_str(),
        "vendorInvoiceNumber": vendor_invoice_number,
        # currencyCode: Inherit from PO if available
    }
    if po.get("currencyCode"):
        invoice_payload["currencyCode"] = po.get("currencyCode")

    print("Creating purchase invoice header...")
    invoice = client.create_purchase_invoice(company_id, invoice_payload)
    invoice_id = invoice.get("id")
    invoice_number = invoice.get("number")
    if not invoice_id:
        print("Invoice creation returned no id.", file=sys.stderr)
        return 6
    print(f"Created draft purchase invoice: id={invoice_id}, number={invoice_number}")

    print("Creating purchase invoice lines...")
    created_count = 0
    for line in invoice_lines:
        client.create_purchase_invoice_line(company_id, invoice_id, line)
        created_count += 1

    print(f"Created {created_count} invoice lines. Posting invoice...")
    # Sometimes posting immediately after creating lines can fail due to eventual consistency; retry briefly
    last_err: Optional[Exception] = None
    for attempt in range(3):
        try:
            client.post_purchase_invoice(company_id, invoice_id)
            last_err = None
            break
        except Exception as exc:
            last_err = exc
            time.sleep(2 * (attempt + 1))
    if last_err:
        print(f"Failed to post purchase invoice: {last_err}", file=sys.stderr)
        return 7
    print("Invoice posted successfully.")

    print("Attaching PDF to invoice...")
    try:
        attach_resp = client.attach_pdf_to_invoice(company_id, invoice_id, args.pdf)
        attachment_id = attach_resp.get("id")
        print(f"Attached PDF as attachment id={attachment_id}")
    except Exception as exc:
        print(f"Warning: Failed to attach PDF: {exc}", file=sys.stderr)

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

