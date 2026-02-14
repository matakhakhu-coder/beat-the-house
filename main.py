import sqlite3
import random
import time
import os
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List, Tuple, Dict, Any

app = FastAPI(title="Beat the House: Season 1")

# --- CONFIGURATION ---
DB_NAME = "game.db"
SEED_VAULT_AMOUNT = 1000
DEV_TAX = 0.10
VAULT_SPLIT = 0.90
PAYOUT_RATIO = 0.05
COST_PER_PLAY = 10
GRAND_SOLVE_ANSWER = "timestamp % 10 == 7 AND volume >= 3"

# --- FRONTEND SETUP ---
# 1. Create static directory if it doesn't exist
if not os.path.exists("static"):
    os.makedirs("static")

# 2. Mount the static folder to serve CSS/JS/Images
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- DATABASE INITIALIZATION ---
def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        c = conn.cursor()
        
        # 1. The Vault (Holds the money)
        c.execute('''CREATE TABLE IF NOT EXISTS vault (id INTEGER PRIMARY KEY, balance INTEGER)''')
        # Seed the vault if empty
        c.execute('SELECT count(*) FROM vault')
        if c.fetchone()[0] == 0:
            c.execute('INSERT INTO vault (id, balance) VALUES (1, ?)', (SEED_VAULT_AMOUNT,))
            
        # 2. Players (Tracks individual performance)
        c.execute('''CREATE TABLE IF NOT EXISTS players 
                     (user_id TEXT PRIMARY KEY, total_spent INTEGER, total_won INTEGER)''')
        
        # 3. Transactions (The Ledger)
        c.execute('''CREATE TABLE IF NOT EXISTS transactions 
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, 
                      input_amt INTEGER, output_amt INTEGER, vault_balance INTEGER, timestamp REAL)''')
        
        # 4. Player Wins (Tracks difficulty level per player)
        c.execute('''CREATE TABLE IF NOT EXISTS player_wins 
                     (user_id TEXT PRIMARY KEY, l1_wins INTEGER DEFAULT 0)''')
        
        conn.commit()

# Run DB init on startup
init_db()

# --- DATA MODELS ---
class PlayRequest(BaseModel):
    user_id: str

class PlayResponse(BaseModel):
    user_id: str
    outcome: str
    payout: int
    vault_balance: int
    message: str

class LeaderboardEntry(BaseModel):
    user_id: str
    roi_percent: float
    net_profit: int

class SubmitRequest(BaseModel):
    user_id: str
    formula: str

class SubmitResponse(BaseModel):
    user_id: str
    outcome: str
    payout: int
    message: str
    next_step: str = ""

# --- CORE HELPERS ---

def get_vault_balance():
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute('SELECT balance FROM vault WHERE id=1').fetchone()[0]

def update_vault(amount_change):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute('UPDATE vault SET balance = balance + ? WHERE id=1', (amount_change,))
        conn.commit()
        return conn.execute('SELECT balance FROM vault WHERE id=1').fetchone()[0]

def log_transaction(user_id, input_amt, output_amt, vault_bal):
    with sqlite3.connect(DB_NAME) as conn:
        # Log the raw transaction
        conn.execute('''INSERT INTO transactions (user_id, input_amt, output_amt, vault_balance, timestamp) 
                        VALUES (?, ?, ?, ?, ?)''', (user_id, input_amt, output_amt, vault_bal, time.time()))
        
        # Update player stats
        conn.execute('''INSERT INTO players (user_id, total_spent, total_won) VALUES (?, ?, ?)
                        ON CONFLICT(user_id) DO UPDATE SET 
                        total_spent = total_spent + ?,
                        total_won = total_won + ?''', 
                        (user_id, input_amt, output_amt, input_amt, output_amt))
        conn.commit()

# --- GAME LOGIC (THE ALGORITHM) ---

def check_win_condition(user_id: str) -> Tuple[bool, str]:
    with sqlite3.connect(DB_NAME) as conn:
        # 1. Get Player's "Security Clearance" (Win Count)
        row = conn.execute('SELECT l1_wins FROM player_wins WHERE user_id=?', (user_id,)).fetchone()
        l1_wins = row[0] if row else 0
        
        # 2. Check The Universal Constant (Time must end in 7)
        current_time = int(time.time())
        time_aligned = str(current_time)[-1] == '7'
        
        # Immediate Fail if time is wrong
        if not time_aligned:
            return False, "Signal Missed. Align with the clock."

        # --- DIFFICULTY BRANCHING ---
        
        # LAYER 1: Simple Time Sync (Easy Mode)
        # Active for the first 3 wins
        if l1_wins < 3:
            # Increment win count
            conn.execute('''INSERT INTO player_wins (user_id, l1_wins) VALUES (?, 1) 
                            ON CONFLICT(user_id) DO UPDATE SET l1_wins = l1_wins + 1''', (user_id,))
            conn.commit()
            return True, f"LAYER 1 BREACH CONFIRMED. (Wins: {l1_wins+1}/3)"

        # LAYER 2: High Velocity Injection (Hard Mode)
        # Active after 3 wins. Requires Time Sync + Volume Spike.
        
        # Calculate Volume (Plays in last 10 seconds)
        volume = conn.execute('SELECT COUNT(*) FROM transactions WHERE timestamp > ?', 
                              (time.time() - 10,)).fetchone()[0]
        threshold = 3 
        
        if volume >= threshold:
            return True, f"LAYER 2 BREACH: Volume Surge ({volume}) + Time Sync Verified."
        
        # Fail Message for Layer 2
        return False, f"Time aligned, but Network Entropy too low ({volume}/{threshold}). Flood the system."

