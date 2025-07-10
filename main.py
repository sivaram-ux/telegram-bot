import os
import logging
from io import StringIO

from telegram import Update, InputFile
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters
)

from prompt_engine import (
    optimize_prompt, explain_prompt, deep_research_questions,
    log_prompt_to_supabase, save_deep_research_questions_separately,
    save_explanation_separately, extract_json_from_response
)

from keys import TELEGRAM_BOT_TOKEN

ASK_PROMPT, ASK_MODE, ASK_FOLLOWUP, ASK_EXPLAIN = range(4)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

    strengths = data.get("original_prompt", {}).get("strengths", [])
    if strengths:
        messages.append("👍 *Original Prompt Strengths*\n" + "\n".join(f"• {s}" for s in strengths))

    weaknesses = data.get("original_prompt", {}).get("weaknesses", [])
    if weaknesses:
        messages.append("👎 *Original Prompt Weaknesses*\n" + "\n".join(f"• {w}" for w in weaknesses))

    improvements = data.get("llm_understanding_improvements", [])
    if improvements:
        messages.append("🧠 *What the LLM Understands Better Now*\n" + "\n".join(f"• {u}" for u in improvements))

    tips = data.get("tips_for_future_prompts", [])
    if tips:
        messages.append("💡 *Tips for Future Prompts*\n" + "\n".join(f"• {t}" for t in tips))

    return messages

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
    context.user_data["prompt_id"] = "telegram-user"  # default

    if os.environ.get("SUPABASE_KEY") and os.environ.get("SUPABASE_URL"):
        prompt_id = log_prompt_to_supabase(
            original_prompt=prompt,
            optimized_prompt=optimized,
            mode=mode,
            model_used="gemini-2.5-flash"
        )
        context.user_data["prompt_id"] = prompt_id  # store real ID

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

def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    conv = ConversationHandler(
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

    app.add_handler(conv)
    app.run_polling()

if __name__ == "__main__":
    main()
