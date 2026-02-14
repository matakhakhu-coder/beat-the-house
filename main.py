import sqlite3
import time
import os
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List, Tuple, Optional

app = FastAPI(title="Beat the House: Season 1 (Gold)")

# --- CONFIGURATION ---
if os.path.exists("/app/data"):
    DB_NAME = "/app/data/game.db"
    print(">>> PRODUCTION MODE: Persistent Volume")
else:
    DB_NAME = "game.db"
    print(">>> LOCAL MODE: Local File")

# Constants
SEED_VAULT_AMOUNT = 1000
COST_PER_PLAY = 10
GRAND_SOLVE_ANSWER = "timestamp % 10 == 7 AND volume >= 3"

# Tuning
LAYER2_THRESHOLD = 3         # Concurrent plays required
WIN_COOLDOWN = 120           # Seconds between WINS
PLAY_COOLDOWN = 5            # Seconds between PLAYS (Anti-Spam)
BROADCAST_COOLDOWN = 300     # 5 Minutes per broadcast
BROADCAST_LIMIT = 5          # Show last 5 messages

# --- FRONTEND SETUP ---
if not os.path.exists("static"):
    os.makedirs("static")
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- DATABASE LAYER ---
def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        c = conn.cursor()
        
        # 1. Vault
        c.execute('''CREATE TABLE IF NOT EXISTS vault (id INTEGER PRIMARY KEY, balance INTEGER)''')
        c.execute('SELECT count(*) FROM vault')
        if c.fetchone()[0] == 0:
            c.execute('INSERT INTO vault (id, balance) VALUES (1, ?)', (SEED_VAULT_AMOUNT,))
            
        # 2. Players 
        # Added last_play_time for anti-spam
        c.execute('''CREATE TABLE IF NOT EXISTS players 
                     (user_id TEXT PRIMARY KEY, total_spent INTEGER, total_won INTEGER, 
                      last_win_time REAL DEFAULT 0, last_play_time REAL DEFAULT 0, 
                      last_broadcast_time REAL DEFAULT 0)''')
        
        # 3. Transactions
        c.execute('''CREATE TABLE IF NOT EXISTS transactions 
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, 
                      input_amt INTEGER, output_amt INTEGER, vault_balance INTEGER, timestamp REAL)''')
        
        # 4. Player Difficulty Tracking
        c.execute('''CREATE TABLE IF NOT EXISTS player_wins 
                     (user_id TEXT PRIMARY KEY, l1_wins INTEGER DEFAULT 0)''')

        # 5. Broadcasts
        c.execute('''CREATE TABLE IF NOT EXISTS broadcasts 
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, message TEXT, timestamp REAL)''')
        
        # 6. Hall of Fame (BUG FIX 3: Added PRIMARY KEY constraint on season_id for Highlander Lock)
        c.execute('''CREATE TABLE IF NOT EXISTS hall_of_fame 
                     (season_id INTEGER PRIMARY KEY, winner_id TEXT, payout INTEGER, win_date TEXT, method TEXT)''')
        
        # MIGRATION HELPER (Run once to add columns if they don't exist in old DB)
        cols = [
            ("last_win_time", "REAL DEFAULT 0"),
            ("last_play_time", "REAL DEFAULT 0"),
            ("last_broadcast_time", "REAL DEFAULT 0")
        ]
        for col, dtype in cols:
            try: c.execute(f"ALTER TABLE players ADD COLUMN {col} {dtype}")
            except: pass

        conn.commit()

init_db()

# --- MODELS ---
class PlayRequest(BaseModel):
    user_id: str

class PlayResponse(BaseModel):
    user_id: str
    outcome: str
    payout: int
    vault_balance: int
    message: str
    season_active: bool = True

class BroadcastRequest(BaseModel):
    user_id: str
    message: str

class SubmitRequest(BaseModel):
    user_id: str
    formula: str

# --- CORE HELPERS ---

