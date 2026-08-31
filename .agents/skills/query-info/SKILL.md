---
name: query-info
description: Retrieve weather and general transport context. Use for weather, route, flight, airport, or local-transport questions; never use as general web search. Rail/train schedules belong to train-query, not this skill.
---

# Retrieve External Trip Information

Use the shared travel-information service for weather and local routes:

- Prefer AMap weather for mainland-China cities; fall back to Open-Meteo when the AMap key is unavailable, the city is outside mainland China, or AMap fails.
- Use AMap transit routing only when both endpoints resolve to unambiguous server-verified POIs. Never invent city-center coordinates for a city-to-city trip.
- Use restricted web search only for flight, airport, intercity, or other public context that AMap does not provide.

- In a workflow, execute only the capabilities listed in `active_task.capabilities`; do not fetch an omitted facet.
- Weather and public-transport lookups do not require a company-trip context.
- Railway schedules, fares, and seat availability remain exclusive to train-query.
- Prefer authoritative transport operators and official sources.
- Treat search snippets as advisory, not proof of availability or price.
- Tell the user to verify schedules, fares, and availability through official or authorized travel channels.
- Never claim a booking or transaction was completed.

Return a concise summary and source links.
