"""Django runtime-structure extractor run with the project's Python inside the evidence boundary.

Writes models, reverse relations, signal receivers, and URL routes as JSON. It must not import
gitea_auto_reviewer because the project's CI interpreter does not have it installed.
"""

import inspect
import json
import sys
import weakref
from pathlib import Path

REPOSITORY = Path.cwd().resolve()


def location(obj):
    obj = inspect.unwrap(obj)
    try:
        path = Path(inspect.getsourcefile(obj)).resolve().relative_to(REPOSITORY).as_posix()
        return path, inspect.getsourcelines(obj)[1]
    except (TypeError, OSError, ValueError):
        return None, None


def attribute_line(cls, name):
    path, start = location(cls)
    if path is None:
        return None
    try:
        lines = inspect.getsourcelines(cls)[0]
    except (OSError, TypeError):
        return None
    for offset, line in enumerate(lines):
        if line.strip().startswith(f"{name} ") or line.strip().startswith(f"{name}="):
            return start + offset
    return start


def symbol(obj):
    obj = inspect.unwrap(obj)
    path, line = location(obj)
    if path is None:
        return None
    return {"file": path, "line": line, "name": getattr(obj, "__qualname__", getattr(obj, "__name__", "?"))}


def models(apps):
    items = []
    for model in apps.get_models():
        path, line = location(model)
        if path is None:
            continue
        fields, reverse = [], []
        for field in model._meta.get_fields():
            if field.auto_created and not field.concrete:
                if field.is_relation and field.related_model is not None:
                    reverse.append({"accessor": field.get_accessor_name(),
                                    "related_model": field.related_model._meta.label,
                                    "field": field.field.name})
                continue
            fields.append({
                "name": field.name,
                "attname": getattr(field, "attname", field.name),
                "line": attribute_line(model, field.name),
                "related_model": field.related_model._meta.label
                if field.is_relation and field.related_model is not None else None,
            })
        items.append({"label": model._meta.label, "name": model.__name__, "file": path, "line": line,
                      "fields": fields, "reverse": reverse})
    return items


def signals(apps):
    from django.core import signals as core_signals
    from django.db.models import signals as model_signals

    senders = {id(model): model._meta.label for model in apps.get_models(include_auto_created=True)}
    named = {f"{module.__name__.rsplit('.', 1)[-1]}.{name}": value
             for module in (model_signals, core_signals)
             for name, value in vars(module).items()
             if type(value).__name__ in {"Signal", "ModelSignal"}}
    items = []
    for name, signal in sorted(named.items()):
        for entry in list(signal.receivers):
            # Entries are (lookup_key, receiver_or_weakref[, is_async]) depending on Django version.
            lookup, reference = entry[0], entry[1]
            receiver = reference() if isinstance(reference, weakref.ReferenceType) else reference
            if receiver is None:
                continue
            target = symbol(receiver)
            if target is None:
                continue
            sender_id = lookup[1] if isinstance(lookup, tuple) and len(lookup) > 1 else None
            items.append({"signal": name.rsplit(".", 1)[-1], "sender": senders.get(sender_id),
                          "receiver": target})
    return items


def urls():
    from django.urls import URLPattern, URLResolver, get_resolver

    items = []

    def walk(patterns, prefix):
        for pattern in patterns:
            if isinstance(pattern, URLResolver):
                walk(pattern.url_patterns, prefix + str(pattern.pattern))
            elif isinstance(pattern, URLPattern):
                callback = pattern.callback
                view = getattr(callback, "view_class", None) or getattr(callback, "cls", None) or callback
                target = symbol(view)
                if target is not None:
                    items.append({"route": "/" + prefix + str(pattern.pattern), "view": target})

    try:
        walk(get_resolver().url_patterns, "")
    except Exception as exc:  # urlconf import errors are reported, not fatal
        return [], f"{type(exc).__name__}: {exc}"[:300]
    return items, None


def main():
    sys.path.insert(0, str(REPOSITORY))
    import django

    django.setup()
    from django.apps import apps

    routes, url_error = urls()
    payload = {"version": 1, "models": models(apps), "signals": signals(apps), "urls": routes,
               "url_error": url_error}
    Path(sys.argv[1]).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