def get_vault_balance(conn):
    return conn.execute('SELECT balance FROM vault WHERE id=1').fetchone()[0]

def update_vault(conn, amount_change):
    conn.execute('UPDATE vault SET balance = balance + ? WHERE id=1', (amount_change,))
    # Ensure we return the new balance
    return conn.execute('SELECT balance FROM vault WHERE id=1').fetchone()[0]

def calculate_hybrid_payout(current_vault):
    # Hybrid: MIN(vault, MAX(20, 3% of Vault))
    if current_vault <= 0: return 0
    raw_payout = max(20, int(current_vault * 0.03))
    return min(current_vault, raw_payout) # BUG FIX 1: Cap at current vault

def log_transaction(conn, user_id, input_amt, output_amt, vault_bal):
    conn.execute('''INSERT INTO transactions (user_id, input_amt, output_amt, vault_balance, timestamp) 
                    VALUES (?, ?, ?, ?, ?)''', (user_id, input_amt, output_amt, vault_bal, time.time()))
    
    # Upsert player stats
    conn.execute('''INSERT INTO players (user_id, total_spent, total_won) VALUES (?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET 
                    total_spent = total_spent + ?,
                    total_won = total_won + ?''', 
                    (user_id, input_amt, output_amt, input_amt, output_amt))

def log_attempt(user_id, formula, outcome):
    """
    Logs every Grand Solve attempt to a local file for analysis.
    Format: [TIMESTAMP] USER_ID | OUTCOME | FORMULA
    """
    try:
        with open("attempts.log", "a") as f:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
            f.write(f"[{timestamp}] {user_id} | {outcome} | {formula}\n")
        
        # Also print to console so you see it in Railway logs
        print(f">>> ATTEMPT: {user_id} tried '{formula}' -> {outcome}")
    except Exception as e:
        print(f"Error logging attempt: {e}")

# --- GAME LOGIC ---

def check_win_condition(conn, user_id: str) -> Tuple[bool, str]:
    # 1. WIN COOLDOWN CHECK (Anti-Drain)
    row = conn.execute('SELECT last_win_time FROM players WHERE user_id=?', (user_id,)).fetchone()
    last_win = row[0] if row else 0
    
    # If they are in win cooldown, they can PLAY but cannot WIN
    if (time.time() - last_win) < WIN_COOLDOWN:
        remaining = int(WIN_COOLDOWN - (time.time() - last_win))
        return False, f"ERR_HEAT_CRITICAL: WIN COOLDOWN ACTIVE ({remaining}s)"

    # 2. TIME CHECK (Layer 1)
    current_time = int(time.time())
    if str(current_time)[-1] != '7':
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
    volume = conn.execute('SELECT COUNT(*) FROM transactions WHERE timestamp > ?', 
                          (time.time() - 10,)).fetchone()[0]
    
    if volume >= LAYER2_THRESHOLD:
        return True, "ENTROPY_SURGE_CONFIRMED"
    
    return False, f"ERR_ENTROPY_INSUFFICIENT (Current: {volume}/{LAYER2_THRESHOLD})"

# --- ENDPOINTS ---

@app.get("/")
async def read_root():
    return FileResponse('static/index.html')

