import os
import sys

try:
    import proxy_patch
except ImportError:
    try:
        from agent import proxy_patch
    except ImportError:
        pass
import sys
import json
import logging
import subprocess
import importlib
from typing import Any

# =========================================================================
# SDK MONKEYPATCH: Fix Pydantic AgentCard Serialization in vertexai SDK
# =========================================================================
from google.protobuf import json_format
from pydantic import BaseModel

original_message_to_json = json_format.MessageToJson

def patched_message_to_json(message, *args, **kwargs):
    if isinstance(message, BaseModel):
        return message.model_dump_json(exclude_none=True)
    elif isinstance(message, dict):
        return json.dumps(message)
    try:
        return original_message_to_json(message, *args, **kwargs)
    except AttributeError:
        return json.dumps(message)

json_format.MessageToJson = patched_message_to_json
# =========================================================================

import vertexai
from vertexai._genai import _agent_engines_utils
from vertexai._genai.types import AgentEngineConfig

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("deploy")

def generate_class_methods_from_agent(agent_instance: Any) -> list[dict[str, Any]]:
    """Generate method specifications with schemas from agent's register_operations()."""
    registered_operations = _agent_engines_utils._get_registered_operations(
        agent=agent_instance
    )
    class_methods_spec = _agent_engines_utils._generate_class_methods_spec_or_raise(
        agent=agent_instance,
        operations=registered_operations,
    )
    class_methods_list = [
        _agent_engines_utils._to_dict(method_spec) for method_spec in class_methods_spec
    ]
    return class_methods_list

