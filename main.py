import sqlite3
import time
from typing import Tuple, List, Optional
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# --- CONFIGURATION ---
DB_NAME = "game.db"
WIN_COOLDOWN = 60       # Seconds a player must wait between wins
LAYER2_THRESHOLD = 3    # How many transactions needed in 10s to trigger "Entropy Surge"
GRAND_SOLVE_ANSWER = "timestamp % 10 == 7 AND volume >= 3"
ADMIN_KEY = "CHIMERA_ROOT_OVERRIDE_2026"

app = FastAPI()

# Enable CORS (helpful for local testing)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- DATA MODELS ---
class PlayRequest(BaseModel):
    user_id: str

class SubmitRequest(BaseModel):
    user_id: str
    formula: str

class AdminRequest(BaseModel):
    key: str
    new_vault_balance: int = 1000

class ChatMessage(BaseModel):
    user_id: str
    message: str

# --- DATABASE INITIALIZATION ---
def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        # 1. Main Game Tables
        conn.execute('''CREATE TABLE IF NOT EXISTS players (
                        user_id TEXT PRIMARY KEY, 
                        balance INTEGER DEFAULT 100,
                        last_played REAL,
                        last_win_time REAL DEFAULT 0
                    )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS vault (
                        id INTEGER PRIMARY KEY, 
                        balance INTEGER
                    )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS transactions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT,
                        amount INTEGER,
                        timestamp REAL
                    )''')
        
        # 2. Season / Hall of Fame
        conn.execute('''CREATE TABLE IF NOT EXISTS hall_of_fame (
                        season_id INTEGER PRIMARY KEY,
                        winner_id TEXT,
                        payout INTEGER,
                        win_date TEXT,
                        method TEXT
                    )''')
        
        # 3. Difficulty Tracking
        conn.execute('''CREATE TABLE IF NOT EXISTS player_wins (
                        user_id TEXT PRIMARY KEY,
                        l1_wins INTEGER DEFAULT 0
                    )''')

        # 4. CHAT SYSTEM (NEW)
        conn.execute('''CREATE TABLE IF NOT EXISTS chat (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT,
                        message TEXT,
                        timestamp REAL
                    )''')

        # Seed Vault if empty
        vault = conn.execute('SELECT balance FROM vault WHERE id=1').fetchone()
        if not vault:
            conn.execute('INSERT INTO vault (id, balance) VALUES (1, 1000)')

# Initialize DB on startup
init_db()

# --- HELPER FUNCTIONS ---

def get_vault_balance(conn) -> int:
    cursor = conn.execute('SELECT balance FROM vault WHERE id=1')
    row = cursor.fetchone()
    return row[0] if row else 0

def log_transaction(conn, user_id, amount, vault_delta, fee):
    conn.execute('INSERT INTO transactions (user_id, amount, timestamp) VALUES (?, ?, ?)', 
                 (user_id, amount, time.time()))
    
    # Update Player
    conn.execute('''INSERT INTO players (user_id, balance, last_played) 
                    VALUES (?, 100 - ?, ?) 
                    ON CONFLICT(user_id) DO UPDATE SET 
                    balance = balance - ?, last_played = ?''', 
                    (user_id, fee, time.time(), fee, time.time()))
    
    # Update Vault
    conn.execute('UPDATE vault SET balance = balance + ? WHERE id=1', (vault_delta,))

def log_attempt(user_id, input_str, outcome):
    # Simple file logging for debug
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    with open("attempts.log", "a") as f:
        f.write(f"[{timestamp}] {user_id} | {input_str} | {outcome}\n")

# --- CORE GAME LOGIC (WITH LATENCY FIX) ---

