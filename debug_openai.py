import openai
print(f"OpenAI Version: {openai.__version__}")
client = openai.OpenAI(api_key="test")
print(f"Has 'responses'? {hasattr(client, 'responses')}")
print(f"Has 'beta'? {hasattr(client, 'beta')}")
if hasattr(client, 'beta'):
    print(f"Has 'beta.responses'? {hasattr(client.beta, 'responses')}")
print("Dir of client:", dir(client))
