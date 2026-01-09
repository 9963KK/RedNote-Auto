import os
import logging
import time
import random
import json
from typing import List, Dict, Any, Optional
from openai import OpenAI

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class OpenAIClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model_name: Optional[str] = None,
        vision_api_key: Optional[str] = None,
        vision_base_url: Optional[str] = None,
    ):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL")
        self.model_name = model_name or os.getenv("OPENAI_MODEL") or "gpt-5.2"
        self.vision_api_key = vision_api_key or os.getenv("QWENVL_API_KEY")
        self.vision_base_url = vision_base_url or os.getenv("QWENVL_BASE_URL")
        
        if not self.api_key:
            raise ValueError("OpenAI API Key is required. Set OPENAI_API_KEY env var or pass it to constructor.")

        timeout_s = float(os.getenv("LLM_REQUEST_TIMEOUT_S", "45"))
        max_retries = int(os.getenv("LLM_MAX_RETRIES", "2"))
        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=timeout_s, max_retries=max_retries)

    @staticmethod
    def _parse_json_from_text(text: str) -> Dict[str, Any]:
        cleaned = (text or "").strip()
        if not cleaned:
            raise ValueError("empty output_text")

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            # Common cases: JSON wrapped in markdown fences or with extra prose.
            # Best-effort extraction: take the first {...} block.
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end != -1 and end > start:
                candidate = cleaned[start : end + 1]
                return json.loads(candidate)
            raise

    def analyze_pdf(self, pdf_url: str) -> Dict[str, Any]:
        """
        Sends the PDF URL to OpenAI using the new Responses API.
        """
        logger.info("Sending PDF URL to OpenAI (Responses API): %s", pdf_url)

        prompt = (
            "You are an expert AI researcher. Give a concise, publication-ready summary.\n"
            "Focus on: (1) what problem the paper tackles, (2) the core method/architecture,\n"
            "(3) the main result/metric improvement. Keep it tight (<= 120 words).\n"
            "Also provide a one-sentence description of the core architecture diagram.\n\n"
            "Return ONLY valid JSON (no markdown, no backticks, no extra keys):\n"
            '{ "summary": "...", "diagram_description": "..." }'
        )

        max_attempts = 3
        last_err: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                response = self.client.responses.create(
                    model=self.model_name,
                    input=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": prompt},
                                {"type": "input_file", "file_url": pdf_url},
                            ],
                        },
                    ],
                )

                content = getattr(response, "output_text", "") or ""
                return self._parse_json_from_text(content)
            except Exception as e:
                last_err = e
                logger.warning(
                    "OpenAI analysis attempt %d/%d failed: %s",
                    attempt,
                    max_attempts,
                    e,
                )
                if attempt < max_attempts:
                    sleep_s = min(2 ** (attempt - 1), 8) + random.random() * 0.25
                    time.sleep(sleep_s)

        logger.error("OpenAI Analysis Failed after %d attempts: %s", max_attempts, last_err)
        return {"error": str(last_err) if last_err else "unknown error"}

    def select_best_image(
        self,
        images: List[Dict[str, Any]],
        description: str,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Sends extracted extracted images (top N) to a Vision Model to pick the best match for the description.
        """
        if not images:
            return None
            
        target_model = model or self.model_name
        logger.info(f"Selecting best image from {len(images)} candidates using {target_model}...")

        vision_api_key = api_key or self.vision_api_key
        vision_base_url = base_url or self.vision_base_url
        if not vision_api_key or not vision_base_url:
            logger.error("Vision API credentials missing (QWENVL_API_KEY/QWENVL_BASE_URL).")
            return None

        try:
            timeout_s = float(os.getenv("LLM_REQUEST_TIMEOUT_S", "45"))
            max_retries = int(os.getenv("LLM_MAX_RETRIES", "2"))
            vision_client = OpenAI(api_key=vision_api_key, base_url=vision_base_url, timeout=timeout_s, max_retries=max_retries)
        except Exception:
            vision_client = OpenAI(api_key=vision_api_key, base_url=vision_base_url)
        
        # Prepare content parts
        content_parts = [
            {"type": "text", "text": f"Find the image that best matches this description of a Core Architecture Diagram:\n\n{description}\n\nReturn the index of the best image and a reason."}
        ]
        
        # Add images
        valid_images = []
        # MinerU images are usually saved to disk, so we read them or use base64 if available in memory?
        # In current flow, we will likely have them on disk from MinerU.
        # But wait, MinerUClient returns "data_base64" in its dict if we used process_batch!
        # Let's check mineru_client.py. Yes, it returns "data_base64".
        # If we reload from disk, we need to read them.
        # Let's support both: if 'data_base64' is present, use it. Else read from 'path'.
        
        import base64
        
        for i, img in enumerate(images):
            try:
                b64 = img.get('data_base64')
                if not b64 and img.get('path'):
                     with open(img['path'], "rb") as f:
                        b64 = base64.b64encode(f.read()).decode('utf-8')
                
                if b64:
                    content_parts.append({
                        "type": "text",
                        "text": f"Image {i}:"
                    })
                    content_parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"} # Assuming JPEG/PNG compat
                    })
                    valid_images.append(img)
            except Exception as e:
                logger.warning(f"Could not prepare image {i}: {e}")

        if not valid_images:
            return None

        # Use Chat Completion for Vision (QwenVL usually compatible with Chat API)
        messages = [
            {"role": "user", "content": content_parts}
        ]
        
        try:
            response = vision_client.chat.completions.create(
                model=target_model,
                messages=messages,
                max_tokens=300
            )
            
            result_text = response.choices[0].message.content
            logger.info(f"Vision Decision: {result_text}")
            
            return {"decision_text": result_text, "candidates_count": len(valid_images)}

        except Exception as e:
            logger.error(f"Vision Selection Failed: {e}")
            return {"error": str(e)}

if __name__ == "__main__":
    pass
