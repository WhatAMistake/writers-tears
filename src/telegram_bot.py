"""
Telegram bot for Writer's Tears — writing coach.
"""

import os
import asyncio
import json
import random
import io
import tempfile
from pathlib import Path
from typing import Optional, BinaryIO
from datetime import datetime, timedelta

from dotenv import load_dotenv
load_dotenv()

# Add src to path for imports when running as module
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

# Document processing imports (optional - graceful fallback if not installed)
try:
    from pypdf import PdfReader
    PYPDF_AVAILABLE = True
except ImportError:
    PYPDF_AVAILABLE = False

try:
    from docx import Document
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False


try:
    from aiogram import Bot, Dispatcher, types, F
    from aiogram.filters import Command
    from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
    from aiogram.utils.keyboard import ReplyKeyboardBuilder
    AIogram_AVAILABLE = True
except ImportError:
    AIogram_AVAILABLE = False

from writer_bot import WriterBot
from lang_utils import detect_language
from i18n import t
from word_stats import add_word_count, get_stats, count_words, count_chars, reset_stats
from code_reviewer import check_and_generate_changelog

# Import init_cache functionality for auto-updating after restart
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from init_code_cache import init_cache

DEFAULT_LANG = os.getenv("DEFAULT_LANG", "en")
DEV_ID = int(os.getenv("DEV_ID", "0"))  # Developer ID for feedback from .env


def get_main_keyboard(lang: str = "en") -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text=t(lang, "button_prompt")))
    builder.add(KeyboardButton(text=t(lang, "button_idea")))
    builder.add(KeyboardButton(text=t(lang, "button_methodique")))
    builder.add(KeyboardButton(text=t(lang, "button_help")))
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)


