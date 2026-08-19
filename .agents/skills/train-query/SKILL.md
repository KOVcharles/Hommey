---
name: train-query
description: Query real China Railway (12306) train schedules, times, durations, fares and seat availability. Use for train-ticket (车票), train-number, high-speed-rail, or railway schedule questions; never general web search and never booking.
---

# Query Train Schedules and Availability

Return structured train rows (train number, departure/arrival station and time, duration, seat availability, fares) with an official-verification reminder.

- Public train-schedule lookups do not require a company-trip context.
- Use origin, destination and travel date from the trip card when present.
- If the travel date is missing, use today's date in the Asia/Shanghai timezone and query immediately; do not ask a follow-up question just for the date.
- Prefer the official railway 12306 source over third-party snippets.
- Treat a successful 12306 response with an empty result list as “no direct/remaining trains found”, not as an upstream outage.
- Treat schedule, fare and availability data as advisory — tell the user to verify through the official 12306 app or authorized travel channels.
- Never claim a booking or transaction was completed.
- Never answer policy/RAG questions (制度/标准/报销) or act as general web search.
