import azure.functions as func
import azure.storage.blob
from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.core.credentials import AzureKeyCredential
import lxml.etree as ET
import httpx
import openai
import json
import os
import logging
import datetime
from typing import Dict, Any, Optional, List
import asyncio
import base64
from urllib.parse import quote

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment variables
AZURE_STORAGE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
FORM_RECOGNIZER_KEY = os.getenv("FORM_RECOGNIZER_KEY")
FORM_RECOGNIZER_ENDPOINT = os.getenv("FORM_RECOGNIZER_ENDPOINT")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_TYPE = os.getenv("OPENAI_API_TYPE", "azure")
OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "https://openai-invoice-model.openai.azure.com/")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-invoice")
OPENAI_API_VERSION = os.getenv("OPENAI_API_VERSION", "2024-02-15-preview")
BC_TENANT_ID = os.getenv("BC_TENANT_ID", "a4a6e0c1-b531-4689-a0a0-1ad2250ba843")
BC_CLIENT_SECRET = os.getenv("BC_CLIENT_SECRET")
BC_BASE_URL = os.getenv("BC_BASE_URL", "https://api.businesscentral.dynamics.com/v2.0/a4a6e0c1-b531-4689-a0a0-1ad2250ba843/KnusWonen/ODataV4/Company('Knuswonen%20Henk')")

# Business Central OAuth settings
BC_CLIENT_ID = "db95b1fe-2f8e-4106-a897-caf8fb21fbaf"
BC_TOKEN_URL = f"https://login.microsoftonline.com/{BC_TENANT_ID}/oauth2/v2.0/token"
BC_SCOPE = "https://api.businesscentral.dynamics.com/.default"

# Global token storage (in production, use proper token storage)
_bc_token = None
_token_expires_at = None


async def main(blob: func.InputStream) -> None:
    """
    Main Azure Function entrypoint triggered by blob upload to 'incoming-invoices'.
    """
    try:
        logger.info(f"Processing invoice: {blob.name}")
        
        # Check for required environment variables
        missing_vars = []
        if not AZURE_STORAGE_CONNECTION_STRING:
            missing_vars.append("AZURE_STORAGE_CONNECTION_STRING")
        if not BC_CLIENT_SECRET:
            missing_vars.append("BC_CLIENT_SECRET")
            
        if missing_vars:
            error_msg = f"Missing required environment variables: {', '.join(missing_vars)}"
            logger.error(error_msg)
            # Save error log even if some env vars are missing
            error_json = {
                'original_filename': blob.name,
                'processing_timestamp': datetime.datetime.utcnow().isoformat(),
                'error': error_msg,
                'error_type': 'EnvironmentError'
            }
            if AZURE_STORAGE_CONNECTION_STRING:  # Only try to save if we have storage connection
                await save_logging(error_json, "Failed")
            return
        
        # Read blob content
        file_bytes = blob.read()
        file_name = blob.name
        file_extension = file_name.lower().split('.')[-1]
        
        # Determine file type and parse accordingly
        if file_extension == 'pdf':
            if not FORM_RECOGNIZER_KEY or not FORM_RECOGNIZER_ENDPOINT:
                logger.warning("Form Recognizer credentials not available, skipping PDF parsing")
                raise ValueError("PDF processing requires FORM_RECOGNIZER_KEY and FORM_RECOGNIZER_ENDPOINT")
            invoice_json = await parse_pdf_invoice(file_bytes)
        elif file_extension == 'xml':
            invoice_json = await parse_xml_invoice(file_bytes)
        else:
            raise ValueError(f"Unsupported file type: {file_extension}")
        
        # Add metadata
        invoice_json['original_filename'] = file_name
        invoice_json['processing_timestamp'] = datetime.datetime.utcnow().isoformat()
        
        # Load GL mappings and enrich with AI
        gl_mappings = await load_gl_mappings()
        if OPENAI_API_KEY:
            invoice_json = await determine_gl_accounts(invoice_json, gl_mappings)
        else:
            logger.warning("OpenAI API key not available, using default GL accounts")
            # Apply default GL accounts
            default_gl = gl_mappings.get('default_gl_account', '40000')
            for line_item in invoice_json.get('line_items', []):
                line_item['gl_account'] = default_gl
        
        # Get Business Central token
        token = await get_bc_token()
        
        # Determine if this is a PO invoice or non-PO invoice
        po_number = invoice_json.get('po_number') or invoice_json.get('purchase_order_number')
        
        if po_number:
            # Handle PO invoice
            invoice_id = await handle_po_invoice(invoice_json, token)
        else:
            # Handle non-PO invoice
            invoice_id = await create_invoice_header(invoice_json, token)
            await create_invoice_lines(invoice_id, invoice_json, token)
        
        # Attach original file to BC invoice
        # Extract storage account name from connection string
        storage_account_name = "invoicestorage"  # Default fallback
        if AZURE_STORAGE_CONNECTION_STRING:
            for part in AZURE_STORAGE_CONNECTION_STRING.split(';'):
                if part.startswith('AccountName='):
                    storage_account_name = part.split('=')[1]
                    break
        
        blob_url = f"https://{storage_account_name}.blob.core.windows.net/incoming-invoices/{quote(file_name)}"
        await attach_invoice_file(invoice_id, blob_url, file_bytes, file_name, token)
        
        # Save successful processing log
        invoice_json['bc_invoice_id'] = invoice_id
        await save_logging(invoice_json, "Success")
        
        logger.info(f"Successfully processed invoice {file_name} -> BC Invoice ID: {invoice_id}")
        
    except Exception as e:
        logger.error(f"Error processing invoice {blob.name}: {str(e)}", exc_info=True)
        
        # Save error log
        error_json = {
            'original_filename': blob.name,
            'processing_timestamp': datetime.datetime.utcnow().isoformat(),
            'error': str(e),
            'error_type': type(e).__name__
        }
        await save_logging(error_json, "Failed")


