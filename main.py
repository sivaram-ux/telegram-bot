import os
import logging
from io import StringIO
from fastapi import FastAPI, Request
from telegram import Update, InputFile
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters, AIORateLimiter
)
from prompt_engine import (
    optimize_prompt, explain_prompt, deep_research_questions,
    log_prompt_to_supabase, save_deep_research_questions_separately,
    save_explanation_separately, extract_json_from_response
)

# ENVIRONMENT CONFIG
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
BASE_URL = os.environ["RENDER_EXTERNAL_URL"]  # Render auto provides this
WEBHOOK_SECRET = TELEGRAM_BOT_TOKEN  # secret path for webhook

# CONVERSATION STATES
ASK_PROMPT, ASK_MODE, ASK_FOLLOWUP, ASK_EXPLAIN = range(4)

# LOGGING
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== UTILITIES ==========

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

# ========== BOT HANDLERS ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Welcome! Please send your raw prompt.")
    return ASK_PROMPT

async def handle_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["prompt"] = update.message.text
    await update.message.reply_text("🔧 Enter the mode (e.g., clarity, deep_research, creative, etc):")
    return ASK_MODE

async def handle_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = update.message.text
    prompt = context.user_data["prompt"]
    mode = context.user_data["mode"]

    await update.message.reply_text("⚙️ Optimizing your prompt...")

    optimized = ""
    for chunk in optimize_prompt(prompt, mode):
        optimized += chunk.content

    context.user_data["optimized"] = optimized
    context.user_data["prompt_id"] = "telegram-user"

    if os.environ.get("SUPABASE_KEY") and os.environ.get("SUPABASE_URL"):
        prompt_id = log_prompt_to_supabase(
            original_prompt=prompt,
            optimized_prompt=optimized,
            mode=mode,
            model_used="gemini-2.5-flash"
        )
        context.user_data["prompt_id"] = prompt_id

    strategy, output = get_send_strategy(optimized)
    if strategy == "text":
        await update.message.reply_text(output)
    elif strategy == "chunks":
        for part in output:
            await update.message.reply_text(part)
    else:
        await update.message.reply_document(output)

    if mode == "deep_research":
        await update.message.reply_text("🤔 Want to answer follow-up questions? (yes/no)")
        return ASK_FOLLOWUP
    else:
        await update.message.reply_text("📘 Want explanation of the optimization? (yes/no)")
        return ASK_EXPLAIN

async def handle_followup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.lower().startswith("y"):
        await update.message.reply_text("✍️ Please enter the questions asked by the model:")
        context.user_data["wants_followup"] = True
        return ASK_FOLLOWUP + 10
    else:
        await update.message.reply_text("📘 Want explanation of the optimization? (yes/no)")
        return ASK_EXPLAIN

async def collect_questions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["questions_asked"] = update.message.text
    await update.message.reply_text("💬 Any preferences/answers to the questions? (or type 'no')")
    return ASK_FOLLOWUP + 11

async def collect_answers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    preferences = update.message.text
    if preferences.lower() == "no":
        preferences = ""
    context.user_data["preferences"] = preferences

    questions = context.user_data["questions_asked"]
    answers = context.user_data["optimized"]

    response = ""
    for chunk in deep_research_questions(questions, answers, preferences):
        response += chunk.content

    save_deep_research_questions_separately(
        prompt_id=context.user_data.get("prompt_id", "telegram-user"),
        questions_asked=questions,
        answers=response,
        preferences=preferences
    )

    strategy, output = get_send_strategy(response)
    if strategy == "text":
        await update.message.reply_text(output)
    elif strategy == "chunks":
        for part in output:
            await update.message.reply_text(part)
    else:
        await update.message.reply_document(output)

    await update.message.reply_text("📘 Want explanation of the optimization? (yes/no)")
    return ASK_EXPLAIN

async def handle_explain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.lower().startswith("y"):
        prompt = context.user_data["prompt"]
        optimized = context.user_data["optimized"]
        mode = context.user_data["mode"]

        explanation = ""
        for chunk in explain_prompt(prompt, optimized, mode):
            explanation += chunk.content

        parsed = extract_json_from_response(explanation)
        if parsed:
            save_explanation_separately(
                context.user_data.get("prompt_id", "telegram-user"),
                parsed
            )
            messages = format_explanation_to_messages(parsed)
            for msg in messages:
                await update.message.reply_text(msg, parse_mode="Markdown")
        else:
            await update.message.reply_text(explanation)
    else:
        await update.message.reply_text("✅ Done. You can send another prompt with /start.")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Canceled.")
    return ConversationHandler.END

# ========== FASTAPI SERVER ==========

from contextlib import asynccontextmanager
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 🚀 Set Telegram Webhook on startup
    await telegram_app.initialize()
    await telegram_app.bot.set_webhook(f"{BASE_URL}/webhook/{WEBHOOK_SECRET}")
    yield
    # (Optional) Cleanup logic on shutdown goes here

app = FastAPI(lifespan=lifespan)

telegram_app = (
    ApplicationBuilder()
    .token(TELEGRAM_BOT_TOKEN)
    .rate_limiter(AIORateLimiter())
    .build()
)

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
async def health_check():
    return {"status": "ok", "message": "Bot is running"}

@app.post(f"/webhook/{WEBHOOK_SECRET}")
async def telegram_webhook(request: Request):
    data = await request.json()
    logger.info("📩 Webhook received: %s", data)
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.update_queue.put(update)
    return {"ok": True}

