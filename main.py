import sqlite3
import time
import os
import json
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Tuple, Optional

app = FastAPI(title="Beat the House: Season 1 (Restored)")

# Enable CORS for local testing comfort
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- CONFIGURATION ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "data", "audit_manifest.json")

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
DEEP_GRID_SOLVE_ANSWER = "timestamp % 10 == 0 AND volume >= 5" # SEASON 3 HARD MODE

# Tuning
LAYER2_THRESHOLD = 3         # Concurrent plays required
WIN_COOLDOWN = 120           # Seconds between WINS
PLAY_COOLDOWN = 5            # Seconds between PLAYS (Anti-Spam)
BROADCAST_COOLDOWN = 300     # 5 Minutes per broadcast
BROADCAST_LIMIT = 5          # Show last 5 messages

# --- DATABASE LAYER ---
def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        c = conn.cursor()
        
        # 0. System State (The Era Switch)
        c.execute('''CREATE TABLE IF NOT EXISTS system_state 
                     (key TEXT PRIMARY KEY, value TEXT)''')
        
        # Set default season if new
        check = c.execute("SELECT value FROM system_state WHERE key='current_season'").fetchone()
        if not check:
            c.execute("INSERT INTO system_state (key, value) VALUES ('current_season', '1')")

        # 1. Vault
        c.execute('''CREATE TABLE IF NOT EXISTS vault (id INTEGER PRIMARY KEY, balance INTEGER)''')
        c.execute('SELECT count(*) FROM vault')
        if c.fetchone()[0] == 0:
            c.execute('INSERT INTO vault (id, balance) VALUES (1, ?)', (SEED_VAULT_AMOUNT,))
            
        # 2. Players 
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

        # 5. Broadcasts (Legacy)
        c.execute('''CREATE TABLE IF NOT EXISTS broadcasts 
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, message TEXT, timestamp REAL)''')
        
        # 6. Hall of Fame
        c.execute('''CREATE TABLE IF NOT EXISTS hall_of_fame 
                     (season_id INTEGER PRIMARY KEY, winner_id TEXT, payout INTEGER, win_date TEXT, method TEXT)''')
                      
        # 7. CHAT (NEW - For V2.1 UI)
        c.execute('''CREATE TABLE IF NOT EXISTS chat (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT,
                        message TEXT,
                        timestamp REAL
                    )''')
        
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

class ChatMessage(BaseModel):
    user_id: str
    message: str

# --- CORE HELPERS ---

def get_vault_balance(conn):
    return conn.execute('SELECT balance FROM vault WHERE id=1').fetchone()[0]

def update_vault(conn, amount_change):
    conn.execute('UPDATE vault SET balance = balance + ? WHERE id=1', (amount_change,))
    return conn.execute('SELECT balance FROM vault WHERE id=1').fetchone()[0]

def get_current_season():
    with sqlite3.connect(DB_NAME) as conn:
        res = conn.execute("SELECT value FROM system_state WHERE key='current_season'").fetchone()
        return int(res[0]) if res else 1

def calculate_hybrid_payout(current_vault):
    if current_vault <= 0: return 0
    raw_payout = max(20, int(current_vault * 0.03))
    return min(current_vault, raw_payout)

def log_transaction(conn, user_id, input_amt, output_amt, vault_bal):
    conn.execute('''INSERT INTO transactions (user_id, input_amt, output_amt, vault_balance, timestamp) 
                    VALUES (?, ?, ?, ?, ?)''', (user_id, input_amt, output_amt, vault_bal, time.time()))
    
    conn.execute('''INSERT INTO players (user_id, total_spent, total_won) VALUES (?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET 
                    total_spent = total_spent + ?,
                    total_won = total_won + ?''', 
                    (user_id, input_amt, output_amt, input_amt, output_amt))

def log_attempt(user_id, formula, outcome):
    try:
        with open("attempts.log", "a") as f:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
            f.write(f"[{timestamp}] {user_id} | {outcome} | {formula}\n")
        print(f">>> ATTEMPT: {user_id} tried '{formula}' -> {outcome}")
    except Exception as e:
        print(f"Error logging attempt: {e}")

