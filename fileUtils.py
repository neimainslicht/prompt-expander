import json
def saveJSON(text):
    cleanText = text.replace("```json", "").replace("```", "").strip()
    jsonObj = json.loads(cleanText)
    timestamp = jsonObj["timestamp"]
    with open(f"output/prompt-expander-{timestamp}.json", "w") as f:
        f.write(cleanText)