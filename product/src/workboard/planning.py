"""Schedule display uses authorized terminal work, never a stale parent plan."""
from sqlalchemy import select

from .errors import require
from .models import Task
from .policy import can_read


def visible_leaf_dates(db, actor, roots):
    starts, ends, visited = [], [], set()
    pending = [(task, 0) for task in roots]
    while pending:
        task, depth = pending.pop()
        require(depth <= 32, '工作階層異常，請洽維護人員。', 409)
        if task.id in visited:
            continue
        visited.add(task.id)
        if task.deleted or task.cancelled or task.cleaned:
            continue
        children = list(db.scalars(select(Task).where(Task.parent_id == task.id, Task.deleted.is_(False))))
        if children:
            pending.extend((child, depth + 1) for child in children)
        elif can_read(db, actor, task):
            if task.planned_start:
                starts.append(task.planned_start)
            if task.planned_end:
                ends.append(task.planned_end)
    return min(starts) if starts else None, max(ends) if ends else None
