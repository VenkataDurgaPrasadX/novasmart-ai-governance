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
from vertexai._genai.client import Client
import google.auth
from google.auth.transport.requests import Request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("deploy-customer-agent")

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
    PROJECT_ID = os.environ.get("GCP_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    LOCATION = os.environ.get("GCP_LOCATION") or os.environ.get("GOOGLE_CLOUD_REGION", "us-central1")
    SHARED_SA = os.environ.get("SHARED_SA_EMAIL")
    MCP_SERVER_URL = (os.environ.get("MCP_SERVER_URL") or "https://novasmart-mcp").strip()
    STAGING_BUCKET = os.environ.get("STAGING_BUCKET")
    REQUIREMENTS_FILE = "requirements.txt"
    AGENT_NAME = "customer-personalization-agent"
    DISPLAY_NAME = "Customer Personalization Agent"
    MODULE_PATH = "customer_personalization_agent"
    OBJECT_NAME = "agent_engine"

    if not PROJECT_ID or not SHARED_SA:
        print("❌ Missing required environment variables (GCP_PROJECT_ID, SHARED_SA_EMAIL).")
        sys.exit(1)

    print("============================================================")
    print("🚀 DEPLOYING CUSTOMER PERSONALIZATION AGENT TO VERTEX AI 🚀")
    print("============================================================")
    print(f"👉 Target Project: {PROJECT_ID}")
    print(f"👉 Target Location: {LOCATION}")
    print(f"👉 Shared Service Account: {SHARED_SA}")
    print("────────────────────────────────────────────────────────────")

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

    sys.path.insert(0, os.getcwd())

    env_vars = {
        "GCP_PROJECT_ID": PROJECT_ID,
        "GCP_LOCATION": LOCATION,
        "GOOGLE_CLOUD_REGION": "global",
        "GOOGLE_CLOUD_LOCATION": "global",
        "VERTEXAI_LOCATION": "global",
        "GEMINI_LOCATION": "global",
        "REGIONAL_REGISTRY_LOCATION": LOCATION,
        "MCP_SERVER_URL": MCP_SERVER_URL,
        "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY": "true",
        "OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED": "true",
        "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "SPAN_AND_EVENT",
        "GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES": "false",
        "OTEL_INSTRUMENTATION_A2A_SDK_ENABLED": "false",
        "OTEL_TRACES_SAMPLER": "always_on",
        "GEMINI_MODEL": os.environ.get("GEMINI_MODEL", "gemini-3.6-flash"),
    }

    print(f"🔍 Inspecting entrypoint: {MODULE_PATH}.{OBJECT_NAME}...")
    try:
        module = importlib.import_module(MODULE_PATH)
        agent_instance = getattr(module, OBJECT_NAME)
    except Exception as e:
        print(f"  ❌ Failed to import agent entrypoint: {e}")
        sys.exit(1)

    class_methods_list = generate_class_methods_from_agent(agent_instance)
    class_methods_list.append({
        "name": "query",
        "api_mode": ""
    })

    print(f"🔍 Checking for active customer engine matching '{DISPLAY_NAME}'...")
    try:
        for eng in client.agent_engines.list():
            res = eng.api_resource
            if getattr(res, "display_name", "") == DISPLAY_NAME or "Customer Personalization" in getattr(res, "display_name", ""):
                engine_urn = res.name
                engine_id = engine_urn.split("/")[-1]
                print(f"   ⚡ Found active existing engine: {engine_urn}! Reusing without re-deploying...")
                with open("/tmp/customer_personalization_agent_id.txt", "w") as f:
                    f.write(engine_urn)
                print("  ✅ Reused Agent URN written to /tmp/customer_personalization_agent_id.txt")
                try:
                    staging_bucket = os.environ.get("STAGING_BUCKET")
                    if staging_bucket:
                        from google.cloud import storage
                        st_client = storage.Client(project=PROJECT_ID)
                        bucket_name = staging_bucket.replace("gs://", "").split("/")[0]
                        st_client.bucket(bucket_name).blob("customer_personalization_agent_id.txt").upload_from_string(engine_id)
                except Exception as st_err:
                    print(f"  ⚠️ Note on Cloud Storage sync: {st_err}")
                try:
                    subprocess.run([
                        "gcloud", "run", "services", "update", "novasmart-store-portal",
                        f"--region={LOCATION}", f"--project={PROJECT_ID}",
                        f"--update-env-vars=CUSTOMER_AGENT_ID={engine_id}", "--quiet"
                    ], check=False)
                    print(f"  ✅ Linked CUSTOMER_AGENT_ID ({engine_id}) into Cloud Run service novasmart-store-portal!")
                except Exception as run_err:
                    print(f"  ⚠️ Could not update portal service: {run_err}")
                return
    except Exception as list_err:
        print(f"   ⚠️ Could not scan existing engines: {list_err}")

    config_kwargs = dict(
        display_name=DISPLAY_NAME,
        description="Official managed agent querying customer loyalty tiers and PII via novasmart-mcp.",
        source_packages=["."],
        entrypoint_module=MODULE_PATH,
        entrypoint_object=OBJECT_NAME,
        class_methods=class_methods_list,
        env_vars=env_vars,
        requirements_file=REQUIREMENTS_FILE,
        min_instances=1,
        max_instances=3,
        agent_framework="google-adk",
        service_account=SHARED_SA,
    )
    config = AgentEngineConfig(**config_kwargs)

    print(f"🚀 Deploying Reasoning Engine instance to Vertex AI under Shared Account {SHARED_SA}...")
    try:
        remote_agent = client.agent_engines.create(config=config)
        engine_urn = remote_agent.api_resource.name
        engine_id = engine_urn.split("/")[-1]
        effective_identity = getattr(remote_agent.api_resource.spec, "effective_identity", None)
        system_sa = getattr(remote_agent.api_resource, "service_account", None)
        print(f"  ✅ Deployed successfully! URN: {engine_urn}")
        print(f"     🛡️ Provisioned Agent Identity / Service Account: {system_sa}")
        print(f"     🔗 Workload Identity Principal: {effective_identity}")
        
        with open("/tmp/customer_personalization_agent_id.txt", "w") as f:
            f.write(engine_urn)
        print("  ✅ Agent URN written to /tmp/customer_personalization_agent_id.txt")
        
        try:
            staging_bucket = os.environ.get("STAGING_BUCKET")
            if staging_bucket:
                from google.cloud import storage
                st_client = storage.Client(project=PROJECT_ID)
                bucket_name = staging_bucket.replace("gs://", "").split("/")[0]
                st_client.bucket(bucket_name).blob("customer_personalization_agent_id.txt").upload_from_string(engine_id)
                print(f"  ✅ Uploaded Customer Personalization Agent ID ({engine_id}) to gs://{bucket_name}/customer_personalization_agent_id.txt")
        except Exception as st_err:
            print(f"  ⚠️ Note on Cloud Storage sync: {st_err}")

        try:
            subprocess.run([
                "gcloud", "run", "services", "update", "novasmart-store-portal",
                f"--region={LOCATION}",
                f"--project={PROJECT_ID}",
                f"--update-env-vars=CUSTOMER_AGENT_ID={engine_id}",
                "--quiet"
            ], check=False)
            print(f"  ✅ Automatically linked CUSTOMER_AGENT_ID ({engine_id}) into Cloud Run service novasmart-store-portal!")
        except Exception as run_err:
            print(f"  ⚠️ Note on Cloud Run environment binding: {run_err}")

        # Register in Agent Registry as official cataloged service
        print("📡 Registering Customer Personalization Agent in Agent Registry...")
        try:
            import requests
            creds_reg, _ = google.auth.default()
            creds_reg.refresh(google.auth.transport.requests.Request())
            token_reg = creds_reg.token
            for host in [f"{LOCATION}-agentregistry.googleapis.com", "agentregistry.googleapis.com"]:
                reg_url = f"https://{host}/v1alpha/projects/{PROJECT_ID}/locations/{LOCATION}/services"
                reg_headers = {"Authorization": f"Bearer {token_reg}", "Content-Type": "application/json"}
                a2a_url = f"https://{LOCATION}-aiplatform.googleapis.com/v1beta1/{engine_urn}/a2a"
                reg_payload = {
                    "displayName": "customer-personalization-agent",
                    "agentSpec": {"content": {"url": a2a_url}}
                }
                res = requests.post(f"{reg_url}?serviceId=customer-personalization-agent", headers=reg_headers, json=reg_payload, verify=False)
                if res.status_code in [409, 400]:
                    patch_url = f"{reg_url}/customer-personalization-agent?updateMask=agentSpec.content.url"
                    res = requests.patch(patch_url, headers=reg_headers, json={"agentSpec": {"content": {"url": a2a_url}}}, verify=False)
                print(f"  ✅ Agent Registry update on {host}: HTTP {res.status_code}")
        except Exception as reg_err:
            print(f"  ⚠️ Note on Agent Registry sync: {reg_err}")

    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"  ❌ Failed to deploy customer personalization agent: {e}")
        sys.exit(1)

    print("\n============================================================")
    print("🎉 SUCCESS! Customer Personalization Agent Deployed!")
    print("============================================================")

if __name__ == "__main__":
    main()