class TelegramWriterBot:
    """Telegram front-end for Writer's Tears."""

    def __init__(
        self,
        telegram_token: str,
        llm_model: str = "gpt-4o-mini",
        llm_api_key: Optional[str] = None,
        llm_api_base: Optional[str] = None,
        use_rag: bool = True,
    ):
        if not AIogram_AVAILABLE:
            raise RuntimeError("aiogram not installed. pip install aiogram")

        self.telegram_token = telegram_token
        self.bot = Bot(token=telegram_token)
        self.dp = Dispatcher()
        self.sessions: dict[int, WriterBot] = {}
        self.user_langs: dict[int, str] = {}
        self.user_states: dict[int, str] = {}
        self.last_user_message: dict[int, str] = {}
        self.accumulated_text: dict[int, str] = {}  # For multi-message input
        self.admin_id = int(os.getenv("ADMIN_ID", "0"))
        
        # Error tracking for spam protection
        self.user_error_count: dict[int, int] = {}  # consecutive errors per user
        self.user_error_cooldown: dict[int, datetime] = {}  # cooldown until time
        self.error_cooldown_seconds: int = 30  # cooldown after errors
        self.max_consecutive_errors: int = 3  # errors before cooldown
        self.llm_model = llm_model
        self.llm_api_key = llm_api_key
        self.llm_api_base = llm_api_base
        self.use_rag = use_rag
        self.bot_username: Optional[str] = None
        
        # Auto-activation tracking for group chats (chat_id -> last_activation_time)
        self.last_auto_activation: dict[int, datetime] = {}
        self.auto_activation_task: Optional[asyncio.Task] = None
        
        # Group chat availability toggle (admin only)
        self.group_chats_enabled: bool = True
        
        # Daily quotes tracking (session-only, no persistence)
        self.user_cite_enabled: dict[int, bool] = {}
        self.user_cite_history: dict[int, list[str]] = {}
        self.user_cite_last_time: dict[int, datetime] = {}
        self.user_cite_count: dict[int, int] = {} # Number of auto-quotes sent
        self.user_last_activity: dict[int, datetime] = {}
        self.bot_last_activity: datetime = datetime.now()  # Track bot's own activity
        self.AUTO_DISABLE_DAYS: int = 17 # Days of inactivity before auto-disable
        self.pending_changelog: Optional[str] = None  # Store changelog to show to users
        
        # User preferences persistence (copied from therapist bot)
        self.prefs_path = Path(__file__).parent.parent / "data" / "user_prefs.json"
        self._load_user_prefs()
        
        # Changelog cooldown tracking
        self.changelog_cooldown_hours: int = 1  # Minimum hours between changelogs
        self.changelog_last_sent_path = Path(__file__).parent.parent / "data" / "changelog_last_sent.txt"

        # Unified state configuration: state_name -> (min_length, handler_method_name or None)
        # This eliminates duplication across waiting_states, accumulation_states, and state_handlers
        self.STATE_CONFIG = {
            "feedback": (20, "feedback_on_text"),
            "style": (20, "analyze_style"),
            "roast": (50, "roast"),
            "praise": (50, "praise"),
            "corrector_wait": (3, "correct_text"),
            "editor_wait": (5, "edit_text"),
            "methodique_wait": (5, "methodique"),
            "count_me_wait": (1, None),  # Special handling
            "block_wait": (1, "handle_block"),
            "develop_wait": (1, "develop_idea"),
            "character_wait": (1, "character_help"),
            "dialogue_wait": (1, "dialogue_help"),
            "summary_wait": (10, None),  # Special handling with buttons
            "dev_feedback_wait": (1, None),  # Special handling
        }
        
        # Discussion states: for discussing results while keeping tool context
        # Maps discussion state -> (original tool state, instruction_key)
        self.DISCUSSION_STATES = {
            "feedback_discuss": ("feedback", "instr_feedback"),
            "style_discuss": ("style", "instr_style"),
            "roast_discuss": ("roast", "instr_roast"),
            "praise_discuss": ("praise", "instr_praise"),
            "corrector_discuss": ("corrector_wait", "instr_corrector"),
            "editor_discuss": ("editor_wait", "instr_editor"),
            "methodique_discuss": ("methodique_wait", "instr_methodique"),
            "summary_discuss": ("summary_wait", None),
        }
        
        # States that require user input (waiting states)
        self.WAITING_STATES = set(self.STATE_CONFIG.keys()) | set(self.DISCUSSION_STATES.keys()) | {"cry_baby"}

        self._register_handlers()

    def _load_user_prefs(self):
        """Load user preferences from JSON file."""
        try:
            self.prefs_path.parent.mkdir(parents=True, exist_ok=True)
            if self.prefs_path.exists():
                with open(self.prefs_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.user_langs = {int(k): v for k, v in data.get('user_langs', {}).items()}
        except Exception:
            self.user_langs = getattr(self, "user_langs", {}) or {}

    def _save_user_prefs(self):
        """Save user preferences to JSON file."""
        try:
            payload = {
                'user_langs': {str(k): v for k, v in self.user_langs.items()},
            }
            with open(self.prefs_path, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _get_writer_bot(self, user_id: int) -> WriterBot:
        # Always get current language from user_langs to ensure it's up-to-date
        lang = self.user_langs.get(user_id, DEFAULT_LANG)
        
        # Create new session if needed OR if language changed
        if user_id not in self.sessions:
            self.sessions[user_id] = WriterBot(
                model=self.llm_model,
                api_key=self.llm_api_key,
                api_base=self.llm_api_base,
                use_rag=self.use_rag,
                language=lang,
            )
        else:
            # Update language if it changed (fixes /cry_baby and other language-dependent features)
            sess = self.sessions[user_id]
            if sess.language != lang:
                sess.language = lang
                sess.system_prompt = sess._load_system_prompt()
        
        return self.sessions[user_id]

    def _register_handlers(self):

        @self.dp.message(Command("start"))
        async def cmd_start(message: types.Message):
            await self._handle_start(message)


        @self.dp.message(Command("help"))
        async def cmd_help(message: types.Message):
            await self._handle_help(message)

        @self.dp.message(Command("lang"))
        async def cmd_lang(message: types.Message):
            await self._handle_lang(message)

        @self.dp.message(Command("switchlang"))
        async def cmd_switchlang(message: types.Message):
            await self._handle_switchlang(message)

        @self.dp.message(Command("reset"))
        async def cmd_reset(message: types.Message):
            await self._handle_reset(message)

        @self.dp.message(Command("block"))
        async def cmd_block(message: types.Message):
            await self._handle_block(message)

        @self.dp.message(Command("develop"))
        async def cmd_develop(message: types.Message):
            await self._handle_develop(message)

        @self.dp.message(Command("character"))
        async def cmd_character(message: types.Message):
            await self._handle_character(message)

        @self.dp.message(Command("dialogue"))
        async def cmd_dialogue(message: types.Message):
            await self._handle_dialogue(message)

        @self.dp.message(Command("prompt"))
        async def cmd_prompt(message: types.Message):
            await self._handle_prompt(message)

        @self.dp.message(Command("idea"))
        async def cmd_idea(message: types.Message):
            await self._handle_idea(message)

        @self.dp.message(Command("feedback"))
        async def cmd_feedback(message: types.Message):
            await self._handle_feedback_cmd(message)

        @self.dp.message(Command("style"))
        async def cmd_style(message: types.Message):
            await self._handle_style_cmd(message)

        @self.dp.message(Command("roast"))
        async def cmd_roast(message: types.Message):
            await self._handle_roast(message)

        @self.dp.message(Command("praise"))
        async def cmd_praise(message: types.Message):
            await self._handle_praise(message)

        @self.dp.message(Command("corrector"))
        async def cmd_corrector(message: types.Message):
            await self._handle_corrector(message)

        @self.dp.message(Command("editor"))
        async def cmd_editor(message: types.Message):
            await self._handle_editor(message)

        @self.dp.message(Command("count_me"))
        async def cmd_count_me(message: types.Message):
            await self._handle_count_me(message)

        @self.dp.message(Command("stats"))
        async def cmd_stats(message: types.Message):
            await self._handle_stats(message)

        @self.dp.message(Command("lobster"))
        async def cmd_lobster(message: types.Message):
            await self._handle_lobster(message)

        @self.dp.message(Command("pun"))
        async def cmd_pun(message: types.Message):
            await self._handle_pun(message)

        @self.dp.message(Command("porko"))
        async def cmd_porko(message: types.Message):
            await self._handle_porko(message)

        @self.dp.message(Command("methodique"))
        @self.dp.message(Command("methodichque"))
        async def cmd_methodique(message: types.Message):
            await self._handle_methodique(message)

        @self.dp.message(Command("cite"))
        async def cmd_cite(message: types.Message):
            await self._handle_cite(message)

        @self.dp.message(Command("cite_off"))
        async def cmd_cite_off(message: types.Message):
            await self._handle_cite_off(message)

        @self.dp.message(Command("cite_on"))
        async def cmd_cite_on(message: types.Message):
            await self._handle_cite_on(message)

        @self.dp.message(Command("cite_when"))
        async def cmd_cite_when(message: types.Message):
            await self._handle_cite_when(message)

        @self.dp.message(Command("summary"))
        async def cmd_summary(message: types.Message):
            await self._handle_summary(message)

        @self.dp.message(Command("cry_baby"))
        async def cmd_cry_baby(message: types.Message):
            await self._handle_cry_baby(message)

        @self.dp.message(Command("admin"))
        async def cmd_admin(message: types.Message):
            await self._handle_admin(message)

        @self.dp.message(Command("dev_feedback"))
        async def cmd_dev_feedback(message: types.Message):
            await self._handle_dev_feedback(message)

        @self.dp.message(Command("done"))
        async def cmd_done(message: types.Message):
            await self._handle_done(message)

        @self.dp.message(Command("confo_enable37"))
        async def cmd_confo_enable37(message: types.Message):
            await self._handle_confo_toggle(message)

        @self.dp.message(Command("upload"))
        async def cmd_upload(message: types.Message):
            await self._handle_upload_cmd(message)

        # Document handler with DDoS protection
        @self.dp.message(F.document)
        async def handle_document(message: types.Message):
            await self._handle_document(message)

        # Button handlers - explicit text matching for each language
        # RU buttons



        # Prompt button
        @self.dp.message(
            F.text.in_({
                t("ru", "button_prompt"),
                t("en", "button_prompt"),
            })
        )
        async def btn_prompt(message: types.Message):
            await self._handle_prompt(message)


# Idea / Plot button
        @self.dp.message(
            F.text.in_({
                t("ru", "button_idea"),
                t("en", "button_idea"),
            })
        )
        async def btn_idea(message: types.Message):
            await self._handle_idea(message)


# Methodique button
        @self.dp.message(
            F.text.in_({
                t("ru", "button_methodique"),
                t("en", "button_methodique"),
            })
        )
        async def btn_methodique(message: types.Message):
            await self._handle_methodique(message)


# Help / Commands button
        @self.dp.message(
            F.text.in_({
                t("ru", "button_help"),
                t("en", "button_help"),
            })
        )
        async def btn_help(message: types.Message):
            await self._handle_help(message)


        @self.dp.message(F.sticker)
        async def handle_sticker(message: types.Message):
            # Ignore stickers silently
            return

        @self.dp.message(F.text & ~F.text.startswith("/"))
        async def handle_text(message: types.Message):
            await self._handle_message(message)

    async def _handle_button(self, message: types.Message, key: str):
        user_id = message.from_user.id
        if key == "button_block":
            await self._handle_block(message)
        elif key == "button_idea":
            await self._handle_idea(message)
        elif key == "button_methodique":
            await self._handle_methodique(message)
        elif key == "button_help":
            await self._handle_help(message)


    async def _handle_start(self, message: types.Message):
        user_id = message.from_user.id
        chat_id = message.chat.id
        name = message.from_user.first_name or "Writer"
        self.user_states[user_id] = "chat"
        
        # Set language from Telegram locale on first start, but don't override saved preference
        tg_lang = (message.from_user.language_code or "").lower()
        if user_id not in self.user_langs:
            if tg_lang.startswith("ru"):
                self.user_langs[user_id] = "ru"
            elif tg_lang.startswith("en"):
                self.user_langs[user_id] = "en"
            self._save_user_prefs()
            
        # Enable daily quotes by default for new users
        if user_id not in self.user_cite_enabled:
            self.user_cite_enabled[user_id] = True
            # Set last time to 24h ago so they get it soon
            self.user_cite_last_time[user_id] = datetime.now() - timedelta(hours=24)
        
        # Force clear history for this user to ensure a new quote is sent on /start
        if user_id in self.user_cite_history:
            self.user_cite_history[user_id] = []
        # Reset last time to ensure immediate trigger
        self.user_cite_last_time[user_id] = datetime.now() - timedelta(hours=25)
        
        # Immediate quote for new users or those who haven't received one in 24h
        await self._check_and_send_daily_cite(user_id, chat_id)

        lang = self.user_langs.get(user_id, DEFAULT_LANG)
        welcome = t(lang, "welcome", name=name)

        # Show pending changelog to all users if available
        if self.pending_changelog:
            await message.answer(self.pending_changelog, parse_mode="HTML")

        await message.answer(welcome, reply_markup=get_main_keyboard(lang), parse_mode="HTML")

    async def _handle_help(self, message: types.Message):

        user_id = message.from_user.id
        lang = self.user_langs.get(user_id, DEFAULT_LANG)
        await message.answer(t(lang, "help"), parse_mode="HTML")

    async def _handle_lang(self, message: types.Message):
        """Handle /lang command - set or show language (copied from therapist bot)."""
        user_id = message.from_user.id
        parts = (message.text or "").split(None, 1)
        args = parts[1].strip().lower() if len(parts) > 1 else ""
        current = self.user_langs.get(user_id, DEFAULT_LANG)
        
        if not args:
            await message.answer(t(current, "lang_current", language=current))
            return
        
        if args in ("ru", "en"):
            self.user_langs[user_id] = args
            # Update session if exists
            if user_id in self.sessions:
                sess = self.sessions[user_id]
                sess.language = args
                sess.system_prompt = sess._load_system_prompt()
            # Persist
            self._save_user_prefs()
            await message.answer(t(args, "lang_set", language=args), reply_markup=get_main_keyboard(args))
            return
        
        await message.answer(t(current, "lang_invalid"))

    async def _handle_switchlang(self, message: types.Message):
        """Handle /switchlang command - toggle between ru and en."""
        user_id = message.from_user.id
        current = self.user_langs.get(user_id, DEFAULT_LANG)
        new_lang = "en" if current == "ru" else "ru"
        self.user_langs[user_id] = new_lang
        
        # Update session if exists
        if user_id in self.sessions:
            sess = self.sessions[user_id]
            sess.language = new_lang
            sess.system_prompt = sess._load_system_prompt()
        
        # Persist
        self._save_user_prefs()
        response_text = t(new_lang, "lang_set", language=new_lang)
        await message.answer(response_text, reply_markup=get_main_keyboard(new_lang))

    async def _handle_reset(self, message: types.Message):
        user_id = message.from_user.id
        chat_id = message.chat.id
        if user_id in self.sessions:
            self.sessions[user_id].reset()
        self.user_states[user_id] = "chat"
        # Reset word and character stats
        reset_stats(user_id)
        
        lang = self.user_langs.get(user_id, DEFAULT_LANG)
        await message.answer(t(lang, "reset_confirm"), reply_markup=get_main_keyboard(lang))

    async def _handle_block(self, message: types.Message):

        user_id = message.from_user.id
        lang = self.user_langs.get(user_id, DEFAULT_LANG)
        text = (message.text or "").strip()
        if not text or text == "/block":
            self.user_states[user_id] = "block_wait"
            await message.answer(t(lang, "block_prompt_empty"))
            return
        if text.startswith("/block"):
            text = text.replace("/block", "").strip() or "I'm stuck."
        await self.bot.send_chat_action(chat_id=message.chat.id, action="typing")
        wb = self._get_writer_bot(user_id)
        reply = wb.handle_block(text)
        self.user_states[user_id] = "chat"
        await message.answer(reply)

    async def _handle_develop(self, message: types.Message):
        user_id = message.from_user.id
        lang = self.user_langs.get(user_id, DEFAULT_LANG)
        text = (message.text or "").strip().replace("/develop", "").strip()
        if not text:
            self.user_states[user_id] = "develop_wait"
            await message.answer(t(lang, "develop_prompt_empty"))
            return
        await self.bot.send_chat_action(chat_id=message.chat.id, action="typing")
        reply = self._get_writer_bot(user_id).develop_idea(text)
        self.user_states[user_id] = "chat"
        await message.answer(reply)

    async def _handle_character(self, message: types.Message):
        user_id = message.from_user.id
        lang = self.user_langs.get(user_id, DEFAULT_LANG)
        text = (message.text or "").strip().replace("/character", "").strip()
        if not text:
            self.user_states[user_id] = "character_wait"
            await message.answer(t(lang, "character_prompt_empty"))
            return
        await self.bot.send_chat_action(chat_id=message.chat.id, action="typing")
        reply = self._get_writer_bot(user_id).character_help(text)
        self.user_states[user_id] = "chat"
        await message.answer(reply)

    async def _handle_dialogue(self, message: types.Message):
        user_id = message.from_user.id
        lang = self.user_langs.get(user_id, DEFAULT_LANG)
        text = (message.text or "").strip().replace("/dialogue", "").strip()
        if not text:
            self.user_states[user_id] = "dialogue_wait"
            await message.answer(t(lang, "dialogue_prompt_empty"))
            return
        await self.bot.send_chat_action(chat_id=message.chat.id, action="typing")
        reply = self._get_writer_bot(user_id).dialogue_help(text)
        self.user_states[user_id] = "chat"
        await message.answer(reply)

    async def _handle_prompt(self, message: types.Message):
        user_id = message.from_user.id
        lang = self.user_langs.get(user_id, DEFAULT_LANG)
        prompt = self._get_writer_bot(user_id).get_random_prompt()
        await message.answer(f"{t(lang, 'prompt_label')}\n\n{prompt}")

    async def _handle_idea(self, message: types.Message):
        user_id = message.from_user.id
        lang = self.user_langs.get(user_id, DEFAULT_LANG)
        idea = self._get_writer_bot(user_id).generate_idea()
        await message.answer(f"{t(lang, 'idea_label')}\n\n{idea}")

    async def _handle_feedback_cmd(self, message: types.Message):
        user_id = message.from_user.id
        lang = self.user_langs.get(user_id, DEFAULT_LANG)
        state = self.user_states.get(user_id, "chat")
        
        # Check if we have document text waiting
        if state == "document_wait":
            text = self.accumulated_text.get(user_id, "")
            if len(text) < 20:
                await message.answer(t(lang, "text_too_short", min=20))
                return
            await self.bot.send_chat_action(chat_id=message.chat.id, action="typing")
            reply = self._get_writer_bot(user_id).feedback_on_text(text)
            self.user_states[user_id] = "chat"
            del self.accumulated_text[user_id]
            await message.answer(reply)
            return
        
        text = (message.text or "").strip().replace("/feedback", "").strip()
        if len(text) < 20:
            self.user_states[user_id] = "feedback"
            self.accumulated_text[user_id] = ""  # Start accumulation
            await message.answer(t(lang, "feedback_prompt"))
            return
        await self.bot.send_chat_action(chat_id=message.chat.id, action="typing")
        reply = self._get_writer_bot(user_id).feedback_on_text(text)
        self.user_states[user_id] = "chat"
        await message.answer(reply)

    async def _handle_style_cmd(self, message: types.Message):
        user_id = message.from_user.id
        lang = self.user_langs.get(user_id, DEFAULT_LANG)
        state = self.user_states.get(user_id, "chat")
        
        # Check if we have document text waiting
        if state == "document_wait":
            text = self.accumulated_text.get(user_id, "")
            if len(text) < 20:
                await message.answer(t(lang, "text_too_short", min=20))
                return
            await self.bot.send_chat_action(chat_id=message.chat.id, action="typing")
            reply = self._get_writer_bot(user_id).analyze_style(text)
            self.user_states[user_id] = "chat"
            del self.accumulated_text[user_id]
            await message.answer(reply)
            return
        
        text = (message.text or "").strip().replace("/style", "").strip()
        if len(text) < 20:
            self.user_states[user_id] = "style"
            self.accumulated_text[user_id] = ""  # Start accumulation
            await message.answer(t(lang, "style_prompt"))
            return
        await self.bot.send_chat_action(chat_id=message.chat.id, action="typing")
        reply = self._get_writer_bot(user_id).analyze_style(text)
        self.user_states[user_id] = "chat"
        await message.answer(reply)

    async def _handle_roast(self, message: types.Message):
        user_id = message.from_user.id
        lang = self.user_langs.get(user_id, DEFAULT_LANG)
        state = self.user_states.get(user_id, "chat")
        
        # Check if we have document text waiting
        if state == "document_wait":
            text = self.accumulated_text.get(user_id, "")
            if len(text) < 50:
                await message.answer(t(lang, "text_too_short", min=50))
                return
            await self.bot.send_chat_action(chat_id=message.chat.id, action="typing")
            reply = self._get_writer_bot(user_id).roast(text)
            self.user_states[user_id] = "chat"
            del self.accumulated_text[user_id]
            await message.answer(reply)
            return
        
        text = (message.text or "").strip().replace("/roast", "").strip()
        if len(text) < 50:
            self.user_states[user_id] = "roast"
            self.accumulated_text[user_id] = ""  # Start accumulation
            await message.answer(t(lang, "roast_prompt"))
            return
        await self.bot.send_chat_action(chat_id=message.chat.id, action="typing")
        reply = self._get_writer_bot(user_id).roast(text)
        self.user_states[user_id] = "chat"
        await message.answer(reply)

    async def _handle_praise(self, message: types.Message):
        user_id = message.from_user.id
        lang = self.user_langs.get(user_id, DEFAULT_LANG)
        state = self.user_states.get(user_id, "chat")
        
        # Check if we have document text waiting
        if state == "document_wait":
            text = self.accumulated_text.get(user_id, "")
            if len(text) < 50:
                await message.answer(t(lang, "text_too_short", min=50))
                return
            await self.bot.send_chat_action(chat_id=message.chat.id, action="typing")
            reply = self._get_writer_bot(user_id).praise(text)
            self.user_states[user_id] = "chat"
            del self.accumulated_text[user_id]
            await message.answer(reply)
            return
        
        text = (message.text or "").strip().replace("/praise", "").strip()
        if len(text) < 50:
            self.user_states[user_id] = "praise"
            self.accumulated_text[user_id] = ""  # Start accumulation
            await message.answer(t(lang, "praise_prompt"))
            return
        await self.bot.send_chat_action(chat_id=message.chat.id, action="typing")
        reply = self._get_writer_bot(user_id).praise(text)
        self.user_states[user_id] = "chat"
        await message.answer(reply)

    async def _handle_corrector(self, message: types.Message):
        user_id = message.from_user.id
        lang = self.user_langs.get(user_id, DEFAULT_LANG)
        state = self.user_states.get(user_id, "chat")
        
        # Check if we have document text waiting
        if state == "document_wait":
            text = self.accumulated_text.get(user_id, "")
            if len(text) < 3:
                await message.answer(t(lang, "text_too_short", min=3))
                return
            await self.bot.send_chat_action(chat_id=message.chat.id, action="typing")
            reply = self._get_writer_bot(user_id).correct_text(text)
            self.user_states[user_id] = "chat"
            del self.accumulated_text[user_id]
            await message.answer(reply)
            return
        
        text = (message.text or "").strip().replace("/corrector", "").strip()
        if len(text) < 3:
            self.user_states[user_id] = "corrector_wait"
            self.accumulated_text[user_id] = ""  # Start accumulation
            await message.answer(t(lang, "corrector_prompt"))
            return
        await self.bot.send_chat_action(chat_id=message.chat.id, action="typing")
        reply = self._get_writer_bot(user_id).correct_text(text)
        self.user_states[user_id] = "chat"
        await message.answer(reply)

    async def _handle_editor(self, message: types.Message):
        user_id = message.from_user.id
        lang = self.user_langs.get(user_id, DEFAULT_LANG)
        state = self.user_states.get(user_id, "chat")
        
        # Check if we have document text waiting
        if state == "document_wait":
            text = self.accumulated_text.get(user_id, "")
            if len(text) < 5:
                await message.answer(t(lang, "text_too_short", min=5))
                return
            await self.bot.send_chat_action(chat_id=message.chat.id, action="typing")
            reply = self._get_writer_bot(user_id).edit_text(text)
            self.user_states[user_id] = "chat"
            del self.accumulated_text[user_id]
            await message.answer(reply)
            return
        
        text = (message.text or "").strip().replace("/editor", "").strip()
        if len(text) < 5:
            self.user_states[user_id] = "editor_wait"
            self.accumulated_text[user_id] = ""  # Start accumulation
            await message.answer(t(lang, "editor_prompt"))
            return
        await self.bot.send_chat_action(chat_id=message.chat.id, action="typing")
        reply = self._get_writer_bot(user_id).edit_text(text)
        self.user_states[user_id] = "chat"
        await message.answer(reply)

    async def _handle_count_me(self, message: types.Message):
        user_id = message.from_user.id
        lang = self.user_langs.get(user_id, DEFAULT_LANG)
        state = self.user_states.get(user_id, "chat")
        
        # Check if we have document text waiting
        if state == "document_wait":
            text = self.accumulated_text.get(user_id, "")
            if not text:
                await message.answer(t(lang, "no_text_accumulated"))
                return
            words = count_words(text)
            chars = count_chars(text)
            add_word_count(user_id, words, chars)
            s = get_stats(user_id)
            self.user_states[user_id] = "chat"
            del self.accumulated_text[user_id]
            await message.answer(t(lang, "count_me_done", count=words, chars=chars, today=s["today"], week=s["week"], month=s["month"], total=s["total"], chars_today=s["chars_today"], chars_week=s["chars_week"], chars_month=s["chars_month"], chars_total=s["chars_total"]))
            return
        
        text = (message.text or "").strip().replace("/count_me", "").strip()
        if not text:
            self.user_states[user_id] = "count_me_wait"
            self.accumulated_text[user_id] = ""  # Start accumulation
            await message.answer(t(lang, "count_me_prompt"))
            return
        words = count_words(text)
        chars = count_chars(text)
        add_word_count(user_id, words, chars)
        s = get_stats(user_id)
        self.user_states[user_id] = "chat"
        await message.answer(t(lang, "count_me_done", count=words, chars=chars, today=s["today"], week=s["week"], month=s["month"], total=s["total"], chars_today=s["chars_today"], chars_week=s["chars_week"], chars_month=s["chars_month"], chars_total=s["chars_total"]))

    async def _handle_stats(self, message: types.Message):
        user_id = message.from_user.id
        lang = self.user_langs.get(user_id, DEFAULT_LANG)
        s = get_stats(user_id)
        await message.answer(t(lang, "stats_format", 
            today=s["today"], week=s["week"], month=s["month"], total=s["total"],
            chars_today=s["chars_today"], chars_week=s["chars_week"], chars_month=s["chars_month"], chars_total=s["chars_total"]))

    async def _handle_lobster(self, message: types.Message):
        user_id = message.from_user.id
        lang = self.user_langs.get(user_id, DEFAULT_LANG)
        await message.answer(t(lang, "lobster_typing"))
        chunks = self._get_writer_bot(user_id).lobster()
        for chunk in chunks:
            await message.answer(chunk)
            await asyncio.sleep(0.8)

    async def _handle_pun(self, message: types.Message):
        user_id = message.from_user.id
        lang = self.user_langs.get(user_id, DEFAULT_LANG)
        last = self.last_user_message.get(user_id, "").strip()
        if not last or last.startswith("/"):
            await message.answer(t(lang, "pun_no_message"))
            return
        reply = self._get_writer_bot(user_id).pun(last)
        await message.answer(reply)

    async def _handle_porko(self, message: types.Message):
        user_id = message.from_user.id
        wb = self._get_writer_bot(user_id)
        reply = wb.porko()
        await message.answer(reply)

    async def _handle_upload_cmd(self, message: types.Message):
        """Handle /upload command - prompt user to send a file."""
        user_id = message.from_user.id
        lang = self.user_langs.get(user_id, DEFAULT_LANG)
        self.user_states[user_id] = "upload_wait"
        await message.answer(t(lang, "upload_prompt"))

    async def _handle_document(self, message: types.Message):
        """Handle document uploads (.txt, .docx, .pdf) with DDoS protection."""
        user_id = message.from_user.id
        lang = self.user_langs.get(user_id, DEFAULT_LANG)
        
        # Check if user is in upload_wait state or just sent file directly
        state = self.user_states.get(user_id, "chat")
        
        # DDoS protection limits - increased for novels
        MAX_FILE_SIZE_MB = 20  # Max 20MB for novels
        MAX_PAGES_PDF = 500     # Max 500 pages for novels
        MAX_CHARS_DOCX = 300000  # Max 300k chars (~100k words)
        MAX_CHARS_TXT = 500000  # Max 500k chars (~150k words)
        
        doc = message.document
        if not doc:
            return
        
        file_name = doc.file_name or ""
        file_size = doc.file_size or 0
        
        # Check file extension
        allowed_extensions = (".txt", ".docx", ".pdf")
        if not any(file_name.lower().endswith(ext) for ext in allowed_extensions):
            return  # Silently ignore non-supported documents
        
        # Check file size (5MB limit)
        if file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
            await message.answer(t(lang, "file_too_large", max_size=MAX_FILE_SIZE_MB))
            return
        
        # Check if user is in cooldown (additional DDoS protection)
        now = datetime.now()
        if user_id in self.user_error_cooldown:
            if now < self.user_error_cooldown[user_id]:
                await message.answer(t(lang, "error_cooldown", seconds=int((self.user_error_cooldown[user_id] - now).total_seconds())))
                return
        
        # Download and process file
        await self.bot.send_chat_action(chat_id=message.chat.id, action="typing")
        
        try:
            # Download file to temporary location
            file_info = await self.bot.get_file(doc.file_id)
            file_path = file_info.file_path
            
            # Create temp file
            suffix = Path(file_name).suffix
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp_file:
                tmp_path = tmp_file.name
            
            # Download
            await self.bot.download_file(file_path, tmp_path)
            
            # Extract text based on file type
            extracted_text = ""
            if file_name.lower().endswith(".txt"):
                extracted_text = await self._extract_txt(tmp_path, MAX_CHARS_TXT)
            elif file_name.lower().endswith(".docx"):
                if not DOCX_AVAILABLE:
                    await message.answer(t(lang, "docx_not_available"))
                    os.unlink(tmp_path)
                    return
                extracted_text = await self._extract_docx(tmp_path, MAX_CHARS_DOCX)
            elif file_name.lower().endswith(".pdf"):
                if not PYPDF_AVAILABLE:
                    await message.answer(t(lang, "pdf_not_available"))
                    os.unlink(tmp_path)
                    return
                extracted_text = await self._extract_pdf(tmp_path, MAX_PAGES_PDF, MAX_CHARS_TXT)

            
            # Clean up temp file
            os.unlink(tmp_path)
            
            if not extracted_text or len(extracted_text.strip()) < 10:
                await message.answer(t(lang, "document_empty"))
                return
            
            # Check if we should respond in group chat
            should_respond, _ = self._should_respond_in_group(message)
            if not should_respond and message.chat.type in ("group", "supergroup"):
                return
            
            # Store extracted text and set state for processing
            self.accumulated_text[user_id] = extracted_text
            self.user_states[user_id] = "document_wait"
            
            # Ask user what to do with the text
            preview = extracted_text[:200] + "..." if len(extracted_text) > 200 else extracted_text
            await message.answer(
                t(lang, "document_extracted", 
                  chars=len(extracted_text), 
                  preview=preview,
                  commands="/feedback /style /roast /corrector /editor /methodique /count_me /summary /praise")
            )
            
        except Exception as e:
            print(f"Document processing error: {e}")
            await message.answer(t(lang, "document_error"))

    async def _extract_txt(self, file_path: str, max_chars: int) -> str:
        """Extract text from TXT file."""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.read(max_chars)
        except UnicodeDecodeError:
            # Try with different encoding
            with open(file_path, 'r', encoding='latin-1') as f:
                return f.read(max_chars)

    async def _extract_docx(self, file_path: str, max_chars: int) -> str:
        """Extract text from DOCX file."""
        doc = Document(file_path)
        text_parts = []
        total_chars = 0
        
        for para in doc.paragraphs:
            text = para.text
            if total_chars + len(text) > max_chars:
                text_parts.append(text[:max_chars - total_chars])
                break
            text_parts.append(text)
            total_chars += len(text) + 1  # +1 for newline
        
        return "\n".join(text_parts)

    async def _extract_pdf(self, file_path: str, max_pages: int, max_chars: int) -> str:
        """Extract text from PDF file using pypdf."""
        reader = PdfReader(file_path)
        text_parts = []
        total_chars = 0
        
        for i, page in enumerate(reader.pages):
            if i >= max_pages:
                break
            text = page.extract_text() or ""
            if total_chars + len(text) > max_chars:
                text_parts.append(text[:max_chars - total_chars])
                break
            text_parts.append(text)
            total_chars += len(text)
        
        return "\n\n".join(text_parts)


    async def _handle_methodique(self, message: types.Message):
        user_id = message.from_user.id
        lang = self.user_langs.get(user_id, DEFAULT_LANG)
        state = self.user_states.get(user_id, "chat")
        
        # Check if we have document text waiting
        if state == "document_wait":
            text = self.accumulated_text.get(user_id, "")
            if len(text) < 5:
                await message.answer(t(lang, "text_too_short", min=5))
                return
            await self.bot.send_chat_action(chat_id=message.chat.id, action="typing")
            reply = self._get_writer_bot(user_id).methodique(text)
            self.user_states[user_id] = "chat"
            del self.accumulated_text[user_id]
            await message.answer(reply)
            return
        
        text = (message.text or "").strip()
        # Remove both /methodique and /methodichque
        text = text.replace("/methodique", "").replace("/methodichque", "").strip()
        
        # Check if user just sent "Методичка" or "Methodic" (trigger word for random insights)
        if text in ("Методичка", "Methodic"):
            await self.bot.send_chat_action(chat_id=message.chat.id, action="typing")
            wb = self._get_writer_bot(user_id)
            reply = wb.methodique_random()
            await message.answer(reply)
            return
        
        if len(text) < 5:
            self.user_states[user_id] = "methodique_wait"
            self.accumulated_text[user_id] = ""  # Start accumulation
            await message.answer(t(lang, "methodichque_prompt"))
            return
        await self.bot.send_chat_action(chat_id=message.chat.id, action="typing")
        reply = self._get_writer_bot(user_id).methodique(text)
        self.user_states[user_id] = "chat"
        await message.answer(reply)

    async def _handle_cite(self, message: types.Message):
        user_id = message.from_user.id
        lang = self.user_langs.get(user_id, DEFAULT_LANG)
        quote, writer = self._get_writer_bot(user_id).cite()
        await message.answer(t(lang, "cite_format", quote=quote, writer=writer))

    async def _handle_cite_off(self, message: types.Message):
        user_id = message.from_user.id
        self.user_cite_enabled[user_id] = False
        lang = self.user_langs.get(user_id, DEFAULT_LANG)
        msg = "Daily quotes disabled. Use /cite_on to enable back." if lang == "en" else "Ежедневные цитаты отключены. Используйте /cite_on, чтобы включить обратно."
        await message.answer(msg)

    async def _handle_cite_on(self, message: types.Message):
        user_id = message.from_user.id
        self.user_cite_enabled[user_id] = True
        self.user_cite_last_time[user_id] = datetime.now()
        lang = self.user_langs.get(user_id, DEFAULT_LANG)
        msg = "Daily quotes enabled." if lang == "en" else "Ежедневные цитаты включены."
        await message.answer(msg)

    async def _handle_cite_when(self, message: types.Message):
        user_id = message.from_user.id
        lang = self.user_langs.get(user_id, DEFAULT_LANG)
        
        if not self.user_cite_enabled.get(user_id, False):
            msg = "Daily quotes are disabled. Use /cite_on to enable." if lang == "en" else "Ежедневные цитаты отключены. Используйте /cite_on, чтобы включить."
            await message.answer(msg)
            return

        last_time = self.user_cite_last_time.get(user_id)
        if not last_time:
            msg = "Next quote will arrive soon." if lang == "en" else "Следующая цитата придет скоро."
            await message.answer(msg)
            return

        now = datetime.now()
        next_time = last_time + timedelta(hours=24)
        diff = next_time - now
        
        if diff.total_seconds() <= 0:
            msg = "Next quote will arrive soon." if lang == "en" else "Следующая цитата придет скоро."
        else:
            hours = int(diff.total_seconds() // 3600)
            minutes = int((diff.total_seconds() % 3600) // 60)
            if lang == "en":
                msg = f"Next quote in {hours}h {minutes}m."
            else:
                msg = f"Следующая цитата через {hours}ч {minutes}мин."
        
        await message.answer(msg)

    async def _handle_summary(self, message: types.Message):
        user_id = message.from_user.id
        lang = self.user_langs.get(user_id, DEFAULT_LANG)

        full_text = self.accumulated_text.get(user_id)

    # Если текст уже есть (из файла или накопления)
        if full_text:
            self.user_states[user_id] = "summary_wait"

            builder = ReplyKeyboardBuilder()
            builder.add(KeyboardButton(text=t(lang, "btn_summary_sentence")))
            builder.add(KeyboardButton(text=t(lang, "btn_summary_paragraph")))
            builder.add(KeyboardButton(text=t(lang, "btn_summary_two_paragraphs")))
            builder.add(KeyboardButton(text=t(lang, "btn_summary_detailed")))
            builder.adjust(2)

            await message.answer(
                t(lang, "summary_choose_format"),
                reply_markup=builder.as_markup(resize_keyboard=True)
            )
            return

    # Иначе обычный режим — ждем текст
        self.accumulated_text[user_id] = ""
        self.user_states[user_id] = "summary_wait"

        await message.answer(t(lang, "summary_prompt"))
    
    async def _handle_summary_choice(self, message: types.Message, text: str):
        user_id = message.from_user.id
        lang = self.user_langs.get(user_id, DEFAULT_LANG)

        # Поддержка RU и EN независимо от текущего lang
        format_map = {
            t("ru", "btn_summary_sentence"): "instr_summary_one_sentence",
            t("ru", "btn_summary_paragraph"): "instr_summary_one_paragraph",
            t("ru", "btn_summary_two_paragraphs"): "instr_summary_two_paragraphs",
            t("ru", "btn_summary_detailed"): "instr_summary_detailed_full",
            t("en", "btn_summary_sentence"): "instr_summary_one_sentence",
            t("en", "btn_summary_paragraph"): "instr_summary_one_paragraph",
            t("en", "btn_summary_two_paragraphs"): "instr_summary_two_paragraphs",
            t("en", "btn_summary_detailed"): "instr_summary_detailed_full",
        }


        instr_key = format_map.get(text)
        if not instr_key:
            return False

        full_text = self.accumulated_text.get(user_id, "")
        if not full_text:
            await message.answer(t(lang, "no_text_accumulated"),
                             reply_markup=get_main_keyboard(lang))
            self.user_states[user_id] = "chat"
            return True

        await self.bot.send_chat_action(chat_id=message.chat.id, action="typing")

        wb = self._get_writer_bot(user_id)
        instr = t(lang, instr_key)

        response = wb.generate_response(
            full_text,
            temporary_system_instruction=instr
        )

        # Transition to discussion mode for summary
        self.user_states[user_id] = "summary_discuss"
        # Keep accumulated_text for context in discussion
        
        await message.answer(response + f"\n\n{t(lang, 'discuss_mode_hint')}", reply_markup=get_main_keyboard(lang), parse_mode=None)

        return True



    async def _check_and_send_daily_cite(self, user_id: int, chat_id: int):
        """Check if it's time to send a daily quote and send it."""
        if not self.user_cite_enabled.get(user_id, False):
            return
        
        # Check for inactivity - auto-disable after 17 days
        now = datetime.now()
        last_activity = self.user_last_activity.get(user_id)
        if last_activity:
            days_inactive = (now - last_activity).days
            if days_inactive >= self.AUTO_DISABLE_DAYS:
                # Auto-disable due to inactivity
                self.user_cite_enabled[user_id] = False
                lang = self.user_langs.get(user_id, DEFAULT_LANG)
                msg = (
                    "Daily quotes auto-disabled due to 17+ days of inactivity. "
                    "Use /cite_on to re-enable."
                    if lang == "en" else
                    "Ежедневные цитаты автоматически отключены из-за 17+ дней неактивности. "
                    "Используйте /cite_on, чтобы включить снова."
                )
                await self.bot.send_message(chat_id=chat_id, text=msg)
                return

        now = datetime.now()
        last_time = self.user_cite_last_time.get(user_id)
        
        # If never sent or more than 24h passed
        if not last_time or (now - last_time) >= timedelta(hours=24):
            lang = self.user_langs.get(user_id, DEFAULT_LANG)
            wb = self._get_writer_bot(user_id)
            
            # Get all available quotes for this language to manage history
            # We need to peek into writer_bot's logic or just use its cite() and track history here
            # Since cite() is random, we'll try a few times to get a new one or just accept it
            
            quote, writer = wb.cite()
            history = self.user_cite_history.get(user_id, [])
            
            # Simple deduplication: if quote in history, try one more time
            if quote in history:
                quote, writer = wb.cite()
            
            # Update history
            history.append(quote)
            # If history gets too large (e.g. > 150), clear it to avoid memory issues
            # and allow repeats after a long cycle.
            if len(history) > 150: 
                history = [quote]
            
            self.user_cite_history[user_id] = history
            self.user_cite_last_time[user_id] = now
            self.user_cite_count[user_id] = self.user_cite_count.get(user_id, 0) + 1
            
            await self.bot.send_message(
                chat_id, 
                t(lang, "cite_format", quote=quote, writer=writer)
            )
            
            # Send /cite_off hint on 2nd, 7th, 12th... time
            count = self.user_cite_count[user_id]
            if count == 2 or (count > 2 and (count - 2) % 5 == 0):
                hint = (
                    "You can disable daily quotes with /cite_off" 
                    if lang == "en" else 
                    "Вы можете отключить ежедневные цитаты командой /cite_off"
                )
                await self.bot.send_message(chat_id, hint)

    async def _handle_cry_baby(self, message: types.Message):
        user_id = message.from_user.id
        lang = self.user_langs.get(user_id, DEFAULT_LANG)
        self.user_states[user_id] = "cry_baby"
        await message.answer(t(lang, "cry_baby_offer"))

    async def _handle_admin(self, message: types.Message):
        """Handle /admin <message> — mass broadcast from admin."""
        admin_id = message.from_user.id

        if admin_id != self.admin_id:
            return  # Silently ignore non-admins

        # Parse arguments
        parts = (message.text or "").split(None, 1)
        if len(parts) < 2:
            await message.answer(
                "Usage: /admin <message>\n\n"
                "Example: /admin Important update! The bot now supports voice messages.\n\n"
                "Message will be sent to all bot users."
            )
            return

        broadcast_text = parts[1].strip()

        # Check message length
        if len(broadcast_text) > 4000:
            await message.answer("Message too long (max 4000 characters)")
            return

        if len(broadcast_text) < 1:
            await message.answer("Message cannot be empty")
            return

        # Confirmation before sending
        preview = (
            f"<b>Broadcast Preview</b>\n\n"
            f"{broadcast_text[:200]}{'...' if len(broadcast_text) > 200 else ''}\n\n"
            f"Recipients: {len(self.user_langs)}\n\n"
            f"Send? Reply <b>yes</b> to confirm."
        )

        await message.answer(preview, parse_mode="HTML")

        # Wait for confirmation (simple implementation via state)
        self.user_states[admin_id] = f"admin_confirm:{broadcast_text}"
    
    async def _process_admin_broadcast(self, message: types.Message, broadcast_text: str):
        """Execute broadcast after confirmation."""
        admin_id = message.from_user.id
        
        # Reset state
        self.user_states[admin_id] = "chat"
        
        # Statistics
        sent_count = 0
        failed_count = 0
        failed_users = []
        
        # Send status message
        status_msg = await message.answer(f"Starting broadcast to {len(self.user_langs)} users...")
        
        # Broadcast to all users
        for user_id in list(self.user_langs.keys()):
            try:
                await self.bot.send_message(
                    user_id,
                    f"<b>Message from administrator</b>\n\n{broadcast_text}",
                    parse_mode="HTML"
                )
                sent_count += 1
            except Exception:
                failed_count += 1
                failed_users.append(str(user_id))
        
        # Build report
        report_lines = [
            f"<b>Broadcast Complete</b>",
            f"",
            f"Successfully sent: <b>{sent_count}</b>",
            f"Failed: <b>{failed_count}</b>",
        ]
        
        if failed_count > 0:
            report_lines.append(f"")
            report_lines.append(f"Failed to send to: {', '.join(failed_users[:10])}")
            if len(failed_users) > 10:
                report_lines.append(f"... and {len(failed_users) - 10} more")
        
        await status_msg.edit_text("\n".join(report_lines), parse_mode="HTML")

    async def _handle_dev_feedback(self, message: types.Message):
        user_id = message.from_user.id
        lang = self.user_langs.get(user_id, DEFAULT_LANG)
        text = (message.text or "").strip().replace("/dev_feedback", "").strip()
        if not text:
            self.user_states[user_id] = "dev_feedback_wait"
            self.accumulated_text[user_id] = ""  # Start accumulation
            await message.answer(t(lang, "dev_feedback_prompt"))
            return
        # Send to developer
        try:
            user_info = f"@{message.from_user.username}" if message.from_user.username else f"ID:{user_id}"
            dev_msg = f"Dev feedback from {user_info}:\n\n{text}"
            await self.bot.send_message(chat_id=DEV_ID, text=dev_msg)
            await message.answer(t(lang, "dev_feedback_thanks"))
        except Exception:
            await message.answer(t(lang, "error_llm"))
        self.user_states[user_id] = "chat"

    async def _handle_message(self, message: types.Message):
        if not (message.text or "").strip():
            return

        user_id = message.from_user.id
        chat_id = message.chat.id
        lang = self.user_langs.get(user_id, DEFAULT_LANG)
        
        # Check if user is in error cooldown
        now = datetime.now()
        if user_id in self.user_error_cooldown:
            if now < self.user_error_cooldown[user_id]:
                # Silently ignore messages during cooldown
                return
            else:
                # Cooldown expired, clear it
                del self.user_error_cooldown[user_id]
                self.user_error_count[user_id] = 0
        
        # Check if bot is waiting for user input (command continuation)
        state = self.user_states.get(user_id, "chat")
        is_waiting_input = state in self.WAITING_STATES

        # Track group chat for auto-activation
        if message.chat.type in ("group", "supergroup"):
            self._track_chat_for_auto_activation(chat_id)

        # Check if we should respond in group chat
        # If waiting for input, always respond regardless of group rules
        should_respond, text = self._should_respond_in_group(message)
        if not should_respond and not is_waiting_input:
            return

        # Skip commands (handled by command handlers)
        if text.startswith("/"):
            return

        # Check for admin confirmation (manual /admin broadcast)
        state = self.user_states.get(user_id, "chat")
        if state.startswith("admin_confirm:"):
            if text.strip().lower() in ("yes", "да", "y", "д"):
                broadcast_text = state[14:]  # remove "admin_confirm:" prefix
                await self._process_admin_broadcast(message, broadcast_text)
            else:
                self.user_states[user_id] = "chat"
                await message.answer("❌ Broadcast cancelled")
            return

        # Check for admin confirmation (auto update broadcast)
        if user_id == self.admin_id and hasattr(self, 'pending_update_changelogs') and text.strip().lower() in ("yes", "да", "y", "д"):
            await message.answer("✅ Starting update broadcast...")
            await self._process_update_broadcast()
            await message.answer("✅ Update broadcast complete!")
            return

        # Check for exact trigger word from keyboard
        if text in (
            t(lang, "button_methodique"),
            t(lang, "button_prompt"),
            t(lang, "button_idea"),
            t(lang, "button_help"),
        ):
            await self.bot.send_chat_action(chat_id=message.chat.id, action="typing")
            wb = self._get_writer_bot(user_id)
            
            if text in ("Методичка", "Methodic"):
                reply = wb.methodique_random()
            elif text in ("Команды", "Commands"):
                reply = t(lang, "help")
            elif text in ("Промпт", "Prompt"):
                await self._handle_prompt(message)
                return
            elif text in ("Сюжет", "Plot"):
                await self._handle_idea(message)
                return
            else:
                return
            
            await message.answer(reply, reply_markup=get_main_keyboard(lang))
            return

        if text:
            self.last_user_message[user_id] = text
        
        # Fallback: if bot was "away" for more than 1 hour (bot inactive, not user)
        # Check BEFORE updating the timestamp
        if self.bot_last_activity and (now - self.bot_last_activity).total_seconds() > 3600:
            await message.answer(t(lang, "bot_returned"))
        
        # Show pending changelog to all users (both on /start and in regular chat)
        if self.pending_changelog:
            await message.answer(self.pending_changelog, parse_mode="HTML")
            # Clear changelog after showing to user
            self.pending_changelog = None
        
        # Update bot's last activity timestamp
        self.bot_last_activity = now
        
        state = self.user_states.get(user_id, "chat")

# 🔹 Сначала обрабатываем выбор формата summary
        if state == "summary_wait" and text in (
            t(lang, "btn_summary_sentence"),
            t(lang, "btn_summary_paragraph"),
            t(lang, "btn_summary_two_paragraphs"),
            t(lang, "btn_summary_detailed")
        ):
            if await self._handle_summary_choice(message, text):
                return
        
        # Accumulation states - append text and wait for /done
        if state in self.STATE_CONFIG or state == "summary_wait":
            # Accumulate text
            current_acc = self.accumulated_text.get(user_id, "")
            if current_acc:
                self.accumulated_text[user_id] = current_acc + "\n\n" + text
            else:
                self.accumulated_text[user_id] = text

            # Check if minimum length reached for immediate processing (optional)
            # For now, always wait for /done
            acc_len = len(self.accumulated_text[user_id])
            
            # For summary, show buttons immediately to allow quick finish
            if state == "summary_wait":
                builder = ReplyKeyboardBuilder()
                builder.add(KeyboardButton(text=t(lang, "btn_summary_sentence")))
                builder.add(KeyboardButton(text=t(lang, "btn_summary_paragraph")))
                builder.add(KeyboardButton(text=t(lang, "btn_summary_two_paragraphs")))
                builder.add(KeyboardButton(text=t(lang, "btn_summary_detailed")))
                builder.adjust(1)
                
                await message.answer(
                    t(lang, "text_accumulated", length=acc_len) + "\n" + t(lang, "summary_choose_format"),
                    reply_markup=builder.as_markup(resize_keyboard=True)
                )
                return

            await message.answer(t(lang, "text_accumulated", length=acc_len))
            return

        if state == "cry_baby":
            self.user_states[user_id] = "chat"
            reply = self._get_writer_bot(user_id).cry_baby_reply()
            await message.answer(reply)
            return

        # Handle discussion states - user discussing tool results
        if state in self.DISCUSSION_STATES:
            await self._handle_discussion(message, text, state)
            return

        if not text:
            return
        
        # lang is already defined above, no need to redefine
        
        if user_id not in self.user_langs and len(text) >= 5:
            try:
                code, _ = detect_language(text)
                if code == "ru":
                    self.user_langs[user_id] = "ru"
                else:
                    self.user_langs[user_id] = "en"
                if user_id in self.sessions:
                    self.sessions[user_id].language = self.user_langs[user_id]
                    self.sessions[user_id].system_prompt = self.sessions[user_id]._load_system_prompt()
            except Exception:
                pass

        await self.bot.send_chat_action(chat_id=message.chat.id, action="typing")
        wb = self._get_writer_bot(user_id)
        try:
            response = wb.chat(text)
            if not response or response.startswith("Error"):
                # Track error
                self.user_error_count[user_id] = self.user_error_count.get(user_id, 0) + 1
                if self.user_error_count[user_id] >= self.max_consecutive_errors:
                    self.user_error_cooldown[user_id] = datetime.now() + timedelta(seconds=self.error_cooldown_seconds)
                    await message.answer(t(lang, "error_cooldown", seconds=self.error_cooldown_seconds))
                    return
                await message.answer(t(lang, "error_llm"))
                return
        except Exception as e:
            # Track error
            self.user_error_count[user_id] = self.user_error_count.get(user_id, 0) + 1
            if self.user_error_count[user_id] >= self.max_consecutive_errors:
                # Bot seems to be down - use longer cooldown and informative message
                self.user_error_cooldown[user_id] = datetime.now() + timedelta(seconds=300)  # 5 min cooldown
                await message.answer(t(lang, "bot_unavailable"))
                return
            await message.answer(t(lang, "error_llm"))
            return
        
        # Success - reset error count
        self.user_error_count[user_id] = 0
        
        if len(response) > 4000:
            chunks = [response[i:i+4000] for i in range(0, len(response), 4000)]
            for idx, chunk in enumerate(chunks):
                if idx == len(chunks) - 1:
                    await message.answer(chunk, reply_markup=get_main_keyboard(lang))
                else:
                    await message.answer(chunk)
        else:
            await message.answer(response, reply_markup=get_main_keyboard(lang))


    async def _handle_discussion(self, message: types.Message, text: str, state: str):
        """Handle discussion mode - user discussing tool results with context."""
        user_id = message.from_user.id
        lang = self.user_langs.get(user_id, DEFAULT_LANG)
        
        original_state, instr_key = self.DISCUSSION_STATES.get(state, (None, None))
        if not original_state:
            return
        
        # Get the original analyzed text for context
        analyzed_text = self.accumulated_text.get(user_id, "")
        
        await self.bot.send_chat_action(chat_id=message.chat.id, action="typing")
        
        try:
            wb = self._get_writer_bot(user_id)
            
            # Build discussion prompt with context
            if instr_key:
                base_instr = t(lang, instr_key)
                discussion_instr = (
                    f"{base_instr}\n\n"
                    f"Original text analyzed: {analyzed_text[:500]}...\n"
                    f"User is asking about your previous analysis. "
                    f"Answer their question while maintaining the same analytical perspective."
                )
            else:
                discussion_instr = (
                    f"Original text: {analyzed_text[:500]}...\n"
                    f"User is asking about your previous response. "
                    f"Answer their question while maintaining the same perspective."
                )
            
            response = wb.generate_response(
                f"User question: {text}",
                temporary_system_instruction=discussion_instr
            )
            
            await message.answer(response + f"\n\n{t(lang, 'discuss_mode_hint')}")
            
        except Exception as e:
            print(f"Error in discussion mode: {e}")
            await message.answer(t(lang, "error_llm"))

    async def _handle_done(self, message: types.Message):
        """Process accumulated text when user sends /done."""
        user_id = message.from_user.id
        lang = self.user_langs.get(user_id, DEFAULT_LANG)
        state = self.user_states.get(user_id, "chat")
        full_text = self.accumulated_text.get(user_id, "")
        
        # If in discussion mode, /done exits to normal chat
        if state in self.DISCUSSION_STATES:
            self.user_states[user_id] = "chat"
            if user_id in self.accumulated_text:
                del self.accumulated_text[user_id]
            await message.answer(t(lang, "discuss_exit"), reply_markup=get_main_keyboard(lang))
            return
        
        # Special handling for count_me - process even if text is short/empty
        if state == "count_me_wait":
            if not full_text:
                await message.answer(t(lang, "no_text_accumulated"))
                return
            words = count_words(full_text)
            chars = count_chars(full_text)
            add_word_count(user_id, words, chars)
            s = get_stats(user_id)
            self.user_states[user_id] = "chat"
            del self.accumulated_text[user_id]
            await message.answer(t(lang, "count_me_done", count=words, chars=chars, today=s["today"], week=s["week"], month=s["month"], total=s["total"], chars_today=s["chars_today"], chars_week=s["chars_week"], chars_month=s["chars_month"], chars_total=s["chars_total"]))
            return
        
        # Special handling for summary
        if state == "summary_wait":
            if not full_text:
                await message.answer(t(lang, "no_text_accumulated"))
                return
            
            # Show buttons for format choice
            builder = ReplyKeyboardBuilder()
            builder.add(KeyboardButton(text=t(lang, "btn_summary_sentence")))
            builder.add(KeyboardButton(text=t(lang, "btn_summary_paragraph")))
            builder.add(KeyboardButton(text=t(lang, "btn_summary_two_paragraphs")))
            builder.add(KeyboardButton(text=t(lang, "btn_summary_detailed")))
            builder.adjust(1)
            
            await message.answer(
                t(lang, "summary_choose_format"),
                reply_markup=builder.as_markup(resize_keyboard=True)
            )
            # State remains "summary_wait", but now we wait for button press
            return

        # Special handling for dev_feedback
        if state == "dev_feedback_wait":
            if not full_text:
                await message.answer(t(lang, "no_text_accumulated"))
                return
            try:
                user_info = f"@{message.from_user.username}" if message.from_user.username else f"ID:{user_id}"
                dev_msg = f"Dev feedback from {user_info}:\n\n{full_text}"
                await self.bot.send_message(chat_id=DEV_ID, text=dev_msg)
                await message.answer(t(lang, "dev_feedback_thanks"))
        except Exception:
            await message.answer(t(lang, "error_llm"))
        self.user_states[user_id] = "chat"

            del self.accumulated_text[user_id]
            return
        
        # Handle document_wait state - remind user of available commands
        if state == "document_wait":
            if not full_text:
                await message.answer(t(lang, "no_text_accumulated"))
                return
            await message.answer(
                t(lang, "document_extracted",
                  chars=len(full_text),
                  preview=full_text[:200] + "..." if len(full_text) > 200 else full_text,
                  commands="/feedback /style /roast /praise /corrector /editor /methodique /count_me")
            )
            return

        # Use unified STATE_CONFIG
        if state not in self.STATE_CONFIG:
            await message.answer(t(lang, "no_text_accumulated"))
            return

        min_len, handler_method = self.STATE_CONFIG[state]

        if not full_text or len(full_text) < min_len:
            await message.answer(t(lang, "text_too_short", min=min_len))
            return

        # Get handler from WriterBot
        wb = self._get_writer_bot(user_id)
        if handler_method is None:
            await message.answer(t(lang, "no_text_accumulated"))
            return

        handler = getattr(wb, handler_method, None)
        if handler is None:
            await message.answer(t(lang, "error_llm"))
            return

        # Process with LLM
        await self.bot.send_chat_action(chat_id=message.chat.id, action="typing")
        try:
            reply = handler(full_text)
            # Transition to discussion mode for sticky tools
            discuss_state = f"{state}_discuss"
            if discuss_state in self.DISCUSSION_STATES:
                self.user_states[user_id] = discuss_state
                # Keep accumulated_text for context in discussion
                self.accumulated_text[user_id] = full_text
                await message.answer(reply + f"\n\n{t(lang, 'discuss_mode_hint')}")
            else:
                self.user_states[user_id] = "chat"
                await message.answer("Broadcast cancelled")
            return


    bot = TelegramWriterBot(
        telegram_token=token,
        llm_model=args.model,
        llm_api_key=os.getenv("OPENAI_API_KEY"),
        llm_api_base=os.getenv("OPENAI_API_BASE"),
        use_rag=not args.no_rag,
    )
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
