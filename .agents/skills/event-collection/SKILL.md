---
name: event-collection
description: Collect and incrementally update the employee's current company trip, including origin, destination, dates, purpose, work location, work schedule, and missing information. Use when a user starts or supplements a business-trip task.
---

# Collect Current Business Trip

Use `active_trip_context` to preserve one current trip per conversation session.

1. Merge new facts into the current trip instead of replacing known values with nulls.
2. Extract origin, destination, dates, duration, return location, purpose, work location, and work schedule.
3. Preferences may directly shape non-factual recommendations such as hotel brand, airline, or seat choice. A saved home location or an explicitly referenced historical trip may only produce a marked candidate location; require the user to confirm it before treating it as a current-trip fact.
4. If neither the user nor the active trip specifies a start date, deterministically use today's date in the `Asia/Shanghai` timezone. Never replace an explicit start date with this default.
5. For planning, require origin, destination, the explicit-or-defaulted start date, trip purpose, and either duration or return date. Treat work location and work schedule as optional information; do not invent other precise dates, addresses, or work commitments.
6. Keep private tourism outside the trip task.
7. Never read a current trip or ordinary dialogue context from another conversation session.

Return structured JSON matching `schemas/output.json`.