@app.post("/play", response_model=PlayResponse)
def play_game(request: PlayRequest):
    with sqlite3.connect(DB_NAME) as conn:
        # 1. Check Season Status
        vault = get_vault_balance(conn)
        if vault <= 0:
            return {
                "user_id": request.user_id, "outcome": "SEASON_ENDED", 
                "payout": 0, "vault_balance": 0, 
                "message": "VAULT DRAINED. SEASON OVER.", "season_active": False
            }

        # 2. Check PLAY Cooldown (Anti-Spam)
        row = conn.execute('SELECT last_play_time FROM players WHERE user_id=?', (request.user_id,)).fetchone()
        last_play = row[0] if row else 0
        if (time.time() - last_play) < PLAY_COOLDOWN:
            return {
                "user_id": request.user_id, "outcome": "ERROR", 
                "payout": 0, "vault_balance": vault, 
                "message": "RATE_LIMITED: WAIT 5s", "season_active": True
            }

        # Update Play Time immediately
        conn.execute('''INSERT INTO players (user_id, last_play_time, total_spent, total_won) 
                        VALUES (?, ?, 0, 0) ON CONFLICT(user_id) 
                        DO UPDATE SET last_play_time = ?''', 
                        (request.user_id, time.time(), time.time()))

        # 3. Process Entry Fee (House keeps 10%)
        update_vault(conn, int(COST_PER_PLAY * 0.9)) 
        
        # 4. Check Win
        is_win, msg = check_win_condition(conn, request.user_id)
        
        outcome = "LOSS"
        payout = 0
        
        if is_win:
            current_vault = get_vault_balance(conn)
            # BUG FIX 1: Cap payout at current vault
            raw_payout = calculate_hybrid_payout(current_vault)
            payout = min(raw_payout, current_vault)
            
            update_vault(conn, -payout)
            outcome = "WIN"
            
            # Update Win Timer
            conn.execute('UPDATE players SET last_win_time = ? WHERE user_id=?', 
                         (time.time(), request.user_id))
        
        # 5. Log
        new_vault = get_vault_balance(conn)
        log_transaction(conn, request.user_id, COST_PER_PLAY, payout, new_vault)
        conn.commit()
        
        return {
            "user_id": request.user_id, "outcome": outcome, 
            "payout": payout, "vault_balance": new_vault, "message": msg,
            "season_active": new_vault > 0
        }

@app.post("/broadcast")
def send_broadcast(req: BroadcastRequest):
    msg = req.message[:60].upper()
    
    with sqlite3.connect(DB_NAME) as conn:
        # Rate Limit Check
        row = conn.execute('SELECT last_broadcast_time FROM players WHERE user_id=?', (req.user_id,)).fetchone()
        last_b = row[0] if row else 0
        
        if (time.time() - last_b) < BROADCAST_COOLDOWN:
            return {"status": "ERROR", "message": f"COOLDOWN: WAIT {int(BROADCAST_COOLDOWN - (time.time() - last_b))}s"}

        # BUG FIX 4: Handle new players correctly with default values
        conn.execute('''INSERT INTO players (user_id, last_broadcast_time, total_spent, total_won) 
                        VALUES (?, ?, 0, 0)
                        ON CONFLICT(user_id) DO UPDATE SET last_broadcast_time = ?''', 
                        (req.user_id, time.time(), time.time()))

        conn.execute("INSERT INTO broadcasts (user_id, message, timestamp) VALUES (?, ?, ?)", 
                     (req.user_id, msg, time.time()))
        conn.commit()
        
    return {"status": "SENT"}

@app.get("/broadcast/feed")
def get_broadcasts():
    with sqlite3.connect(DB_NAME) as conn:
        rows = conn.execute('''SELECT user_id, message FROM broadcasts 
                               ORDER BY id DESC LIMIT ?''', (BROADCAST_LIMIT,)).fetchall()
    return {"feed": [{"user": r[0], "message": r[1]} for r in rows]}

