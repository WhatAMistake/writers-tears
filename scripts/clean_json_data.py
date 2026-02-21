import json
import re
import os
from pathlib import Path

def clean_text(text):
    if not text:
        return text
    
    # 1. Fix broken words with hyphens at line breaks (e.g. "доказател-\nьства" -> "доказательства")
    # This is common in PDF extraction
    text = re.sub(r'(\w+)-\s*\n\s*(\w+)', r'\1\2', text)
    
    # 2. Fix broken words without hyphens (e.g. "помнишь\nь", "с\nсмерти", "У\nУ меня")
    # This looks like a specific artifact where the last letter is repeated or split
    # Pattern: letter + newline + same letter (or part of word)
    # Example: "с\nсмерти" -> "смерти"
    # Example: "помнишь\nь" -> "помнишь"
    
    # Case A: Single letter repeated after newline (e.g. "с\nсмерти")
    # We look for: char + newline + same char
    def fix_repeated_char(match):
        char1 = match.group(1)
        char2 = match.group(2)
        if char1.lower() == char2.lower():
            return char2
        return match.group(0)
    
    text = re.sub(r'(\w)\s*\n\s*(\w)', fix_repeated_char, text)

    # 3. Fix random newlines in the middle of sentences
    # Replace newline with space if it's not followed by an uppercase letter (start of new sentence)
    # and not preceded by punctuation (end of sentence)
    # This is a heuristic and might be too aggressive for poetry, but good for prose.
    # We'll be conservative: only replace newline with space if surrounded by lowercase letters or comma
    text = re.sub(r'(?<=[а-яa-z,])\s*\n\s*(?=[а-яa-z])', ' ', text)
    
    # 4. Collapse multiple spaces
    text = re.sub(r'\s+', ' ', text)
    
    return text.strip()

def process_file(filepath):
    print(f"Processing {filepath}...")
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        modified = False
        if isinstance(data, list):
            for item in data:
                if 'text' in item:
                    original = item['text']
                    cleaned = clean_text(original)
                    if original != cleaned:
                        item['text'] = cleaned
                        modified = True
        elif isinstance(data, dict):
            # Handle structure like {"prompts": [...]}
            for key, value in data.items():
                if isinstance(value, list):
                    for i, item in enumerate(value):
                        if isinstance(item, str):
                            cleaned = clean_text(item)
                            if item != cleaned:
                                value[i] = cleaned
                                modified = True
                        elif isinstance(item, dict) and 'text' in item:
                            original = item['text']
                            cleaned = clean_text(original)
                            if original != cleaned:
                                item['text'] = cleaned
                                modified = True

        if modified:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"Fixed and saved {filepath}")
        else:
            print(f"No changes needed for {filepath}")
            
    except Exception as e:
        print(f"Error processing {filepath}: {e}")

def main():
    data_dir = Path("data")
    files_to_clean = [
        "style.json",
        "craft.json",
        "book_chunks.json"
    ]
    
    for filename in files_to_clean:
        filepath = data_dir / filename
        if filepath.exists():
            process_file(filepath)
        else:
            print(f"File not found: {filepath}")

if __name__ == "__main__":
    main()