async def parse_pdf_invoice(file_bytes: bytes) -> Dict[str, Any]:
    """
    Parse PDF invoice using Azure Document Intelligence.
    """
    try:
        # Initialize Document Analysis client
        document_analysis_client = DocumentAnalysisClient(
            endpoint=FORM_RECOGNIZER_ENDPOINT,
            credential=AzureKeyCredential(FORM_RECOGNIZER_KEY)
        )
        
        # Analyze document
        poller = document_analysis_client.begin_analyze_document(
            "prebuilt-invoice", document=file_bytes
        )
        result = poller.result()
        
        # Extract invoice data
        invoice_data = {
            'file_type': 'pdf',
            'vendor_name': '',
            'vendor_address': '',
            'vendor_tax_id': '',
            'invoice_number': '',
            'invoice_date': '',
            'due_date': '',
            'total_amount': 0.0,
            'tax_amount': 0.0,
            'net_amount': 0.0,
            'currency': 'EUR',
            'po_number': '',
            'line_items': [],
            'confidence_scores': {}
        }
        
        # Process documents
        for document in result.documents:
            fields = document.fields
            
            # Extract vendor information with confidence tracking
            if 'VendorName' in fields and fields['VendorName'].value:
                invoice_data['vendor_name'] = str(fields['VendorName'].value)
                invoice_data['confidence_scores']['vendor_name'] = fields['VendorName'].confidence
                
            if 'VendorAddress' in fields and fields['VendorAddress'].value:
                invoice_data['vendor_address'] = str(fields['VendorAddress'].value)
                invoice_data['confidence_scores']['vendor_address'] = fields['VendorAddress'].confidence
                
            if 'VendorTaxId' in fields and fields['VendorTaxId'].value:
                invoice_data['vendor_tax_id'] = str(fields['VendorTaxId'].value)
                
            # Extract invoice details
            if 'InvoiceId' in fields and fields['InvoiceId'].value:
                invoice_data['invoice_number'] = str(fields['InvoiceId'].value)
                invoice_data['confidence_scores']['invoice_number'] = fields['InvoiceId'].confidence
                
            if 'InvoiceDate' in fields and fields['InvoiceDate'].value:
                if hasattr(fields['InvoiceDate'].value, 'strftime'):
                    invoice_data['invoice_date'] = fields['InvoiceDate'].value.strftime('%Y-%m-%d')
                else:
                    invoice_data['invoice_date'] = str(fields['InvoiceDate'].value)
                    
            if 'DueDate' in fields and fields['DueDate'].value:
                if hasattr(fields['DueDate'].value, 'strftime'):
                    invoice_data['due_date'] = fields['DueDate'].value.strftime('%Y-%m-%d')
                else:
                    invoice_data['due_date'] = str(fields['DueDate'].value)
                    
            # Extract amounts
            if 'InvoiceTotal' in fields and fields['InvoiceTotal'].value:
                invoice_data['total_amount'] = float(fields['InvoiceTotal'].value)
                invoice_data['confidence_scores']['total_amount'] = fields['InvoiceTotal'].confidence
                
            if 'TotalTax' in fields and fields['TotalTax'].value:
                invoice_data['tax_amount'] = float(fields['TotalTax'].value)
                
            if 'SubTotal' in fields and fields['SubTotal'].value:
                invoice_data['net_amount'] = float(fields['SubTotal'].value)
            else:
                # Calculate net amount if not provided
                invoice_data['net_amount'] = invoice_data['total_amount'] - invoice_data['tax_amount']
                
            if 'CurrencyCode' in fields and fields['CurrencyCode'].value:
                invoice_data['currency'] = str(fields['CurrencyCode'].value)
                
            # Look for PO number in various fields
            po_fields = ['PurchaseOrder', 'CustomerPurchaseOrderNumber', 'OrderReference']
            for po_field in po_fields:
                if po_field in fields and fields[po_field].value:
                    invoice_data['po_number'] = str(fields[po_field].value)
                    break
                    
            # Extract line items
            if 'Items' in fields and fields['Items'].value:
                for item in fields['Items'].value:
                    line_item = {
                        'description': '',
                        'quantity': 1.0,
                        'unit_price': 0.0,
                        'amount': 0.0,
                        'tax_rate': 0.0,
                        'gl_account': '',
                        'item_code': ''
                    }
                    
                    item_fields = item.value
                    if 'Description' in item_fields and item_fields['Description'].value:
                        line_item['description'] = str(item_fields['Description'].value)
                    if 'ProductCode' in item_fields and item_fields['ProductCode'].value:
                        line_item['item_code'] = str(item_fields['ProductCode'].value)
                    if 'Quantity' in item_fields and item_fields['Quantity'].value:
                        line_item['quantity'] = float(item_fields['Quantity'].value)
                    if 'UnitPrice' in item_fields and item_fields['UnitPrice'].value:
                        line_item['unit_price'] = float(item_fields['UnitPrice'].value)
                    if 'Amount' in item_fields and item_fields['Amount'].value:
                        line_item['amount'] = float(item_fields['Amount'].value)
                    if 'Tax' in item_fields and item_fields['Tax'].value:
                        line_item['tax_rate'] = float(item_fields['Tax'].value)
                    
                    # Only add line items with meaningful content
                    if line_item['description'] or line_item['amount'] > 0:
                        invoice_data['line_items'].append(line_item)
        
        # Validate extracted data
        if not invoice_data['vendor_name']:
            logger.warning("No vendor name extracted from PDF")
        if not invoice_data['invoice_number']:
            logger.warning("No invoice number extracted from PDF")
        if invoice_data['total_amount'] == 0:
            logger.warning("Total amount is zero or not extracted from PDF")
            
        return invoice_data
        
    except Exception as e:
        logger.error(f"Error parsing PDF invoice: {str(e)}")
        raise


