import base64
import json
import logging
import os
import re
from datetime import date, datetime
from difflib import SequenceMatcher
from urllib.parse import unquote

import requests
from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.ai.formrecognizer._models import CurrencyValue
from azure.core.credentials import AzureKeyCredential
import azure.functions as func


# ========= ENVIRONMENT VARIABLES =========
FR_KEY = os.getenv("FORM_RECOGNIZER_KEY")
FR_ENDPOINT = os.getenv("FORM_RECOGNIZER_ENDPOINT")

# Business Central Configuration (moved to ENV; required)
BC_TENANT_ID = os.getenv("BC_TENANT_ID", "")
BC_CLIENT_ID = os.getenv("BC_CLIENT_ID", "")
BC_CLIENT_SECRET = os.getenv("BC_CLIENT_SECRET", "")
BC_SCOPE = os.getenv("BC_SCOPE", "https://api.businesscentral.dynamics.com/.default")
BC_ENVIRONMENT = os.getenv("BC_ENVIRONMENT", "Production")
BC_COMPANY = os.getenv("BC_COMPANY", "")  # display name


# ========= BASIC VALIDATION =========
def _require_env():
    missing = [
        ("FORM_RECOGNIZER_KEY", FR_KEY),
        ("FORM_RECOGNIZER_ENDPOINT", FR_ENDPOINT),
        ("BC_TENANT_ID", BC_TENANT_ID),
        ("BC_CLIENT_ID", BC_CLIENT_ID),
        ("BC_CLIENT_SECRET", BC_CLIENT_SECRET),
        ("BC_COMPANY", BC_COMPANY),
    ]
    not_set = [k for k, v in missing if not v]
    if not_set:
        raise RuntimeError(f"Missing required environment variables: {', '.join(not_set)}")


# ========= URL CONSTANTS (derived) =========
def _token_url() -> str:
    return f"https://login.microsoftonline.com/{BC_TENANT_ID}/oauth2/v2.0/token"


BC_API_PREFIX = None  # e.g. https://api.businesscentral.dynamics.com/v2.0/{tenant}/{env}/api/v2.0
COMPANY_ID_CACHE = None
COMPANY_BASE_URL = None


def get_api_prefix() -> str:
    global BC_API_PREFIX
    if BC_API_PREFIX:
        return BC_API_PREFIX
    env = BC_ENVIRONMENT or "Production"
    BC_API_PREFIX = f"https://api.businesscentral.dynamics.com/v2.0/{BC_TENANT_ID}/{env}/api/v2.0"
    return BC_API_PREFIX


def get_bc_access_token() -> str:
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    body = {
        "grant_type": "client_credentials",
        "client_id": BC_CLIENT_ID,
        "client_secret": BC_CLIENT_SECRET,
        "scope": BC_SCOPE,
    }
    response = requests.post(_token_url(), headers=headers, data=body, timeout=30)
    response.raise_for_status()
    return response.json()["access_token"]


