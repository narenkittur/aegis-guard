import os
from google import genai

client = genai.Client(
    http_options={'headers': {'x-goog-api-key': "AQ.testkey"}}
)
print("Client initialized without api_key parameter")
