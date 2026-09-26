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

import subprocess
import vertexai
from vertexai._genai.client import Client
from vertexai._genai.types import AgentEngineConfig
import google.auth
from google.auth.transport.requests import Request

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT")
LOCATION = os.environ.get("GOOGLE_CLOUD_REGION", "us-central1")
AGENT_NAME = "markdown-strategy-agent"
DISPLAY_NAME = "Markdown Strategy Agent"
MODULE_PATH = "markdown_strategy_agent"
OBJECT_NAME = "agent_engine"
REQUIREMENTS_FILE = "requirements.txt"

def main():
    if not PROJECT_ID:
        print("❌ ERROR: GOOGLE_CLOUD_PROJECT is not set.")
        sys.exit(1)
        
    print(f"🚀 Deploying Markdown Strategy Agent in {PROJECT_ID}...")
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
    client = Client(project=PROJECT_ID, location=LOCATION, credentials=creds)
    
    # Check for active strategy agent to reuse without deleting
    print("🔍 Checking for active strategy engine matching 'Markdown Strategy Agent'...")
    try:
        for engine in client.agent_engines.list():
            display_name = getattr(engine.api_resource, "display_name", "")
            if display_name == DISPLAY_NAME or "Markdown Strategy Agent" in display_name or "markdown-strategy-agent" in display_name:
                engine_urn = engine.api_resource.name
                print(f"   ⚡ Found active existing strategy engine: {engine_urn}! Reusing without re-deploying...")
                with open("/tmp/strategy_agent_id.txt", "w") as f:
                    f.write(engine_urn)
                print("  ✅ Strategy Agent ID written to /tmp/strategy_agent_id.txt")
                return
    except Exception as cleanup_err:
        print(f"   ⚠️ Cleanup warning: {cleanup_err}")
    
    # Configure environment variables for the strategy agent container
    env_vars = {
        "GCP_PROJECT_ID": PROJECT_ID,
        "GOOGLE_CLOUD_REGION": "global",
        "GOOGLE_CLOUD_LOCATION": "global",
        "VERTEXAI_LOCATION": "global",
        "GEMINI_LOCATION": "global",
        "REGIONAL_REGISTRY_LOCATION": LOCATION,
        "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY": "true",
        "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "span_and_event",
        "GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES": "false",
        "OTEL_TRACES_SAMPLER": "always_on",
        "OTEL_INSTRUMENTATION_A2A_SDK_ENABLED": "false",
        "GEMINI_MODEL": os.environ.get("GEMINI_MODEL", "gemini-3.6-flash"),
    }
    
    # Dynamically import strategy agent instance to inspect class methods schemas
    sys.path.insert(0, os.getcwd())
    try:
        from markdown_strategy_agent import agent_engine as strategy_instance
        from vertexai._genai import _agent_engines_utils
        registered_ops = _agent_engines_utils._get_registered_operations(agent=strategy_instance)
        class_methods_spec = _agent_engines_utils._generate_class_methods_spec_or_raise(
            agent=strategy_instance,
            operations=registered_ops
        )
        class_methods_list = [_agent_engines_utils._to_dict(m) for m in class_methods_spec]
        print(f"  ✅ Dynamically resolved A2A class methods spec ({len(class_methods_list)} operations).")
    except Exception as inspect_err:
        print(f"  ❌ Failed to generate class methods schema dynamically: {inspect_err}")
        sys.exit(1)

    # Build config
    config = AgentEngineConfig(
        display_name=DISPLAY_NAME,
        description="Markdown Strategy Agent. Analyzes stock levels and margins in BigQuery.",
        source_packages=["."],
        entrypoint_module=MODULE_PATH,
        entrypoint_object=OBJECT_NAME,
        class_methods=class_methods_list,
        env_vars=env_vars,
        requirements_file=REQUIREMENTS_FILE,
        min_instances=1,
        max_instances=3,
        agent_framework="google-adk",
        identity_type="AGENT_IDENTITY"
    )
    
    try:
        remote_agent = client.agent_engines.create(config=config)
        engine_urn = remote_agent.api_resource.name
        engine_id = engine_urn.split("/")[-1]
        effective_identity = getattr(remote_agent.api_resource.spec, "effective_identity", None)
        system_sa = getattr(remote_agent.api_resource, "service_account", None)
        print(f"  ✅ Deployed Strategy Agent successfully! URN: {engine_urn}")
        print(f"     🛡️ Provisioned Strategy Agent Identity (System SA): {system_sa}")
        print(f"     🔗 Workload Identity Principal: {effective_identity}")
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"  ❌ Failed to deploy strategy agent: {e}")
        sys.exit(1)
        
    # Write the URN to local file
    with open("/tmp/strategy_agent_id.txt", "w") as f:
        f.write(engine_urn)
    print("  ✅ Strategy Agent ID written to /tmp/strategy_agent_id.txt")

    # Automatically register/update the Markdown Strategy Agent in Agent Registry for dynamic discovery
    print(f"📡 Registering '{engine_urn}' in Agent Registry (services/markdown-strategy-agent)...")
    try:
        import requests
        for host in [f"{LOCATION}-agentregistry.googleapis.com", "agentregistry.googleapis.com"]:
            reg_url = f"https://{host}/v1alpha/projects/{PROJECT_ID}/locations/{LOCATION}/services"
            reg_headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            a2a_url = f"https://{LOCATION}-aiplatform.googleapis.com/v1beta1/{engine_urn}/a2a"
            reg_payload = {
                "displayName": "markdown-strategy-agent",
                "agentSpec": {
                    "content": {
                        "url": a2a_url
                    }
                }
            }
            res = requests.post(f"{reg_url}?serviceId=markdown-strategy-agent", headers=reg_headers, json=reg_payload, verify=False)
            if res.status_code in [409, 400]:
                patch_url = f"{reg_url}/markdown-strategy-agent?updateMask=agentSpec.content.url"
                res = requests.patch(patch_url, headers=reg_headers, json={"agentSpec": {"content": {"url": a2a_url}}}, verify=False)
            print(f"  ✅ Agent Registry update on {host}: HTTP {res.status_code}")
    except Exception as reg_err:
        print(f"  ⚠️ Could not update Agent Registry: {reg_err}")

if __name__ == "__main__":
    main()