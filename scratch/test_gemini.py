import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google import genai
from core.config import settings

def test_gemini():
    api_key = settings.GEMINI_API_KEY or settings.GOOGLE_API_KEY
    print(f"Using model: {settings.GEMINI_MODEL}")
    print(f"API key prefix: {api_key[:10] if api_key else 'None'}...")
    
    client = genai.Client(api_key=api_key)
    try:
        response = client.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents="Say hello in one word."
        )
        print(f"Response: {response.text}")
    except Exception as e:
        print(f"Error calling Gemini: {e}")

if __name__ == "__main__":
    test_gemini()
