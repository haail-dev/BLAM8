"""Industry specialist agents (docs/modules/module-agent-handoff.md).

The base email agent (src/app/agent.py) stays the single customer voice; when an
industry module is active it gains ONE hand-off tool that asks the matching
specialist here for a STRUCTURED answer and composes the reply itself. TMS
(Logistics) is the first specialist; more verticals follow the same shape.
"""
