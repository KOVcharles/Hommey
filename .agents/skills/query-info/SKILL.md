---
name: query-info
description: Retrieve weather and general transport context. Use for weather, route, flight, airport, or local-transport questions; never use as general web search. Rail/train schedules belong to train-query, not this skill.
---

# Retrieve External Trip Information

Use `weather` for destination forecasts and `web_search` for public transport context.

- In a workflow, execute only the capabilities listed in `active_task.capabilities`; do not fetch an omitted facet.
- Weather and public-transport lookups do not require a company-trip context.
- Prefer authoritative transport operators and official sources.
- Treat search snippets as advisory, not proof of availability or price.
- Tell the user to verify schedules, fares, and availability through official or authorized travel channels.
- Never claim a booking or transaction was completed.

Return a concise summary and source links.
