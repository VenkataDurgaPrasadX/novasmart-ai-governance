with open("/tmp/m5-local/extracted/price_match_agent.py", "r") as f:
    lines = f.readlines()

out = []
in_escalate = False
for line in lines:
    if line.startswith("async def escalate_to_strategy_agent"):
        in_escalate = True
        out.append("def escalate_to_strategy_agent(sku: str, requested_price: float) -> str:\n")
        out.append("    \"\"\"Escalates a price match request to the Markdown Strategy Agent.\"\"\"\n")
        out.append("    return 'Local evaluation: Request escalated to Markdown Strategy Agent (Agent 2). Escalation mock response.'\n\n")
        continue
    if in_escalate:
        if line.startswith("_VERIFICATION_INSTRUCTION ="):
            in_escalate = False
        else:
            continue
    out.append(line)

code_mod = "".join(out)
code_mod += "\nroot_agent = price_match_agent\napp = adk_app\n"

with open("/tmp/m5-local/m5-scaffold/app/agent.py", "w") as f:
    f.write(code_mod)

print("Updated /tmp/m5-local/m5-scaffold/app/agent.py with clean escalation stub!")