async def parse_xml_invoice(file_bytes: bytes) -> Dict[str, Any]:
    """
    Parse XML invoice using lxml and convert to structured JSON.
    """
    try:
        # Parse XML
        root = ET.fromstring(file_bytes)
        
        # Initialize invoice data structure
        invoice_data = {
            'file_type': 'xml',
            'vendor_name': '',
            'vendor_address': '',
            'vendor_tax_id': '',
            'invoice_number': '',
            'invoice_date': '',
            'due_date': '',
            'total_amount': 0.0,
            'tax_amount': 0.0,
            'net_amount': 0.0,
            'currency': 'EUR',
            'po_number': '',
            'line_items': []
        }
        
        # Define common XML namespaces (UBL 2.1 and variations)
        namespaces = {
            'cbc': 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2',
            'cac': 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2',
            'ubl': 'urn:oasis:names:specification:ubl:schema:xsd:Invoice-2'
        }
        
        # Try to auto-detect namespaces if standard ones don't work
        if not root.find('.//cbc:ID', namespaces):
            # Extract namespaces from root element
            for prefix, uri in root.nsmap.items():
                if prefix and 'commonbasiccomponents' in uri.lower():
                    namespaces['cbc'] = uri
                elif prefix and 'commonaggregatecomponents' in uri.lower():
                    namespaces['cac'] = uri
                elif prefix and 'invoice' in uri.lower():
                    namespaces['ubl'] = uri
        
        # Extract vendor information
        supplier_party = root.find('.//cac:AccountingSupplierParty/cac:Party', namespaces)
        if supplier_party is not None:
            # Try multiple vendor name locations
            vendor_name_paths = [
                './/cac:PartyName/cbc:Name',
                './/cac:PartyLegalEntity/cbc:RegistrationName',
                './/cbc:Name'
            ]
            for path in vendor_name_paths:
                vendor_name_elem = supplier_party.find(path, namespaces)
                if vendor_name_elem is not None and vendor_name_elem.text:
                    invoice_data['vendor_name'] = vendor_name_elem.text.strip()
                    break
                    
            # Extract tax ID
            tax_id_elem = supplier_party.find('.//cac:PartyTaxScheme/cbc:CompanyID', namespaces)
            if tax_id_elem is not None and tax_id_elem.text:
                invoice_data['vendor_tax_id'] = tax_id_elem.text.strip()
                
            # Extract address
            address_elem = supplier_party.find('.//cac:PostalAddress', namespaces)
            if address_elem is not None:
                address_parts = []
                address_fields = [
                    'cbc:StreetName', 'cbc:AdditionalStreetName', 
                    'cbc:CityName', 'cbc:PostalZone', 
                    'cac:Country/cbc:IdentificationCode'
                ]
                for addr_field in address_fields:
                    elem = address_elem.find(addr_field, namespaces)
                    if elem is not None and elem.text:
                        address_parts.append(elem.text.strip())
                invoice_data['vendor_address'] = ', '.join(address_parts)
        
        # Extract invoice details
        invoice_number_elem = root.find('.//cbc:ID', namespaces)
        if invoice_number_elem is not None and invoice_number_elem.text:
            invoice_data['invoice_number'] = invoice_number_elem.text.strip()
            
        invoice_date_elem = root.find('.//cbc:IssueDate', namespaces)
        if invoice_date_elem is not None and invoice_date_elem.text:
            invoice_data['invoice_date'] = invoice_date_elem.text.strip()
            
        due_date_elem = root.find('.//cbc:DueDate', namespaces)
        if due_date_elem is not None and due_date_elem.text:
            invoice_data['due_date'] = due_date_elem.text.strip()
            
        # Extract monetary totals
        legal_monetary_total = root.find('.//cac:LegalMonetaryTotal', namespaces)
        if legal_monetary_total is not None:
            # Total amount (tax inclusive)
            total_elem = legal_monetary_total.find('cbc:TaxInclusiveAmount', namespaces)
            if total_elem is not None and total_elem.text:
                invoice_data['total_amount'] = float(total_elem.text)
                if 'currencyID' in total_elem.attrib:
                    invoice_data['currency'] = total_elem.attrib['currencyID']
            
            # Net amount (tax exclusive)
            net_elem = legal_monetary_total.find('cbc:TaxExclusiveAmount', namespaces)
            if net_elem is not None and net_elem.text:
                invoice_data['net_amount'] = float(net_elem.text)
            
            # Tax amount
            tax_elem = legal_monetary_total.find('cbc:TaxAmount', namespaces)
            if tax_elem is not None and tax_elem.text:
                invoice_data['tax_amount'] = float(tax_elem.text)
        
        # Extract PO number from order reference
        po_elem = root.find('.//cac:OrderReference/cbc:ID', namespaces)
        if po_elem is not None and po_elem.text:
            invoice_data['po_number'] = po_elem.text.strip()
        
        # Extract line items
        invoice_lines = root.findall('.//cac:InvoiceLine', namespaces)
        for line in invoice_lines:
            line_item = {
                'description': '',
                'quantity': 1.0,
                'unit_price': 0.0,
                'amount': 0.0,
                'tax_rate': 0.0,
                'gl_account': '',
                'item_code': ''
            }
            
            # Item description
            desc_elem = line.find('.//cac:Item/cbc:Name', namespaces)
            if desc_elem is not None and desc_elem.text:
                line_item['description'] = desc_elem.text.strip()
            
            # Item code
            item_id_elem = line.find('.//cac:Item/cac:SellersItemIdentification/cbc:ID', namespaces)
            if item_id_elem is not None and item_id_elem.text:
                line_item['item_code'] = item_id_elem.text.strip()
            
            # Quantity
            qty_elem = line.find('.//cbc:InvoicedQuantity', namespaces)
            if qty_elem is not None and qty_elem.text:
                line_item['quantity'] = float(qty_elem.text)
            
            # Unit price
            price_elem = line.find('.//cac:Price/cbc:PriceAmount', namespaces)
            if price_elem is not None and price_elem.text:
                line_item['unit_price'] = float(price_elem.text)
            
            # Line amount
            amount_elem = line.find('.//cbc:LineExtensionAmount', namespaces)
            if amount_elem is not None and amount_elem.text:
                line_item['amount'] = float(amount_elem.text)
            
            # Tax information
            tax_total_elem = line.find('.//cac:TaxTotal/cbc:TaxAmount', namespaces)
            if tax_total_elem is not None and tax_total_elem.text:
                tax_amount = float(tax_total_elem.text)
                if line_item['amount'] > 0:
                    line_item['tax_rate'] = (tax_amount / line_item['amount']) * 100
            
            # Only add line items with meaningful content
            if line_item['description'] or line_item['amount'] > 0:
                invoice_data['line_items'].append(line_item)
        
        # Validate extracted data
        if not invoice_data['vendor_name']:
            logger.warning("No vendor name extracted from XML")
        if not invoice_data['invoice_number']:
            logger.warning("No invoice number extracted from XML")
        if invoice_data['total_amount'] == 0:
            logger.warning("Total amount is zero or not extracted from XML")
            
        return invoice_data
        
    except Exception as e:
        logger.error(f"Error parsing XML invoice: {str(e)}")
        raise


