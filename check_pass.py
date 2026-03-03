import hashlib, json

with open("password.json") as f:
    stored = json.load(f)["password"]

password_try = "rutland!2025_secure"
hashed = hashlib.sha256(password_try.encode()).hexdigest()

print("Expected :", stored)
print("Entered  :", hashed)
print("Match?   :", stored == hashed)

