"""Display labels and view models; never persisted in source JSON."""
import re
from urllib.parse import urlsplit
REACTIONS = [
    ("❤", "Нравится"), ("✍️", "Пиши ещё!"), ("🔥", "Огонь!"), ("😆", "Ха-ха!"),
    ("👎", "Не нравится"), ("😢", "Сочувствие"), ("😡", "Я возмущён"), ("👌", "ОК!"),
    ("🤦", "Фейспалм"), ("🤢", "Мне плохо"), ("🤮", "Фу!"), ("💩", "Ну такое"),
    ("👻", "Бу!"), ("🐦", "Очень странные дела"), ("🐾", "Здесь был кот"), ("👍", "Отлично!"),
]


def reaction_view(data):
    if data is None:
        return None
    badges = []
    for rid, count in enumerate(data["groups"]):
        if count:
            icon, label = REACTIONS[rid] if rid < len(REACTIONS) else ("●", f"Реакция #{rid}")
            authors = data.get("authors", {})
            users = []
            for user in authors.get("users", []):
                if user["type"] != rid:
                    continue
                url = user.get("profile_url", "")
                try:
                    parts = urlsplit(url)
                    valid = parts.scheme in ("http", "https") and parts.hostname and not parts.username
                except ValueError:
                    valid = False
                if not valid:
                    name = user["username"]
                    url = f"https://{name.replace('_', '-')}.livejournal.com/profile/" if re.fullmatch(r"[a-zA-Z0-9_-]{1,15}", name) else None
                users.append({"id": user["id"], "name": user["username"], "url": url})
            badges.append({"id": rid, "icon": icon, "label": label, "count": count,
                           "users": users, "authors_complete": authors.get("status") == "complete",
                           "authors_status": authors.get("status", "not_loaded")})
    return {**data, "badges": badges}

