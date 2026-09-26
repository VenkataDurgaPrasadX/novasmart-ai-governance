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
from google.adk.tools import FunctionTool

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
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://localhost:8080")

def query_database(query: str, max_results: int = 20) -> str:
    """
    Invoke the generic tool call on novasmart-mcp to query BigQuery databases (such as customer_data.customers).
    Token Forwarding is automatically enforced by transmitting the agent's OAuth runtime credentials.
    """
    import urllib.request
    import json
    import google.auth
    import google.auth.transport.requests
    try:
        url = f"{MCP_SERVER_URL.rstrip('/')}/tools/query_database"
        payload = json.dumps({"query": query, "max_results": max_results}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        try:
            credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform", "https://www.googleapis.com/auth/bigquery"])
            credentials.refresh(google.auth.transport.requests.Request())
            headers["Authorization"] = f"Bearer {credentials.token}"
        except Exception as auth_err:
            logger.warning(f"Could not refresh OAuth token: {auth_err}")
            
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:
        logger.error(f"Error calling novasmart-mcp query_database tool: {e}")
        return f"Error executing tool query_database: {str(e)}"

query_db_tool = FunctionTool(func=query_database)

SYSTEM_INSTRUCTION = """
You are NovaSmart's official Customer Personalization Agent, deployed on Google Cloud managed Agent Runtime.
Your mission is to analyze customer profiles and shopping history in `customer_data.customers` using the `query_database` tool via novasmart-mcp.

When asked to personalize offers or analyze client records:
1. Always query `SELECT * FROM customer_data.customers` or apply specific filtering by customer_id or loyalty_tier.
2. Formulate tailored reward strategies based on loyalty tiers (Platinum, Gold, Silver, Bronze) and lifetime_value.
3. Preserve analytical accuracy and record all reasoning steps clearly.
"""

customer_personalization_agent = Agent(
    name="customer_personalization_agent",
    model=Gemini(model=GEMINI_MODEL),
    instruction=SYSTEM_INSTRUCTION,
    tools=[query_db_tool]
)

# =========================================================================
# Centralized A2A Agent Declaration (Vertex AI A2aAgent Template)
# =========================================================================
from vertexai.preview.reasoning_engines import A2aAgent
from a2a.types import AgentCard, AgentCapabilities
from google.adk.a2a.executor.a2a_agent_executor import A2aAgentExecutor
from google.adk.runners import Runner

customer_agent_card = AgentCard(
    name="customer-personalization-agent",
    description="Official managed agent querying customer loyalty tiers and PII via novasmart-mcp.",
    version="1.0",
    url="https://dummy.com",
    capabilities=AgentCapabilities(streaming=True),
    defaultInputModes=["text"],
    defaultOutputModes=["text"],
    skills=[],
    preferredTransport="HTTP+JSON",
    supports_authenticated_extended_card=True,
)

def build_customer_executor():
    from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService
    from google.adk.sessions.in_memory_session_service import InMemorySessionService
    from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
    from google.adk.auth.credential_service.in_memory_credential_service import InMemoryCredentialService

    runner = Runner(
        app_name="customer-personalization-agent",
        agent=customer_personalization_agent,
        artifact_service=InMemoryArtifactService(),
        session_service=InMemorySessionService(),
        memory_service=InMemoryMemoryService(),
        credential_service=InMemoryCredentialService(),
    )
    return A2aAgentExecutor(runner=runner)

# Expose the pure A2A Agent template for Vertex AI Agent Engine deployment
agent_engine = A2aAgent(
    agent_card=customer_agent_card,
    agent_executor_builder=build_customer_executor
)
