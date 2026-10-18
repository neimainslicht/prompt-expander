import os
import sys

import questionary

import services


def show_expand_result(result):
    if result.ok:
        for prompt in result.data["prompts"]:
            print(f"  {prompt['id']}. {prompt['text']}")
        print(f"[✓] Saved to {result.prompt_file}")
    else:
        print(f"[Error] {result.error}")


def process_single_idea():
    prompt_idea = input("Enter a short idea for an image, and the Prompt Expander will transform it into multiple detailed text to image prompts.")
    show_expand_result(services.expand_idea(prompt_idea))
    input("\nPress Enter to return to main menu...")


def process_batch_mode():
    print("\n--- Batch Mode Activation ---")

    file_path = questionary.text(
        "Enter path to .txt file:",
        default="input/batch.txt"
    ).ask()

    # Handle Ctrl+C cancel during the text prompt gracefully
    if file_path is None:
        print("\nBatch mode canceled.")
        input("\nPress Enter to return to main menu...")
        return

    if not os.path.exists(file_path):
        print(f"\n[Error] File not found at: {file_path}")
        input("\nPress Enter to return to main menu...")
        return

    print("\n[!] Loading file...")
    print("working on it...\n")

    try:
        ideas = services.read_ideas_file(file_path)
    except Exception as e:
        print(f"[Error] Failed to read file: {e}")
        input("\nPress Enter to return to main menu...")
        return

    def report(idea, result):
        if result is None:
            print(f"-> Processing: '{idea}'")
        else:
            show_expand_result(result)

    results = services.expand_ideas(ideas, on_progress=report)

    failed = sum(1 for r in results if not r.ok)
    print(f"[✓] Processing complete: {len(results) - failed} saved, {failed} failed.")
    input("\nPress Enter to return to main menu...")


def process_image_generation():
    output_files = services.list_prompt_files()
    if not output_files:
        input("\nNo prompt files found. Press Enter to return to main menu...")
        return
    prompt_file = questionary.select(
        "Choose a File:",
        choices=output_files
    ).ask()
    if prompt_file is None:
        return
    prompt_data = services.load_prompt_file(prompt_file)
    if prompt_data is None:
        input("\nCould not read that file. Press Enter to return to main menu...")
        return

    # user selects one or more prompts to generate images from
    choices_list = [questionary.Choice(title=item["text"], value=item["id"]) for item in prompt_data["prompts"]]
    chosen_ids = questionary.checkbox(
        "Generate images from which prompts?",
        choices=choices_list
    ).ask()

    def report(prompt_id, result):
        if result is None:
            print(f"\nGenerating image for prompt {prompt_id}...")
        elif result.ok:
            print(f"Saved image for prompt {prompt_id}: {result.image_path}")
        else:
            print(f"\nFailed to generate image for prompt {prompt_id}: {result.error}")

    services.generate_images(prompt_data, chosen_ids or [], on_progress=report)

    input("\nPress Enter to return to main menu...")


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

        if choice == "Single Idea":
            process_single_idea()

        elif choice == "Batch Mode":
            process_batch_mode()

        elif choice == "Generate Images From Prompts":
            process_image_generation()

        elif choice == "Exit" or choice is None:  # None handles Ctrl+C gracefully
            print("\nGoodbye!")
            sys.exit()


if __name__ == "__main__":
    main_menu()
