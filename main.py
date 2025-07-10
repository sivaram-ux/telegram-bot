import os
import logging
from io import StringIO
from fastapi import FastAPI, Request
from telegram import Update, InputFile
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler,
    ConversationHandler, filters
)
from telegram.ext import AIORateLimiter

from prompt_engine import (
    optimize_prompt, explain_prompt, deep_research_questions,
    log_prompt_to_supabase, save_deep_research_questions_separately,
    save_explanation_separately, extract_json_from_response
)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
WEBHOOK_SECRET = TELEGRAM_BOT_TOKEN  # simple: use bot token as secret path
BASE_URL = os.environ["RENDER_EXTERNAL_URL"]  # Render provides this automatically

ASK_PROMPT, ASK_MODE, ASK_FOLLOWUP, ASK_EXPLAIN = range(4)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===========================
# BOT HANDLERS (same as before)
# ===========================

def get_send_strategy(response_text: str, filename: str = "response.txt"):
    MAX_LENGTH = 4000
    if len(response_text) <= MAX_LENGTH:
        return "text", response_text
    elif len(response_text) <= MAX_LENGTH * 5:
        chunks = [response_text[i:i + MAX_LENGTH] for i in range(0, len(response_text), MAX_LENGTH)]
        return "chunks", chunks
    else:
        buffer = StringIO(response_text)
        return "file", InputFile(buffer, filename)

def format_explanation_to_messages(data: dict) -> list[str]:
    messages = ["🧠 *Prompt Feedback Analysis*"]
    if (s := data.get("original_prompt", {}).get("strengths", [])):
        messages.append("👍 *Original Prompt Strengths*\n" + "\n".join(f"• {x}" for x in s))
    if (w := data.get("original_prompt", {}).get("weaknesses", [])):
        messages.append("👎 *Weaknesses*\n" + "\n".join(f"• {x}" for x in w))
    if (u := data.get("llm_understanding_improvements", [])):
        messages.append("🧠 *LLM Understands Better*\n" + "\n".join(f"• {x}" for x in u))
    if (t := data.get("tips_for_future_prompts", [])):
        messages.append("💡 *Tips*\n" + "\n".join(f"• {x}" for x in t))
    return messages

# async conversation handlers here (same as your code)...

# e.g. start, handle_prompt, handle_mode, handle_followup, collect_questions, collect_answers, handle_explain, cancel
# OMITTED for brevity — reuse exactly as you sent

# ===========================
# FASTAPI SERVER + TELEGRAM APP
# ===========================

app = FastAPI()
telegram_app = (
    ApplicationBuilder()
    .token(TELEGRAM_BOT_TOKEN)
    .rate_limiter(AIORateLimiter())
    .build()
)

# Add all your conversation handlers
conv_handler = ConversationHandler(
    entry_points=[CommandHandler("start", start)],
    states={
        ASK_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_prompt)],
        ASK_MODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_mode)],
        ASK_FOLLOWUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_followup)],
        ASK_FOLLOWUP + 10: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_questions)],
        ASK_FOLLOWUP + 11: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_answers)],
        ASK_EXPLAIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_explain)],
    },
    fallbacks=[CommandHandler("cancel", cancel)]
)

telegram_app.add_handler(conv_handler)

@app.get("/")
async def root():
    return {"status": "ok", "message": "Bot is up"}

@app.post(f"/webhook/{WEBHOOK_SECRET}")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.update_queue.put(update)
    return {"ok": True}

@app.on_event("startup")
async def setup_webhook():
    await telegram_app.bot.set_webhook(url=f"{BASE_URL}/webhook/{WEBHOOK_SECRET}")