def get_bc_headers() -> dict:
    token = get_bc_access_token()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def ensure_company_base():
    global COMPANY_ID_CACHE, COMPANY_BASE_URL
    if COMPANY_ID_CACHE and COMPANY_BASE_URL:
        return COMPANY_ID_CACHE, COMPANY_BASE_URL

    headers = get_bc_headers()
    api_prefix = get_api_prefix()
    companies_url = f"{api_prefix}/companies"
    resp = requests.get(companies_url, headers=headers, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    target = (BC_COMPANY or "").strip().lower()
    for comp in payload.get("value", []):
        display = (comp.get("displayName") or comp.get("name") or "").strip().lower()
        if display == target:
            cid = comp.get("id")
            COMPANY_ID_CACHE = cid
            COMPANY_BASE_URL = f"{api_prefix}/companies({cid})"
            return COMPANY_ID_CACHE, COMPANY_BASE_URL
    raise RuntimeError(f"Company '{BC_COMPANY}' not found in Business Central")


def try_get_urls(urls, headers, timeout=30):
    last_resp = None
    for url in urls:
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            last_resp = resp
            if resp.status_code == 200:
                return resp
        except Exception:
            last_resp = None
            continue
    return last_resp


def try_post_urls(urls, headers, payload=None, timeout=30):
    last_resp = None
    per_url_mode = bool(isinstance(urls, list) and urls and isinstance(urls[0], dict) and "url" in urls[0])

    if per_url_mode:
        for entry in urls:
            url = entry.get("url")
            url_payload = entry.get("payload", payload or {})
            try:
                resp = requests.post(url, headers=headers, json=url_payload or {}, timeout=timeout)
                last_resp = resp
                if resp.status_code in (200, 201, 204):
                    return resp
            except Exception:
                last_resp = None
                continue
    else:
        for url in urls:
            try:
                resp = requests.post(url, headers=headers, json=payload or {}, timeout=timeout)
                last_resp = resp
                if resp.status_code in (200, 201, 204):
                    return resp
            except Exception:
                last_resp = None
                continue
    return last_resp


def escape_odata_string(value: str) -> str:
    if not isinstance(value, str):
        return value
    return value.replace("'", "''")


# ========= PO HELPERS =========
def get_purchase_order(po_number: str):
    if not po_number:
        return None
    try:
        headers = get_bc_headers()
        ensure_company_base()
        escaped = escape_odata_string(po_number)
        urls = [f"{COMPANY_BASE_URL}/GripPurchaseOrders?$filter=No eq '{escaped}'"]
        resp = try_get_urls(urls, headers)
        if resp is not None and resp.status_code == 200:
            data = resp.json()
            if data.get("value"):
                return data["value"][0]
    except Exception as e:
        logging.warning(f"get_purchase_order error: {e}")
    return None


def get_purchase_order_lines(po_number: str):
    if not po_number:
        return []
    try:
        headers = get_bc_headers()
        ensure_company_base()
        escaped = escape_odata_string(po_number)
        urls = [f"{COMPANY_BASE_URL}/GripPurchaseOrderLines?$filter=Document_No eq '{escaped}'"]
        resp = try_get_urls(urls, headers)
        if resp is not None and resp.status_code == 200:
            return resp.json().get("value", [])
    except Exception as e:
        logging.warning(f"get_purchase_order_lines error: {e}")
    return []


def calculate_po_balance(po_data):
    try:
        total_amount = po_data.get("Amount", 0) or 0
        invoiced_amount = po_data.get("Amount_Invoiced", 0) or 0
        balance = total_amount - invoiced_amount
        return {
            "total_amount": total_amount,
            "invoiced_amount": invoiced_amount,
            "balance": balance,
            "vendor_no": po_data.get("Buy_from_Vendor_No", ""),
            "currency_code": po_data.get("Currency_Code", ""),
        }
    except Exception:
        return None


def get_po_line_open_quantity(po_line: dict) -> float:
    candidates = [
        "Qty_to_Invoice",
        "Outstanding_Quantity",
        "Remaining_Quantity",
        "Open_Quantity",
    ]
    for k in candidates:
        if k in po_line and isinstance(po_line[k], (int, float)):
            try:
                return float(po_line[k])
            except Exception:
                pass
    # Fallback compute from Quantity - Quantity_Invoiced
    qty = float(po_line.get("Quantity", 0) or 0)
    qty_inv = float(po_line.get("Quantity_Invoiced", 0) or 0)
    return max(qty - qty_inv, 0.0)


# ========= INVOICE LINE MATCHING =========
def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def match_invoice_line_to_po_line(invoice_line: dict, po_lines: list):
    if not invoice_line.get("description") or not po_lines:
        return None
    invoice_desc = invoice_line.get("description", "").lower().strip()
    best_match = None
    best_score = 0.0
    threshold = 0.6
    for po_line in po_lines:
        po_desc = (po_line.get("Description", "") or "").lower().strip()
        if not po_desc:
            continue
        score = SequenceMatcher(None, invoice_desc, po_desc).ratio()
        po_words = set(po_desc.split())
        invoice_words = set(invoice_desc.split())
        word_overlap = len(po_words.intersection(invoice_words)) / max(len(po_words), 1)
        combined_score = (score * 0.7) + (word_overlap * 0.3)
        if combined_score > best_score:
            best_score = combined_score
            best_match = po_line
    if best_match and best_score >= threshold:
        return best_match
    return None


def get_entity_fields(entity_collection_name: str):
    try:
        headers = get_bc_headers()
        ensure_company_base()
        url = f"{COMPANY_BASE_URL}/{entity_collection_name}?$top=1"
        resp = try_get_urls([url], headers)
        if resp is not None and resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict) and data.get("value"):
                return list(data["value"][0].keys())
    except Exception:
        return []
    return []


def get_invoice_line_fields():
    return get_entity_fields("FactuurKetenPurchaseInvoiceLines")


def pick_po_reference_fields(entity_fields: list):
    po_candidates = [
        "Order_No",
        "Purchase_Order_No",
        "PurchaseOrder_No",
        "PO_No",
        "Document_No_2",
        "External_Document_No",
        "Reference_No",
    ]
    po_line_candidates = [
        "Order_Line_No",
        "Purchase_Order_Line_No",
        "PurchaseOrder_Line_No",
        "PO_Line_No",
        "Line_No_2",
        "Reference_Line_No",
    ]
    result = {}
    for c in po_candidates:
        if c in entity_fields:
            result["po_field"] = c
            break
    for c in po_line_candidates:
        if c in entity_fields:
            result["po_line_field"] = c
            break
    return result if result else None


