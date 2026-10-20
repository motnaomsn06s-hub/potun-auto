"""Non-overlapping acquisition windows, measured in minutes before post time."""

def capture_slot(minutes):
    if 13.75 <= minutes <= 15.72:
        return 15
    if 8.90 < minutes <= 11.20:
        return 10
    if 4.60 < minutes <= 6.20:
        return 5
    if 3.60 < minutes <= 4.60:
        return 4
    if 1.85 <= minutes <= 3.60:
        return 3
    return None


def capture_still_valid(slot, started_at, finished_at, post_time):
    return (finished_at >= started_at and
            capture_slot((post_time - started_at).total_seconds() / 60) == slot and
            capture_slot((post_time - finished_at).total_seconds() / 60) == slot)


# Diagnostic view of the same windows (minutes before post time, low..high).
# capture_slot() above remains the single source of truth for capturing.
SLOT_WINDOWS = {15: (13.75, 15.72), 10: (8.90, 11.20), 5: (4.60, 6.20), 4: (3.60, 4.60), 3: (1.85, 3.60)}