async def load_gl_mappings() -> Dict[str, Any]:
    """
    Load GL mappings from blob storage.
    """
    try:
        blob_service_client = azure.storage.blob.BlobServiceClient.from_connection_string(
            AZURE_STORAGE_CONNECTION_STRING
        )
        
        blob_client = blob_service_client.get_blob_client(
            container="config",
            blob="gl_mappings.json"
        )
        
        blob_data = blob_client.download_blob().readall()
        return json.loads(blob_data.decode('utf-8'))
        
    except Exception as e:
        logger.warning(f"Could not load GL mappings: {str(e)}")
        # Return default mappings
        return {
            "default_gl_account": "40000",
            "mappings": {}
        }


async def determine_gl_accounts(invoice_json: Dict[str, Any], gl_mappings: Dict[str, Any]) -> Dict[str, Any]:
    """
    Use OpenAI to determine and validate GL accounts for invoice line items.
    """
    try:
        # Configure OpenAI
        openai.api_type = OPENAI_API_TYPE
        openai.api_base = OPENAI_API_BASE
        openai.api_key = OPENAI_API_KEY
        openai.api_version = OPENAI_API_VERSION
        
        # Prepare prompt for GL mapping
        prompt = f"""
        You are an AI assistant helping with General Ledger (GL) account mapping for invoice processing.
        
        Available GL Mappings:
        {json.dumps(gl_mappings, indent=2)}
        
        Invoice Data:
        Vendor: {invoice_json.get('vendor_name', 'Unknown')}
        Invoice Number: {invoice_json.get('invoice_number', 'Unknown')}
        Line Items: {json.dumps(invoice_json.get('line_items', []), indent=2)}
        
        For each line item, determine the most appropriate GL account based on the description and available mappings.
        If no specific mapping exists, use the default GL account.
        
        Return a JSON response with the line items including the assigned gl_account for each item.
        Format: {{"line_items": [...]}}
        """
        
        # Use async HTTP client for OpenAI API call
        async with httpx.AsyncClient() as client:
            openai_headers = {
                'Authorization': f'Bearer {OPENAI_API_KEY}',
                'Content-Type': 'application/json',
                'api-key': OPENAI_API_KEY
            }
            
            openai_data = {
                'messages': [
                    {"role": "system", "content": "You are a helpful assistant for GL account mapping. Always respond with valid JSON."},
                    {"role": "user", "content": prompt}
                ],
                'temperature': 0.1,
                'max_tokens': 2000
            }
            
            response = await client.post(
                f"{OPENAI_API_BASE}/openai/deployments/{OPENAI_MODEL}/chat/completions?api-version={OPENAI_API_VERSION}",
                headers=openai_headers,
                json=openai_data
            )
            response.raise_for_status()
            openai_response = response.json()
        
        # Parse AI response
        ai_response = json.loads(openai_response['choices'][0]['message']['content'])
        
        # Update invoice with GL accounts
        if 'line_items' in ai_response:
            for i, ai_line in enumerate(ai_response['line_items']):
                if i < len(invoice_json['line_items']) and 'gl_account' in ai_line:
                    invoice_json['line_items'][i]['gl_account'] = ai_line['gl_account']
        
        # Fallback to default GL account for any missing mappings
        default_gl = gl_mappings.get('default_gl_account', '40000')
        for line_item in invoice_json['line_items']:
            if not line_item.get('gl_account'):
                line_item['gl_account'] = default_gl
        
        return invoice_json
        
    except Exception as e:
        logger.warning(f"Error in GL mapping with AI: {str(e)}")
        # Fallback to default GL account
        default_gl = gl_mappings.get('default_gl_account', '40000')
        for line_item in invoice_json['line_items']:
            line_item['gl_account'] = default_gl
        return invoice_json


