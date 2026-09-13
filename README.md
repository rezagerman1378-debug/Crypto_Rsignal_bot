# Early Pump + Dump Scanner V2

نسخه محافظه‌کار برای Futures روی Bitunix و Toobit.

دو نوع هشدار:
- 🟢 EARLY PUMP
- 🔴 EARLY DUMP

فیلترها:
Volume Spike، Price Momentum، Order-Book Imbalance، و در Toobit: Open Interest و Funding.
سیستم حرکت‌هایی را که بیش از حد از قبل کشیده شده‌اند جریمه می‌کند تا تا حد امکان دنبال «شروع حرکت» باشد، نه تعقیب پامپ/دامپ انجام‌شده.

نصب:
1. Python 3.10+
2. به @BotFather در Telegram برو و /newbot را اجرا کن.
3. Token را بگیر.
4. به ربات خودت پیام بده.
5. Chat ID را از getUpdates پیدا کن.
6. `.env.example` را به `.env` تغییر بده و مقادیر را وارد کن.
7. `pip install -r requirements.txt`
8. `python scanner.py`

این نسخه کلید API صرافی نمی‌خواهد و هیچ معامله‌ای انجام نمی‌دهد.
هشدارها سیگنال احتمالی‌اند و تضمین دامپ/پامپ نیستند.
