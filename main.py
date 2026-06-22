import generatePrompts

prompt_idea = input("Enter a short idea for an image, and the Prompt Expander will transform it into multiple detailed text to image prompts.")

response = generatePrompts.generatePrompt(prompt_idea)
print(response)