# --- GAME LOGIC ---

def check_win_condition(conn, user_id: str) -> Tuple[bool, str]:
    season = get_current_season()
    
    # HARD MODE SEASON 3 SCALING
    target_digit = '0' if season == 3 else '7'
    vol_threshold = 5 if season == 3 else LAYER2_THRESHOLD

    # 1. WIN COOLDOWN
    row = conn.execute('SELECT last_win_time FROM players WHERE user_id=?', (user_id,)).fetchone()
    last_win = row[0] if row else 0
    if (time.time() - last_win) < WIN_COOLDOWN:
        remaining = int(WIN_COOLDOWN - (time.time() - last_win))
        return False, f"ERR_HEAT_CRITICAL: WIN COOLDOWN ACTIVE ({remaining}s)"

    # 2. TIME CHECK (Layer 1)
    current_time = int(time.time())
    if str(current_time)[-1] != target_digit:
        return False, "SIGNAL_MISMATCH"

    # 3. DIFFICULTY CHECK
    p_row = conn.execute('SELECT l1_wins FROM player_wins WHERE user_id=?', (user_id,)).fetchone()
    l1_wins = p_row[0] if p_row else 0
    
    if l1_wins < 3:
        conn.execute('''INSERT INTO player_wins (user_id, l1_wins) VALUES (?, 1) 
                        ON CONFLICT(user_id) DO UPDATE SET l1_wins = l1_wins + 1''', (user_id,))
        return True, f"PROTOCOL_BYPASS_SUCCESS"

    # Layer 2 (Hard)
    volume = conn.execute('SELECT COUNT(*) FROM transactions WHERE timestamp > ?', 
                          (time.time() - 10,)).fetchone()[0]
    
    if volume >= vol_threshold:
        return True, "ENTROPY_SURGE_CONFIRMED"
    
    return False, f"ERR_ENTROPY_INSUFFICIENT (Current: {volume}/{vol_threshold})"

# --- ENDPOINTS ---

@app.get("/")
async def read_root():
    """
    THE FINAL SWITCH: Now checks the database for the Era Shift.
    """
    season = get_current_season()
    headers = {"Cache-Control": "no-store, must-revalidate"}
    
    # Season 3: The Deep Grid
    if season == 3:
        if os.path.exists("deep_grid.html"):
            return FileResponse("deep_grid.html", headers=headers)
            
    # Season 2: The Audit
    if season == 2:
        return FileResponse("audit.html", headers=headers)
    
    # Season 1: The Green Heist
    if os.path.exists("heist.html"):
        return FileResponse("heist.html", headers=headers)
    return FileResponse("index.html", headers=headers)

@app.get("/api/manifest")
def get_manifest():
    """
    Serves the Season 2 Audit Data.
    Only accessible if Season 2 is active in the database.
    """
    if get_current_season() < 2:
        return JSONResponse(status_code=403, content={"error": "TIMELINE_LOCKED"})
    
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
        return data
    return JSONResponse(status_code=404, content={"error": "MANIFEST_MISSING"})

@app.post("/admin/trigger_s2")
def trigger_season_2():
    """
    DEBUG TOOL: Forces the Era Shift.
    """
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT OR REPLACE INTO system_state (key, value) VALUES ('current_season', '2')")
        conn.commit()
    return {"status": "ERA_SHIFT_COMPLETE", "mode": "AUDIT"}

@app.post("/admin/trigger_s3")
def trigger_season_3():
    """
    THE TRAPDOOR: Activates the Deep Grid. Refills the vault to bait the players back in.
    """
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT OR REPLACE INTO system_state (key, value) VALUES ('current_season', '3')")
        # The Vane Office refills the bait
        conn.execute("UPDATE vault SET balance = 5000 WHERE id = 1")
        # Wipe the slate clean
        conn.execute("DELETE FROM hall_of_fame")
        conn.execute("DELETE FROM transactions")
        conn.execute("DELETE FROM players")
        conn.execute("DELETE FROM player_wins")
        conn.commit()
    return {"status": "REROUTED_TO_DEEP_GRID", "mode": "DEEP_GRID"}