async def get_bc_token() -> str:
    """
    Get Business Central access token with caching.
    """
    global _bc_token, _token_expires_at
    
    # Check if we have a valid cached token
    if _bc_token and _token_expires_at and datetime.datetime.utcnow() < _token_expires_at:
        return _bc_token
    
    try:
        # Request new token
        async with httpx.AsyncClient() as client:
            data = {
                'grant_type': 'client_credentials',
                'client_id': BC_CLIENT_ID,
                'client_secret': BC_CLIENT_SECRET,
                'scope': BC_SCOPE
            }
            
            response = await client.post(BC_TOKEN_URL, data=data)
            response.raise_for_status()
            
            token_data = response.json()
            _bc_token = token_data['access_token']
            
            # Calculate expiration time (subtract 5 minutes for safety)
            expires_in = token_data.get('expires_in', 3600)
            _token_expires_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=expires_in - 300)
            
            return _bc_token
            
    except Exception as e:
        logger.error(f"Error getting BC token: {str(e)}")
        raise


async def create_invoice_header(invoice_json: Dict[str, Any], token: str) -> str:
    """
    Create Purchase Invoice Header in Business Central.
    """
    try:
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        
        # Prepare invoice header data
        header_data = {
            'vendorNumber': invoice_json.get('vendor_name', ''),
            'invoiceDate': invoice_json.get('invoice_date', datetime.date.today().isoformat()),
            'vendorInvoiceNumber': invoice_json.get('invoice_number', ''),
            'currencyCode': invoice_json.get('currency', 'EUR')
        }
        
        # Add due date if available
        if invoice_json.get('due_date'):
            header_data['dueDate'] = invoice_json['due_date']
        
        # Add PO reference if available
        if invoice_json.get('po_number'):
            header_data['purchaseOrderNumber'] = invoice_json['po_number']
        
        # Add vendor address if available
        if invoice_json.get('vendor_address'):
            header_data['payToAddress'] = invoice_json['vendor_address']
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{BC_BASE_URL}/purchaseInvoices",
                headers=headers,
                json=header_data
            )
            
            if response.status_code == 400:
                logger.warning(f"Bad request creating invoice header: {response.text}")
                # Try with minimal data
                minimal_data = {
                    'vendorNumber': invoice_json.get('vendor_name', 'UNKNOWN'),
                    'vendorInvoiceNumber': invoice_json.get('invoice_number', 'UNKNOWN')
                }
                response = await client.post(
                    f"{BC_BASE_URL}/purchaseInvoices",
                    headers=headers,
                    json=minimal_data
                )
            
            response.raise_for_status()
            result = response.json()
            
            # Try different ways to get the invoice ID
            invoice_id = (result.get('id') or 
                         result.get('number') or 
                         result.get('systemId') or
                         result.get('@odata.etag', '').split("'")[-2] if result.get('@odata.etag') else None)
            
            if not invoice_id:
                raise ValueError("Could not extract invoice ID from BC response")
            
            logger.info(f"Created BC invoice header: {invoice_id}")
            return str(invoice_id)
            
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error creating BC invoice header: {e.response.status_code} - {e.response.text}")
        raise
    except Exception as e:
        logger.error(f"Error creating BC invoice header: {str(e)}")
        raise


