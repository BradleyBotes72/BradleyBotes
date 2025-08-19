import base64
import json
import os
import sys
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import requests


class BusinessCentralApiError(Exception):
    pass


@dataclass
class BusinessCentralConfig:
    tenant_id: str
    client_id: str
    client_secret: str
    environment: str
    company_id: str
    # Example: https://api.businesscentral.dynamics.com
    resource_url: str = "https://api.businesscentral.dynamics.com"
    api_version: str = "v2.0"

    def base_api_url(self) -> str:
        return f"{self.resource_url}/{self.api_version}/{self.tenant_id}/{self.environment}/api/v2.0"


class BusinessCentralClient:
    def __init__(self, config: BusinessCentralConfig, session: Optional[requests.Session] = None) -> None:
        self.config = config
        self.session = session or requests.Session()
        self._access_token: Optional[str] = None

    # --- Auth ---
    def authenticate(self) -> None:
        token_url = f"https://login.microsoftonline.com/{self.config.tenant_id}/oauth2/v2.0/token"
        scope = f"{self.config.resource_url}/.default"
        data = {
            "grant_type": "client_credentials",
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
            "scope": scope,
        }
        response = self.session.post(token_url, data=data)
        if response.status_code != 200:
            raise BusinessCentralApiError(f"Auth failed: {response.status_code} {response.text}")
        payload = response.json()
        self._access_token = payload.get("access_token")
        if not self._access_token:
            raise BusinessCentralApiError("Auth failed: access_token missing in response")

    # --- HTTP helpers ---
    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        if not self._access_token:
            self.authenticate()
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
        }
        if extra:
            headers.update(extra)
        return headers

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        response = self.session.request(method, url, timeout=60, **kwargs)
        if response.status_code >= 400:
            raise BusinessCentralApiError(f"{method} {url} -> {response.status_code}: {response.text}")
        return response

    # --- Purchase Orders ---
    def find_purchase_order_by_number(self, po_number: str) -> Optional[Dict[str, Any]]:
        base_url = self.config.base_api_url()
        url = f"{base_url}/companies({self.config.company_id})/purchaseOrders?$filter=number eq '{po_number}'"
        response = self._request("GET", url, headers=self._headers())
        items = response.json().get("value", [])
        return items[0] if items else None

    def get_purchase_order_lines(self, purchase_order_id: str) -> List[Dict[str, Any]]:
        base_url = self.config.base_api_url()
        url = f"{base_url}/companies({self.config.company_id})/purchaseOrders({purchase_order_id})/purchaseOrderLines"
        response = self._request("GET", url, headers=self._headers())
        return response.json().get("value", [])

    # --- Purchase Invoices ---
    def create_purchase_invoice(self, invoice_body: Dict[str, Any]) -> Dict[str, Any]:
        base_url = self.config.base_api_url()
        url = f"{base_url}/companies({self.config.company_id})/purchaseInvoices"
        response = self._request("POST", url, headers=self._headers({"Content-Type": "application/json"}), json=invoice_body)
        return response.json()

    def create_purchase_invoice_line(self, purchase_invoice_id: str, line_body: Dict[str, Any]) -> Dict[str, Any]:
        base_url = self.config.base_api_url()
        url = f"{base_url}/companies({self.config.company_id})/purchaseInvoices({purchase_invoice_id})/purchaseInvoiceLines"
        response = self._request("POST", url, headers=self._headers({"Content-Type": "application/json"}), json=line_body)
        return response.json()

    def post_purchase_invoice(self, purchase_invoice_id: str) -> None:
        base_url = self.config.base_api_url()
        url = f"{base_url}/companies({self.config.company_id})/purchaseInvoices({purchase_invoice_id})/Microsoft.NAV.post"
        self._request("POST", url, headers=self._headers())

    def attach_pdf_to_purchase_invoice(self, purchase_invoice_id: str, pdf_path: str, file_name: Optional[str] = None) -> Dict[str, Any]:
        if not os.path.isfile(pdf_path):
            raise FileNotFoundError(f"PDF not found: {pdf_path}")
        base_url = self.config.base_api_url()
        url = f"{base_url}/companies({self.config.company_id})/purchaseInvoices({purchase_invoice_id})/attachments"

        with open(pdf_path, "rb") as f:
            content_b64 = base64.b64encode(f.read()).decode("utf-8")

        payload = {
            "fileName": file_name or os.path.basename(pdf_path),
            "contentType": "application/pdf",
            "content": content_b64,
        }
        response = self._request(
            "POST",
            url,
            headers=self._headers({"Content-Type": "application/json"}),
            json=payload,
        )
        return response.json()

    def find_posted_purchase_invoice_by_vendor_invoice_number(self, vendor_invoice_number: str) -> Optional[Dict[str, Any]]:
        base_url = self.config.base_api_url()
        # Escape single quotes for OData filter
        vin_safe = vendor_invoice_number.replace("'", "''")
        url = f"{base_url}/companies({self.config.company_id})/postedPurchaseInvoices?$filter=vendorInvoiceNumber eq '{vin_safe}'"
        response = self._request("GET", url, headers=self._headers())
        items = response.json().get("value", [])
        return items[0] if items else None

    def attach_pdf_to_posted_purchase_invoice(self, posted_purchase_invoice_id: str, pdf_path: str, file_name: Optional[str] = None) -> Dict[str, Any]:
        if not os.path.isfile(pdf_path):
            raise FileNotFoundError(f"PDF not found: {pdf_path}")
        base_url = self.config.base_api_url()
        url = f"{base_url}/companies({self.config.company_id})/postedPurchaseInvoices({posted_purchase_invoice_id})/attachments"

        with open(pdf_path, "rb") as f:
            content_b64 = base64.b64encode(f.read()).decode("utf-8")

        payload = {
            "fileName": file_name or os.path.basename(pdf_path),
            "contentType": "application/pdf",
            "content": content_b64,
        }
        response = self._request(
            "POST",
            url,
            headers=self._headers({"Content-Type": "application/json"}),
            json=payload,
        )
        return response.json()

    # --- Utilities ---
    @staticmethod
    def compute_po_balance_and_lines(po_lines: List[Dict[str, Any]]) -> Tuple[float, List[Dict[str, Any]]]:
        total = 0.0
        for line in po_lines:
            line_amount = float(line.get("lineAmount", 0) or 0)
            total += line_amount
        return total, po_lines