def check_win_condition(conn, user_id: str) -> Tuple[bool, str]:
    # 1. WIN COOLDOWN CHECK (Anti-Drain)
    row = conn.execute('SELECT last_win_time FROM players WHERE user_id=?', (user_id,)).fetchone()
    last_win = row[0] if row else 0
    
    # If they are in win cooldown, they can PLAY but cannot WIN
    if (time.time() - last_win) < WIN_COOLDOWN:
        remaining = int(WIN_COOLDOWN - (time.time() - last_win))
        return False, f"ERR_HEAT_CRITICAL: WIN COOLDOWN ACTIVE ({remaining}s)"

    # 2. TIME CHECK (LATENCY PATCHED)
    now = time.time()
    current_sec = int(now)
    current_ms = now - current_sec # The decimal part (e.g. 0.456)
    last_digit = int(str(current_sec)[-1])
    
    is_valid_time = False

    # Scenario A: Perfect Hit (XX7.0 - XX7.9)
    if last_digit == 7:
        is_valid_time = True
        
    # Scenario B: Late Arrival / Network Lag (XX8.0 - XX8.4)
    # Allows for 400ms of latency (South Africa -> US typical lag)
    elif last_digit == 8 and current_ms < 0.4:
        is_valid_time = True
        
    # Scenario C: Early Click / Clock Drift (XX6.8 - XX6.9)
    # Allows for client clock being slightly fast (~200ms)
    elif last_digit == 6 and current_ms > 0.8:
        is_valid_time = True

    if not is_valid_time:
        return False, "SIGNAL_MISMATCH"

    # 3. DIFFICULTY CHECK
    p_row = conn.execute('SELECT l1_wins FROM player_wins WHERE user_id=?', (user_id,)).fetchone()
    l1_wins = p_row[0] if p_row else 0
    
    # Layer 1 (Easy)
    if l1_wins < 3:
        conn.execute('''INSERT INTO player_wins (user_id, l1_wins) VALUES (?, 1) 
                        ON CONFLICT(user_id) DO UPDATE SET l1_wins = l1_wins + 1''', (user_id,))
        return True, f"PROTOCOL_BYPASS_SUCCESS"

    # Layer 2 (Hard - Fixed Threshold 3)
    # Check total transactions in last 10 seconds
    volume = conn.execute('SELECT COUNT(*) FROM transactions WHERE timestamp > ?', 
                          (time.time() - 10,)).fetchone()[0]
    
    if volume >= LAYER2_THRESHOLD:
        return True, "ENTROPY_SURGE_CONFIRMED"
    
    return False, f"ERR_ENTROPY_INSUFFICIENT (Current: {volume}/{LAYER2_THRESHOLD})"


# --- ENDPOINTS ---

@app.get("/")
async def get_index():
    return FileResponse('index.html')

@app.get("/history")
def get_history():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.execute('SELECT user_id, amount, timestamp FROM transactions ORDER BY id DESC LIMIT 10')
        history = [{"user": row[0], "amt": row[1], "time": row[2]} for row in cursor.fetchall()]
        return history

@app.get("/season/status")
def get_status():
    with sqlite3.connect(DB_NAME) as conn:
        vault = get_vault_balance(conn)
        # Check if season is locked
        winner = conn.execute('SELECT winner_id FROM hall_of_fame WHERE season_id=1').fetchone()
        
        status = "ACTIVE"
        if vault <= 0: status = "LOCKED_VAULT_EMPTY"
        if winner: status = f"LOCKED_WINNER_{winner[0]}"
        
        return {
            "season_id": 1,
            "status": status,
            "vault_balance": vault,
            "message": "SEASON 1 IN PROGRESS" if not winner else "SEASON ENDED"
        }

@app.get("/broadcast/feed")
def get_feed():
    # Simple ticker logic
    with sqlite3.connect(DB_NAME) as conn:
        last_tx = conn.execute('SELECT user_id, amount FROM transactions ORDER BY id DESC LIMIT 1').fetchone()
        if last_tx:
            return {"message": f"LAST SIGNAL: {last_tx[0]} EXTRACTED {last_tx[1]} CREDITS"}
        return {"message": "SYSTEM IDLE. WAITING FOR INPUT."}

# --- PLAY ENDPOINT ---
@app.post("/play")
def play_game(req: PlayRequest):
    FEE = 10
    
    with sqlite3.connect(DB_NAME, isolation_level="IMMEDIATE") as conn:
        # Check if Season is Over
        winner = conn.execute('SELECT winner_id FROM hall_of_fame WHERE season_id=1').fetchone()
        if winner:
            return {"outcome": "LOCKED", "message": f"SEASON OVER. WON BY {winner[0]}"}

        vault = get_vault_balance(conn)
        if vault <= 0:
            return {"outcome": "LOCKED", "message": "VAULT EMPTY"}

        # Check Win Condition
        is_win, reason = check_win_condition(conn, req.user_id)
        
        if is_win:
            # Payout
            payout = int(vault * 0.05) # 5% of vault
            if payout < 1: payout = 1
            
            log_transaction(conn, req.user_id, payout, -payout, FEE)
            
            # Record Win Time for Cooldown
            conn.execute('UPDATE players SET last_win_time = ? WHERE user_id=?', (time.time(), req.user_id))
            conn.commit()
            
            return {"outcome": "WIN", "payout": payout, "message": reason}
        else:
            # Loss - User pays fee to Vault
            log_transaction(conn, req.user_id, 0, FEE, FEE)
            conn.commit()
            return {"outcome": "LOSS", "payout": 0, "message": reason}

