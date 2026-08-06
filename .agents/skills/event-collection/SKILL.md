---
name: event-collection
description: Collect and incrementally update the employee's current company trip, including origin, destination, dates, purpose, work location, work schedule, and missing information. Use when a user starts or supplements a business-trip task.
---

# Collect Current Business Trip

Use `active_trip_context` to preserve one current trip per user.

1. Merge new facts into the current trip instead of replacing known values with nulls.
2. Extract origin, destination, dates, duration, return location, purpose, work location, and work schedule.
3. Use a saved home location only as an explicitly marked inference.
4. For planning, require origin, destination, start date, trip purpose, and either duration or return date. Treat work location and work schedule as optional information; do not invent precise dates, addresses, or work commitments.
5. Keep private tourism outside the trip task.

Return structured JSON matching `schemas/output.json`.
