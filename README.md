# Azure Function - Invoice Processing

This Azure Function automatically processes invoices (PDF and XML) uploaded to the "incoming-invoices" blob container, extracts data using Azure Document Intelligence and XML parsing, maps GL accounts using OpenAI, and creates invoices in Microsoft Dynamics 365 Business Central.

## Features

- **Multi-format Support**: Processes both PDF and XML invoices
- **Azure Document Intelligence**: Extracts structured data from PDF invoices
- **XML Parsing**: Handles UBL 2.1 and other XML invoice formats
- **AI-Powered GL Mapping**: Uses OpenAI to determine appropriate General Ledger accounts
- **Business Central Integration**: Creates purchase invoices with proper line items
- **PO Invoice Handling**: Matches invoices to existing purchase orders
- **File Attachments**: Attaches original invoice files to Business Central invoices
- **Comprehensive Logging**: Saves processing logs to "parsed-invoices" container
- **Error Handling**: Graceful failure handling with detailed logging

## Prerequisites

1. Azure Function App (Python 3.9+)
2. Azure Storage Account with containers:
   - `incoming-invoices` (for uploaded invoices)
   - `parsed-invoices` (for processing logs)
   - `config` (for GL mappings configuration)
3. Azure Document Intelligence service
4. OpenAI Azure service
5. Microsoft Dynamics 365 Business Central with API access

## Environment Variables

Set the following environment variables in your Function App:

```
AzureWebJobsStorage=<storage_connection_string>
AZURE_STORAGE_CONNECTION_STRING=<storage_connection_string>
FORM_RECOGNIZER_KEY=<document_intelligence_key>
FORM_RECOGNIZER_ENDPOINT=<document_intelligence_endpoint>
OPENAI_API_KEY=<openai_api_key>
OPENAI_API_TYPE=azure
OPENAI_API_BASE=https://openai-invoice-model.openai.azure.com/
OPENAI_MODEL=gpt-4o-invoice
OPENAI_API_VERSION=2024-02-15-preview
FUNCTIONS_WORKER_RUNTIME=python
BC_TENANT_ID=a4a6e0c1-b531-4689-a0a0-1ad2250ba843
BC_CLIENT_SECRET=<business_central_client_secret>
BC_BASE_URL=https://api.businesscentral.dynamics.com/v2.0/a4a6e0c1-b531-4689-a0a0-1ad2250ba843/KnusWonen/ODataV4/Company('Knuswonen%20Henk')
```

## Setup Instructions

1. **Deploy the Function**:
   - Copy all files to your Azure Function App
   - Install dependencies from `requirements.txt`

2. **Configure GL Mappings**:
   - Upload `sample_gl_mappings.json` to the `config` container in your storage account
   - Rename it to `gl_mappings.json`
   - Customize the mappings according to your chart of accounts

3. **Business Central Setup**:
   - Ensure the application registration has proper permissions
   - Configure the API endpoints in Business Central
   - Test the connection using the provided client credentials

4. **Test the Function**:
   - Upload a test PDF or XML invoice to the `incoming-invoices` container
   - Monitor the function logs for processing status
   - Check the `parsed-invoices` container for processing logs
   - Verify invoice creation in Business Central

## File Structure

```
├── __init__.py              # Main function code
├── function.json           # Function binding configuration
├── host.json              # Function app configuration
├── requirements.txt       # Python dependencies
├── sample_gl_mappings.json # Sample GL account mappings
└── README.md             # This file
```

## Processing Flow

1. **Trigger**: Invoice file uploaded to `incoming-invoices` container
2. **File Type Detection**: Determines if file is PDF or XML
3. **Data Extraction**:
   - PDF: Uses Azure Document Intelligence
   - XML: Parses using lxml with UBL 2.1 support
4. **GL Account Mapping**: Uses OpenAI to determine appropriate GL accounts
5. **Business Central Processing**:
   - Non-PO invoices: Creates new purchase invoice
   - PO invoices: Matches to existing purchase order
6. **File Attachment**: Attaches original file to BC invoice
7. **Logging**: Saves processing results to `parsed-invoices` container

## Error Handling

- Individual line item failures don't stop the entire process
- Attachment failures are logged but don't fail the invoice creation
- GL mapping failures fall back to default GL account
- Comprehensive error logging for troubleshooting

## Monitoring

- Function execution logs are available in Azure Monitor
- Processing logs are saved to the `parsed-invoices` blob container
- Each processed invoice gets a JSON log with full details
- Success/failure status is clearly indicated in logs

## Customization

- Modify GL mappings in the `gl_mappings.json` file
- Adjust OpenAI prompts in the `determine_gl_accounts` function
- Customize Business Central field mappings as needed
- Add additional file format support by extending the parsing functions

## Support

For issues or questions:
1. Check the function logs in Azure Monitor
2. Review the processing logs in the `parsed-invoices` container
3. Verify all environment variables are correctly set
4. Test Business Central API connectivity separately