# ========= LINES CREATION =========
def create_po_invoice_lines(invoice_no: str, invoice_data: dict, po_lines: list):
    created_lines = []
    headers = get_bc_headers()

    if not invoice_data.get("lineItems"):
        return created_lines

    invoice_line_fields = get_invoice_line_fields()
    po_reference_fields = pick_po_reference_fields(invoice_line_fields)

    ensure_company_base()
    lines_collection = "FactuurKetenPurchaseInvoiceLines"
    candidate_lines_urls = [f"{COMPANY_BASE_URL}/{lines_collection}"]

    for idx, invoice_line in enumerate(invoice_data.get("lineItems", [])):
        try:
            matching_po_line = match_invoice_line_to_po_line(invoice_line, po_lines) if po_lines else None

            quantity = float(invoice_line.get("quantity", 1) or 1)
            if matching_po_line:
                open_qty = get_po_line_open_quantity(matching_po_line)
                if open_qty > 0:
                    quantity = min(quantity, open_qty)

            line_payload = {
                "Document_No": invoice_no,
                "Description": invoice_line.get("description", ""),
                "Quantity": quantity,
            }
            unit_cost = invoice_line.get("unitPrice", 0)
            if not unit_cost and matching_po_line:
                unit_cost = matching_po_line.get("Direct_Unit_Cost") or matching_po_line.get("Unit_Cost") or 0
            if unit_cost:
                line_payload["Direct_Unit_Cost"] = unit_cost

            if "Document_Type" in invoice_line_fields:
                line_payload["Document_Type"] = "Invoice"

            if matching_po_line and po_reference_fields:
                if "No" in invoice_line_fields and matching_po_line.get("No"):
                    line_payload["No"] = matching_po_line.get("No")
                if "Type" in invoice_line_fields:
                    line_payload["Type"] = matching_po_line.get("Type", "Item") or "Item"
                if "Location_Code" in invoice_line_fields and matching_po_line.get("Location_Code"):
                    line_payload["Location_Code"] = matching_po_line.get("Location_Code")
                if "Unit_of_Measure_Code" in invoice_line_fields and matching_po_line.get("Unit_of_Measure_Code"):
                    line_payload["Unit_of_Measure_Code"] = matching_po_line.get("Unit_of_Measure_Code")
                if po_reference_fields.get("po_field"):
                    line_payload[po_reference_fields["po_field"]] = matching_po_line.get("Document_No", "")
                if po_reference_fields.get("po_line_field"):
                    line_payload[po_reference_fields["po_line_field"]] = matching_po_line.get("Line_No", 0)

            # Remove empty/null values except Quantity/Direct_Unit_Cost which may be zero by design
            line_payload = {k: v for k, v in line_payload.items() if v not in [None, ""] or k in ["Quantity", "Direct_Unit_Cost"]}

            line_response = try_post_urls(candidate_lines_urls, headers, payload=line_payload, timeout=30)
            if line_response is None:
                raise Exception("No response when creating invoice line")
            line_response.raise_for_status()
            created_lines.append(line_response.json() if line_response.status_code != 204 else {})
        except Exception as line_error:
            logging.error(f"Error creating invoice line {idx+1}: {line_error}")
            try:
                minimal_payload = {
                    "Document_No": invoice_no,
                    "Description": invoice_line.get("description", ""),
                    "Quantity": quantity,
                }
                minimal_response = try_post_urls(candidate_lines_urls, headers, payload=minimal_payload, timeout=30)
                if minimal_response is not None:
                    minimal_response.raise_for_status()
                    created_lines.append(minimal_response.json() if minimal_response.status_code != 204 else {})
            except Exception as minimal_error:
                logging.error(f"Failed minimal line {idx+1}: {minimal_error}")

    return created_lines


# ========= POST/RELEASE INVOICE =========
def post_invoice(invoice_no: str, invoice_header: dict) -> bool:
    if not invoice_no:
        return False
    headers = get_bc_headers()
    try:
        ensure_company_base()
        system_id = invoice_header.get("id") or invoice_header.get("Id") or invoice_header.get("systemId") or invoice_header.get("SystemId")
        if not system_id:
            escaped_invoice_no = escape_odata_string(invoice_no)
            get_url = f"{COMPANY_BASE_URL}/purchaseInvoices?$filter=No eq '{escaped_invoice_no}'"
            r = requests.get(get_url, headers=headers, timeout=30)
            if r.status_code == 200:
                vals = r.json().get("value", [])
                if vals:
                    system_id = vals[0].get("id") or vals[0].get("systemId")
        if system_id:
            post_url = f"{COMPANY_BASE_URL}/purchaseInvoices({system_id})/Microsoft.NAV.post"
            pr = requests.post(post_url, headers=headers, json={}, timeout=30)
            return pr.status_code in (200, 201, 204)
    except Exception as e:
        logging.warning(f"post_invoice error: {e}")
    return False


