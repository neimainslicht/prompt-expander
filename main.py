import generatePrompts
import fileUtils
import sys
import os
import questionary
import time

def process_batch_mode(prompt_idea):
    print("\n--- Batch Mode Activation ---")
    
    # 1. Ask for the file path with a built-in default option
    file_path = questionary.text(
        "Enter path to .txt file:",
        default="input/batch.txt"
    ).ask()
    
    # Handle Ctrl+C cancel during the text prompt gracefully
    if file_path is None:
        print("\nBatch mode canceled.")
        input("\nPress Enter to return to main menu...")
        return

    # 2. Check if the file actually exists before loading
    if not os.path.exists(file_path):
        print(f"\n[Error] File not found at: {file_path}")
        input("\nPress Enter to return to main menu...")
        return

    # 3. Load file and Process each line
    print("\n[!] Loading file...")
    print("working on it...\n")
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                clean_line = line.strip()
                if not clean_line: 
                    continue # Skip empty lines
                
                print(f"-> Processing line {line_num}: '{clean_line}'")
                response = generatePrompts.generatePrompt(clean_line)
                print(response)
                if response:
                    parsed = fileUtils.parseJSON(response, prompt_idea)
                    fileUtils.saveJSON(parsed)

                # Sleep for 1.5 seconds in between API calls
                time.sleep(1.5)
                
    except Exception as e:
        print(f"[Error] Failed to read file: {e}")
        input("\nPress Enter to return to main menu...")
        return

    
            
    print("[✓] Processing complete and saved successfully!")
    
    # 5. Return to menu
    input("\nPress Enter to return to main menu...")

def process_image_generation():
    output_files = fileUtils.getOutputFiles()
    
    # Create the numbered list entries
    lines = [f"{i}. {name}" for i, name in enumerate(output_files, start=1)]
    lines = "\n".join(lines)
    
    prompt_file_num = input(f"""Available prompt files:\n\n{lines}\n\nChoose:""").strip()
    if prompt_file_num.isdigit():
        prompt_file_num = int(prompt_file_num)
        if prompt_file_num >= 1 and prompt_file_num <= len(lines):
            prompt_file_path = f"output/{output_files[prompt_file_num-1]}"
            prompt_json_obj = fileUtils.getJSONObject(prompt_file_path)
            prompt_list = fileUtils.getPromptListFromJSON(prompt_json_obj)
            chosen_prompts = input(f"""Available prompts:\n\n{prompt_list}\n\nGenerate images from which prompts? """).strip()
            



def main_menu():
    while True:
        # Clear the terminal
        print("\033[H\033[J", end="") 
        
        # Create the interactive selection menu
        choice = questionary.select(
            "What would you like to do?",
            choices=[
                "Single Idea",
                "Batch Mode",
                "Generate Images From Prompts",
                "Exit"
            ]
        ).ask()

        # Handle the chosen selection
        if choice == "Single Idea":
            prompt_idea = input("Enter a short idea for an image, and the Prompt Expander will transform it into multiple detailed text to image prompts.")

            response = generatePrompts.generatePrompt(prompt_idea)
            print(response)
            if response:
                parsed = fileUtils.parseJSON(response, prompt_idea)
                fileUtils.saveJSON(parsed)
            
        elif choice == "Batch Mode":
            process_batch_mode(prompt_idea)
        
        elif choice == "Generate Images From Prompts":
            process_image_generation()
            
        elif choice == "Exit" or choice is None: # None handles Ctrl+C gracefully
            print("\nGoodbye!")
            sys.exit()
main_menu()