@app.post("/submit")
def grand_solve(req: SubmitRequest):
    submission = " ".join(req.formula.split()).lower()
    target = " ".join(GRAND_SOLVE_ANSWER.split()).lower()
    
    # Connect with isolation level IMMEDIATE to lock db for this transaction
    with sqlite3.connect(DB_NAME, isolation_level="IMMEDIATE") as conn:
        try:
            vault = get_vault_balance(conn)
            
            if vault <= 0:
                # Check Hall of Fame
                winner = conn.execute('SELECT winner_id FROM hall_of_fame WHERE season_id=1').fetchone()
                outcome = "LOCKED" if winner else "ERROR_SEASON_CLOSED"
                log_attempt(req.user_id, submission, outcome)  # <--- LOG HERE
                
                if winner:
                    return {"outcome": "LOCKED", "message": f"ALREADY CLAIMED BY {winner[0]}"}
                return {"outcome": "ERROR", "message": "SEASON CLOSED"}

            if submission == target:
                prize = int(vault * 0.60)
                
                # BUG FIX 3: Constraint handling for Highlander Lock
                # We try to insert FIRST. If it fails (Unique constraint), we know we lost the race.
                conn.execute('''INSERT INTO hall_of_fame (season_id, winner_id, payout, win_date, method)
                                VALUES (1, ?, ?, ?, 'GRAND_SOLVE')''', 
                                (req.user_id, prize, time.ctime()))
                
                # BUG FIX 2: Explicitly set vault to 0 to prevent double drain
                conn.execute('UPDATE vault SET balance = 0 WHERE id=1')
                
                # Record the specific win transaction
                log_transaction(conn, req.user_id, 0, prize, 0)
                
                conn.commit()
                
                log_attempt(req.user_id, submission, "GRAND_SOLVE_WIN") # <--- LOG HERE
                return {"outcome": "GRAND_SOLVE", "payout": prize, "message": "SYSTEM COMPROMISED. SEASON ENDED."}
            
            else:
                log_attempt(req.user_id, submission, "REJECTED") # <--- LOG HERE
                return {"outcome": "REJECTED", "message": "INVALID KEY"}

        except sqlite3.IntegrityError:
            # This catches the race condition if two people submit at once
            log_attempt(req.user_id, submission, "LOCKED_RACE_CONDITION") # <--- LOG HERE
            return {"outcome": "LOCKED", "message": "ALREADY CLAIMED BY ANOTHER PLAYER"}

# BUG FIX 5: Complete Season Status Endpoint
@app.get("/season/status")
def get_season_status():
    with sqlite3.connect(DB_NAME) as conn:
        vault = get_vault_balance(conn)
        
        # Check for winner
        row = conn.execute('SELECT winner_id, payout, win_date FROM hall_of_fame WHERE season_id=1').fetchone()
        
        if row:
            return {
                "active": False,
                "vault": 0,
                "winner": {
                    "user": row[0],
                    "payout": row[1],
                    "date": row[2]
                }
            }
        
        # Fallback if vault is 0 but no grand solve (e.g. drained by players)
        if vault <= 0:
             return {
                "active": False,
                "vault": 0,
                "winner": None
            }
            
        return {"active": True, "vault": vault, "winner": None}
    
@app.get("/history")
def get_history():
    with sqlite3.connect(DB_NAME) as conn:
        rows = conn.execute('SELECT * FROM transactions ORDER BY id DESC LIMIT 20').fetchall()
    return {"history": rows}

@app.get("/analytics")
def get_analytics():
    with sqlite3.connect(DB_NAME) as conn:
        one_hour_ago = time.time() - 3600
        total_plays_1h = conn.execute('SELECT COUNT(*) FROM transactions WHERE timestamp > ?', (one_hour_ago,)).fetchone()[0]
        total_wins = conn.execute("SELECT COUNT(*) FROM transactions WHERE output_amt > 0").fetchone()[0]
        l1_players = conn.execute("SELECT COUNT(*) FROM player_wins WHERE l1_wins < 3 AND l1_wins > 0").fetchone()[0]
        l2_players = conn.execute("SELECT COUNT(*) FROM player_wins WHERE l1_wins >= 3").fetchone()[0]
        vault_bal = conn.execute('SELECT balance FROM vault WHERE id=1').fetchone()[0]

    return {
        "metrics_timestamp": time.time(),
        "activity": { "plays_last_hour": total_plays_1h, "total_global_wins": total_wins },
        "player_distribution": { "active_on_layer_1": l1_players, "active_on_layer_2": l2_players },
        "economy": { "vault_balance": vault_bal }
    }