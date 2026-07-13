import json
import re
from datetime import datetime

def parseJSON(api_response, prompt_idea):
    data = json.loads(api_response)
    output = {
    "timestamp": datetime.now().isoformat(timespec="seconds"),
    "concept": prompt_idea,
    "prompts": []
    }

    for i, prompt in enumerate(data["prompts"], start=1):
        output["prompts"].append({
            "id": i,
            "text": prompt["text"],
            "status": "not generated"
        })
    return output

def saveJSON(jsonObj):
    timestamp = jsonObj["timestamp"].replace('T','_').replace(':','-')

    concept = jsonObj["concept"].strip().lower()
    concept = re.sub(r'[^a-zA-Z0-9\s]', "", concept)
    concept = re.sub(r'[\s]', "-", concept)
    filename = f"output/{concept}_{timestamp}.json"

    with open(filename, "w", encoding="utf-8") as f:
        json.dump(jsonObj, f, indent=2, ensure_ascii=False)