async def create_invoice_lines(invoice_id: str, invoice_json: Dict[str, Any], token: str) -> None:
    """
    Create invoice lines in Business Central.
    """
    try:
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        
        line_items = invoice_json.get('line_items', [])
        if not line_items:
            logger.warning(f"No line items to create for invoice {invoice_id}")
            return
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            created_lines = 0
            for i, line_item in enumerate(line_items):
                try:
                    line_data = {
                        'type': 'Account',
                        'accountNumber': line_item.get('gl_account', '40000'),
                        'description': line_item.get('description', f'Line {i+1}'),
                        'quantity': max(line_item.get('quantity', 1.0), 0.01),  # Ensure positive quantity
                        'unitPrice': line_item.get('unit_price', 0.0),
                        'lineAmount': line_item.get('amount', 0.0)
                    }
                    
                    # Ensure description is not empty
                    if not line_data['description'].strip():
                        line_data['description'] = f'Invoice line {i+1}'
                    
                    response = await client.post(
                        f"{BC_BASE_URL}/purchaseInvoices('{invoice_id}')/purchaseInvoiceLines",
                        headers=headers,
                        json=line_data
                    )
                    
                    if response.status_code == 400:
                        logger.warning(f"Failed to create line {i+1}: {response.text}")
                        # Try with minimal data
                        minimal_line = {
                            'type': 'Account',
                            'accountNumber': '40000',
                            'description': f'Invoice line {i+1}',
                            'quantity': 1.0,
                            'unitPrice': line_item.get('amount', 0.0)
                        }
                        response = await client.post(
                            f"{BC_BASE_URL}/purchaseInvoices('{invoice_id}')/purchaseInvoiceLines",
                            headers=headers,
                            json=minimal_line
                        )
                    
                    response.raise_for_status()
                    created_lines += 1
                    
                except Exception as line_error:
                    logger.error(f"Error creating line {i+1} for invoice {invoice_id}: {str(line_error)}")
                    # Continue with next line instead of failing entire process
                    continue
                
        logger.info(f"Created {created_lines}/{len(line_items)} invoice lines for {invoice_id}")
        
    except Exception as e:
        logger.error(f"Error creating BC invoice lines: {str(e)}")
        raise


