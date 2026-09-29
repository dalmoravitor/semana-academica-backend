from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import json
import os
import random
import sqlite3
from pathlib import Path
from typing import List, Optional

app = FastAPI(title="AI Tinder API")

cors_origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:5173,http://localhost:5174",
    ).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).parent
CARDS_PATH = BASE_DIR / "cards.json"
DB_PATH = BASE_DIR / "leaderboard.db"
MEDIA_DIR = BASE_DIR / "media"

app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")

# Inicializa o banco de dados do Leaderboard
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS leaderboard (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                participant_id TEXT NOT NULL DEFAULT '',
                score INTEGER NOT NULL,
                streak INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        columns = {row[1] for row in cursor.execute("PRAGMA table_info(leaderboard)")}
        if "participant_id" not in columns:
            cursor.execute("ALTER TABLE leaderboard ADD COLUMN participant_id TEXT NOT NULL DEFAULT ''")
        conn.commit()

init_db()

def load_cards() -> list:
    if not CARDS_PATH.exists():
        return []
    try:
        with open(CARDS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Erro ao ler cards.json: {e}")
        return []

@app.get("/api/cards/next")
def get_next_card(exclude: Optional[str] = Query(None)):
    """Retorna um card aleatório, evitando os já jogados na sessão atual."""
    cards = load_cards()
    if not cards:
        raise HTTPException(status_code=404, detail="Nenhum card cadastrado.")

    excluded_ids = set(exclude.split(",")) if exclude else set()
    available_cards = [c for c in cards if str(c.get("id")) not in excluded_ids]

    # Se todos já foram usados, sorteia entre todos
    selected_card = random.choice(available_cards if available_cards else cards)

    image_url = selected_card.get("imageUrl") or selected_card.get("image_url", "")
    if image_url.startswith("http://localhost:8000/"):
        image_url = image_url.removeprefix("http://localhost:8000")

    return {
        "id": str(selected_card.get("id")),
        "imageUrl": image_url,
        "category": selected_card.get("category", "wildlife"),
        "prompt": selected_card.get("prompt", "Prompt indisponível.")
    }

class GuessBody(BaseModel):
    choice: str  # "real" ou "ai"

@app.post("/api/cards/{card_id}/guess")
def submit_guess(card_id: str, body: GuessBody):
    cards = load_cards()
    card = next((c for c in cards if str(c.get("id")) == str(card_id)), None)
    if not card:
        raise HTTPException(status_code=404, detail="Card não encontrado")

    is_ai = card.get("isAi", card.get("is_ai", False))
    is_correct = (body.choice == "ai" and is_ai) or (body.choice == "real" and not is_ai)

    return {
        "correct": is_correct,
        "is_ai": is_ai,
        "subject": card.get("subject", "Sem título"),
        "explanation": card.get("explanation", "Sem descrição.")
    }

class LeaderboardEntry(BaseModel):
    name: str = Field(..., min_length=2, max_length=20)
    participant_id: str = Field(..., min_length=2, max_length=30)
    score: int = Field(..., ge=0)
    streak: int = Field(..., ge=0)
    first_time: bool = False

def get_unique_display_name(cursor: sqlite3.Cursor, base_name: str) -> str:
    candidate = base_name
    suffix = 2
    while cursor.execute(
        "SELECT 1 FROM leaderboard WHERE LOWER(name) = LOWER(?) LIMIT 1",
        (candidate,),
    ).fetchone():
        candidate = f"{base_name} ({suffix})"
        suffix += 1
    return candidate

@app.get("/api/leaderboard")
def get_leaderboard():
    """Retorna o Top 10 do ranking ordenado por pontuação e streak."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT name, participant_id, score, streak
            FROM leaderboard AS current
            WHERE participant_id = ''
               OR id = (
                    SELECT candidate.id
                    FROM leaderboard AS candidate
                    WHERE candidate.participant_id = current.participant_id
                    ORDER BY candidate.score DESC, candidate.streak DESC, candidate.id ASC
                    LIMIT 1
               )
            ORDER BY score DESC, streak DESC, id ASC
            """
        )
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

@app.post("/api/leaderboard")
def save_score(entry: LeaderboardEntry):
    """Cria uma entrada nova ou atualiza a pontuação do participante."""
    clean_name = entry.name.strip()
    clean_participant_id = entry.participant_id.strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="Nome inválido.")
    if not clean_participant_id:
        raise HTTPException(status_code=400, detail="Identificador do participante inválido.")

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        existing = cursor.execute(
            "SELECT name FROM leaderboard WHERE participant_id = ? ORDER BY id ASC LIMIT 1",
            (clean_participant_id,),
        ).fetchone()

        if entry.first_time or not existing:
            display_name = get_unique_display_name(cursor, clean_name)
            cursor.execute(
                "INSERT INTO leaderboard (name, participant_id, score, streak) VALUES (?, ?, ?, ?)",
                (display_name, clean_participant_id, entry.score, entry.streak),
            )
        else:
            display_name = existing[0]
            cursor.execute(
                "UPDATE leaderboard SET score = ?, streak = ? WHERE participant_id = ?",
                (entry.score, entry.streak, clean_participant_id),
            )
        conn.commit()

    return {
        "status": "success",
        "message": "Pontuação salva com sucesso!",
        "name": display_name,
        "participant_id": clean_participant_id,
    }