# ========= ATTACHMENT UPLOAD =========
def upload_invoice_attachment_pdf(invoice_header: dict, invoice_no: str, invoice_bytes: bytes, filename: str = None) -> dict:
    """Upload the PDF as an attachment to the created invoice using multiple API fallbacks.

    Returns a dict with { success: bool, status: int, endpoint: str, response: dict|str }
    """
    if not invoice_bytes:
        return {"success": False, "status": 0, "endpoint": "", "response": "no-bytes"}

    headers = get_bc_headers()
    ensure_company_base()

    # Resolve invoice record id
    record_id = invoice_header.get("id") or invoice_header.get("Id") or invoice_header.get("systemId") or invoice_header.get("SystemId")
    if not record_id and invoice_no:
        try:
            escaped_invoice_no = escape_odata_string(invoice_no)
            get_url = f"{COMPANY_BASE_URL}/purchaseInvoices?$filter=No eq '{escaped_invoice_no}'"
            r = requests.get(get_url, headers=headers, timeout=30)
            if r.status_code == 200:
                vals = r.json().get("value", [])
                if vals:
                    record_id = vals[0].get("id") or vals[0].get("systemId")
        except Exception:
            pass

    if not record_id:
        return {"success": False, "status": 404, "endpoint": "", "response": "invoice id not found"}

    b64 = base64.b64encode(invoice_bytes).decode("ascii")
    fname = filename or f"invoice_{invoice_no or 'document'}.pdf"

    # Candidate endpoints + payload variants (BC standard + common customizations)
    base = f"{COMPANY_BASE_URL}/purchaseInvoices({record_id})"
    candidates = [
        {"url": f"{base}/attachments", "payload": {"fileName": fname, "content": b64}},
        {"url": f"{base}/attachments", "payload": {"fileName": fname, "attachmentContent": b64}},
        {"url": f"{base}/attachments", "payload": {"name": fname, "contentType": "application/pdf", "content": b64}},
        {"url": f"{base}/documentAttachments", "payload": {"fileName": fname, "documentContent": b64}},
    ]

    last = None
    for entry in candidates:
        try:
            resp = requests.post(entry["url"], headers=headers, json=entry["payload"], timeout=60)
            last = resp
            if resp.status_code in (200, 201, 204):
                try:
                    return {"success": True, "status": resp.status_code, "endpoint": entry["url"], "response": resp.json()}
                except Exception:
                    return {"success": True, "status": resp.status_code, "endpoint": entry["url"], "response": resp.text}
        except Exception as e:
            last = None
            logging.warning(f"Attachment try failed for {entry['url']}: {e}")
            continue

    status = getattr(last, "status_code", 0)
    body = None
    try:
        body = last.json() if last is not None else None
    except Exception:
        body = last.text if last is not None else None
    return {"success": False, "status": status, "endpoint": candidates[-1]["url"], "response": body}


# ========= DATE/JSON HELPERS =========
def parse_date_to_ymd(value):
    if not value:
        return ""
    try:
        from dateutil import parser as _parser
        parsed = _parser.parse(value) if isinstance(value, str) else value
        return parsed.strftime("%Y-%m-%d")
    except Exception:
        try:
            if isinstance(value, str):
                parsed = datetime.fromisoformat(value)
                return parsed.strftime("%Y-%m-%d")
            elif isinstance(value, (datetime, date)):
                return value.strftime("%Y-%m-%d")
        except Exception:
            pass
    return value if isinstance(value, str) else ""


