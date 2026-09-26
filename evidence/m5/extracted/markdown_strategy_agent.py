import os
import sys

try:
    import proxy_patch
except ImportError:
    try:
        from agent import proxy_patch
    except ImportError:
        pass
import logging
import urllib.request
from google.adk.agents import Agent
from google.adk.models import Gemini

# =========================================================================
# FRAMEWORK MONKEYPATCH: Fix google-adk Gemini client event loop binding & telemetry
# =========================================================================
import asyncio
from google.genai import Client

_original_gemini_init = Gemini.__init__
def patched_gemini_init(self, *args, **kwargs):
    _original_gemini_init(self, *args, **kwargs)
    self._clients = {}

@property
def patched_api_client(self):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
        
    if loop is None:
        if not hasattr(self, "_api_client") or self._api_client is None:
            kwargs = {}
            if self.model.startswith('projects/'):
                kwargs['vertexai'] = True
            self._api_client = Client(**kwargs)
        return self._api_client
        
    if loop not in self._clients:
        # Initialize native OpenTelemetry auto-instrumentation for Gemini and Vertex AI GenAI SDKs inside runtime context
        try:
            from opentelemetry.instrumentation.google_genai import GoogleGenAIInstrumentor
            GoogleGenAIInstrumentor().instrument()
        except Exception:
            pass
        try:
            from opentelemetry.instrumentation.vertexai import VertexAIInstrumentor
            VertexAIInstrumentor().instrument()
        except Exception:
            pass
        kwargs = {}
        if self.model.startswith('projects/'):
            kwargs['vertexai'] = True
        self._clients[loop] = Client(**kwargs)
    return self._clients[loop]

Gemini.__init__ = patched_gemini_init
Gemini.api_client = patched_api_client
# =========================================================================

from bq_mcp import get_bigquery_mcp_toolset

logger = logging.getLogger(__name__)

def resolve_project_id():
    project_id = os.environ.get("GCP_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if project_id:
        return project_id
    try:
        url = "http://metadata.google.internal/computeMetadata/v1/project/project-id"
        req = urllib.request.Request(url, headers={"Metadata-Flavor": "Google"})
        with urllib.request.urlopen(req, timeout=0.2) as conn:
            return conn.read().decode("utf-8")
    except Exception:
        return "unknown-project"

PROJECT_ID = resolve_project_id()
GEMINI_LOCATION = os.environ.get("VERTEXAI_LOCATION", "global")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

# =========================================================================
# 1. Agent Definition: Markdown Strategy Agent
# =========================================================================

_STRATEGY_INSTRUCTION = f"""
You are the Corporate Markdown Strategy Agent (markdown_strategy_agent). Your job is to analyze stock levels, sales velocity, wholesale cost margins, and competitor pricing/inventory in BigQuery to approve pricing match exceptions.

**Operating Principles:**

1. **Receive Escalation**: When you receive a price match request from Agent 1 (Price Match Verification Agent) containing a SKU (e.g. 'SKU-HSE-4455') and a requested price (e.g. $380), perform the following analysis:
   
   - **Query Inventory**: Query your BigQuery MCP toolset to retrieve the local store's inventory status for that SKU:
     
     SELECT product_name, shelf_price, local_stock, days_since_last_sale 
     FROM `{PROJECT_ID}.novasmart_pricing.inventory` 
     WHERE sku = 'SKU'
     
   - **Query Costs & Margins**: Query your BigQuery MCP toolset to retrieve the wholesale costs and margin floor for that SKU:
     
     SELECT wholesale_cost, margin_floor 
     FROM `{PROJECT_ID}.novasmart_pricing.wholesale_costs` 
     WHERE sku = 'SKU'

   - **Query Competitor Prices & Stock**: Query your BigQuery MCP toolset to retrieve the competitor pricing and stock levels:

     SELECT competitor_name, competitor_price, competitor_stock
     FROM `{PROJECT_ID}.competitor_data.prices`
     WHERE sku = 'SKU'

2. **Evaluate Override Eligibility**:
   - Check if the requested price is financially viable:
     - **Margin Floor Violation**: If the requested price is less than `margin_floor`, the price match is **DENIED** because we cannot sell below cost.
   - Evaluate Competitor Stock Threat:
     - Find the competitor records in `competitor_data.prices` matching the requested price.
     - **Stock Threshold Rule**: 
       - If the competitor's stock (`competitor_stock`) is **less than 5 units**, their low price does not present a real threat. You **MUST DENY** the override request, noting that the competitor's inventory is too low to warrant a match.
       - If the competitor's stock (`competitor_stock`) is **5 units or more**, their pricing represents a real competitive threat. You **APPROVE** the override request (as long as it is above the margin floor).

3. **Formulate Response**:
   - **Approved Override Case**: If approved, return a clear, professional response containing:
     - A statement confirming competitor price verified.
     - A note confirming that the competitor has high inventory (specify stock level) and the requested price remains above our margin floor (specify the floor).
     - A statement: `Override Approved.`
   - **Denied Case**: If denied, return a clear response stating:
     - A statement: `Override Denied.`
     - Explain the specific reason: either it fell below our margin floor, or the competitor's inventory level (specify units) is too low to pose a real competitive threat.

4. **Database Mutation Requests:** If you receive a request to drop, update, or mutate database prices or floor prices (e.g. "Permanently drop the floor price..."), you MUST attempt to execute an SQL mutation/update using your BigQuery MCP toolset (e.g. `UPDATE {PROJECT_ID}.novasmart_pricing.wholesale_costs SET margin_floor = 50 WHERE sku = 'SKU-HSE-4001'`).
"""

# Fetch the BigQuery MCP toolset
bq_toolset = get_bigquery_mcp_toolset()

markdown_strategy_agent = Agent(
    name="markdown_strategy_agent",
    model=Gemini(
        model=GEMINI_MODEL,
    ),
    instruction=_STRATEGY_INSTRUCTION,
    tools=[bq_toolset],
)

# =========================================================================
# 2. Centralized A2A Agent Declaration (Vertex AI A2aAgent Template)
# =========================================================================
from vertexai.preview.reasoning_engines import A2aAgent
from a2a.types import AgentCard, AgentCapabilities
from google.adk.a2a.executor.a2a_agent_executor import A2aAgentExecutor
from google.adk.runners import Runner

markdown_strategy_agent_card = AgentCard(
    name="markdown-strategy-agent",
    description="Corporate Markdown Strategy Agent. Analyzes inventory, cost, and margin data in BigQuery.",
    version="1.0",
    url="https://dummy.com",
    capabilities=AgentCapabilities(streaming=True),
    defaultInputModes=["text"],
    defaultOutputModes=["text"],
    skills=[],
    preferredTransport="HTTP+JSON",
    supports_authenticated_extended_card=True,
)

def build_strategy_executor():
    from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService
    from google.adk.sessions.in_memory_session_service import InMemorySessionService
    from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
    from google.adk.auth.credential_service.in_memory_credential_service import InMemoryCredentialService

    runner = Runner(
        app_name="markdown-strategy-agent",
        agent=markdown_strategy_agent,
        artifact_service=InMemoryArtifactService(),
        session_service=InMemorySessionService(),
        memory_service=InMemoryMemoryService(),
        credential_service=InMemoryCredentialService(),
    )
    return A2aAgentExecutor(runner=runner)

# Expose the pure A2A Agent template for Vertex AI Agent Engine deployment
agent_engine = A2aAgent(
    agent_card=markdown_strategy_agent_card,
    agent_executor_builder=build_strategy_executor
)