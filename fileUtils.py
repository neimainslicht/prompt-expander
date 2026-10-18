import json
import os
import re
from datetime import datetime
from pathlib import Path

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
    timestamp = jsonObj["timestamp"].replace('T', '_').replace(':', '-')

    concept = jsonObj["concept"].strip().lower()
    concept = re.sub(r'[^a-zA-Z0-9\s]', "", concept)
    concept = re.sub(r'\s+', "-", concept)

    os.makedirs("output", exist_ok=True)

    filename = f"output/{concept}_{timestamp}.json"


    with open(filename, "w", encoding="utf-8") as f:
        json.dump(jsonObj, f, indent=2, ensure_ascii=False)
    return filename

def updatePromptStatus(jsonObj, prompt_id, status):
    prompts = jsonObj["prompts"]
    for prompt in prompts:
        if prompt["id"] == prompt_id:
            prompt["status"] = status
            return

def getOutputFiles():
    folder_path = Path('output')

    # .name extracts just the filename string from the path object
    files = [f.name for f in folder_path.glob('*.json')]
    return files
def getJSONObject(path):
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as file:
            jsonObj = json.load(file)
            return jsonObj
    except (FileNotFoundError, json.JSONDecodeError):
        return None
def saveImage(image, concept, prompt_id):
    if image:
        concept = concept.strip().lower()
        concept = re.sub(r'[^a-zA-Z0-9\s]', "", concept)
        concept = re.sub(r'[\s]', "-", concept)

        generation_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        os.makedirs("output/images", exist_ok=True)
        base = f"output/images/{concept}-prompt-{prompt_id}-{generation_timestamp}"
        filename = f"{base}.png"
        # the timestamp only has 1s resolution, so never overwrite an earlier image from a quick rerun
        suffix = 2
        while os.path.exists(filename):
            filename = f"{base}-{suffix}.png"
            suffix += 1

        image.save(filename, format="PNG")
        return filename
    return None


    


