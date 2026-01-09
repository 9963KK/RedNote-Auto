import fitz  # PyMuPDF
import os
import io
import logging
from typing import List, Dict, Any

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class LocalPDFExtractor:
    def __init__(self):
        pass

    def extract_images(self, pdf_path: str, output_dir: str, min_size: int = 2048) -> List[Dict[str, Any]]:
        """
        Extract images from a local PDF file.
        
        Args:
            pdf_path: Path to the PDF file.
            output_dir: Directory to save extracted images.
            min_size: Minimum file size in bytes to keep (filter out small icons).
            
        Returns:
            List of dicts containing image path and metadata.
        """
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
            
        doc = fitz.open(pdf_path)
        extracted_images = []
        
        logger.info(f"Extracting images from {pdf_path} (Pages: {len(doc)})...")
        
        for page_index in range(len(doc)):
            page = doc[page_index]
            image_list = page.get_images(full=True)
            
            for img_index, img in enumerate(image_list):
                xref = img[0]
                base_image = doc.extract_image(xref)
                image_bytes = base_image["image"]
                image_ext = base_image["ext"]
                
                if len(image_bytes) < min_size:
                    continue
                
                image_filename = f"page{page_index + 1}_img{img_index + 1}.{image_ext}"
                image_filepath = os.path.join(output_dir, image_filename)
                
                with open(image_filepath, "wb") as f:
                    f.write(image_bytes)
                    
                extracted_images.append({
                    "path": image_filepath,
                    "page": page_index + 1,
                    "size": len(image_bytes),
                    "ext": image_ext,
                    "xref": xref
                })
                
        logger.info(f"Extracted {len(extracted_images)} images to {output_dir}")
        return extracted_images

if __name__ == "__main__":
    # Test
    # extract_images("test.pdf", "output/test_images")
    pass
