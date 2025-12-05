import requests

BACKEND_URL = "https://spin-api-pba3.onrender.com"

def save_user_settings(phone, name, description, upi_id):
    try:
        payload = {
            "phone": phone,
            "name": name,
            "description": description,
            "upi_id": upi_id,
        }
        response = requests.post(f"{BACKEND_URL}/save-settings/", json=payload, timeout=5)
        return response.status_code == 200
    except Exception as e:
        print(f"[ERROR] Failed to save settings: {e}")
        return False