# --- GRAND SOLVE ENDPOINT ---
@app.post("/submit")
def grand_solve(req: SubmitRequest):
    # Normalize string
    submission = " ".join(req.formula.split()).lower()
    target = " ".join(GRAND_SOLVE_ANSWER.split()).lower()
    
    with sqlite3.connect(DB_NAME, isolation_level="IMMEDIATE") as conn:
        try:
            vault = get_vault_balance(conn)
            
            if vault <= 0:
                return {"outcome": "ERROR", "message": "SEASON CLOSED"}

            winner = conn.execute('SELECT winner_id FROM hall_of_fame WHERE season_id=1').fetchone()
            if winner:
                 return {"outcome": "LOCKED", "message": f"ALREADY CLAIMED BY {winner[0]}"}

            if submission == target:
                prize = int(vault * 0.60)
                
                conn.execute('''INSERT INTO hall_of_fame (season_id, winner_id, payout, win_date, method)
                                VALUES (1, ?, ?, ?, 'GRAND_SOLVE')''', 
                                (req.user_id, prize, time.ctime()))
                
                # Drain the vault
                conn.execute('UPDATE vault SET balance = 0 WHERE id=1')
                log_transaction(conn, req.user_id, 0, prize, 0)
                conn.commit()
                
                log_attempt(req.user_id, submission, "GRAND_SOLVE_WIN")
                return {"outcome": "GRAND_SOLVE", "payout": prize, "message": "SYSTEM COMPROMISED. SEASON ENDED."}
            
            else:
                log_attempt(req.user_id, submission, "REJECTED")
                return {"outcome": "REJECTED", "message": "INVALID KEY"}

        except sqlite3.IntegrityError:
            log_attempt(req.user_id, submission, "LOCKED_RACE_CONDITION")
            return {"outcome": "LOCKED", "message": "ALREADY CLAIMED BY ANOTHER PLAYER"}

# --- ADMIN OVERRIDE ---
@app.post("/admin/reset_season")
def root_access_override(req: AdminRequest):
    if req.key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="ACCESS_DENIED")

    with sqlite3.connect(DB_NAME, isolation_level="IMMEDIATE") as conn:
        conn.execute('DELETE FROM hall_of_fame WHERE season_id=1')
        conn.execute('UPDATE vault SET balance = ? WHERE id=1', (req.new_vault_balance,))
        conn.commit()
        
    return {"status": "REALITY_RESTORED", "message": "SEASON 1 RESET COMPLETE."}

# --- NEW: ENCRYPTED CHAT ENDPOINTS ---

@app.get("/discuss")
def get_chat():
    with sqlite3.connect(DB_NAME) as conn:
        # Get last 50 messages, newest at bottom
        cursor = conn.execute('SELECT user_id, message, timestamp FROM chat ORDER BY id DESC LIMIT 50')
        messages = [{"user": row[0], "text": row[1], "time": row[2]} for row in cursor.fetchall()]
        return messages[::-1] # Reverse to show chronological order

@app.post("/discuss")
def post_chat(msg: ChatMessage):
    if len(msg.message) > 140:
        return {"status": "ERROR", "message": "PAYLOAD_TOO_LARGE"}
    
    with sqlite3.connect(DB_NAME, isolation_level="IMMEDIATE") as conn:
        conn.execute('INSERT INTO chat (user_id, message, timestamp) VALUES (?, ?, ?)', 
                     (msg.user_id, msg.message, time.time()))
        conn.commit()
    return {"status": "SENT", "message": "PACKET_INJECTED"}

# --- STATIC FILES ---
# Mount static files (ensure you have an 'static' folder or serve index directly if in root)
# Since you likely have index.html in the same folder:
# app.mount("/", StaticFiles(directory="."), html=True) 
# BUT since we defined @app.get("/") manually above, we don't strictly need this unless you have CSS/JS files.