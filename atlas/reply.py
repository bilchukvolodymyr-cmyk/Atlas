"""Short human answer (Ukrainian) describing what Atlas actually wrote. Built from the applied
actions and the database, never from Claude's own words."""
from __future__ import annotations

from typing import Any

METRIC_NAMES = {
    "trucks_unload": "машин на розвантаження",
    "trucks_load": "машин на завантаження",
    "workers_total": "людей",
    "workers_agenzia": "людей Agenzia",
    "workers_cooperativa": "людей Cooperativa",
}
STATE_NAMES = {"active": "в роботі", "waiting": "чекаю", "needs_info": "бракує даних",
               "dropped": "скасовано", "verifying": "чекаю підтвердження"}


def _date(iso: str | None) -> str:
    if not iso:
        return ""
    d = iso[:10].split("-")
    out = f"{d[2]}.{d[1]}"
    if len(iso) > 10:
        out += f" о {iso[11:16]}"
    return out


def describe(result: dict[str, Any], store: Any) -> str:
    status = result.get("status")
    if status == "retry_later":
        return "Зберіг повідомлення. Claude зараз недоступний, оброблю пізніше."
    if status == "already_applied":
        return "Це повідомлення вже оброблене раніше."
    if status == "kept_as_note":
        return "Зберіг як нотатку: не зміг надійно розібрати. Подивимось разом."
    if status == "pending":
        return "Прийняв. Обробка ще триває."
    if status != "applied":
        return f"Зберіг повідомлення, але обробка не вдалася: {result.get('error', status)}"

    created = result.get("created") or {}
    lines: list[str] = []

    def loop_title(ref: Any) -> str:
        if isinstance(ref, str) and ref.startswith("$"):
            ref = created.get(ref[1:], {}).get("id")
        loop = store.loop(ref) if isinstance(ref, int) else None
        return f"«{loop['title']}»" if loop else "справа"

    for a in result.get("actions", []):
        t = a["type"]
        if t == "create_loop":
            line = f"Справа: «{a['title']}»"
            if a.get("due_at"):
                line += f" до {_date(a['due_at'])}"
            if a.get("assigned_by"):
                line += f" (доручив {a['assigned_by']})"
            if a.get("waiting_on"):
                line += f", чекаю на {a['waiting_on']}"
            lines.append(line)
        elif t == "update_loop":
            f = a["fields"]
            extra = f", строк {_date(f['due_at'])}" if f.get("due_at") else ""
            lines.append(f"Оновив {loop_title(a['id'])}{extra}")
        elif t == "transition_loop" and a["to"] != "done_claimed":
            lines.append(f"{loop_title(a['id'])}: {STATE_NAMES.get(a['to'], a['to'])}")
        elif t == "verify_loop":
            lines.append(f"Закрив {loop_title(a['id'])}")
        elif t == "create_entry":
            text = a["text"] if len(a["text"]) <= 80 else a["text"][:77] + "…"
            lines.append(f"У щоденник: {text}")
        elif t == "create_idea":
            lines.append(f"Ідея: «{a['title']}»")
        elif t == "record_metric":
            name = METRIC_NAMES.get(a["name"], a["name"])
            site = "" if a["site"] == "all" else f" ({a['site'].replace('polo', 'Polo ')})"
            value = int(a["value"]) if float(a["value"]).is_integer() else a["value"]
            lines.append(f"{_date(a['date'])}: {value} {name}{site}")
        elif t == "ask_question":
            lines.append(f"Питання: {a['text']}")
    n = len(result.get("rejected") or [])
    if n:
        lines.append(f"Не записав {n} пункт(и): не знайшов їх у тексті.")
    return "\n".join(lines) if lines else "Записав."
