#!/usr/bin/env python3
"""
Initialize code hash cache to prevent "bot updated" message on next restart.
Run this after deploying new code to mark current state as "baseline".

Usage:
    python3 scripts/init_code_cache.py
    # or from scripts directory:
    cd scripts && python3 init_code_cache.py
"""

import sys
import json
import hashlib
from pathlib import Path

# Setup paths - works from any directory
script_dir = Path(__file__).parent.resolve()
project_root = script_dir.parent.resolve()

# Files to track (same as in code_reviewer.py)
TRACKED_FILES = [
    "src/telegram_bot.py",
    "src/writer_bot.py", 
    "src/i18n.py",
    "src/writer_rag.py",
    "src/lang_utils.py",
    "src/word_stats.py",
]


def calculate_file_hash(file_path: Path) -> str:
    """Calculate SHA256 hash of file contents."""
    try:
        with open(file_path, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except Exception:
        return ""


def get_backup_dir(data_dir: Path) -> Path:
    """Get backup directory for file contents."""
    return data_dir / "code_backup"


def save_file_backup(data_dir: Path, rel_path: str, content: str) -> None:
    """Save file content backup."""
    backup_dir = get_backup_dir(data_dir)
    backup_file = backup_dir / rel_path.replace("/", "_")
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        with open(backup_file, 'w', encoding='utf-8') as f:
            f.write(content)
    except Exception as e:
        print(f"   Warning: Failed to backup {rel_path}: {e}")


def init_cache():
    """Initialize code hash cache with current file states."""
    data_dir = project_root / "data"
    hash_file = data_dir / "code_hashes.json"
    
    # Ensure data directory exists
    data_dir.mkdir(parents=True, exist_ok=True)
    
    # Calculate hashes for all tracked files and save backups
    current_hashes = {}
    for rel_path in TRACKED_FILES:
        file_path = project_root / rel_path
        if file_path.exists():
            # Calculate hash
            current_hash = calculate_file_hash(file_path)
            current_hashes[rel_path] = current_hash
            
            # Save file content backup for diff generation
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                save_file_backup(data_dir, rel_path, content)
            except Exception as e:
                print(f"   Warning: Could not backup {rel_path}: {e}")
            
            print(f"✓ {rel_path}: {current_hash}")
        else:
            print(f"✗ {rel_path}: file not found")
    
    # Save to cache
    with open(hash_file, 'w', encoding='utf-8') as f:
        json.dump(current_hashes, f, ensure_ascii=False, indent=2)
    
    print(f"\n✅ Cache initialized: {hash_file}")
    print(f"   Backups saved to: {get_backup_dir(data_dir)}")
    print(f"   Next restart will NOT show 'bot updated' message")
    print(f"   (unless you actually change the code)")
    
    return 0


if __name__ == "__main__":
    sys.exit(init_cache())