def json_default(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    elif isinstance(obj, CurrencyValue):
        currency = (
            getattr(obj, "currency_symbol", "")
            or getattr(obj, "currency", "")
            or getattr(obj, "currency_code", "")
        )
        return {"amount": obj.amount, "currency": currency}
    raise TypeError(f"Type {type(obj)} not serializable")


# ========= GL MAPPINGS =========
def load_gl_mappings():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    possible_paths = [
        os.path.join(os.path.dirname(current_dir), "gl_mapping.json"),
        os.path.join(os.path.dirname(current_dir), "gl_mappings.json"),
        os.path.join(current_dir, "gl_mapping.json"),
        os.path.join(current_dir, "gl_mappings.json"),
        os.path.join(os.path.dirname(current_dir), "shared", "gl_mapping.json"),
        os.path.join(os.path.dirname(current_dir), "shared", "gl_mappings.json"),
        os.path.join(current_dir, "shared", "gl_mapping.json"),
        os.path.join(current_dir, "shared", "gl_mappings.json"),
    ]
    for path in possible_paths:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                mappings = json.load(f)
                return mappings.get("gl_account_mapping", mappings)
    return {}


def match_gl_account(description: str, gl_mappings: dict, threshold: float = 0.8):
    if not description:
        return None
    description_lower = description.lower().strip()
    best_match = None
    best_score = 0.0
    for gl_number, mapping in gl_mappings.items():
        keywords = mapping.get("keywords", [])
        for keyword in keywords:
            keyword_lower = keyword.lower().strip()
            if keyword_lower in description_lower:
                return gl_number
            keyword_tokens = set(keyword_lower.split())
            if keyword_tokens and keyword_tokens.issubset(set(description_lower.split())):
                return gl_number
            score = similarity(description_lower, keyword_lower)
            if score > best_score:
                best_score = score
                best_match = gl_number
    if best_match and best_score >= threshold:
        return best_match
    return None


# ========= DOCUMENT INTELLIGENCE =========
def analyze_invoice(invoice_bytes: bytes):
    client = DocumentAnalysisClient(endpoint=FR_ENDPOINT, credential=AzureKeyCredential(FR_KEY))
    poller = client.begin_analyze_document("prebuilt-invoice", document=invoice_bytes)
    return poller.result()


def extract_fields(result, gl_mappings):
    def parse_numeric_field(field):
        if not field or not getattr(field, "value", None):
            return 0.0, ""
        if isinstance(field.value, CurrencyValue):
            return float(field.value.amount), getattr(field.value, "currency", "") or getattr(field.value, "currency_code", "")
        try:
            return float(field.value), ""
        except (ValueError, TypeError):
            return 0.0, ""

    invoice = {
        "vendorId": "",
        "vendorName": "",
        "invoiceNumber": "",
        "invoiceDate": "",
        "dueDate": "",
        "poNumber": "",
        "currencyCode": "",
        "totalAmount": 0.0,
        "lineItems": [],
    }
    if not getattr(result, "documents", None):
        return invoice
    fields = result.documents[0].fields
    invoice["vendorName"] = fields.get("VendorName").value if fields.get("VendorName") else ""
    invoice["invoiceNumber"] = fields.get("InvoiceId").value if fields.get("InvoiceId") else ""
    invoice["invoiceDate"] = fields.get("InvoiceDate").value if fields.get("InvoiceDate") else ""
    invoice["dueDate"] = fields.get("DueDate").value if fields.get("DueDate") else ""
    invoice["poNumber"] = fields.get("PurchaseOrder").value if fields.get("PurchaseOrder") else ""
    items_field = fields.get("Items")
    total_amount = 0.0
    currency_code = ""
    if items_field and items_field.value:
        for item in items_field.value:
            if not item or not getattr(item, "value", None):
                continue
            description_field = item.value.get("Description")
            description = description_field.value if description_field else ""
            quantity_field = item.value.get("Quantity")
            quantity = float(quantity_field.value) if quantity_field and quantity_field.value else 1
            unit_price_field = item.value.get("UnitPrice")
            unit_price, unit_currency = parse_numeric_field(unit_price_field)
            if unit_currency:
                currency_code = unit_currency
            amount_field = item.value.get("Amount")
            amount_value, amount_currency = parse_numeric_field(amount_field)
            if amount_currency:
                currency_code = amount_currency
            gl_account_number = match_gl_account(description, gl_mappings)
            invoice["lineItems"].append(
                {
                    "description": description,
                    "quantity": quantity,
                    "unitPrice": unit_price if unit_price else amount_value,
                    "amount": amount_value,
                    "glAccount": gl_account_number,
                }
            )
            total_amount += amount_value
    invoice["totalAmount"] = total_amount
    invoice["currencyCode"] = currency_code
    return invoice


# ========= INVOICE CREATION =========
def find_vendor_number_from_po(po_data):
    if not po_data:
        return None
    candidates = [
        "Buy_from_Vendor_No",
        "buyFromVendorNo",
        "Vendor_No",
        "VendorNo",
        "vendorNumber",
        "vendorNo",
        "Buy_from_Vendor_No_x003a_",
    ]
    for key in candidates:
        val = po_data.get(key)
        if val:
            return val
    return None


def find_vendor_number_from_invoice(invoice_data):
    if not invoice_data:
        return None
    candidates = ["vendorNumber", "vendorId", "vendorNo", "vendor_number", "vendor"]
    for key in candidates:
        val = invoice_data.get(key)
        if val:
            return val
    return None


def extract_vendor_number_from_name(name: str):
    if not name or not isinstance(name, str):
        return None
    m = re.search(r"\(([A-Za-z0-9\-\_]+)\)\s*$", name.strip())
    if m:
        return m.group(1)
    m2 = re.search(r"\(([A-Za-z0-9\-\_]+)\)", name)
    if m2:
        return m2.group(1)
    return None


def resolve_vendor_number_by_name(name: str):
    if not name:
        return None
    try:
        ensure_company_base()
        headers = get_bc_headers()
        escaped = escape_odata_string(name)
        vendors_url_exact = f"{COMPANY_BASE_URL}/vendors?$filter=displayName eq '{escaped}'"
        resp = requests.get(vendors_url_exact, headers=headers, timeout=30)
        if resp and resp.status_code == 200:
            data = resp.json()
            vals = data.get("value", [])
            if vals:
                v = vals[0]
                for candidate in ["number", "no", "vendorNumber", "vendorNo", "No"]:
                    if candidate in v and v.get(candidate):
                        return v.get(candidate)
        vendors_url_contains = f"{COMPANY_BASE_URL}/vendors?$filter=contains(tolower(displayName), tolower('{escaped}'))"
        resp2 = requests.get(vendors_url_contains, headers=headers, timeout=30)
        if resp2 and resp2.status_code == 200:
            data2 = resp2.json()
            vals2 = data2.get("value", [])
            if vals2:
                v = vals2[0]
                for candidate in ["number", "no", "vendorNumber", "vendorNo", "No"]:
                    if candidate in v and v.get(candidate):
                        return v.get(candidate)
    except Exception:
        return None
    return None


def create_po_invoice(invoice_data, po_data):
    try:
        headers = get_bc_headers()
        invoice_date = parse_date_to_ymd(invoice_data.get("invoiceDate"))
        due_date = parse_date_to_ymd(invoice_data.get("dueDate"))

        vendor_no_from_po = find_vendor_number_from_po(po_data)
        api_payload = {}
        if vendor_no_from_po:
            api_payload["vendorNumber"] = vendor_no_from_po
        if invoice_data.get("invoiceNumber"):
            api_payload["vendorInvoiceNumber"] = invoice_data.get("invoiceNumber")
        if invoice_date:
            api_payload["invoiceDate"] = invoice_date
        if due_date:
            api_payload["dueDate"] = due_date
        if invoice_data.get("currencyCode"):
            api_payload["currencyCode"] = invoice_data.get("currencyCode")

        if not api_payload.get("vendorNumber"):
            vendor_from_invoice = find_vendor_number_from_invoice(invoice_data)
            if vendor_from_invoice:
                api_payload["vendorNumber"] = vendor_from_invoice

        if not api_payload.get("vendorNumber") and invoice_data.get("vendorName"):
            extracted = extract_vendor_number_from_name(invoice_data.get("vendorName"))
            if extracted:
                api_payload["vendorNumber"] = extracted
            else:
                resolved = resolve_vendor_number_by_name(invoice_data.get("vendorName"))
                if resolved:
                    api_payload["vendorNumber"] = resolved

        ensure_company_base()
        header_collection = "FactuurKetenPurchaseInvoices"
        candidate_header_calls = [
            {"url": f"{COMPANY_BASE_URL}/{header_collection}", "payload": {k: v for k, v in api_payload.items() if v not in [None, ""]}},
        ]

        response = try_post_urls(candidate_header_calls, headers, timeout=30)
        if response is None:
            raise Exception("No response when creating invoice header")
        response.raise_for_status()
        invoice_header = response.json() if response.status_code != 204 else {}
        invoice_no = (
            invoice_header.get("no")
            or invoice_header.get("No")
            or invoice_header.get("Document_No")
            or invoice_data.get("invoiceNumber", "")
        )

        # Create lines against open PO lines
        po_number = invoice_data.get("poNumber", "") or (po_data.get("No") if po_data else "")
        po_lines = get_purchase_order_lines(po_number) if po_number else []
        created_lines = create_po_invoice_lines(invoice_no, invoice_data, po_lines)

        # Post the invoice (optional)
        posted = post_invoice(invoice_no, invoice_header)

        return {
            "success": True,
            "invoice_no": invoice_no,
            "type": "PO_INVOICE",
            "posted": posted,
            "lines_created": len(created_lines),
            "total_lines": len(invoice_data.get("lineItems", [])),
            "invoice_header": invoice_header,
        }
    except requests.exceptions.HTTPError as http_err:
        status = http_err.response.status_code if hasattr(http_err, "response") and http_err.response is not None else "N/A"
        body = http_err.response.text if hasattr(http_err, "response") and http_err.response is not None else str(http_err)
        return {"success": False, "error": f"HTTP {status}: {body}", "type": "PO_INVOICE", "status_code": status}
    except Exception as e:
        return {"success": False, "error": str(e), "type": "PO_INVOICE"}


def create_non_po_invoice(invoice_data):
    try:
        headers = get_bc_headers()
        invoice_date = parse_date_to_ymd(invoice_data.get("invoiceDate"))
        due_date = parse_date_to_ymd(invoice_data.get("dueDate"))

        api_payload = {}
        if invoice_data.get("invoiceNumber"):
            api_payload["vendorInvoiceNumber"] = invoice_data.get("invoiceNumber")
        if invoice_date:
            api_payload["invoiceDate"] = invoice_date
        if due_date:
            api_payload["dueDate"] = due_date
        if invoice_data.get("currencyCode"):
            api_payload["currencyCode"] = invoice_data.get("currencyCode")

        vendor_from_invoice = find_vendor_number_from_invoice(invoice_data)
        if vendor_from_invoice:
            api_payload["vendorNumber"] = vendor_from_invoice
        else:
            if invoice_data.get("vendorName"):
                extracted = extract_vendor_number_from_name(invoice_data.get("vendorName"))
                if extracted:
                    api_payload["vendorNumber"] = extracted
                else:
                    resolved = resolve_vendor_number_by_name(invoice_data.get("vendorName"))
                    if resolved:
                        api_payload["vendorNumber"] = resolved

        ensure_company_base()
        header_collection = "FactuurKetenPurchaseInvoices"
        candidate_header_calls = [
            {"url": f"{COMPANY_BASE_URL}/{header_collection}", "payload": {k: v for k, v in api_payload.items() if v not in [None, ""]}},
        ]

        response = try_post_urls(candidate_header_calls, headers, timeout=30)
        if response is None:
            raise Exception("No response when creating non-PO invoice header")
        response.raise_for_status()

        invoice_header = response.json() if response.status_code != 204 else {}
        invoice_no = (
            invoice_header.get("no")
            or invoice_header.get("No")
            or invoice_header.get("Document_No")
            or invoice_data.get("invoiceNumber", "")
        )

        # Create minimal lines based on extracted items (G/L if provided)
        created_lines = []
        if invoice_no and invoice_data.get("lineItems"):
            invoice_line_fields = get_invoice_line_fields()
            ensure_company_base()
            lines_collection = "FactuurKetenPurchaseInvoiceLines"
            candidate_lines_urls = [f"{COMPANY_BASE_URL}/{lines_collection}"]

            for idx, line_item in enumerate(invoice_data.get("lineItems", [])):
                try:
                    line_payload = {
                        "Document_No": invoice_no,
                        "Description": line_item.get("description", ""),
                    }
                    if line_item.get("quantity", 0) > 0:
                        line_payload["Quantity"] = line_item.get("quantity", 1)
                    if line_item.get("unitPrice", 0) > 0:
                        line_payload["Direct_Unit_Cost"] = line_item.get("unitPrice", 0)
                    if line_item.get("amount", 0) > 0:
                        line_payload["Line_Amount"] = line_item.get("amount", 0)
                    if line_item.get("glAccount"):
                        if "No" in invoice_line_fields:
                            line_payload["No"] = line_item.get("glAccount")
                        else:
                            line_payload["No"] = line_item.get("glAccount")

                    line_payload = {k: v for k, v in line_payload.items() if v not in [None, ""]}

                    line_response = try_post_urls(candidate_lines_urls, headers, payload=line_payload, timeout=30)
                    if line_response is None:
                        raise Exception("No response creating non-PO invoice line")
                    line_response.raise_for_status()
                    created_lines.append(line_response.json() if line_response.status_code != 204 else {})
                except Exception as line_error:
                    logging.error(f"Error creating invoice line {idx+1}: {line_error}")
                    try:
                        minimal_payload = {
                            "Document_No": invoice_no,
                            "Description": line_item.get("description", ""),
                        }
                        minimal_response = try_post_urls(candidate_lines_urls, headers, payload=minimal_payload, timeout=30)
                        if minimal_response is not None:
                            minimal_response.raise_for_status()
                            created_lines.append(minimal_response.json() if minimal_response.status_code != 204 else {})
                    except Exception as minimal_error:
                        logging.error(f"Failed to create minimal non-PO line {idx+1}: {minimal_error}")

        posted = post_invoice(invoice_no, invoice_header)

        return {
            "success": True,
            "invoice_no": invoice_no,
            "type": "NON_PO_INVOICE",
            "lines_created": len(created_lines),
            "total_lines": len(invoice_data.get("lineItems", [])),
            "invoice_header": invoice_header,
            "posted": posted,
        }
    except requests.exceptions.HTTPError as http_err:
        status = http_err.response.status_code if hasattr(http_err, "response") and http_err.response is not None else "N/A"
        body = http_err.response.text if hasattr(http_err, "response") and http_err.response is not None else str(http_err)
        return {"success": False, "error": f"HTTP {status}: {body}", "type": "NON_PO_INVOICE", "status_code": status}
    except Exception as e:
        return {"success": False, "error": str(e), "type": "NON_PO_INVOICE"}


# ========= LOGGING & UTIL =========
def create_processing_log(invoice_data, po_data, bc_result, attachment_result=None, processing_errors=None):
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "invoice_number": invoice_data.get("invoiceNumber", ""),
        "vendor_name": invoice_data.get("vendorName", ""),
        "po_number": invoice_data.get("poNumber", ""),
        "total_amount": invoice_data.get("totalAmount", 0),
        "currency_code": invoice_data.get("currencyCode", ""),
        "po_found": po_data is not None,
        "po_balance": None,
        "bc_processing": bc_result,
        "attachment": attachment_result or {},
        "line_items_count": len(invoice_data.get("lineItems", [])),
        "errors": processing_errors or [],
    }
    if po_data:
        balance_info = calculate_po_balance(po_data)
        if balance_info:
            log_entry["po_balance"] = balance_info
    return log_entry


