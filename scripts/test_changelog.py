#!/usr/bin/env python3
"""
Test changelog generation without restarting the bot.
Run from project root: python3 scripts/test_changelog.py [--lang ru|en] [--save-hashes]

Examples:
    python3 scripts/test_changelog.py              # Test in Russian (default)
    python3 scripts/test_changelog.py --lang en    # Test in English
    python3 scripts/test_changelog.py --save-hashes # Test and update cache
"""

import sys
import os
import argparse
import importlib.util
from pathlib import Path

# Setup paths
script_dir = Path(__file__).parent.resolve()
project_root = script_dir.parent.resolve()
src_dir = project_root / "src"

# Add src to path for imports
sys.path.insert(0, str(src_dir))

# Change to project root for dotenv
os.chdir(project_root)

# Load dotenv
from dotenv import load_dotenv
load_dotenv()

# Import code_reviewer functions
from code_reviewer import (
    generate_changelog_with_llm,
    get_changed_files, 
    load_stored_hashes,
    save_hashes,
    calculate_file_hash
)

# Tracked files list (must match code_reviewer.py)
TRACKED_FILES = [
    "src/telegram_bot.py",
    "src/writer_bot.py", 
    "src/i18n.py",
    "src/writer_rag.py",
    "src/lang_utils.py",
    "src/word_stats.py",
]


def load_writer_bot():
    """Load WriterBot class directly from file."""
    spec = importlib.util.spec_from_file_location("writer_bot", src_dir / "writer_bot.py")
    wb_module = importlib.util.module_from_spec(spec)
    sys.modules["writer_bot"] = wb_module
    spec.loader.exec_module(wb_module)
    return wb_module.WriterBot


def print_header(text, width=70):
    """Print formatted header."""
    print("\n" + "=" * width)
    print(f" {text}")
    print("=" * width)


def print_section(text):
    """Print section header."""
    print(f"\n▶ {text}")


def print_success(text):
    """Print success message."""
    print(f"  ✓ {text}")


def print_error(text):
    """Print error message."""
    print(f"  ✗ {text}")


def print_info(text):
    """Print info message."""
    print(f"  • {text}")


def check_env():
    """Check if required environment variables are set."""
    required = ["OPENAI_API_KEY"]
    missing = [var for var in required if not os.getenv(var)]
    
    if missing:
        print_error(f"Missing environment variables: {', '.join(missing)}")
        print_info("Make sure .env file exists in project root")
        return False
    return True


def test_changelog(lang="ru", save_hashes=False):
    """Test changelog generation."""
    print_header(f"CHANGELOG TEST [{lang.upper()}]")
    
    # Check environment
    print_section("Checking environment...")
    if not check_env():
        return 1
    print_success("Environment OK")
    
    # Load WriterBot
    print_section("Loading WriterBot...")
    try:
        WriterBot = load_writer_bot()
        print_success("WriterBot class loaded")
    except Exception as e:
        print_error(f"Failed to load WriterBot: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    # Create instance
    print_section("Initializing LLM client...")
    try:
        wb = WriterBot(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            api_key=os.getenv("OPENAI_API_KEY"),
            api_base=os.getenv("OPENAI_API_BASE"),
            use_rag=False,
        )
        print_success(f"LLM client ready (model: {wb.model})")
    except Exception as e:
        print_error(f"Failed to initialize: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    # Check for changes
    print_section("Scanning for code changes...")
    data_dir = project_root / "data"
    stored_hashes = load_stored_hashes(data_dir)
    changed_files = get_changed_files(project_root, stored_hashes)
    
    if not changed_files:
        print_info("No code changes detected")
        print_info("Next restart will NOT show 'bot updated' message")
        print_info("To force changelog, modify any tracked file or delete data/code_hashes.json")
        return 0
    
    print_success(f"Found {len(changed_files)} changed file(s):")
    for rel_path, old_hash, new_hash in changed_files:
        status = "NEW" if not old_hash else "MODIFIED"
        print_info(f"{rel_path:30} [{status}]")
    
    # Generate changelog
    print_section("Generating changelog with LLM...")
    print_info("This may take a few seconds...")
    
    try:
        changelog = generate_changelog_with_llm(
            wb, changed_files, project_root, lang
        )
        
        if changelog:
            print_header("CHANGELOG PREVIEW", width=70)
            print(changelog)
            print("=" * 70)
            
            print_section("Summary")
            print_success("Changelog generated successfully")
            print_info(f"Language: {lang}")
            print_info(f"Changed files: {len(changed_files)}")
            
            if save_hashes:
                print_section("Updating hash cache...")
                current_hashes = {}
                for rel_path in TRACKED_FILES:
                    file_path = project_root / rel_path
                    if file_path.exists():
                        current_hashes[rel_path] = calculate_file_hash(file_path)
                save_hashes(data_dir, current_hashes)
                print_success("Cache updated - next restart won't trigger notifications")
            else:
                print_info("Run with --save-hashes to update cache")
                print_info("Or run: python3 scripts/init_code_cache.py")
        else:
            print_error("No changelog generated (check code_reviewer logs above)")
        
    except Exception as e:
        print_error(f"Error generating changelog: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    return 0


def main():
    """Parse arguments and run test."""
    parser = argparse.ArgumentParser(
        description="Test changelog generation without restarting the bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 scripts/test_changelog.py              # Test in Russian (default)
  python3 scripts/test_changelog.py --lang en    # Test in English
  python3 scripts/test_changelog.py --save-hashes # Test and update cache
        """
    )
    parser.add_argument(
        "--lang", 
        choices=["ru", "en"], 
        default="ru",
        help="Language for changelog (default: ru)"
    )
    parser.add_argument(
        "--save-hashes",
        action="store_true",
        help="Update hash cache after testing (prevents duplicate notifications)"
    )
    
    args = parser.parse_args()
    exit(test_changelog(lang=args.lang, save_hashes=args.save_hashes))


if __name__ == "__main__":
    main()