def map_po_line_to_invoice_line(po_line: Dict[str, Any]) -> Dict[str, Any]:
    # Map a purchase order line to a purchase invoice line body.
    # The fields available vary by line type. We will map core fields that generally exist.
    invoice_line: Dict[str, Any] = {}

    # Preserve type-specific fields when available
    if "lineType" in po_line:
        invoice_line["lineType"] = po_line["lineType"]

    # Item/G/L/Resource identifiers
    for key in ("itemId", "accountId", "resourceId", "itemVariantId"):
        if key in po_line and po_line[key]:
            invoice_line[key] = po_line[key]

    # Use same number if provided
    if "lineObjectNumber" in po_line and po_line["lineObjectNumber"]:
        invoice_line["lineObjectNumber"] = po_line["lineObjectNumber"]

    # Quantities and pricing
    for key in ("quantity", "directUnitCost", "discountAmount", "discountPercent"):
        if key in po_line and po_line[key] is not None:
            invoice_line[key] = po_line[key]

    # Fallback description
    if po_line.get("description"):
        invoice_line["description"] = po_line["description"]

    return invoice_line


def process_invoice(
    bc: BusinessCentralClient,
    invoice_payload: Dict[str, Any],
    pdf_path: Optional[str] = None,
) -> Dict[str, Any]:
    # 1) Determine PO number from invoice payload
    po_number = (
        invoice_payload.get("poNumber")
        or invoice_payload.get("purchaseOrderNumber")
        or invoice_payload.get("orderNumber")
    )
    if not po_number:
        raise ValueError("Invoice payload does not include a PO number (poNumber/purchaseOrderNumber/orderNumber)")

    # 2) Find PO in BC
    po = bc.find_purchase_order_by_number(po_number)
    if not po:
        raise BusinessCentralApiError(f"Purchase Order not found in BC for number: {po_number}")

    # 3) Get PO lines and compute balance
    po_lines = bc.get_purchase_order_lines(po["id"])
    po_balance, _ = bc.compute_po_balance_and_lines(po_lines)

    # 4) Create purchase invoice using vendor + invoice meta and mapped lines
    invoice_body: Dict[str, Any] = {
        # Required/typical fields
        "invoiceDate": invoice_payload.get("invoiceDate") or date.today().isoformat(),
        "postingDate": invoice_payload.get("postingDate") or date.today().isoformat(),
        "dueDate": invoice_payload.get("dueDate"),
        "vendorInvoiceNumber": invoice_payload.get("vendorInvoiceNumber") or invoice_payload.get("invoiceNumber"),
        # One of vendorId or vendorNumber should be provided
        "vendorId": invoice_payload.get("vendorId"),
        "vendorNumber": invoice_payload.get("vendorNumber"),
        "currencyCode": invoice_payload.get("currencyCode"),
    }

    created = bc.create_purchase_invoice(invoice_body)
    created_invoice_id = created.get("id")
    if not created_invoice_id:
        raise BusinessCentralApiError(f"Failed to create purchase invoice: {json.dumps(created)}")

    # 5) Create invoice lines based on PO lines
    for po_line in po_lines:
        # Skip comment/blank lines if any
        if not po_line.get("lineType") and not po_line.get("description"):
            continue
        line_body = map_po_line_to_invoice_line(po_line)
        bc.create_purchase_invoice_line(created_invoice_id, line_body)

    # 6) Post invoice
    bc.post_purchase_invoice(created_invoice_id)

    # 7) Attach PDF to the posted invoice if provided
    attachment: Optional[Dict[str, Any]] = None
    posted: Optional[Dict[str, Any]] = None
    if pdf_path:
        vin = invoice_body.get("vendorInvoiceNumber") or ""
        posted = bc.find_posted_purchase_invoice_by_vendor_invoice_number(vin) if vin else None
        if posted and posted.get("id"):
            attachment = bc.attach_pdf_to_posted_purchase_invoice(posted["id"], pdf_path)
        else:
            # Fallback: try to attach on the (now posted) original invoice record if still allowed
            try:
                attachment = bc.attach_pdf_to_purchase_invoice(created_invoice_id, pdf_path)
            except BusinessCentralApiError:
                attachment = None

    return {
        "purchaseOrderNumber": po_number,
        "purchaseOrderId": po["id"],
        "purchaseOrderBalance": po_balance,
        "createdPurchaseInvoiceId": created_invoice_id,
        "postedPurchaseInvoiceId": posted.get("id") if posted else None,
        "attachment": attachment,
    }


