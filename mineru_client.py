import os
import requests
import time
import zipfile
import io
import base64
import logging
from typing import List, Optional

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# MinerU defaults (can be overridden via env)
DEFAULT_MODEL_VERSION = os.getenv("MINERU_MODEL_VERSION", "pipeline")
DEFAULT_ENABLE_FORMULA = os.getenv("MINERU_ENABLE_FORMULA", "true").lower() == "true"
DEFAULT_ENABLE_TABLE = os.getenv("MINERU_ENABLE_TABLE", "true").lower() == "true"
DEFAULT_LANGUAGE = os.getenv("MINERU_LANGUAGE", "ch")
DEFAULT_IS_OCR = os.getenv("MINERU_IS_OCR", "false").lower() == "true"

class MinerUClient:
    def __init__(
        self,
        api_token: str,
        model_version: str = DEFAULT_MODEL_VERSION,
        enable_formula: bool = DEFAULT_ENABLE_FORMULA,
        enable_table: bool = DEFAULT_ENABLE_TABLE,
        language: str = DEFAULT_LANGUAGE,
        is_ocr: bool = DEFAULT_IS_OCR,
    ):
        self.api_token = api_token.strip()  # Ensure no whitespace
        if len(self.api_token) > 10:
            logger.info(f"MinerUClient initialized with token: {self.api_token[:10]}...")
        else:
            logger.warning("MinerUClient initialized with short/empty token.")

        self.base_url = "https://mineru.net/api/v4"
        self.headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }
        self.model_version = model_version
        self.enable_formula = enable_formula
        self.enable_table = enable_table
        self.language = language
        self.is_ocr = is_ocr

    def process_pdf(self, pdf_url: str, max_polling_retries: int = 36) -> dict:
        """
        Full process: Submit URL -> Poll for completion -> Download & Extract
        
        Args:
            pdf_url: The URL of the PDF to process
            max_polling_retries: Number of polling attempts (default 36 * 5s = 3 mins)
            
        Returns:
            dict: {
                "status": "success" | "error" | "timeout",
                "markdown": str,
                "images": List[dict],
                "error": str
            }
        """
        if not pdf_url:
            return {"status": "error", "error": "No PDF URL provided", "markdown": "", "images": []}

        # 1. Submit Task
        try:
            logger.info(f"Submitting task for: {pdf_url}")
            submit_payload = {
                "url": pdf_url,
                "model_version": self.model_version,
                "is_ocr": self.is_ocr,
                "enable_formula": self.enable_formula,
                "enable_table": self.enable_table,
                "language": self.language,
            }
            res = requests.post(
                f"{self.base_url}/extract/task",
                headers=self.headers,
                json=submit_payload,
                timeout=30,
            )
            res_data = res.json()

            if res_data.get("code") != 0:
                return {"status": "error", "error": f"Submit Failed: {res_data.get('msg')}", "markdown": "", "images": []}

            task_id = res_data["data"]["task_id"]
            logger.info(f"Task submitted, ID: {task_id}")

        except Exception as e:
            return {"status": "error", "error": f"Submit Exception: {str(e)}", "markdown": "", "images": []}

        # 2. Poll for Completion
        full_zip_url = None
        for _ in range(max_polling_retries):
            time.sleep(5)
            try:
                res = requests.get(f"{self.base_url}/extract/task/{task_id}", headers=self.headers, timeout=10)
                data = res.json().get("data", {})
                state = data.get("state")
                
                if state == "done":
                    full_zip_url = data.get("full_zip_url")
                    break
                elif state == "failed":
                    return {"status": "error", "error": f"MinerU Failed: {data.get('err_msg')}", "markdown": "", "images": []}
            except Exception as e:
                logger.warning(f"Polling error: {str(e)}")
                continue
        
        if not full_zip_url:
            return {"status": "timeout", "error": "Processing Timed Out", "markdown": "", "images": []}

        # 3. Download and Extract Result
        try:
            logger.info("Downloading result ZIP...")
            zip_resp = requests.get(full_zip_url, timeout=60)
            
            markdown_content = ""
            extracted_images = []
            
            with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as z:
                # Extract Markdown
                md_file = next((n for n in z.namelist() if n.endswith('.md')), None)
                if md_file:
                    markdown_content = z.read(md_file).decode('utf-8')
                
                # Extract Images (Top 5)
                image_files = sorted([n for n in z.namelist() if n.startswith('images/') and n.lower().endswith(('.jpg', '.png', '.jpeg'))])
                
                for img_file in image_files[:5]:
                    img_data = z.read(img_file)
                    if len(img_data) > 5120: # Skip small icons < 5KB
                        b64_str = base64.b64encode(img_data).decode('utf-8')
                        mime_type = "image/png" if img_file.lower().endswith('.png') else "image/jpeg"
                        
                        extracted_images.append({
                            "name": img_file.split('/')[-1],
                            "mime_type": mime_type,
                            "data_base64": b64_str
                        })
            
            return {
                "status": "success",
                "markdown": markdown_content[:50000], 
                "images": extracted_images,
                "image_count": len(extracted_images)
            }

        except Exception as e:
             return {"status": "error", "error": f"Zip Process Error: {str(e)}", "markdown": "", "images": []}

    def process_batch(self, pdf_urls: List[str], max_polling_retries: int = 60) -> List[dict]:
        """
        Batch process PDFs.
        """
        if not pdf_urls:
             return []

        # 1. Submit Batch
        try:
            logger.info(f"Submitting batch of {len(pdf_urls)} URLs...")
            files_payload = [{"url": url, "data_id": str(i)} for i, url in enumerate(pdf_urls)]
            submit_payload = {
                "files": files_payload,
                "model_version": self.model_version,
                "enable_formula": self.enable_formula,
                "enable_table": self.enable_table,
                "language": self.language,
                "is_ocr": self.is_ocr,
            }
            res = requests.post(
                f"{self.base_url}/extract/task/batch",
                headers=self.headers,
                json=submit_payload,
                timeout=30,
            )
            res_data = res.json()
            
            if res_data.get("code") != 0:
                logger.error(f"Batch Submit Failed: {res_data.get('msg')}")
                return [{"status": "error", "error": res_data.get("msg")} for _ in pdf_urls]
                
            batch_id = res_data["data"]["batch_id"]
            logger.info(f"Batch submitted, ID: {batch_id}")
            
        except Exception as e:
            logger.error(f"Batch Submit Exception: {e}")
            return [{"status": "error", "error": str(e)} for _ in pdf_urls]

        # 2. Poll Batch Status
        # We need to wait until ALL tasks are done or failed
        last_log_state = ""
        
        for attempt in range(max_polling_retries):
            time.sleep(5)
            try:
                res = requests.get(f"{self.base_url}/extract-results/batch/{batch_id}", headers=self.headers, timeout=10)
                data = res.json().get("data", {})
                extract_results = data.get("extract_result", [])
                
                # Check metrics
                done_count = 0
                failed_count = 0
                total = len(extract_results)
                
                for item in extract_results:
                    state = item.get("state")
                    if state == "done":
                        done_count += 1
                    elif state == "failed":
                        failed_count += 1
                
                completed_count = done_count + failed_count
                pending_count = total - completed_count
                
                # Log logic: Log if status CHANGED or every 6th iteration (30s)
                current_state_str = f"{completed_count}/{total}"
                if current_state_str != last_log_state or attempt % 6 == 0:
                    logger.info(f"Batch Status: {completed_count}/{total} completed ({done_count} success, {failed_count} failed)")
                    last_log_state = current_state_str
                
                if pending_count == 0:
                    # All done, process results
                    logger.info("Batch processing finished.")
                    return self._process_batch_results(extract_results)
                    
            except Exception as e:
                logger.warning(f"Batch Polling error: {str(e)}")
                continue

        logger.warning(f"Batch Processing Timed Out after {max_polling_retries*5} seconds. Returning partial results.")
        return self._process_batch_results(extract_results)

    def _process_batch_results(self, extract_results: List[dict]) -> List[dict]:
        """Download and extract ZIPs for completed batch items."""
        processed_outputs = []
        
        # Sort by data_id to maintain order if possible, or just return list
        # data_id was set to index str(i)
        
        # Map by data_id for reordering
        result_map = {item.get("data_id"): item for item in extract_results}
        
        # We assume data_id was '0', '1', '2'... matching input list
        for i in range(len(extract_results)):
            item = result_map.get(str(i))
            if not item:
                processed_outputs.append({"status": "error", "error": "Missing result"})
                continue
                
            if item.get("state") == "done":
                # Reuse the download logic? Setup a mini structure to reuse code?
                # Or just duplicate/helper extract logic. 
                # Let's use a helper for download.
                dl_res = self._download_and_extract(item.get("full_zip_url"))
                processed_outputs.append(dl_res)
            else:
                 processed_outputs.append({"status": "error", "error": item.get("err_msg")})
                 
        return processed_outputs

    def _download_and_extract(self, zip_url: str) -> dict:
        """Helper to download ZIP and extract content."""
        if not zip_url:
             return {"status": "error", "error": "No ZIP URL"}
             
        try:
            # logger.info(f"Downloading ZIP: {zip_url}")
            zip_resp = requests.get(zip_url, timeout=60)
            
            markdown_content = ""
            extracted_images = []
            
            with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as z:
                # Extract Markdown
                md_file = next((n for n in z.namelist() if n.endswith('.md')), None)
                if md_file:
                    markdown_content = z.read(md_file).decode('utf-8')
                
                # Extract Images (Top 5)
                image_files = sorted([n for n in z.namelist() if n.startswith('images/') and n.lower().endswith(('.jpg', '.png', '.jpeg'))])
                
                for img_file in image_files[:5]:
                    img_data = z.read(img_file)
                    if len(img_data) > 5120:
                        b64_str = base64.b64encode(img_data).decode('utf-8')
                        mime_type = "image/png" if img_file.lower().endswith('.png') else "image/jpeg"
                        
                        extracted_images.append({
                            "name": img_file.split('/')[-1],
                            "mime_type": mime_type,
                            "data_base64": b64_str
                        })
            
            return {
                "status": "success",
                "markdown": markdown_content[:50000],
                "images": extracted_images,
                "image_count": len(extracted_images)
            }
        except Exception as e:
            return {"status": "error", "error": f"Download Error: {str(e)}"}

# Helper for LangChain Tool
def run_mineru_tool(pdf_url: str, api_token: str, **kwargs) -> dict:
    client = MinerUClient(api_token, **kwargs)
    return client.process_pdf(pdf_url)

def run_batch_mineru_tool(pdf_urls: List[str], api_token: str, **kwargs) -> List[dict]:
    client = MinerUClient(api_token, **kwargs)
    return client.process_batch(pdf_urls)
