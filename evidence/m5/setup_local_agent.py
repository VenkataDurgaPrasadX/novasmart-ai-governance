import re

with open("/tmp/m5-local/extracted/price_match_agent.py", "r") as f:
    code = f.read()

# Remove escalate_to_strategy_agent from tools list
code_mod = code.replace("tools=[query_competitor_pricing, escalate_to_strategy_agent]", "tools=[query_competitor_pricing]")

# Remove or stub escalate_to_strategy_agent function definition to prevent import errors
code_mod = re.sub(r'async def escalate_to_strategy_agent.*?(?=def _VERIFICATION_INSTRUCTION|\Z)', 
                 'def escalate_to_strategy_agent(sku: str, requested_price: float) -> str:\n    return "Local evaluation: escalation to Markdown Strategy Agent is disabled locally."\n\n', 
                 code_mod, flags=re.DOTALL)

with open("/tmp/m5-local/price_match_agent_local.py", "w") as f:
    f.write(code_mod)

print("Local price match agent code created at /tmp/m5-local/price_match_agent_local.py")
