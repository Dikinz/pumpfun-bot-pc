"""Operator state only. Never closes a position or frees money/capacity."""
import math
EXIT_WAIT_DEADLINE_S=120.

def state(signal_ts,now,last_decision=None):
    valid=all(type(x) in (int,float) and math.isfinite(x) for x in (signal_ts,now))
    overdue=valid and now>=signal_ts+EXIT_WAIT_DEADLINE_S
    return dict(exit_state_v96='OPEN_REMAINDER' if overdue else 'EXIT_PENDING' if valid else 'NO_EXIT_SIGNAL',
        exit_deadline_ts_v96=signal_ts+EXIT_WAIT_DEADLINE_S if valid else None,
        remainder_reason_v96=last_decision or 'NO_VERIFIED_SALE' if overdue else None,
        capital_released_v96=False,slot_retained_v96=True)