def save_log_to_blob(log_data, blob_name: str):
    try:
        log_json = json.dumps(log_data, indent=2, default=json_default)
        logging.info(f"Processing log created: {blob_name}")
        logging.info(f"Log content: {log_json}")
        return True
    except Exception as e:
        logging.error(f"Error saving log to blob: {str(e)}")
        return False


# ========= AZURE FUNCTION ENTRY =========
def main(inputBlob: func.InputStream, outputBlob: func.Out[str]):
    _require_env()
    processing_errors = []
    po_data = None
    bc_result = None
    invoice_data = {}
    attachment_result = None
    try:
        logging.info(f"Processing blob: {inputBlob.name}")
        gl_mappings = load_gl_mappings()
        invoice_bytes = inputBlob.read()
        if not invoice_bytes:
            raise ValueError("Empty input blob")

        result = analyze_invoice(invoice_bytes)
        invoice_data = extract_fields(result, gl_mappings)
        po_number = invoice_data.get("poNumber")

        if po_number:
            po_data = get_purchase_order(po_number)
            if po_data:
                _ = calculate_po_balance(po_data)
                bc_result = create_po_invoice(invoice_data, po_data)
                if not bc_result.get("success"):
                    processing_errors.append(f"Failed to create PO invoice: {bc_result.get('error')}")
            else:
                processing_errors.append(f"PO {po_number} not found in Business Central; falling back to non-PO")
                bc_result = create_non_po_invoice(invoice_data)
        else:
            bc_result = create_non_po_invoice(invoice_data)
            if not bc_result.get("success"):
                processing_errors.append(f"Failed to create non-PO invoice: {bc_result.get('error')}")

        # Upload PDF to created invoice when available
        if bc_result and bc_result.get("success"):
            invoice_header = bc_result.get("invoice_header") or {}
            invoice_no = bc_result.get("invoice_no") or invoice_data.get("invoiceNumber")
            attachment_result = upload_invoice_attachment_pdf(invoice_header, invoice_no, invoice_bytes, filename=f"{invoice_no or 'invoice'}.pdf")
            if not attachment_result.get("success"):
                processing_errors.append(f"Attachment upload failed: {attachment_result}")

        log_data = create_processing_log(invoice_data, po_data, bc_result, attachment_result, processing_errors)
        enhanced_output = {
            **invoice_data,
            "businessCentral": bc_result,
            "purchaseOrder": po_data,
            "attachment": attachment_result,
            "processingLog": log_data,
        }
        json_output = json.dumps(enhanced_output, indent=2, default=json_default)
        outputBlob.set(json_output)

        log_blob_name = f"logs/{invoice_data.get('invoiceNumber', 'unknown')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        save_log_to_blob(log_data, log_blob_name)
        logging.info("Invoice processed and saved to output blob with BC integration and attachment upload.")
    except Exception as e:
        logging.error(f"Error processing invoice: {str(e)}")
        processing_errors.append(str(e))
        error_log = create_processing_log(invoice_data if invoice_data else {}, po_data, bc_result, attachment_result, processing_errors)
        error_output = {
            "error": str(e),
            "vendorId": invoice_data.get("vendorId", "") if invoice_data else "",
            "vendorName": invoice_data.get("vendorName", "") if invoice_data else "",
            "invoiceNumber": invoice_data.get("invoiceNumber", "") if invoice_data else "",
            "invoiceDate": invoice_data.get("invoiceDate", "") if invoice_data else "",
            "dueDate": invoice_data.get("dueDate", "") if invoice_data else "",
            "poNumber": invoice_data.get("poNumber", "") if invoice_data else "",
            "currencyCode": invoice_data.get("currencyCode", "") if invoice_data else "",
            "totalAmount": invoice_data.get("totalAmount", 0.0) if invoice_data else 0.0,
            "lineItems": invoice_data.get("lineItems", []) if invoice_data else [],
            "businessCentral": bc_result,
            "purchaseOrder": po_data,
            "attachment": attachment_result,
            "processingLog": error_log,
        }
        outputBlob.set(json.dumps(error_output, indent=2, default=json_default))
        raise

