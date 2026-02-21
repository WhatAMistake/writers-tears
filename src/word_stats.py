"""
Per-user word and character count stats: today, week, month, overall.
Persisted in data/user_word_stats.json.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


def _path() -> Path:
    return Path(__file__).parent.parent / "data" / "user_word_stats.json"


def _load() -> dict[str, Any]:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data: dict[str, Any]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def add_word_count(user_id: int, words: int, chars: int) -> None:
    """Add both word and character counts."""
    if words <= 0 and chars <= 0:
        return
    data = _load()
    key = str(user_id)
    if key not in data:
        data[key] = {"total_words": 0, "total_chars": 0, "by_date": {}}
    # Migrate old format if needed
    if isinstance(data[key], dict) and "total_words" not in data[key]:
        # Old format: just a number or different structure
        old_total = data[key].get("total", 0) if isinstance(data[key], dict) else 0
        data[key] = {"total_words": old_total, "total_chars": 0, "by_date": {}}
    data[key]["total_words"] = data[key].get("total_words", 0) + words
    data[key]["total_chars"] = data[key].get("total_chars", 0) + chars
    by_date = data[key].setdefault("by_date", {})
    day = _today()
    # Handle old format where day_data might be an int
    if day in by_date and isinstance(by_date[day], int):
        # Old format: just word count as int
        by_date[day] = {"words": by_date[day], "chars": 0}
    day_data = by_date.setdefault(day, {"words": 0, "chars": 0})
    # Ensure day_data is a dict, not an int
    if isinstance(day_data, int):
        day_data = {"words": day_data, "chars": 0}
        by_date[day] = day_data
    day_data["words"] = day_data.get("words", 0) + words
    day_data["chars"] = day_data.get("chars", 0) + chars
    _save(data)


def get_stats(user_id: int) -> dict[str, int]:
    """Return today, week, month, total for both words and chars."""
    data = _load()
    user_data = data.get(str(user_id), {})
    by_date = user_data.get("by_date", {})
    total_words = user_data.get("total_words", 0)
    total_chars = user_data.get("total_chars", 0)
    
    today_str = _today()
    today_data = by_date.get(today_str, {"words": 0, "chars": 0})
    today_words = today_data.get("words", 0)
    today_chars = today_data.get("chars", 0)
    
    now = datetime.now()
    week_start = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    month_start = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    
    week_words = sum(by_date[d].get("words", 0) for d in by_date if d >= week_start)
    week_chars = sum(by_date[d].get("chars", 0) for d in by_date if d >= week_start)
    month_words = sum(by_date[d].get("words", 0) for d in by_date if d >= month_start)
    month_chars = sum(by_date[d].get("chars", 0) for d in by_date if d >= month_start)
    
    return {
        "today": today_words,
        "week": week_words,
        "month": month_words,
        "total": total_words,
        "chars_today": today_chars,
        "chars_week": week_chars,
        "chars_month": month_chars,
        "chars_total": total_chars,
    }


def count_words(text: str) -> int:
    return len(text.split())


def count_chars(text: str) -> int:
    return len(text)


def reset_stats(user_id: int) -> None:
    """Reset all word and character stats for a user."""
    data = _load()
    key = str(user_id)
    if key in data:
        del data[key]
        _save(data)
