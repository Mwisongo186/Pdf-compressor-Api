import requests

API_URL = "https://YOUR-RENDER-SERVICE.onrender.com/compress"
API_KEY = ""  # Optional

input_pdf = "input.pdf"
target_kb = 500

headers = {}
if API_KEY:
    headers["X-API-Key"] = API_KEY

with open(input_pdf, "rb") as f:
    response = requests.post(
        API_URL,
        headers=headers,
        files={"file": ("input.pdf", f, "application/pdf")},
        data={"target_kb": str(target_kb), "exact_size": "true"},
        timeout=240,
    )

if response.status_code != 200:
    print("Error:", response.status_code, response.text)
    raise SystemExit(1)

with open("compressed.pdf", "wb") as out:
    out.write(response.content)

print("Saved compressed.pdf")
print("Target reached:", response.headers.get("X-Target-Reached"))
print("Compressed bytes:", response.headers.get("X-Compressed-Bytes"))
