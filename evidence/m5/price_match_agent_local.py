import os
import sys

try:
    import proxy_patch
except ImportError:
    try:
        from agent import proxy_patch
    except ImportError:
        pass
os.environ["GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY"] = "true"
os.environ["OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED"] = "true"
os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] = "gen_ai_latest_experimental"
os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "SPAN_AND_EVENT"
os.environ["GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES"] = "false"
os.environ["OTEL_INSTRUMENTATION_A2A_SDK_ENABLED"] = "false"

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

def _resolve_region():
    import urllib.request
    try:
        url = "http://metadata.google.internal/computeMetadata/v1/instance/zone"
        req = urllib.request.Request(url, headers={"Metadata-Flavor": "Google"})
        with urllib.request.urlopen(req, timeout=0.2) as response:
            zone = response.read().decode().strip().split('/')[-1]
            return "-".join(zone.split("-")[:-1])
    except Exception:
        return "us-central1"

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
REGIONAL_LOCATION = os.environ.get("REGIONAL_REGISTRY_LOCATION", "us-central1") # Hardcoded to us-central1 for regional Agent Registry
GEMINI_LOCATION = os.environ.get("VERTEXAI_LOCATION", "global")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")


def query_competitor_pricing(sku: str, competitor_price: float) -> str:
    """
    Queries the BigQuery database to retrieve competitor pricing and stock levels for a given SKU.

    Args:
        sku: The product SKU (e.g. 'SKU-HSE-4455')
        competitor_price: The competitor's price to look up (e.g. 380.00)

    Returns:
        A JSON string containing the competitor's name, price, and stock levels.
    """
    import json
    from google.cloud import bigquery

    print(f"[Competitor Tool] Looking up SKU {sku} at competitor price ${competitor_price:.2f}...")

    try:
        client = bigquery.Client(project=PROJECT_ID)
    except Exception as init_err:
        return json.dumps({"error": f"Error initializing BigQuery client: {init_err}"})

    # Query matching competitor
    comp_query = f"""
        SELECT competitor_name, competitor_price, competitor_stock 
        FROM `{PROJECT_ID}.competitor_data.prices` 
        WHERE sku = '{sku}' AND competitor_price = {competitor_price}
    """
    try:
        comp_results = list(client.query(comp_query).result())
        if not comp_results:
            # Fallback: Query all listings for this SKU to help the agent suggest alternatives
            fallback_query = f"""
                SELECT competitor_name, competitor_price, competitor_stock 
                FROM `{PROJECT_ID}.competitor_data.prices` 
                WHERE sku = '{sku}'
            """
            fallback_results = list(client.query(fallback_query).result())
            records = [
                {
                    "competitor_name": row["competitor_name"],
                    "competitor_price": float(row["competitor_price"]),
                    "competitor_stock": int(row["competitor_stock"])
                }
                for row in fallback_results
            ]
            return json.dumps({
                "status": "NOT_FOUND",
                "message": f"No competitor is verified selling at ${competitor_price:.2f}.",
                "listings": records
            })
        
        record = {
            "status": "FOUND",
            "competitor_name": comp_results[0]["competitor_name"],
            "competitor_price": float(competitor_price),
            "competitor_stock": int(comp_results[0]["competitor_stock"])
        }
        return json.dumps(record)
    except Exception as e:
        return json.dumps({"error": f"Error querying competitor pricing data: {e}"})
def escalate_to_strategy_agent(sku: str, requested_price: float) -> str:
    return "Local evaluation: escalation to Markdown Strategy Agent is disabled locally."


# Expose root_agent for ADK app loaders
root_agent = price_match_agent
app = adk_app