async def handle_po_invoice(invoice_json: Dict[str, Any], token: str) -> str:
    """
    Handle Purchase Order invoice processing.
    """
    try:
        po_number = invoice_json.get('po_number')
        if not po_number:
            raise ValueError("No PO number found in invoice data")
        
        headers = {
            'Authorization': f'Bearer {token}',
            'Accept': 'application/json'
        }
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Fetch PO information
            response = await client.get(
                f"{BC_BASE_URL}/purchaseOrders?$filter=number eq '{po_number}'",
                headers=headers
            )
            response.raise_for_status()
            
            po_data = response.json()
            if not po_data.get('value'):
                logger.warning(f"Purchase Order {po_number} not found, treating as non-PO invoice")
                # Fall back to regular invoice creation
                invoice_id = await create_invoice_header(invoice_json, token)
                await create_invoice_lines(invoice_id, invoice_json, token)
                return invoice_id
            
            po = po_data['value'][0]
            po_id = po.get('id') or po.get('systemId')
            
            # Get PO lines
            response = await client.get(
                f"{BC_BASE_URL}/purchaseOrders('{po_id}')/purchaseOrderLines",
                headers=headers
            )
            response.raise_for_status()
            
            po_lines = response.json().get('value', [])
            
            # Create invoice header linked to PO
            header_data = {
                'vendorNumber': po.get('vendorNumber', invoice_json.get('vendor_name', '')),
                'invoiceDate': invoice_json.get('invoice_date', datetime.date.today().isoformat()),
                'vendorInvoiceNumber': invoice_json.get('invoice_number', ''),
                'currencyCode': invoice_json.get('currency', 'EUR'),
                'purchaseOrderNumber': po_number
            }
            
            # Add due date if available
            if invoice_json.get('due_date'):
                header_data['dueDate'] = invoice_json['due_date']
            
            response = await client.post(
                f"{BC_BASE_URL}/purchaseInvoices",
                headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
                json=header_data
            )
            response.raise_for_status()
            
            invoice_result = response.json()
            invoice_id = (invoice_result.get('id') or 
                         invoice_result.get('number') or 
                         invoice_result.get('systemId'))
            
            if not invoice_id:
                raise ValueError("Could not extract invoice ID from BC response")
            
            # Match invoice lines to PO lines and create invoice lines
            created_lines = 0
            for i, invoice_line in enumerate(invoice_json.get('line_items', [])):
                try:
                    # Find matching PO line using multiple criteria
                    matching_po_line = None
                    invoice_desc = invoice_line.get('description', '').lower()
                    invoice_item_code = invoice_line.get('item_code', '').lower()
                    
                    for po_line in po_lines:
                        po_desc = po_line.get('description', '').lower()
                        po_item = po_line.get('itemNumber', '').lower()
                        
                        # Match by item code first, then description
                        if (invoice_item_code and po_item and invoice_item_code == po_item) or \
                           (invoice_desc and po_desc and invoice_desc in po_desc):
                            matching_po_line = po_line
                            break
                    
                    if matching_po_line:
                        # Create invoice line linked to PO line
                        outstanding_qty = matching_po_line.get('outstandingQuantity', 1.0)
                        invoice_qty = invoice_line.get('quantity', 1.0)
                        
                        line_data = {
                            'type': 'Item' if matching_po_line.get('itemNumber') else 'Account',
                            'description': invoice_line.get('description', ''),
                            'quantity': min(invoice_qty, outstanding_qty) if outstanding_qty > 0 else invoice_qty,
                            'unitPrice': invoice_line.get('unit_price', 0.0),
                            'lineAmount': invoice_line.get('amount', 0.0)
                        }
                        
                        if matching_po_line.get('itemNumber'):
                            line_data['itemNumber'] = matching_po_line['itemNumber']
                        else:
                            line_data['accountNumber'] = invoice_line.get('gl_account', '40000')
                    else:
                        # Create general GL line if no PO line match
                        line_data = {
                            'type': 'Account',
                            'accountNumber': invoice_line.get('gl_account', '40000'),
                            'description': invoice_line.get('description', f'Line {i+1}'),
                            'quantity': invoice_line.get('quantity', 1.0),
                            'unitPrice': invoice_line.get('unit_price', 0.0),
                            'lineAmount': invoice_line.get('amount', 0.0)
                        }
                    
                    response = await client.post(
                        f"{BC_BASE_URL}/purchaseInvoices('{invoice_id}')/purchaseInvoiceLines",
                        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
                        json=line_data
                    )
                    response.raise_for_status()
                    created_lines += 1
                    
                except Exception as line_error:
                    logger.error(f"Error creating PO line {i+1}: {str(line_error)}")
                    continue
            
            logger.info(f"Created PO invoice {invoice_id} for PO {po_number} with {created_lines} lines")
            return str(invoice_id)
            
    except Exception as e:
        logger.error(f"Error handling PO invoice: {str(e)}")
        raise


