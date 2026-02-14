import time
import requests

URL = "http://127.0.0.1:8000/play"
USER_ID = "Matamela_Sniper"

print(f"Targeting: {URL}")
print("Waiting for the anomaly (second ends in 7)...")

while True:
    # Get current time
    t = time.time()
    seconds_str = str(int(t))
    
    # Check if the last digit is 7
    if seconds_str[-1] == '7':
        print(f"[*] TARGET ACQUIRED! Firing at {seconds_str}...")
        
        try:
            response = requests.post(URL, json={"user_id": USER_ID})
            data = response.json()
            
            if data["outcome"] == "WIN":
                print(f"!!! SUCCESS !!! Payout: R{data['payout']}")
                print(f"New Vault Balance: R{data['vault_balance']}")
                break # Stop after one win
            else:
                print(f"[-] Missed. Server clock might be slightly off. Retrying...")
        except Exception as e:
            print(f"Error: {e}")
            break
            
    # Sleep a tiny bit to avoid melting the CPU
    time.sleep(0.1)