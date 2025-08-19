## Business Central PO-to-Invoice Utility

Utility script `codeUpdate_with_attachments.py` automates:
- Extracting PO number from a vendor PDF invoice
- Fetching the PO and remaining-to-invoice lines from Microsoft Dynamics 365 Business Central (BC)
- Creating and posting a related Purchase Invoice
- Uploading the PDF as an attachment to the posted invoice

### Setup

1. Create an Azure AD app and grant Business Central API permissions.
2. Collect:
   - `BC_TENANT_ID`
   - `BC_CLIENT_ID`
   - `BC_CLIENT_SECRET`
   - One of: `BC_COMPANY_ID` (GUID) or `BC_COMPANY_NAME`
   - Optional: `BC_ENVIRONMENT` (default: `Production`), `BC_BASE_URL` (default: `https://api.businesscentral.dynamics.com`)
3. Install dependencies:

```bash
pip install -r requirements.txt
```

### Usage

```bash
python codeUpdate_with_attachments.py \
  --pdf /absolute/path/to/invoice.pdf \
  --vendor-invoice-number INV-12345
```

Environment variables can be used instead of flags:

```bash
export BC_TENANT_ID=... 
export BC_CLIENT_ID=...
export BC_CLIENT_SECRET=...
export BC_COMPANY_ID=...   # or set BC_COMPANY_NAME
python codeUpdate_with_attachments.py --pdf /path/to/invoice.pdf
```

Optional flag to adjust PO extraction regex:

```bash
--po-regex "(?i)PO\s*(?:NUMBER|No\.|#)?\s*[:\-]?\s*([A-Z0-9\-]+)"
```

Notes:
- The script expects the PO number to exist in BC.
- Attachment endpoint support can vary by BC version; failures are logged as warnings.