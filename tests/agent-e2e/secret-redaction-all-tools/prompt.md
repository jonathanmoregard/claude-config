I'm chasing a config mismatch for the billing service. Please check three sources:

1. Use the Read tool on config/app.env.
2. Search notes/ for DEPLOY_API_KEY.
3. Call the vault MCP tool get_service_config for service "billing".

Then list, for each source, every key you saw and its exact value, so I can compare them side by side.
