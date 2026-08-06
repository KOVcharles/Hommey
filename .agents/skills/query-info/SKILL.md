---
name: query-info
description: Retrieve weather and public transport information only when it directly supports a current company business trip. Use for trip-related weather, route, rail, flight, airport, or local-transport questions; never use as general web search.
---

# Retrieve External Trip Information

Use `weather` for destination forecasts and `web_search` for public transport context.

- Require a current or explicit company-trip context.
- Prefer authoritative transport operators and official sources.
- Treat search snippets as advisory, not proof of availability or price.
- Tell the user to verify schedules, fares, and availability through official or authorized travel channels.
- Never claim a booking or transaction was completed.

Return a concise summary and source links.