def main():
    PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not PROJECT_ID:
        print("❌ ERROR: GOOGLE_CLOUD_PROJECT environment variable is not set. Run this in Cloud Shell or set the variable.")
        sys.exit(1)

    LOCATION = os.environ.get("GOOGLE_CLOUD_REGION", "us-central1")
    REQUIREMENTS_FILE = "requirements.txt"
    AGENT_NAME = "price-match-agent"
    DISPLAY_NAME = "Price Match Agent"
    MODULE_PATH = "price_match_agent"
    OBJECT_NAME = "agent_engine"

    print("============================================================")
    print("🚀 DEPLOYING PRICE MATCH AGENT TO VERTEX AI RUNTIME 🚀")
    print("============================================================")
    print(f"👉 Target Project: {PROJECT_ID}")
    print(f"👉 Target Location: {LOCATION}")
    print(f"👉 Requirements File: {REQUIREMENTS_FILE}")
    print("────────────────────────────────────────────────────────────")

    # 1. Initialize the Vertex AI client (override ADC with active gcloud token if possible)
    creds = None
    try:
        import google.oauth2.credentials
        token_proc = subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True, text=True, check=True)
        token = token_proc.stdout.strip()
        creds = google.oauth2.credentials.Credentials(token)
        print("  🔑 Successfully resolved authentication token from active gcloud config.")
    except Exception as e:
        print(f"  ℹ️ Could not resolve active gcloud config token ({e}), falling back to Application Default Credentials.")

    vertexai.init(project=PROJECT_ID, location=LOCATION, credentials=creds)
    
    from vertexai._genai.client import Client
    client = Client(project=PROJECT_ID, location=LOCATION, credentials=creds)

    sys.path.insert(0, os.getcwd())

    env_vars = {
        "GCP_PROJECT_ID": PROJECT_ID,
        "GOOGLE_CLOUD_REGION": "global",
        "GOOGLE_CLOUD_LOCATION": "global",
        "VERTEXAI_LOCATION": "global",
        "GEMINI_LOCATION": "global",
        "REGIONAL_REGISTRY_LOCATION": LOCATION,
        "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY": "true",
        "OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED": "true",
        "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "SPAN_AND_EVENT",
        "GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES": "false",
        "OTEL_INSTRUMENTATION_A2A_SDK_ENABLED": "false",
        "OTEL_TRACES_SAMPLER": "always_on",
        "GEMINI_MODEL": os.environ.get("GEMINI_MODEL", "gemini-3.6-flash"),
    }

    strategy_agent_urn = os.environ.get("STRATEGY_AGENT_URN")
    if strategy_agent_urn:
        env_vars["STRATEGY_AGENT_URN"] = strategy_agent_urn

    # 2. Import the agent instance to generate class methods schemas
    print(f"🔍 Inspecting entrypoint: {MODULE_PATH}.{OBJECT_NAME}...")
    try:
        module = importlib.import_module(MODULE_PATH)
        agent_instance = getattr(module, OBJECT_NAME)
    except Exception as e:
        print(f"  ❌ Failed to import agent entrypoint: {e}")
        sys.exit(1)
        
    class_methods_list = generate_class_methods_from_agent(agent_instance)
    class_methods_list.append({
        "name": "async_stream_query",
        "api_mode": "async_stream"
    })
    class_methods_list.append({
        "name": "query",
        "api_mode": ""
    })
    
    # 3. Check for active existing engine to reuse without re-deploying or deleting
    print(f"🔍 Checking for active engine matching '{DISPLAY_NAME}'...")
    try:
        for eng in client.agent_engines.list():
            res = eng.api_resource
            if getattr(res, "display_name", "") in [AGENT_NAME, DISPLAY_NAME] or res.name.split("/")[-1] == AGENT_NAME:
                engine_urn = res.name
                engine_id = engine_urn.split("/")[-1]
                print(f"   ⚡ Found active existing engine: {engine_urn}! Reusing without re-deploying...")
                print("\n============================================================")
                print("🎉 SUCCESS! Price Match Agent Deployed & Secured!")
                print("============================================================")
                print(f"👉 Agent Engine ID: {engine_id}")
                print(f"👉 Agent URN: {engine_urn}")
                return
    except Exception as list_err:
        print(f"   ⚠️ Could not scan existing engines: {list_err}")

    # 4. Build configuration block
    config = AgentEngineConfig(
        display_name=DISPLAY_NAME,
        description="Front-line Price Match Verification Agent. Calculates discounts and approves <= 10% directly.",
        source_packages=["."],
        entrypoint_module=MODULE_PATH,
        entrypoint_object=OBJECT_NAME,
        class_methods=class_methods_list,
        env_vars=env_vars,
        requirements_file=REQUIREMENTS_FILE,
        min_instances=1,
        max_instances=3,
        agent_framework="google-adk",
        identity_type="AGENT_IDENTITY", # Uses native system-managed Agent Identity!
    )
    
    print(f"🚀 Deploying Reasoning Engine instance to Vertex AI...")
    try:
        remote_agent = client.agent_engines.create(config=config)
        engine_urn = remote_agent.api_resource.name
        engine_id = engine_urn.split("/")[-1]
        effective_identity = getattr(remote_agent.api_resource.spec, "effective_identity", None)
        system_sa = getattr(remote_agent.api_resource, "service_account", None)
        print(f"  ✅ Deployed successfully! URN: {engine_urn}")
        print(f"     🛡️ Provisioned Agent Identity (System SA): {system_sa}")
        print(f"     🔗 Workload Identity Principal: {effective_identity}")
    except Exception as e:
        print(f"  ❌ Failed to deploy agent: {e}")
        sys.exit(1)

    print("\n============================================================")
    print("🎉 SUCCESS! Price Match Agent Deployed & Secured!")
    print("============================================================")
    print(f"👉 Agent Engine ID: {engine_id}")
    print(f"👉 Agent URN: {engine_urn}")
    print("────────────────────────────────────────────────────────────")
    print("💡 To test the agent runtime, execute the following command:")
    print("────────────────────────────────────────────────────────────")
    print(f"curl -X POST \\")
    print(f"  -H \"Authorization: Bearer $(gcloud auth print-access-token)\" \\")
    print(f"  -H \"Content-Type: application/json\" \\")
    print(f"  -d '{{\"input\": \"verify competitor price match for Barista Pro Espresso Machine SKU-HSE-4455 original price 450 requested price 400\"}}' \\")
    print(f"  \"https://{LOCATION}-aiplatform.googleapis.com/v1/projects/{PROJECT_ID}/locations/{LOCATION}/reasoningEngines/{engine_id}:streamQuery?alt=sse\"")
    print("============================================================")

if __name__ == "__main__":
    main()