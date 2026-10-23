"""The attendance module's part of permanently erasing one person's data (owner decision
2026-10-23, docs/database-design.md §9.1).

Reasons and decision notes on corrections can hold personal details, so they are overwritten
with the erasure marker. Clock events, voids and correction items are append-only attendance
history (rule 5): they stay, under the erased record's ID, including the address and browser a
clock event was recorded from. Shift assignments name no personal data and stay.
"""

import uuid

from app.modules.attendance.models import (
    AttendanceCorrection,
    AttendanceCorrectionItem,
    AttendanceEvent,
    AttendanceEventVoid,
    ShiftAssignment,
)
from app.platform.db import ERASED, Session
from app.platform.erasure import Erased, Outcome


async def erase_employee(session: Session, employee_id: uuid.UUID) -> Erased:
    result = Erased()
    corrections = await session.find(AttendanceCorrection, {"employee_id": employee_id})
    texts = 0
    for correction in corrections:
        if correction.reason != ERASED:
            correction.reason = ERASED
            texts += 1
        if correction.decision_note is not None and correction.decision_note != ERASED:
            correction.decision_note = ERASED
            texts += 1
    await session.flush()
    result.subject_ids.extend(correction.id for correction in corrections)
    result.add(
        "attendance.correction_texts",
        Outcome.ANONYMIZED,
        texts,
        "Reasons and decision notes typed on attendance corrections.",
    )
    ids = [correction.id for correction in corrections]
    items = await session.count(AttendanceCorrectionItem, {"correction_id": {"$in": ids}}) if ids else 0
    result.add(
        "attendance.corrections",
        Outcome.RETAINED,
        len(corrections) + items,
        "Corrections and the events they added or removed, with who decided and when: attendance history.",
    )
    events = [event.id for event in await session.find(AttendanceEvent, {"employee_id": employee_id})]
    voids = await session.count(AttendanceEventVoid, {"event_id": {"$in": events}}) if events else 0
    result.add(
        "attendance.events",
        Outcome.RETAINED,
        len(events) + voids,
        "Clock-in, break and clock-out events and their voids, with the address and browser each was "
        "recorded from: append-only attendance history (CLAUDE.md rule 5).",
    )
    shifts = await session.count(ShiftAssignment, {"employee_id": employee_id})
    result.add(
        "attendance.shift_assignments",
        Outcome.RETAINED,
        shifts,
        "Which shift applied when; no personal data.",
    )
    return result