@app.post("/play", response_model=PlayResponse)
def play_game(request: PlayRequest):
    with sqlite3.connect(DB_NAME) as conn:
        vault = get_vault_balance(conn)
        if vault <= 0:
            return {
                "user_id": request.user_id, "outcome": "SEASON_ENDED", 
                "payout": 0, "vault_balance": 0, 
                "message": "VAULT DRAINED. SEASON OVER.", "season_active": False
            }

        row = conn.execute('SELECT last_play_time FROM players WHERE user_id=?', (request.user_id,)).fetchone()
        last_play = row[0] if row else 0
        if (time.time() - last_play) < PLAY_COOLDOWN:
            return {
                "user_id": request.user_id, "outcome": "ERROR", 
                "payout": 0, "vault_balance": vault, 
                "message": "RATE_LIMITED: WAIT 5s", "season_active": True
            }

        conn.execute('''INSERT INTO players (user_id, last_play_time, total_spent, total_won) 
                        VALUES (?, ?, 0, 0) ON CONFLICT(user_id) 
                        DO UPDATE SET last_play_time = ?''', 
                        (request.user_id, time.time(), time.time()))

        update_vault(conn, int(COST_PER_PLAY * 0.9)) 
        
        is_win, msg = check_win_condition(conn, request.user_id)
        
        outcome = "LOSS"
        payout = 0
        
        if is_win:
            current_vault = get_vault_balance(conn)
            raw_payout = calculate_hybrid_payout(current_vault)
            payout = min(raw_payout, current_vault)
            
            update_vault(conn, -payout)
            outcome = "WIN"
            
            conn.execute('UPDATE players SET last_win_time = ? WHERE user_id=?', 
                         (time.time(), request.user_id))
        
        new_vault = get_vault_balance(conn)
        log_transaction(conn, request.user_id, COST_PER_PLAY, payout, new_vault)
        conn.commit()
        
        return {
            "user_id": request.user_id, "outcome": outcome, 
            "payout": payout, "vault_balance": new_vault, "message": msg,
            "season_active": new_vault > 0
        }

# --- CHAT ENDPOINTS ---
@app.get("/discuss")
def get_chat():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.execute('SELECT user_id, message, timestamp FROM chat ORDER BY id DESC LIMIT 50')
        messages = [{"user": row[0], "text": row[1], "time": row[2]} for row in cursor.fetchall()]
        return messages[::-1]

@app.post("/discuss")
def post_chat(msg: ChatMessage):
    if len(msg.message) > 140:
        return {"status": "ERROR", "message": "PAYLOAD_TOO_LARGE"}
    with sqlite3.connect(DB_NAME, isolation_level="IMMEDIATE") as conn:
        conn.execute('INSERT INTO chat (user_id, message, timestamp) VALUES (?, ?, ?)', 
                     (msg.user_id, msg.message, time.time()))
        conn.commit()
    return {"status": "SENT", "message": "PACKET_INJECTED"}

# --- BROADCAST ENDPOINTS ---
@app.post("/broadcast")
def send_broadcast(req: BroadcastRequest):
    msg = req.message[:60].upper()
    with sqlite3.connect(DB_NAME) as conn:
        row = conn.execute('SELECT last_broadcast_time FROM players WHERE user_id=?', (req.user_id,)).fetchone()
        last_b = row[0] if row else 0
        if (time.time() - last_b) < BROADCAST_COOLDOWN:
            return {"status": "ERROR", "message": f"COOLDOWN: WAIT {int(BROADCAST_COOLDOWN - (time.time() - last_b))}s"}

        conn.execute('''INSERT INTO players (user_id, last_broadcast_time, total_spent, total_won) 
                        VALUES (?, ?, 0, 0) ON CONFLICT(user_id) DO UPDATE SET last_broadcast_time = ?''', 
                        (req.user_id, time.time(), time.time()))

        conn.execute("INSERT INTO broadcasts (user_id, message, timestamp) VALUES (?, ?, ?)", 
                     (req.user_id, msg, time.time()))
        conn.commit()
    return {"status": "SENT"}

