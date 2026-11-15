# JARVIS agents

Every request passes through the brain: instant answers first (time, date, maths, unit conversion, small talk,
things Android forbids), then routing to ONE agent per step. Every action an agent wants goes through the
executor: permission check, guardrail, approval when it sends or deletes, then the audit log.

| Agent | Handles | Needs a model? |
|---|---|---|
| scheduler | calendar: view, book, move, cancel, free slots, next meeting | for unusual phrasing |
| communication | email and Slack: read, summarise, draft (sent only after approval) | yes, for drafting |
| memory | people, projects, places, preferences (knowledge graph) | for answers in prose |
| device | alarms, timers, reminders, calls, SMS, contacts, toggles, battery, "call me at 6" | for unusual phrasing |
| apps | YouTube, Spotify, WhatsApp, Maps navigation, websites, volume, media keys | no |
| lists | to-dos with due dates, shopping, notes, any named list | no |
| money | expenses, categories, monthly budget | no |
| web | weather, news, facts, currency (free keyless services; results are untrusted text) | to phrase facts |
| routines | "when I say X, do A, B, C" shortcuts | no |
| github | repos, PRs, issues, CI | for unusual phrasing |
| places | travel time, distance, when to leave | for unusual phrasing |
| briefing | your day, overnight report, due tasks, weather | no |
| planner | multi-step goals split into steps for the other agents | yes |
| chat | conversation, explanations, writing help | yes |
| guardrail | lockdown, approvals by voice, refusing unsafe actions | no |

"No" in the last column means the agent understands the common phrasings by itself, so it keeps working
when the free model tier is rate limited.
