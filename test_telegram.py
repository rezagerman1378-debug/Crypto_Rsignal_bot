import os
import requests

token = os.getenv("TELEGRAM_BOT_TOKEN")
chat_id = os.getenv("TELEGRAM_CHAT_ID")

url = f"https://api.telegram.org/bot{token}/sendMessage"

r = requests.post(
    url,
    json={
        "chat_id": chat_id,
        "text": "✅ تست ربات انجام شد؛ اتصال تلگرام درست است."
    },
    timeout=10
)

print(r.status_code)
print(r.text)
