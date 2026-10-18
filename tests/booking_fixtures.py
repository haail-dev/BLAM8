"""Explicit pre-slice effective values for established-business scenarios.

These are test data, not application defaults. Unset-policy tests use raw config.
"""

def carried_config(cfg=None):
    return {"capacity_per_window": 4, "working_days": "mon,tue,wed,thu,fri",
            "open_time": "09:00", "close_time": "17:00", "timezone": "Europe/London",
            **(cfg or {})}
