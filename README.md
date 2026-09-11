# honar_time_bot — consolidated build

Features:
- Separate seat-map message after selecting a show.
- Seat map pagination and <=110 seats per show, numbered continuously by row.
- Multi-seat checkout with one receipt and one admin approval; one QR per seat/ticket.
- Customer first name, last name, and phone stored at registration.
- Receipt sent to all ADMIN_IDS with approve/reject.
- Bank card owner name.
- Event poster upload/display.
- Admin CRUD for events, shows, seats, cards, keywords.
- Admin QR scanning with atomic one-time consumption and manual code fallback.
- Existing SQLite databases are migrated without deleting data.

Railway variables:
- BOT_TOKEN
- ADMIN_IDS (comma-separated Telegram numeric user IDs)
- DB_PATH (optional)
- HOLD_MINUTES (optional)

Start command: `worker: python bot.py`
