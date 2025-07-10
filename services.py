from fastapi import FastAPI
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import os
from prompt_engine import (
    optimize_prompt,
    explain_prompt,
    log_prompt_to_supabase,
    deep_research_questions,
    save_deep_research_questions_separately,
    save_explanation_separately,
    extract_json_from_response
)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# === Models ===

class OptimizeRequest(BaseModel):
    prompt: str
    mode: str

class ExplainRequest(BaseModel):
    original_prompt: str
    optimized_prompt: str
    mode: str

class ResearchFollowupRequest(BaseModel):
    prompt_id: str
    questions_asked: str
    answers: str
    preferences: str = None

class FeedbackLogRequest(BaseModel):
    prompt_id: str
    explanation_json: dict

async def optimize_endpoint(prompt: str,mode: str):
    optimized = ""
    for chunk in optimize_prompt(prompt, mode):
        optimized += chunk.content

    if os.environ.get("SUPABASE_KEY") and os.environ.get("SUPABASE_URL"):
        id = log_prompt_to_supabase(
            original_prompt=prompt,
            optimized_prompt=optimized,
            mode=mode,
            model_used="gemini-2.5-flash"
        )

    return {"id":id,"optimized_prompt": optimized}

async def explain_endpoint(original_prompt: str,optimized_prompt: str,mode: str):
    explanation = ""
    for chunk in explain_prompt(original_prompt, optimized_prompt, mode):
        explanation += chunk.content

    if os.environ.get("SUPABASE_KEY") and os.environ.get("SUPABASE_URL"):
        parsed = extract_json_from_response(explanation)
        if parsed:
            save_explanation_separately(
                prompt_id="external-user",  # replace with real ID if tracked
                explanation_dict=parsed
            )

    return {"explanation": explanation}

async def followup_endpoint(prompt_id: str,questions_asked: str,answers: str,preferences: str = None):
    response = ""
    for chunk in deep_research_questions(questions_asked, answers, preferences or ""):
        response += chunk.content

    if prompt_id:
        save_deep_research_questions_separately(
            prompt_id=prompt_id,
            questions_asked=questions_asked,
            answers=response,
            preferences=preferences
        )

    return {"followup_response": response}


async def log_feedback_endpoint(prompt_id: str,explanation_json: dict):
    save_explanation_separately(prompt_id, explanation_json)
    return {"status": "success"}
