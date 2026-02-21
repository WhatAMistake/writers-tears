"""
Code change reviewer for Writer's Tears bot.
Tracks file hashes and generates changelogs on startup.
"""

import os
import json
import hashlib
import re
import random
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime

# Files to track for changes
TRACKED_FILES = [
    "src/telegram_bot.py",
    "src/writer_bot.py", 
    "src/i18n.py",
    "src/writer_rag.py",
    "src/lang_utils.py",
    "src/word_stats.py",
]

# Existing commands that should NOT be mentioned as "new" in changelogs
EXISTING_COMMANDS = [
    "start", "help", "lang", "switchlang", "reset", "block", "develop",
    "character", "dialogue", "prompt", "idea", "feedback", "style", "roast",
    "praise", "corrector", "editor", "count_me", "stats", "lobster", "pun",
    "porko", "methodique", "methodichque", "cite", "cite_off", "cite_on",
    "cite_when", "summary", "cry_baby", "dev_feedback", "done", "confo_enable37", "upload"
]

# Witty comments for changelog
WITTY_COMMENTS = [
    "Код, как вино — с каждым обновлением становится лучше.",
    "Баги исправлены, новые баги ещё не обнаружены.",
    "Обновление установлено. Надеюсь, ничего не сломал.",
    "Код переписан. Старые костыли заменены на новые, более элегантные.",
    "Код обновлён. Если что-то сломалось — это специально.",
]




