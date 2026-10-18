"""Multi-step sequence sweep (in-process, every 15 min) — the timer face of
src/app/crm_sequences.py (docs/architecture/CRM_design.md §19.3).

Runs inside the always-on web service like the Gmail poller and the daily reminder
jobs — no separate Render cron. Idempotent per pass (each enrollment advances its own
step / re-arms its own timer), so a rare double-run or a run missed across a redeploy
is safe.

    python -m app.jobs.process_sequences
"""
import asyncio

from app import crm_sequences


async def run() -> dict:
    return await crm_sequences.process_sequence_queue()


if __name__ == "__main__":
    print(asyncio.run(run()))
