import json
import re

def saveJSON(text):
    cleanText = text.replace("```json", "").replace("```", "").strip()
    jsonObj = json.loads(cleanText)
    timestamp = jsonObj["timestamp"]
    concept = jsonObj["concept"].strip().lower()
    concept = re.sub(r'[^a-zA-Z0-9\s]', "", concept)
    concept = re.sub(r'[\s]', "-", concept)


    with open(f"output/{concept}-{timestamp}.json", "w") as f:
        f.write(cleanText)