# --- API ENDPOINTS ---

@app.get("/")
async def read_root():
    # Serves the Hacker Terminal Interface
    return FileResponse('static/index.html')

@app.post("/play", response_model=PlayResponse)
def play_game(request: PlayRequest):
    # 1. Process Input (The Buy-In)
    vault_share = int(COST_PER_PLAY * VAULT_SPLIT)
    current_vault = update_vault(vault_share)
    
    # 2. Check Algorithm
    is_win, debug_msg = check_win_condition(request.user_id)
    
    payout = 0
    outcome = "LOSS"
    message = debug_msg 
    
    # 3. Process Payout (if Win)
    if is_win:
        payout = int(current_vault * PAYOUT_RATIO)
        current_vault = update_vault(-payout)
        outcome = "WIN"
        message = debug_msg

    # 4. Log Everything
    log_transaction(request.user_id, COST_PER_PLAY, payout, current_vault)
    
    return {
        "user_id": request.user_id,
        "outcome": outcome,
        "payout": payout,
        "vault_balance": current_vault,
        "message": message
    }

@app.post("/submit", response_model=SubmitResponse)
def submit_grand_solve(request: SubmitRequest):
    # Normalize input (remove spaces, lowercase)
    submission = " ".join(request.formula.split()).lower()
    target = " ".join(GRAND_SOLVE_ANSWER.split()).lower()
    
    with sqlite3.connect(DB_NAME) as conn:
        vault_bal = conn.execute('SELECT balance FROM vault WHERE id=1').fetchone()[0]
        
        # Check if season is already over
        if vault_bal <= 0:
            return {
                "user_id": request.user_id, 
                "outcome": "ERROR", 
                "payout": 0,
                "message": "Season already ended. Wait for reset."
            }

        # Check Answer
        if submission == target:
            # GRAND PRIZE: 60% of Vault
            grand_prize = int(vault_bal * 0.60)
            
            # Deduct from vault
            conn.execute('UPDATE vault SET balance = balance - ? WHERE id=1', (grand_prize,))
            conn.commit() 
            
            # Log Victory
            log_transaction(request.user_id, 0, grand_prize, vault_bal - grand_prize)
            
            return {
                "user_id": request.user_id,
                "outcome": "GRAND_SOLVE",
                "payout": grand_prize,
                "message": "CORRECT. SYSTEM COMPROMISED. SEASON ENDED.",
                "next_step": "Source code released to public ledger. Season 2 generating..."
            }
        
        else:
            return {
                "user_id": request.user_id,
                "outcome": "REJECTED",
                "payout": 0,
                "message": "Incorrect formula. The House remains secure."
            }

@app.get("/leaderboard", response_model=List[LeaderboardEntry])
def get_leaderboard():
    with sqlite3.connect(DB_NAME) as conn:
        # Calculate ROC: (Won - Spent) / Spent
        rows = conn.execute('''
            SELECT user_id, total_spent, total_won 
            FROM players 
            WHERE total_spent > 0
            ORDER BY ((CAST(total_won AS FLOAT) - total_spent) / total_spent) DESC
            LIMIT 5
        ''').fetchall()
        
    leaderboard = []
    for r in rows:
        user_id, spent, won = r
        net_profit = won - spent
        roi = (net_profit / spent) * 100 if spent > 0 else 0
        leaderboard.append({
            "user_id": user_id,
            "roi_percent": round(roi, 2),
            "net_profit": net_profit
        })
        
    return leaderboard

@app.get("/history")
def get_public_history():
    with sqlite3.connect(DB_NAME) as conn:
        # Return last 50 transactions for the transparency ledger
        rows = conn.execute('SELECT * FROM transactions ORDER BY id DESC LIMIT 50').fetchall()
    return {"history": rows}

@app.get("/analytics")
def get_analytics():
    with sqlite3.connect(DB_NAME) as conn:
        # 1. Activity
        one_hour_ago = time.time() - 3600
        total_plays_1h = conn.execute('SELECT COUNT(*) FROM transactions WHERE timestamp > ?', (one_hour_ago,)).fetchone()[0]
        total_wins = conn.execute("SELECT COUNT(*) FROM transactions WHERE output_amt > 0").fetchone()[0]
        
        # 2. Player Distribution (L1 vs L2)
        l1_players = conn.execute("SELECT COUNT(*) FROM player_wins WHERE l1_wins < 3 AND l1_wins > 0").fetchone()[0]
        l2_players = conn.execute("SELECT COUNT(*) FROM player_wins WHERE l1_wins >= 3").fetchone()[0]
        
        # 3. Economy
        vault_bal = conn.execute('SELECT balance FROM vault WHERE id=1').fetchone()[0]
        avg_payout = conn.execute("SELECT AVG(output_amt) FROM transactions WHERE output_amt > 0").fetchone()[0]

    return {
        "metrics_timestamp": time.time(),
        "activity": { 
            "plays_last_hour": total_plays_1h, 
            "total_global_wins": total_wins 
        },
        "player_distribution": { 
            "active_on_layer_1": l1_players, 
            "active_on_layer_2": l2_players 
        },
        "economy": { 
            "vault_balance": vault_bal, 
            "average_win_payout": round(avg_payout, 2) if avg_payout else 0 
        }
    }