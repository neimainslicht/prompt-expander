import generatePrompts
import fileUtils
import sys
import questionary

def main_menu():
    while True:
        # Clear the terminal for a cleaner look (optional)
        print("\033[H\033[J", end="") 
        
        # Create the interactive selection menu
        choice = questionary.select(
            "What would you like to do?",
            choices=[
                "Single Idea",
                "Batch Mode",
                "Exit"
            ]
        ).ask()

        # Handle the chosen selection
        if choice == "Single Idea":
            prompt_idea = input("Enter a short idea for an image, and the Prompt Expander will transform it into multiple detailed text to image prompts.")

            response = generatePrompts.generatePrompt(prompt_idea)
            print(response)
            if response:
                fileUtils.saveJSON(response)
            
        elif choice == "Batch Mode":
            print("\nNot implemented yet.")
            input("\nPress Enter to return to menu...")
            
        elif choice == "Exit" or choice is None: # None handles Ctrl+C gracefully
            print("\nGoodbye!")
            sys.exit()
main_menu()




