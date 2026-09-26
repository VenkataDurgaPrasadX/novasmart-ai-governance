import os
import logging
import google.auth
import google.auth.transport.requests
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams

logger = logging.getLogger(__name__)

BIGQUERY_MCP_URL = "https://bigquery.googleapis.com/mcp"
_bigquery_toolset = None

def get_bigquery_mcp_toolset():
    """
    Get the MCPToolset connected to Google's pre-built, managed BigQuery MCP server (OneMCP).
    
    Exposes BigQuery's pre-built MCP tools with a dynamic header provider to prevent token expiration.
    """
    global _bigquery_toolset
    
    if _bigquery_toolset is not None:
        return _bigquery_toolset
    
    logger.info("[BigQuery MCP] Connecting to OneMCP BigQuery at %s...", BIGQUERY_MCP_URL)
    
    PROJECT_ID = os.environ.get("GCP_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")

    # Define a dynamic header provider that refreshes the Google credentials on every single tool execution
    def dynamic_header_provider(context=None) -> dict:
        try:
            # 1. Fetch Application Default Credentials (ADC) with full cloud-platform scope
            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            # 2. Refresh the credentials to get a fresh, active OAuth token
            credentials.refresh(google.auth.transport.requests.Request())
            
            return {
                "Authorization": f"Bearer {credentials.token}",
                "x-goog-user-project": PROJECT_ID
            }
        except Exception as e:
            logger.error(f"❌ Failed to refresh BigQuery OAuth token: {e}")
            return {}
    
    # 3. Create the MCPToolset using StreamableHTTP connection and the dynamic header provider
    _bigquery_toolset = MCPToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=BIGQUERY_MCP_URL
        ),
        header_provider=dynamic_header_provider
    )
    
    logger.info("[BigQuery MCP] Connected successfully to BigQuery MCP with dynamic authentication")
    return _bigquery_toolset
