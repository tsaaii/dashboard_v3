"""
Hardcoded credentials for the dashboard.

To change credentials:
    1. Pick a new username.
    2. Generate a new password hash:
           >>> from werkzeug.security import generate_password_hash
           >>> generate_password_hash("your_new_password")
       Paste the result into PASSWORD_HASH below.
    3. Restart the app.

Default credentials below: admin / haritha2026
"""
from __future__ import annotations

from werkzeug.security import check_password_hash


# ---- Edit me to change credentials ----
USERNAME = "admin"
PASSWORD_HASH = (
"scrypt:32768:8:1$bHc2khMSZZnVS1BY$04bf51cf4894944eec0a6c835a50d436b660c4162cd53903e5b2f4175bed4166ec4ba3cad511022016a56611dac01c46c52bc1ca0e052ecf444188a075314fcb"
)


def verify_credentials(username: str, password: str) -> bool:
    """True if (username, password) match the configured credentials.

    Username comparison is case-insensitive and whitespace-trimmed.
    """
    if not username or not password:
        return False
    if username.strip().lower() != USERNAME.lower():
        return False
    try:
        return check_password_hash(PASSWORD_HASH, password)
    except Exception:
        return False
