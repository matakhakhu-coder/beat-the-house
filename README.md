# Beat the House // Season 1

![Status](https://img.shields.io/badge/Status-LIVE-success)
![Stack](https://img.shields.io/badge/Tech-FastAPI%20%7C%20SQLite%20%7C%20VanillaJS-blue)
![Type](https://img.shields.io/badge/Type-Multiplayer%20ARG-orange)

**A persistent, multiplayer financial extraction simulation testing collective intelligence and social coordination.**

🔴 **Live Terminal:** [https://web-production-05efb.up.railway.app/](https://web-production-05efb.up.railway.app/)

---

## 📡 The Premise
"Beat the House" is a Capture-the-Flag (CTF) style Alternate Reality Game (ARG) where players interact with a simulated financial terminal. The goal is to discover hidden algorithmic patterns to extract funds from a shared, persistent "Vault."

The game features a **Grand Solve** mechanic: a hidden cryptographic key that, if discovered, allows a single player to drain the entire vault and instantly end the season for everyone.

## ⚙️ Key Mechanics

### 1. Temporal Synchronization (Layer 1)
* **Mechanic:** Players must execute transactions at specific Unix timestamps (seconds ending in `:07`).
* **Tech:** Real-time clock synchronization between client (JS) and server (Python) to prevent latency exploits.
* **UX:** Visual cues (flashing hex digits) guide attentive players to the pattern.

### 2. Social Coordination (Layer 2)
* **Mechanic:** High-value extraction requires a "Volume Surge" – multiple players executing transactions within the same 1-second window.
* **Tech:** The backend monitors concurrent transaction volume. If `volume < 3`, the heist fails.
* **Goal:** Forces players to move from competitive play to cooperative swarming.

### 3. The Broadcast System
* **Mechanic:** A global ticker tape allows players to send unmoderated messages to all active terminals.
* **Purpose:** Enables real-time coordination (or disinformation campaigns) among the player base.
* **Safety:** Rate-limited by User ID (1 msg / 5 mins) to prevent spam while maintaining chaos.

### 4. Persistent Economy
* **Vault Logic:** The central vault fluctuates based on player wins/losses.
* **Hybrid Payouts:** Uses a dynamic payout formula `MIN(vault, MAX(20, 3%))` to ensure the game remains solvent while rewarding early adopters.
* **Anti-Abuse:** Implements a 120-second "Winner's Cooldown" to prevent script-kiddies from draining the vault via automation.

---

## 🛠️ Tech Stack

* **Backend:** Python 3.9 (FastAPI)
* **Database:** SQLite (Running on Persistent Volume)
* **Frontend:** HTML5 / CSS3 (Cyberpunk Terminal Aesthetic) / Vanilla JS
* **Deployment:** Docker container on Railway.app

## 🚀 Installation (Local Dev)

Want to run your own instance?

1.  **Clone the repo**
    ```bash
    git clone [https://github.com/matakhakhu-coder/beat-the-house.git](https://github.com/matakhakhu-coder/beat-the-house.git)
    cd beat-the-house
    ```

2.  **Install Dependencies**
    ```bash
    pip install fastapi uvicorn
    ```

3.  **Run Server**
    ```bash
    uvicorn main:app --reload
    ```

4.  **Access Terminal**
    Open `http://127.0.0.1:8000` in your browser.

---

## 📂 Project Structure

```text
├── main.py              # Core Game Logic & API Endpoints
├── game.db              # SQLite Database (Stores Vault, Players, Logs)
├── attempts.log         # Security Log (Tracks Grand Solve attempts)
└── static/
    └── index.html       # Single-file Frontend (Terminal UI)
