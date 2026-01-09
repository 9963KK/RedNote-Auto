"""
Hugging Face Papers Crawler Module
Standalone version for LangChain integration.
"""

import asyncio
import aiohttp
import re
import logging
import json
import html
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any
from dataclasses import dataclass
from urllib.parse import urljoin, unquote

import aiofiles
from bs4 import BeautifulSoup

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@dataclass
class CrawlerConfig:
    """Crawler configuration"""
    daily_papers_url: str = "https://hf-mirror.com/papers"
    max_papers: int = 10
    request_delay: float = 1.0
    user_agent: str = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36"

@dataclass
class PaperInfo:
    """Paper information data model"""
    title: str
    authors: List[str]
    abstract: str
    arxiv_id: str
    upvotes: int
    pdf_url: str
    hf_url: str
    arxiv_url: str
    local_pdf_path: Optional[str] = None
    published_date: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "title": self.title,
            "authors": self.authors,
            "abstract": self.abstract,
            "arxiv_id": self.arxiv_id,
            "upvotes": self.upvotes,
            "pdf_url": self.pdf_url,
            "hf_url": self.hf_url,
            "arxiv_url": self.arxiv_url,
            "local_pdf_path": self.local_pdf_path,
            "published_date": self.published_date.isoformat() if self.published_date else None
        }

class HuggingFaceCrawler:
    """Hugging Face Papers Crawler"""
    
    def __init__(self, config: Optional[CrawlerConfig] = None):
        self.config = config or CrawlerConfig()
        self.session: Optional[aiohttp.ClientSession] = None
        
    async def __aenter__(self):
        await self._init_session()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._close_session()
        
    async def _init_session(self):
        timeout = aiohttp.ClientTimeout(total=30)
        headers = {
            "User-Agent": self.config.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        self.session = aiohttp.ClientSession(timeout=timeout, headers=headers)
        
    async def _close_session(self):
        if self.session:
            await self.session.close()
            
    async def _make_request(self, url: str, max_retries: int = 3) -> str:
        if not self.session:
            await self._init_session()

        for attempt in range(max_retries + 1):
            try:
                async with self.session.get(url) as response:
                    if response.status == 200:
                        return await response.text()
                    else:
                        logger.warning(f"Request failed: {url}, status: {response.status}, attempt: {attempt + 1}")
            except Exception as e:
                logger.warning(f"Request error: {url}, error: {e}, attempt: {attempt + 1}")
                
            if attempt < max_retries:
                await asyncio.sleep(2 ** attempt)
                
        raise Exception(f"Failed to fetch {url} after {max_retries} retries")

    def _parse_paper_info(self, paper_data: Dict[str, Any]) -> Optional[PaperInfo]:
        """Parse paper info from JSON data"""
        try:
            paper = paper_data.get('paper', {})
            title = paper.get('title', '')
            if not title:
                return None
            
            authors = []
            for author in paper.get('authors', []):
                if isinstance(author, dict):
                    name = author.get('name', '')
                    if name: authors.append(name)
                elif isinstance(author, str):
                    authors.append(author)
                    
            abstract = paper.get('summary', '') or paper.get('abstract', '')
            arxiv_id = paper.get('id', '')
            upvotes = paper.get('upvotes', 0)
            
            hf_url = f"{self.config.daily_papers_url}/{arxiv_id}" if arxiv_id else ""
            arxiv_url = f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else ""
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf" if arxiv_id else ""
            
            return PaperInfo(
                title=title,
                authors=authors,
                abstract=abstract,
                arxiv_id=arxiv_id,
                upvotes=upvotes,
                pdf_url=pdf_url,
                hf_url=hf_url,
                arxiv_url=arxiv_url
            )
        except Exception as e:
            logger.error(f"Error parsing paper data: {e}")
            return None

    async def fetch_papers(self, top_n: Optional[int] = None) -> List[PaperInfo]:
        """Fetch daily papers"""
        limit = top_n or self.config.max_papers
        logger.info(f"Fetching top {limit} daily papers...")
        
        try:
            html_content = await self._make_request(self.config.daily_papers_url)
            
            # Extract JSON data from SVELTE_HYDRATER
            hydrator_pattern = r'<div[^>]*data-target="DailyPapers"[^>]*data-props="([^"]*)"'
            hydrator_match = re.search(hydrator_pattern, html_content)
            
            if not hydrator_match:
                logger.warning("Could not find DailyPapers data in HTML")
                return []
                
            props_json = html.unescape(unquote(hydrator_match.group(1)))
            data = json.loads(props_json)
            
            papers_data = data.get('dailyPapers', []) if isinstance(data, dict) else data
            
            results = []
            for item in papers_data[:limit]:
                paper_info = self._parse_paper_info(item)
                if paper_info:
                    results.append(paper_info)
                    
            logger.info(f"Successfully fetched {len(results)} papers")
            return results
            
        except Exception as e:
            logger.error(f"Error fetching papers: {e}")
            return []

    async def download_pdf(self, paper: PaperInfo, output_dir: Path) -> PaperInfo:
        """Download PDF for a paper"""
        if not paper.pdf_url:
            return paper
            
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        
        safe_title = re.sub(r'[^\w\s-]', '', paper.title)[:50]
        filename = f"{paper.arxiv_id}_{safe_title}.pdf"
        file_path = output_dir / filename
        
        if file_path.exists():
            paper.local_pdf_path = str(file_path)
            return paper
            
        try:
            if not self.session:
                await self._init_session()
                
            async with self.session.get(paper.pdf_url) as response:
                if response.status == 200:
                    async with aiofiles.open(file_path, 'wb') as f:
                        async for chunk in response.content.iter_chunked(8192):
                            await f.write(chunk)
                    paper.local_pdf_path = str(file_path)
                    logger.info(f"Downloaded PDF: {filename}")
                else:
                    logger.warning(f"Failed to download PDF: {paper.pdf_url} (Status: {response.status})")
        except Exception as e:
            logger.error(f"Error downloading PDF for {paper.title}: {e}")
            
        return paper

# --- LangChain Tool Helper ---

def run_crawler_tool(max_papers: int = 5, download_pdfs: bool = False) -> List[Dict[str, Any]]:
    """
    Synchronous helper function to run the crawler, suitable for LangChain tools.
    """
    async def _run():
        config = CrawlerConfig(max_papers=max_papers)
        async with HuggingFaceCrawler(config) as crawler:
            papers = await crawler.fetch_papers()
            if download_pdfs:
                for paper in papers:
                    await crawler.download_pdf(paper, Path("./data/pdfs"))
                    await asyncio.sleep(1)
            return [p.to_dict() for p in papers]

    return asyncio.run(_run())

if __name__ == "__main__":
    # Test run
    papers = run_crawler_tool(max_papers=3)
    print(json.dumps(papers, indent=2, ensure_ascii=False))