@app.get("/broadcast/feed")
def get_broadcasts():
    with sqlite3.connect(DB_NAME) as conn:
        rows = conn.execute('''SELECT user_id, message FROM broadcasts 
                               ORDER BY id DESC LIMIT 1''').fetchall()
        if not rows:
             return {"message": "SYSTEM IDLE"}
        return {"message": f"{rows[0][0]}: {rows[0][1]}"}

@app.post("/submit")
def grand_solve(req: SubmitRequest):
    submission = " ".join(req.formula.split()).lower()
    season = get_current_season()
    target = " ".join(DEEP_GRID_SOLVE_ANSWER.split() if season == 3 else GRAND_SOLVE_ANSWER.split()).lower()
    
    with sqlite3.connect(DB_NAME, isolation_level="IMMEDIATE") as conn:
        try:
            vault = get_vault_balance(conn)
            if vault <= 0:
                winner = conn.execute('SELECT winner_id FROM hall_of_fame WHERE season_id=?', (season,)).fetchone()
                if winner: return {"outcome": "LOCKED", "message": f"ALREADY CLAIMED BY {winner[0]}"}
                return {"outcome": "ERROR", "message": "SEASON CLOSED"}

            if submission == target:
                prize = int(vault * 0.60)
                conn.execute('''INSERT INTO hall_of_fame (season_id, winner_id, payout, win_date, method)
                                VALUES (?, ?, ?, ?, 'GRAND_SOLVE')''', 
                                (season, req.user_id, prize, time.ctime()))
                
                # Drain Vault
                conn.execute('UPDATE vault SET balance = 0 WHERE id=1')
                log_transaction(conn, req.user_id, 0, prize, 0)
                
                # TRIGGER NEXT SEASON (1 -> 2, 3 -> 4)
                next_season = 2 if season == 1 else 4
                conn.execute("INSERT OR REPLACE INTO system_state (key, value) VALUES ('current_season', ?)", (str(next_season),))
                
                conn.commit()
                log_attempt(req.user_id, submission, "GRAND_SOLVE_WIN")
                
                return {"outcome": "GRAND_SOLVE", "payout": prize, "message": "SYSTEM COMPROMISED. ERA SHIFT INITIATED."}
            else:
                log_attempt(req.user_id, submission, "REJECTED")
                return {"outcome": "REJECTED", "message": "INVALID KEY"}

        except sqlite3.IntegrityError:
            log_attempt(req.user_id, submission, "LOCKED_RACE_CONDITION")
            return {"outcome": "LOCKED", "message": "ALREADY CLAIMED BY ANOTHER PLAYER"}

@app.get("/season/status")
def get_season_status():
    season = get_current_season()
    with sqlite3.connect(DB_NAME) as conn:
        vault = get_vault_balance(conn)
        row = conn.execute('SELECT winner_id, payout, win_date FROM hall_of_fame WHERE season_id=?', (season,)).fetchone()
        
        status = "ACTIVE"
        winner_data = None
        if row:
            status = f"LOCKED_WINNER_{row[0]}"
            winner_data = {"user": row[0], "payout": row[1], "date": row[2]}
        elif vault <= 0:
            status = "LOCKED_EMPTY"

        return {
            "status": status,
            "vault_balance": vault,
            "winner": winner_data,
            "active": (vault > 0 and not row),
            "season": season
        }
    
@app.get("/history")
def get_history():
    with sqlite3.connect(DB_NAME) as conn:
        rows = conn.execute('SELECT user_id, input_amt, output_amt, vault_balance, timestamp FROM transactions ORDER BY id DESC LIMIT 20').fetchall()
    
    formatted = []
    for r in rows:
        formatted.append({
            "user": r[0],
            "amt": r[2] if r[2] > 0 else -r[1],
            "time": r[4]
        })
    return formatted

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