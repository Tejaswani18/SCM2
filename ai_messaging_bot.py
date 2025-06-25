import sqlite3
import spacy
import difflib
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
import logging
from collections import defaultdict
import re
from datetime import datetime  # NEW: For parsing reminder times
import asyncio  # NEW: For scheduling reminders

# Set up logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Load spaCy model for NLP
nlp = spacy.load("en_core_web_sm")

# Initialize SQLite database
def init_db():
    conn = sqlite3.connect("group_knowledge.db")
    c = conn.cursor()
    c.execute(
        """CREATE TABLE IF NOT EXISTS knowledge
                 (group_id TEXT, question TEXT, answer TEXT, frequency INTEGER)"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS important_messages
                 (group_id TEXT, message_id INTEGER, content TEXT)"""
    )
    # NEW: Create reminders table
    c.execute(
        """CREATE TABLE IF NOT EXISTS reminders
                 (group_id TEXT, message_id INTEGER, content TEXT, remind_time TIMESTAMP)"""
    )
    conn.commit()
    conn.close()

# Keyword patterns for detecting important messages
IMPORTANT_KEYWORDS = [
    r"\b(announcement|event|reminder|urgent|critical|deadline)\b",
    r"\b(date|time|location|schedule)\b",
]

class AIMessagingBot:
    def __init__(self):
        self.group_context = defaultdict(list)
        init_db()

    def detect_relevance(self, text):
        """Detect if a message is important based on keywords and context."""
        doc = nlp(text.lower())
        for pattern in IMPORTANT_KEYWORDS:
            if re.search(pattern, text.lower()):
                return True
        for ent in doc.ents:
            if ent.label_ in ["DATE", "TIME", "EVENT"]:
                return True
        return False

    def store_important_message(self, group_id, message_id, content):
        """Store important messages in the database."""
        conn = sqlite3.connect("group_knowledge.db")
        c = conn.cursor()
        c.execute(
            "INSERT INTO important_messages (group_id, message_id, content) VALUES (?, ?, ?)",
            (group_id, message_id, content),
        )
        conn.commit()
        conn.close()

    def get_faq_answer(self, group_id, question):
        logger.info(f"Querying FAQ for group_id: {group_id}, question: {question}")
        conn = sqlite3.connect("group_knowledge.db")
        c = conn.cursor()
        c.execute(
            "SELECT answer, frequency FROM knowledge WHERE group_id = ? AND LOWER(question) = ?",
            (group_id, question.lower()),
        )
        result = c.fetchone()
        if result:
            answer, freq = result
            logger.info(f"Found answer: {answer}, frequency: {freq}")
            c.execute(
                "UPDATE knowledge SET frequency = ? WHERE group_id = ? AND question = ?",
                (freq + 1, group_id, result[0]),  # Use original question for update
            )
            conn.commit()
            conn.close()
            return answer
        logger.info("No answer found")
        conn.close()
        return None

    def store_faq(self, group_id, question, answer):
        """Store new FAQ in knowledge base."""
        conn = sqlite3.connect("group_knowledge.db")
        c = conn.cursor()
        c.execute(
            "INSERT INTO knowledge (group_id, question, answer, frequency) VALUES (?, ?, ?, ?)",
            (group_id, question, answer, 1),
        )
        conn.commit()
        conn.close()

    def extract_question(self, text):
        """Extract potential questions from message using NLP."""
        doc = nlp(text)
        for sent in doc.sents:
            if "?" in sent.text or any(token.lemma_ in ["what", "how", "when", "where", "why"] for token in sent):
                return sent.text.strip()
        return None

    # NEW: Store reminder in database
    def store_reminder(self, group_id, message_id, content, remind_time):
        """Store a reminder in the database."""
        try:
            conn = sqlite3.connect("group_knowledge.db")
            c = conn.cursor()
            c.execute(
                "INSERT INTO reminders (group_id, message_id, content, remind_time) VALUES (?, ?, ?, ?)",
                (group_id, message_id, content, remind_time),
            )
            conn.commit()
            logger.info(f"Stored reminder: {content} at {remind_time}")
        except sqlite3.Error as e:
            logger.error(f"SQLite error in store_reminder: {e}")
        finally:
            conn.close()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    await update.message.reply_text(
        "Hi! I'm an AI messaging assistant. I filter important messages, answer FAQs, and provide recommendations."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming messages."""
    bot = context.bot_data.setdefault("bot", AIMessagingBot())
    group_id = str(update.message.chat_id)
    text = update.message.text
    message_id = update.message.message_id

    if bot.detect_relevance(text):
        bot.store_important_message(group_id, message_id, text)
        await update.message.reply_text(f"📢 [Important] {text}")

    question = bot.extract_question(text)
    if question:
        answer = bot.get_faq_answer(group_id, question)
        if answer:
            await update.message.reply_text(f"🤖 Auto-Answer: {answer}")
        else:
            recommendation = f"Could you clarify or provide more details about '{question}'?"
            await update.message.reply_text(recommendation)
            bot.store_faq(group_id, question, "Pending admin response")

    bot.group_context[group_id].append(text)
    if len(bot.group_context[group_id]) > 100:
        bot.group_context[group_id].pop(0)

async def add_faq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /addfaq command to manually add FAQ."""
    bot = context.bot_data.setdefault("bot", AIMessagingBot())
    group_id = str(update.message.chat_id)
    try:
        question, answer = " ".join(context.args).split("|")
        bot.store_faq(group_id, question.strip(), answer.strip())
        await update.message.reply_text(f"FAQ added: {question} -> {answer}")
    except ValueError:
        await update.message.reply_text("Usage: /addfaq question | answer")

# NEW: Handle /setreminder command
async def set_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /setreminder command to schedule a reminder."""
    bot = context.bot_data.setdefault("bot", AIMessagingBot())
    group_id = str(update.message.chat_id)
    message_id = update.message.message_id
    try:
        args = " ".join(context.args).split("|")
        if len(args) != 2:
            await update.message.reply_text("Usage: /setreminder message | YYYY-MM-DD HH:MM")
            return
        content, time_str = [arg.strip() for arg in args]
        remind_time = datetime.strptime(time_str, "%Y-%m-%d %H:%M")
        if remind_time < datetime.now():
            await update.message.reply_text("Reminder time must be in the future.")
            return
        bot.store_reminder(group_id, message_id, content, remind_time)
        await update.message.reply_text(f"Reminder set for '{content}' at {time_str}")
        # Schedule the reminder
        delay = (remind_time - datetime.now()).total_seconds()
        asyncio.create_task(schedule_reminder(update, content, delay))
    except ValueError as e:
        logger.error(f"ValueError in set_reminder: {e}")
        await update.message.reply_text("Invalid format. Use: /setreminder message | YYYY-MM-DD HH:MM")
    except Exception as e:
        logger.error(f"Error in set_reminder: {e}")
        await update.message.reply_text("Failed to set reminder.")

# NEW: Schedule reminder delivery
async def schedule_reminder(update: Update, content: str, delay: float):
    """Send reminder after specified delay."""
    await asyncio.sleep(delay)
    await update.message.reply_text(f"⏰ Reminder: {content}")

def main():
    """Run the bot."""
    application = Application.builder().token("8181677837:AAF79z_HJN8xGH63CPL47-WTmUow_UfbNAo").build()

    application.bot_data["bot"] = AIMessagingBot()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("addfaq", add_faq))
    # NEW: Register set_reminder handler
    application.add_handler(CommandHandler("setreminder", set_reminder))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    application.run_polling()

if __name__ == "__main__":
    main()