def calculate_file_hash(file_path: Path) -> str:
    """Calculate SHA256 hash of file contents."""
    try:
        with open(file_path, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except Exception:
        return ""


def load_stored_hashes(data_dir: Path) -> Dict[str, str]:
    """Load previously stored file hashes."""
    hash_file = data_dir / "code_hashes.json"
    if hash_file.exists():
        try:
            with open(hash_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_hashes(data_dir: Path, hashes: Dict[str, str]) -> None:
    """Save current file hashes."""
    hash_file = data_dir / "code_hashes.json"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        with open(hash_file, 'w', encoding='utf-8') as f:
            json.dump(hashes, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Failed to save hashes: {e}")


def get_changed_files(project_root: Path, stored_hashes: Dict[str, str]) -> List[Tuple[str, str, str]]:
    """
    Compare current files with stored hashes.
    Returns list of (filename, old_hash, new_hash) for changed files.
    """
    changed = []
    for rel_path in TRACKED_FILES:
        file_path = project_root / rel_path
        if file_path.exists():
            current_hash = calculate_file_hash(file_path)
            old_hash = stored_hashes.get(rel_path, "")
            if current_hash != old_hash:
                changed.append((rel_path, old_hash, current_hash))
    return changed


def extract_commands_from_code(content: str) -> List[str]:
    """Extract command names from code content."""
    commands = []
    patterns = [
        r'Command\(["\'](/\w+)["\']',
        r'@self\.dp\.message\(Command\(["\'](\w+)["\']',
        r'async def cmd_(\w+)\(',
    ]
    for pattern in patterns:
        matches = re.findall(pattern, content)
        for match in matches:
            cmd = match if match.startswith('/') else f"/{match}"
            if cmd not in commands:
                commands.append(cmd)
    return commands


def analyze_code_changes(project_root: Path, changed_files: List[Tuple[str, str, str]]) -> Dict[str, List[str]]:
    """Analyze what actually changed in the code."""
    result = {
        "new_commands": [],
        "all_commands": [],
        "has_changes": False,
        "changed_files_count": len(changed_files)
    }
    
    for rel_path, old_hash, new_hash in changed_files:
        file_path = project_root / rel_path
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            commands = extract_commands_from_code(content)
            if commands:
                result["all_commands"].extend(commands)
            
            result["has_changes"] = True
        except Exception:
            result["has_changes"] = True
    
    # Remove duplicates
    result["all_commands"] = list(set(result["all_commands"]))
    return result


def get_witty_comment(num_changes: int, lang: str = "ru") -> str:
    """Get a witty comment."""
    return random.choice(WITTY_COMMENTS)




def generate_changelog_with_llm(writer_bot, changed_files: List[Tuple[str, str, str]], project_root: Path, lang: str = "ru") -> str:
    """
    Use LLM to generate a human-readable changelog based on file changes.
    """
    if not changed_files:
        return ""
    
    num_changes = len(changed_files)
    
    # Generate content for each changed file
    diff_sections = []
    for rel_path, old_hash, new_hash in changed_files:
        file_path = project_root / rel_path
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            diff_sections.append(f"File: {rel_path}\n{content[:5000]}")
        except Exception as e:
            diff_sections.append(f"File: {rel_path}\n[Error: {e}]")
    
    diff_content = "\n\n".join(diff_sections)
    
    if lang == "ru":
        prompt = f"""Ты — циничный PM с 20-летним стажем, пишущий notes к релизу. Твой стиль: саркастичный, но информативный. Без воды и технического жаргона.

АНАЛИЗИРУЙ КОД и выяви что изменилось для ПОЛЬЗОВАТЕЛЯ:
- Новые команды (функции cmd_*) — что делает команда простыми словами
- Улучшения существующих команд — что стало лучше
- Исправленные баги — что чинили
- Новые возможности — как это поможет пользователю

ПРАВИЛА:
1. Пиши ТОЛЬКО о реальных изменениях в коде ниже
2. НЕ придумывай команды или фичи которых нет
3. Пиши простым языком, как для друга, не для программиста
4. Максимум 5 пунктов, только значимое
5. Формат: "- Краткое название: что изменилось и зачем это пользователю"
6. Без повторов "- Что:" в начале, каждый пункт должен быть уникальным

Изменённые файлы:
{diff_content}

Напиши changelog на русском. Будь конкретным, но сохраняй лёгкий сарказм в духе "мы это сделали, и вроде работает"."""
    else:
        prompt = f"""You are a cynical PM with 20 years of experience writing release notes. Your style: sarcastic but informative. No fluff or technical jargon.

ANALYZE THE CODE and identify what changed for USERS:
- New commands (cmd_* functions) — what the command does in simple words
- Improvements to existing commands — what got better
- Fixed bugs — what was broken and now works
- New features — how this helps the user

RULES:
1. Write ONLY about REAL changes in the code below
2. DO NOT invent commands or features that don't exist
3. Write in plain language, like for a friend, not a programmer
4. Maximum 5 items, significant changes only
5. Format: "- Brief name: what changed and why it matters to users"
6. No repetitive "- What:" at the start, each item should be unique

Changed files:
{diff_content}

Write changelog in English. Be specific but keep a light sarcastic tone like "we did this thing and it sort of works"."""

    
    try:
        if writer_bot and writer_bot.client:
            response = writer_bot.client.chat.completions.create(
                model=writer_bot.model,
                messages=[
                    {"role": "system", "content": "Ты пишешь краткие notes к релизам. Не придумывай. Будь честным. НЕ используй markdown (звёздочки, жирный шрифт)." if lang == "ru" else "You write brief release notes. Don't hallucinate. Be honest. NO markdown formatting (no asterisks, no bold)."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1,
                max_tokens=800
            )
            changelog = response.choices[0].message.content.strip()
            
            # Add witty comment if changelog is meaningful
            if changelog and len(changelog) > 10 and "Internal" not in changelog:
                witty = get_witty_comment(num_changes, lang)
                return f"{witty}\n\n{changelog}"
            else:
                return changelog


    except Exception as e:
        print(f"LLM changelog generation failed: {e}")
    
    # Fallback: simple message
    if lang == "ru":
        return "Внутренние улучшения и исправления."
    else:
        return "Internal improvements and fixes."


def generate_user_friendly_fallback(analysis: Dict, lang: str = "ru") -> str:
    """Generate a user-friendly changelog without technical details."""
    num_changes = analysis.get("changed_files_count", 1)
    
    if lang == "ru":
        witty = get_witty_comment(num_changes, lang)
        lines = [witty]
        
        if analysis["all_commands"]:
            for cmd in analysis["all_commands"][:5]:
                lines.append(f"- Доступна команда {cmd}")
        
        if len(lines) == 1:
            lines.append("- Улучшена работа и исправлены ошибки")
    else:
        witty = get_witty_comment(num_changes, lang)
        lines = [witty]
        
        if analysis["all_commands"]:
            for cmd in analysis["all_commands"][:5]:
                lines.append(f"- Command {cmd} is available")
        
        if len(lines) == 1:
            lines.append("- Improvements and bug fixes")



    
    return "\n".join(lines)


def check_and_generate_changelog(project_root: Path, writer_bot, admin_id: int, lang: str = "ru", should_save_hashes: bool = True) -> Optional[str]:
    """
    Main entry point: check for changes and generate changelog.
    Returns changelog text if changes detected, None otherwise.
    
    CRITICAL: Hashes are saved ONLY after successful notifications to prevent 
    duplicate alerts on bot restarts. Use should_save_hashes=False when 
    generating changelogs, then save hashes manually after notifications are sent.
    
    Args:
        project_root: Path to project root
        writer_bot: WriterBot instance for LLM calls
        admin_id: Admin user ID (for reference, not used in this function)
        lang: Language code (ru/en)
        should_save_hashes: If True, saves hashes after generating changelog. 
                           Set to False when testing or when you want to save hashes 
                           manually after confirming notifications were sent.
    """

    try:
        data_dir = project_root / "data"
        stored_hashes = load_stored_hashes(data_dir)
        changed_files = get_changed_files(project_root, stored_hashes)
        
        if not changed_files:
            return None
        
        changelog = generate_changelog_with_llm(writer_bot, changed_files, project_root, lang)
        
        if should_save_hashes:
            current_hashes = {}
            for rel_path in TRACKED_FILES:
                file_path = project_root / rel_path
                if file_path.exists():
                    current_hashes[rel_path] = calculate_file_hash(file_path)
            save_hashes(data_dir, current_hashes)
        
        return changelog

    except Exception as e:
        print(f"Changelog generation error: {e}")
        raise