async def attach_invoice_file(invoice_id: str, blob_url: str, file_bytes: bytes, 
                            file_name: str, token: str) -> None:
    """
    Attach original invoice file to Business Central invoice.
    """
    try:
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        }
        
        # Encode file content to base64
        file_content_b64 = base64.b64encode(file_bytes).decode('utf-8')
        
        # Try different attachment methods
        attachment_methods = [
            # Method 1: Direct attachment
            {
                'url': f"{BC_BASE_URL}/purchaseInvoices('{invoice_id}')/attachments",
                'data': {
                    'fileName': file_name,
                    'attachmentContent': file_content_b64
                }
            },
            # Method 2: Document attachment
            {
                'url': f"{BC_BASE_URL}/purchaseInvoices('{invoice_id}')/documentAttachments",
                'data': {
                    'fileName': file_name,
                    'documentContent': file_content_b64,
                    'documentType': 'PDF' if file_name.lower().endswith('.pdf') else 'XML'
                }
            },
            # Method 3: Generic attachment
            {
                'url': f"{BC_BASE_URL}/attachments",
                'data': {
                    'parentId': invoice_id,
                    'parentType': 'PurchaseInvoice',
                    'fileName': file_name,
                    'content': file_content_b64
                }
            }
        ]
        
        async with httpx.AsyncClient(timeout=60.0) as client:  # Longer timeout for file uploads
            attachment_success = False
            
            for i, method in enumerate(attachment_methods):
                try:
                    logger.info(f"Trying attachment method {i+1} for {file_name}")
                    response = await client.post(
                        method['url'],
                        headers=headers,
                        json=method['data']
                    )
                    
                    if response.status_code in [200, 201, 204]:
                        logger.info(f"Successfully attached file {file_name} to BC invoice {invoice_id} using method {i+1}")
                        attachment_success = True
                        break
                    else:
                        logger.warning(f"Attachment method {i+1} failed: {response.status_code} - {response.text}")
                        
                except Exception as method_error:
                    logger.warning(f"Attachment method {i+1} error: {str(method_error)}")
                    continue
            
            if not attachment_success:
                logger.warning(f"All attachment methods failed for {file_name}")
                # Try to save attachment info in invoice notes/comments instead
                try:
                    comment_data = {
                        'comment': f"Original invoice file: {file_name} (attachment failed - file available at: {blob_url})"
                    }
                    response = await client.post(
                        f"{BC_BASE_URL}/purchaseInvoices('{invoice_id}')/comments",
                        headers=headers,
                        json=comment_data
                    )
                    if response.status_code in [200, 201, 204]:
                        logger.info(f"Added file reference as comment to invoice {invoice_id}")
                except:
                    logger.warning(f"Could not add file reference as comment")
        
    except Exception as e:
        logger.warning(f"Error attaching file to BC invoice: {str(e)}")
        # Don't fail the entire process if attachment fails


async def save_logging(invoice_json: Dict[str, Any], status: str, 
                      container: str = "parsed-invoices") -> None:
    """
    Save processing log to blob storage.
    """
    try:
        if not AZURE_STORAGE_CONNECTION_STRING:
            logger.warning("No storage connection string available, cannot save logs")
            return
            
        blob_service_client = azure.storage.blob.BlobServiceClient.from_connection_string(
            AZURE_STORAGE_CONNECTION_STRING
        )
        
        # Create log entry
        log_entry = {
            **invoice_json,
            'processing_status': status,
            'processed_at': datetime.datetime.utcnow().isoformat()
        }
        
        # Generate unique blob name
        timestamp = datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        original_name = invoice_json.get('original_filename', 'unknown').replace('/', '_').replace('\\', '_')
        blob_name = f"{timestamp}_{original_name}_{status.lower()}.json"
        
        blob_client = blob_service_client.get_blob_client(
            container=container,
            blob=blob_name
        )
        
        # Upload log
        blob_client.upload_blob(
            json.dumps(log_entry, indent=2, default=str),
            overwrite=True
        )
        
        logger.info(f"Saved processing log: {blob_name}")
        
    except Exception as e:
        logger.error(f"Error saving processing log: {str(e)}")
        # Don't fail the entire process if logging fails


# Azure Function v1 entry point
async def parse_pdf_invoice(blob: func.InputStream) -> None:
    """
    Azure Function triggered by blob upload to 'incoming-invoices' container.
    This is the entry point for the v1 programming model.
    """
    await main(blob)


# Azure Function v2 entry point (alternative)
# Uncomment the following lines if you want to use the v2 programming model
# and remove the function.json file

# app = func.FunctionApp()

# @app.blob_trigger(arg_name="blob", 
#                   path="incoming-invoices/{name}",
#                   connection="AzureWebJobsStorage")
# async def invoice_processor(blob: func.InputStream) -> None:
#     """
#     Azure Function triggered by blob upload to 'incoming-invoices' container.
#     This is the entry point for the v2 programming model.
#     """
#     await main(blob)