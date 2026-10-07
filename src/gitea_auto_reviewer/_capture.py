"""Row-level DB change recorder loaded by the reproduction runner inside the project's Python.

This file is copied next to the runner and imported after django.setup(); it must not import
gitea_auto_reviewer because the project's CI interpreter does not have it installed.
"""

from __future__ import annotations

import datetime
import decimal
import re
import uuid
from functools import partial

WRITE_SQL = re.compile(
    r"^\s*(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM)\s+[`\"\[]?([\w.]+)", re.IGNORECASE
)
ROW_LIMIT = 5000


def json_value(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, memoryview)):
        return bytes(value).hex()
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_value(item) for item in value]
    return str(value)


class ChangeRecorder:
    """Snapshot each table right before its first write, then diff it before rollback."""

    def __init__(self, connections, apps):
        self.connections = connections
        self.models = {model._meta.db_table.lower(): model
                       for model in apps.get_models(include_auto_created=True)}
        self.snapshots = {}
        self.busy = False

    def install(self, stack) -> None:
        for alias in self.connections:
            stack.enter_context(self.connections[alias].execute_wrapper(partial(self._wrap, alias)))

    def _wrap(self, alias, execute, sql, params, many, context):
        if not self.busy:
            match = WRITE_SQL.match(sql)
            if match:
                model = self.models.get(match.group(1).split(".")[-1].strip('`"[]').lower())
                if model is not None and (alias, model._meta.label) not in self.snapshots:
                    self._snapshot(alias, model)
        return execute(sql, params, many, context)

    def _snapshot(self, alias, model) -> None:
        from django.db.models import Max

        self.busy = True
        try:
            manager = model._base_manager.using(alias)
            pk = model._meta.pk.attname
            total = manager.count()
            partial_snapshot = total > ROW_LIMIT
            self.snapshots[(alias, model._meta.label)] = {
                "model": model,
                "partial": partial_snapshot,
                "max_pk": manager.aggregate(value=Max(pk))["value"] if partial_snapshot else None,
                "rows": {} if partial_snapshot else self._rows(manager),
            }
        finally:
            self.busy = False

    def _rows(self, queryset) -> dict:
        model = queryset.model
        pk = model._meta.pk.attname
        names = [field.attname for field in model._meta.concrete_fields]
        return {json_value(row[pk]): {name: json_value(row[name]) for name in names}
                for row in queryset.values(*names)}

    def collect(self) -> list:
        """Return created/updated/deleted rows per written table; call before rolling back."""
        self.busy = True
        try:
            changes = []
            for (alias, label), snapshot in sorted(self.snapshots.items(), key=lambda item: item[0]):
                model = snapshot["model"]
                manager = model._base_manager.using(alias)
                before = snapshot["rows"]
                if snapshot["partial"]:
                    max_pk = snapshot["max_pk"]
                    after = (self._rows(manager.filter(pk__gt=max_pk))
                             if isinstance(max_pk, int) else {})
                else:
                    after = self._rows(manager)
                changes.append({
                    "model": label,
                    "alias": alias,
                    "partial": snapshot["partial"],
                    "pk": model._meta.pk.attname,
                    "foreign_keys": {
                        field.attname: field.related_model._meta.label
                        for field in model._meta.concrete_fields
                        if field.is_relation and field.related_model is not None
                    },
                    "volatile": sorted(
                        field.attname for field in model._meta.concrete_fields
                        if getattr(field, "auto_now", False) or getattr(field, "auto_now_add", False)
                    ),
                    "created": [after[key] for key in sorted(set(after) - set(before), key=_order)],
                    "deleted": [] if snapshot["partial"] else [
                        before[key] for key in sorted(set(before) - set(after), key=_order)
                    ],
                    "updated": [] if snapshot["partial"] else [
                        {"pk": key, "before": before[key], "after": after[key]}
                        for key in sorted(set(before) & set(after), key=_order)
                        if before[key] != after[key]
                    ],
                })
            return changes
        finally:
            self.busy = False

    def restored(self) -> bool:
        """After rollback, every fully snapshotted table must equal its pre-write rows."""
        self.busy = True
        try:
            for (alias, _label), snapshot in self.snapshots.items():
                manager = snapshot["model"]._base_manager.using(alias)
                if snapshot["partial"]:
                    max_pk = snapshot["max_pk"]
                    if isinstance(max_pk, int) and manager.filter(pk__gt=max_pk).exists():
                        return False
                elif self._rows(manager) != snapshot["rows"]:
                    return False
            return True
        finally:
            self.busy = False


def _order(key):
    return (0, key, "") if isinstance(key, (int, float)) else (1, 0, str(key))