def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_config_from_env() -> BusinessCentralConfig:
    required_env = [
        "BC_TENANT_ID",
        "BC_CLIENT_ID",
        "BC_CLIENT_SECRET",
        "BC_ENVIRONMENT",
        "BC_COMPANY_ID",
    ]
    missing = [key for key in required_env if not os.getenv(key)]
    if missing:
        raise EnvironmentError(f"Missing required environment variables: {', '.join(missing)}")
    return BusinessCentralConfig(
        tenant_id=os.environ["BC_TENANT_ID"],
        client_id=os.environ["BC_CLIENT_ID"],
        client_secret=os.environ["BC_CLIENT_SECRET"],
        environment=os.environ["BC_ENVIRONMENT"],
        company_id=os.environ["BC_COMPANY_ID"],
        resource_url=os.getenv("BC_RESOURCE_URL", "https://api.businesscentral.dynamics.com"),
    )


def main(argv: List[str]) -> None:
    if len(argv) < 2:
        print(
            "Usage: python codeUpdate_with_attachments.py <invoice_json_path> [pdf_invoice_path]",
            file=sys.stderr,
        )
        sys.exit(2)

    invoice_json_path = argv[1]
    pdf_path = argv[2] if len(argv) >= 3 else None

    config = build_config_from_env()
    client = BusinessCentralClient(config)

    invoice_payload = load_json(invoice_json_path)

    result = process_invoice(client, invoice_payload, pdf_path=pdf_path)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main(sys.argv)

