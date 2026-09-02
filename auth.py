import gspread
from oauth2client.service_account import ServiceAccountCredentials

PLANILHA_ID = "1wy8i8nuUkBFezSeySfnnPw06TVLxhwaXrwEGIoxOewU"

scope = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

creds = ServiceAccountCredentials.from_json_keyfile_name("chaves.json", scope)
gc = gspread.authorize(creds)

plan = gc.open_by_key(PLANILHA_ID)

ws = plan.add_worksheet(title="TESTE_BOT", rows=10, cols=5)
ws.update("A1", [["OK", "FUNCIONANDO"]])

print("SUCESSO")