import os
import json
import logging
import asyncio
import re
import time
from typing import List, Dict, Any, Optional, Callable
import requests
import fitz  # PyMuPDF
from openai import OpenAI
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from langchain_core.tools import StructuredTool
from langchain_core.runnables import RunnableLambda

# Import our custom modules
from crawler import run_crawler_tool
from openai_client import OpenAIClient

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Configuration ---
PIPELINE_MODE = "hybrid"  # kept for compatibility; hybrid now means OpenAI + first-page snapshot

# Load Config
config_path = "openai_config.env"
if os.path.exists(config_path):
    with open(config_path, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                key, sep, val = line.partition("=")
                if key and val:
                    os.environ[key.strip()] = val.strip()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.2")
QWENVL_MODEL = os.getenv("QWENVL_MODEL", "qwen-vl-plus")
QWENVL_API_KEY = os.getenv("QWENVL_API_KEY", "")
QWENVL_BASE_URL = os.getenv("QWENVL_BASE_URL", "")
OPENAI_CONCURRENCY = int(os.getenv("OPENAI_CONCURRENCY", "3"))
IMAGE_CONCURRENCY = int(os.getenv("IMAGE_CONCURRENCY", "2"))
COVER_ZOOM = float(os.getenv("COVER_ZOOM", "1.5"))
LLM_REQUEST_TIMEOUT_S = float(os.getenv("LLM_REQUEST_TIMEOUT_S", "45"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "2"))

# Hardcoded MinerU Token
# (Not used; MinerU flow removed)

# DeepSeek for translation
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "DeepSeek-V3")
DEEPSEEK_CONCURRENCY = int(os.getenv("DEEPSEEK_CONCURRENCY", "3"))
DEEPSEEK_REASONING_EFFORT = os.getenv("DEEPSEEK_REASONING_EFFORT", "none").lower().strip()

# MCP publish (xiaohongshu) config
XHS_MCP_URL = os.getenv("XHS_MCP_URL", "http://1.13.18.167:18060/mcp")
ENABLE_MCP_PUBLISH = os.getenv("ENABLE_MCP_PUBLISH", "false").lower() in ("1", "true", "yes")

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_DEFAULT_XHS_TAGS = ["AI论文", "论文速览", "科研"]
_MAX_XHS_TAGS = 10
_MIN_XHS_TAG_LEN = 2
_MAX_XHS_TAG_LEN = 16
_GENERIC_XHS_TAGS = {
    "生活",
    "日常",
    "学习",
    "分享",
    "成长",
    "打卡",
    "干货",
    "必看",
    "推荐",
    "笔记",
    "教程",
    "攻略",
    "经验",
    "总结",
    "提升",
    "记录",
    "工具",
}


class WorkflowCancelled(Exception):
    pass


def strip_links(text: str) -> str:
    """
    Remove URLs and link labels from a one-liner.
    Xiaohongshu payload renders link on its own line, so summaries should not include URLs.
    """
    if not text:
        return ""

    cleaned = text.strip()
    cleaned = _URL_RE.sub("", cleaned)
    cleaned = re.sub(r"(?:链接|link)\s*[:：]\s*", "", cleaned, flags=re.IGNORECASE)
    # Keep newlines intact here so downstream can reconstruct sentences from multi-line outputs.
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = "\n".join(line.strip() for line in cleaned.splitlines()).strip()
    cleaned = cleaned.rstrip("。；;，,：:|/-").strip()
    return cleaned


def normalize_one_liner(text: str, *, max_chars: Optional[int] = None) -> str:
    """
    Force a single-line Chinese one-liner.
    - remove URLs / '链接:' markers
    - remove common reasoning blocks (best-effort)
    - join lines (models may insert newlines mid-sentence/word)
    - keep only the first sentence-like chunk
    - optionally truncate to max_chars (by Python chars)
    """
    cleaned = strip_links(text)
    if not cleaned:
        return ""

    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"```.*?```", "", cleaned, flags=re.DOTALL)

    lines = [ln.strip() for ln in cleaned.split("\n") if ln.strip()]
    if not lines:
        return ""

    # Prefer content after common "final answer" markers.
    marker_patterns = (
        r"^最终答案[:：]\s*",
        r"^最终[:：]\s*",
        r"^final[:：]\s*",
        r"^answer[:：]\s*",
        r"^output[:：]\s*",
    )
    filtered: List[str] = []
    for ln in lines:
        stripped = ln
        for pat in marker_patterns:
            stripped = re.sub(pat, "", stripped, flags=re.IGNORECASE)
        if re.match(r"^(?:思考|thought|reasoning)[:：]\s*", stripped, flags=re.IGNORECASE):
            continue
        if stripped:
            filtered.append(stripped)
    lines = filtered or lines

    # Join lines while trying to undo accidental mid-word line breaks.
    parts: List[str] = []
    for ln in lines:
        if not parts:
            parts.append(ln)
            continue
        prev = parts[-1]
        if prev and ln and prev[-1].isalnum() and ln[0].isalnum() and len(prev) < 12:
            parts[-1] = prev + ln
        else:
            parts.append(ln)

    cleaned = " ".join(parts).strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    # Keep only the first sentence-like chunk.
    # Be conservative with "." because it commonly appears in version numbers (e.g., "Yume-1.5")
    # and abbreviations, and splitting there would truncate the meaning.
    m = re.search(r"[。！？]", cleaned)
    if m:
        cleaned = cleaned[: m.start()].strip()
    else:
        m = re.search(r"[；;!?]", cleaned)
        if m:
            cleaned = cleaned[: m.start()].strip()
        else:
            # Treat "." as a terminator only when it looks like sentence-ending punctuation:
            # - not preceded by a digit (avoid 1.5, v2.1, etc.)
            # - followed by whitespace or end-of-string
            m = re.search(r"(?<!\d)\.(?=\s|$)", cleaned)
            if m:
                cleaned = cleaned[: m.start()].strip()

    cleaned = cleaned.strip().strip("“”\"'`")
    if max_chars is not None and len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip()
    return cleaned


def _emit_progress(progress_cb: Optional[Callable[[Dict[str, Any]], None]], event: Dict[str, Any]) -> None:
    if not progress_cb:
        return
    try:
        progress_cb(event)
    except Exception:
        # Progress callbacks must never break the main workflow.
        return


def _format_exception_group(exc: BaseException) -> str:
    if not isinstance(exc, BaseExceptionGroup):
        return f"{type(exc).__name__}: {exc}"

    lines: List[str] = []

    def walk(e: BaseException, indent: int = 0) -> None:
        pad = "  " * indent
        if isinstance(e, BaseExceptionGroup):
            lines.append(f"{pad}{type(e).__name__}: {e}")
            for sub in e.exceptions:
                walk(sub, indent + 1)
        else:
            lines.append(f"{pad}{type(e).__name__}: {e}")

    walk(exc)
    return "\n".join(lines)


def _mcp_content_to_dict(content: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if not content:
        return items

    for c in content:
        item: Dict[str, Any] = {}
        for field in ("type", "text", "mimeType", "data"):
            if hasattr(c, field):
                value = getattr(c, field)
                if value is not None and value != "":
                    item[field] = value
        if item:
            items.append(item)
    return items


def generate_one_liner_via_openai(title: str, summary: str, url: str) -> str:
    """
    Fallback one-liner generator using the main OpenAI-compatible endpoint.
    Returns a single-line Chinese sentence without links.
    """
    if not OPENAI_API_KEY:
        return ""

    client = OpenAI(
        api_key=OPENAI_API_KEY,
        base_url=OPENAI_BASE_URL or None,
        timeout=LLM_REQUEST_TIMEOUT_S,
        max_retries=LLM_MAX_RETRIES,
    )
    base = summary or title or ""
    prompt = (
        "请只输出一句中文，概括这篇论文主要在做什么（核心方法/关键改进/带来什么效果）。"
        "语气简洁友好，保留关键技术名词，尽量控制在80字以内。"
        "不要换行、不要列点、不要加引号。"
        "不要输出任何思考过程。"
        "不要包含任何链接/URL，也不要出现“链接:”字样。\n\n"
        f"论文标题: {title}\n"
        f"摘要: {base}\n"
    )
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=200,
            timeout=LLM_REQUEST_TIMEOUT_S,
        )
        return normalize_one_liner(resp.choices[0].message.content or "")
    except Exception as e:
        logger.warning("OpenAI one-liner fallback failed: %s", e)
        return ""


def _parse_json_array(text: str) -> List[str]:
    cleaned = (text or "").strip()
    if not cleaned:
        raise ValueError("empty response")
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("[")
        end = cleaned.rfind("]")
        if start == -1 or end == -1 or end <= start:
            raise
        data = json.loads(cleaned[start : end + 1])
    if not isinstance(data, list):
        raise ValueError("expected JSON array")
    return [str(x) for x in data]


def _sanitize_tags(tags: List[str], *, max_tags: int = _MAX_XHS_TAGS) -> List[str]:
    cleaned: List[str] = []
    seen: set[str] = set()
    for raw in tags:
        t = (raw or "").strip()
        t = t.lstrip("#").strip()
        t = re.sub(r"\s+", "", t)
        t = t.strip("，,。.;；:：/|")
        if not t:
            continue
        if "http" in t.lower() or "www." in t.lower():
            continue
        if t in _GENERIC_XHS_TAGS:
            continue
        if len(t) < _MIN_XHS_TAG_LEN or len(t) > _MAX_XHS_TAG_LEN:
            continue
        if t in seen:
            continue
        seen.add(t)
        cleaned.append(t)
        if len(cleaned) >= max_tags:
            break
    return cleaned


def generate_xhs_tags(items: List[Dict[str, Any]], translator: Optional["DeepseekTranslator"] = None) -> List[str]:
    """
    Generate Xiaohongshu tags based on today's paper topics.
    Falls back to default tags if model call fails.
    """
    if not items:
        return list(_DEFAULT_XHS_TAGS)

    # Build compact context for tag generation
    lines = []
    for i, it in enumerate(items, start=1):
        title = (it.get("title") or "").strip()
        one_line = (it.get("one_line") or "").strip()
        if title and one_line:
            lines.append(f"{i}. {title}：{one_line}")
        elif title:
            lines.append(f"{i}. {title}")
    context = "\n".join(lines)

    tag_count = int(os.getenv("XHS_TAG_COUNT", "8"))
    tag_count = max(3, min(tag_count, _MAX_XHS_TAGS))

    prompt = (
        "你是小红书内容运营。请基于下面「今日论文列表」生成帖子话题标签。\n"
        f"只输出严格 JSON 数组（恰好 {tag_count} 个字符串），不要任何额外文字。\n"
        "规则：\n"
        "1) 标签必须与给定内容强相关，尽量从标题/摘要中提炼技术名词/论文关键词；避免输出纯效果描述（如 提升/优化/更好）\n"
        "2) 覆盖面：尽量涵盖每篇论文至少 1 个标签；同时包含“领域/任务”与“方法/模型”两类标签\n"
        "3) 避免泛标签（如 生活/学习/分享/日常/成长/打卡/干货/推荐/必看/教程/攻略）\n"
        "4) 标签不带 #，不要重复；中文为主，可包含必要英文缩写（如 LLM/RL/3D）\n"
        f"5) 每个标签长度 {_MIN_XHS_TAG_LEN}-{_MAX_XHS_TAG_LEN} 字符\n\n"
        f"今日论文列表：\n{context}\n"
    )

    def call_with(client: OpenAI, model: str) -> str:
        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_completion_tokens": 200,
            "timeout": LLM_REQUEST_TIMEOUT_S,
        }
        if DEEPSEEK_REASONING_EFFORT in ("low", "medium", "high"):
            kwargs["reasoning_effort"] = DEEPSEEK_REASONING_EFFORT
        try:
            resp = client.chat.completions.create(**kwargs)
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            # Some providers reject reasoning_effort; retry without it.
            if "reasoning_effort" in str(e):
                kwargs.pop("reasoning_effort", None)
                resp = client.chat.completions.create(**kwargs)
                return (resp.choices[0].message.content or "").strip()
            raise

    try:
        if translator:
            raw = call_with(translator.client, translator.model)
        elif OPENAI_API_KEY:
            client = OpenAI(
                api_key=OPENAI_API_KEY,
                base_url=OPENAI_BASE_URL or None,
                timeout=LLM_REQUEST_TIMEOUT_S,
                max_retries=LLM_MAX_RETRIES,
            )
            raw = call_with(client, OPENAI_MODEL)
        else:
            return list(_DEFAULT_XHS_TAGS)

        tags = _sanitize_tags(_parse_json_array(raw), max_tags=_MAX_XHS_TAGS)

        # Ensure at least one general tag is present.
        if not any(t in tags for t in ("AI论文", "论文速览", "AIGC")):
            tags.insert(0, "AI论文")

        # If generation produced too few tags, pad with defaults.
        for t in _DEFAULT_XHS_TAGS:
            if len(tags) >= tag_count:
                break
            if t not in tags:
                tags.append(t)

        return tags[:tag_count]
    except Exception as e:
        logger.warning("XHS tag generation failed, fallback to defaults: %s", e)
        return list(_DEFAULT_XHS_TAGS)


def save_first_page_image(pdf_url: str, output_path: str) -> Optional[str]:
    """Render the first page of a PDF URL to an image file."""
    try:
        if output_path and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            logger.info("Cover image cache hit: %s", output_path)
            return output_path

        t0 = time.monotonic()
        resp = requests.get(pdf_url, timeout=60)
        resp.raise_for_status()
        t1 = time.monotonic()
        doc = fitz.open(stream=resp.content, filetype="pdf")
        if doc.page_count == 0:
            logger.warning("PDF has no pages: %s", pdf_url)
            return None
        page = doc.load_page(0)
        zoom = max(1.0, min(COVER_ZOOM, 3.0))
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        pix.save(output_path)
        t2 = time.monotonic()
        logger.info(
            "Saved first page image to %s (download=%.2fs, render=%.2fs, total=%.2fs, zoom=%.2f)",
            output_path,
            t1 - t0,
            t2 - t1,
            t2 - t0,
            zoom,
        )
        return output_path
    except Exception as e:
        logger.warning("Failed to render first page for %s: %s", pdf_url, e)
        return None


class DeepseekTranslator:
    """Simple translator using DeepSeek OpenAI-compatible API."""

    def __init__(self, api_key: str, base_url: str, model: str):
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=LLM_REQUEST_TIMEOUT_S,
            max_retries=LLM_MAX_RETRIES,
        )
        self.model = model

    def translate_to_zh(self, text: str) -> str:
        if not text:
            return ""
        prompt = (
            "请用非常简短的中文概括该论文在做什么：聚焦问题、核心方法/技术、主要结论或收益，限制在3-4句内，语气可读、口语化，保留关键技术名词，不要铺陈背景：\n\n"
            f"{text}"
        )
        try:
            kwargs: Dict[str, Any] = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_completion_tokens": 600,
                "timeout": LLM_REQUEST_TIMEOUT_S,
            }
            if DEEPSEEK_REASONING_EFFORT in ("low", "medium", "high"):
                kwargs["reasoning_effort"] = DEEPSEEK_REASONING_EFFORT
            try:
                resp = self.client.chat.completions.create(**kwargs)
            except Exception as e:
                if "reasoning_effort" in str(e):
                    kwargs.pop("reasoning_effort", None)
                    resp = self.client.chat.completions.create(**kwargs)
                else:
                    raise
            return resp.choices[0].message.content.strip()
        except Exception as e:
            logger.warning("DeepSeek translation failed: %s", e)
            return ""

    def one_line_with_link(self, title: str, summary: str, url: str) -> str:
        """
        Generate a single-sentence Chinese hook for the paper.
        Note: do NOT include URLs here; link is rendered on its own line in build_post_payload().
        """
        base = summary or title or ""
        prompt = (
            "请只输出一句中文，概括这篇论文主要在做什么（核心方法/关键改进/带来什么效果）。"
            "语气简洁友好，保留关键技术名词，尽量控制在80字以内。"
            "不要换行、不要列点、不要加引号。"
            "不要输出任何思考过程（不要出现<think>或“思考：”）。"
            "不要包含任何链接/URL，也不要出现“链接:”字样。\n\n"
            f"论文标题: {title}\n"
            f"摘要: {base}\n"
        )
        try:
            kwargs: Dict[str, Any] = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_completion_tokens": 200,
                "timeout": LLM_REQUEST_TIMEOUT_S,
            }
            if DEEPSEEK_REASONING_EFFORT in ("low", "medium", "high"):
                kwargs["reasoning_effort"] = DEEPSEEK_REASONING_EFFORT
            try:
                resp = self.client.chat.completions.create(**kwargs)
            except Exception as e:
                if "reasoning_effort" in str(e):
                    kwargs.pop("reasoning_effort", None)
                    resp = self.client.chat.completions.create(**kwargs)
                else:
                    raise
            choice = resp.choices[0]
            finish_reason = getattr(choice, "finish_reason", None)
            text = (choice.message.content or "").strip()
            if finish_reason == "length" and len(text) < 20:
                logger.warning(
                    "DeepSeek one-liner seems truncated (finish_reason=length, len=%d, model=%s).",
                    len(text),
                    self.model,
                )
            return text
        except Exception as e:
            logger.warning("DeepSeek one-liner failed: %s", e)
            return ""


def openai_analyze_step(
    papers: List[Dict[str, Any]],
    progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
) -> List[Dict[str, Any]]:
    """
    Analyze PDFs with OpenAI-compatible endpoint.
    Returns list of dicts: {paper, analysis, summary, diagram_desc}.
    """
    valid_papers = [p for p in papers if p.get("pdf_url")]
    if not valid_papers:
        logger.warning("No PDF URLs found from crawler.")
        return []

    openai_cfg = {
        "api_key": OPENAI_API_KEY,
        "base_url": OPENAI_BASE_URL,
        "model": OPENAI_MODEL,
        "vision_api_key": QWENVL_API_KEY,
        "vision_base_url": QWENVL_BASE_URL,
    }

    def analyze_single(paper: Dict[str, Any]) -> Dict[str, Any]:
        pdf_url = paper.get("pdf_url")
        client = None
        if openai_cfg["api_key"]:
            client = OpenAIClient(
                api_key=openai_cfg["api_key"],
                base_url=openai_cfg["base_url"],
                model_name=openai_cfg["model"],
                vision_api_key=openai_cfg["vision_api_key"],
                vision_base_url=openai_cfg["vision_base_url"],
            )

        analysis: Any = {}
        diagram_desc = ""
        summary = ""
        if client and pdf_url:
            analysis = client.analyze_pdf(pdf_url)
            diagram_desc = analysis.get("diagram_description", "") if isinstance(analysis, dict) else ""
            summary = analysis.get("summary", "") if isinstance(analysis, dict) else ""

        return {
            "paper": paper,
            "analysis": analysis if isinstance(analysis, dict) else {},
            "summary": summary,
            "diagram_desc": diagram_desc,
        }

    if cancel_cb and cancel_cb():
        raise WorkflowCancelled("cancelled before openai_analyze")

    analysis_results: List[Dict[str, Any]] = []
    total_papers = len(valid_papers)
    _emit_progress(progress_cb, {"event": "phase_start", "phase": "openai_analyze", "total": total_papers})

    max_workers = min(len(valid_papers), OPENAI_CONCURRENCY if OPENAI_CONCURRENCY > 0 else 1)
    done = 0
    cancelled = False
    executor = ThreadPoolExecutor(max_workers=max_workers)
    try:
        future_map = {executor.submit(analyze_single, p): p for p in valid_papers}
        for future in as_completed(future_map):
            if cancel_cb and cancel_cb():
                cancelled = True
                for f in future_map:
                    try:
                        f.cancel()
                    except Exception:
                        pass
                raise WorkflowCancelled("cancelled during openai_analyze")

            paper_meta = future_map[future] or {}
            title = paper_meta.get("title", "Unknown")
            try:
                res = future.result()
                analysis_results.append(res if isinstance(res, dict) else {})
                ok = True
                err = None
            except Exception as e:
                ok = False
                err = f"{type(e).__name__}: {e}"
                logger.error("OpenAI analysis failed for %s: %s", title, err)
            done += 1
            _emit_progress(
                progress_cb,
                {
                    "event": "phase_progress",
                    "phase": "openai_analyze",
                    "done": done,
                    "total": total_papers,
                    "title": title,
                    "ok": ok,
                    "error": err,
                },
            )
    finally:
        executor.shutdown(wait=not cancelled, cancel_futures=True)

    _emit_progress(progress_cb, {"event": "phase_end", "phase": "openai_analyze", "done": done, "total": total_papers})
    return analysis_results


def render_images_step(
    analysis_results: List[Dict[str, Any]],
    *,
    output_base: str = "output_hybrid",
    progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
) -> List[Dict[str, Any]]:
    """
    Render first-page cover images and write per-paper analysis.md.
    Returns list of dicts: {paper, analysis, summary, selected_image, output_dir}.
    """
    os.makedirs(output_base, exist_ok=True)
    total_items = len(analysis_results)
    _emit_progress(progress_cb, {"event": "phase_start", "phase": "render_images", "total": total_items})

    image_workers = min(total_items, max(1, IMAGE_CONCURRENCY))

    def render_single(item: Dict[str, Any]) -> Dict[str, Any]:
        paper = (item.get("paper") or {}) if isinstance(item, dict) else {}
        title = paper.get("title", "Unknown")
        pdf_url = paper.get("pdf_url")
        summary = item.get("summary", "") if isinstance(item, dict) else ""
        diagram_desc = item.get("diagram_desc", "") if isinstance(item, dict) else ""

        safe_title = "".join([c for c in title if c.isalpha() or c.isdigit() or c == " "]).strip().replace(" ", "_")
        paper_dir = os.path.join(output_base, safe_title)
        os.makedirs(paper_dir, exist_ok=True)

        cover_image_path = save_first_page_image(pdf_url, os.path.join(paper_dir, "cover_page.png")) if pdf_url else None
        best_image_info = {"path": cover_image_path, "source": "pdf_first_page"} if cover_image_path else None

        md_path = os.path.join(paper_dir, "analysis.md")
        with open(md_path, "w") as f:
            f.write(f"# {title}\n\n")
            f.write(f"**PDF URL:** {pdf_url}\n\n")
            f.write(f"## Summary\n{summary}\n\n")
            f.write(f"## Core Architecture Diagram Description\n{diagram_desc}\n\n")
            if best_image_info:
                f.write("## Featured Image\n")
                f.write(f"Saved first page snapshot to: {best_image_info.get('path')}\n")

        return {
            "paper": paper,
            "analysis": item.get("analysis", {}) if isinstance(item, dict) else {},
            "summary": summary,
            "selected_image": best_image_info,
            "output_dir": paper_dir,
        }

    if cancel_cb and cancel_cb():
        raise WorkflowCancelled("cancelled before render_images")

    combined_results: List[Dict[str, Any]] = []
    done = 0
    cancelled = False
    executor = ThreadPoolExecutor(max_workers=image_workers)
    try:
        future_map = {executor.submit(render_single, it): it for it in analysis_results}
        for future in as_completed(future_map):
            if cancel_cb and cancel_cb():
                cancelled = True
                for f in future_map:
                    try:
                        f.cancel()
                    except Exception:
                        pass
                raise WorkflowCancelled("cancelled during render_images")

            item_meta = future_map[future] or {}
            paper_meta = item_meta.get("paper") if isinstance(item_meta, dict) else {}
            title = paper_meta.get("title", "Unknown") if isinstance(paper_meta, dict) else "Unknown"
            try:
                res = future.result()
                if not isinstance(res, dict):
                    raise TypeError("render_single returned non-dict result")
                ok = bool(((res.get("selected_image") or {}) if isinstance(res.get("selected_image"), dict) else {}).get("path"))
                err = None
                combined_results.append(res)
            except Exception as e:
                ok = False
                err = f"{type(e).__name__}: {e}"
                logger.warning("Cover render failed for %s: %s", title, err)

            done += 1
            _emit_progress(
                progress_cb,
                {
                    "event": "phase_progress",
                    "phase": "render_images",
                    "done": done,
                    "total": total_items,
                    "title": title,
                    "ok": ok,
                    "error": err or (None if ok else "cover_page.png not generated"),
                },
            )
    finally:
        executor.shutdown(wait=not cancelled, cancel_futures=True)

    _emit_progress(progress_cb, {"event": "phase_end", "phase": "render_images", "done": done, "total": total_items})
    return combined_results


def build_payload_step(
    combined_results: List[Dict[str, Any]],
    *,
    output_base: str = "output_hybrid",
    progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """
    Build Xiaohongshu-ready payload and persist to output_base/post_payload.json.
    """
    os.makedirs(output_base, exist_ok=True)
    _emit_progress(progress_cb, {"event": "phase_start", "phase": "build_payload", "total": len(combined_results)})

    translator = (
        DeepseekTranslator(DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL)
        if DEEPSEEK_API_KEY and DEEPSEEK_BASE_URL
        else None
    )

    post_payload = build_post_payload(combined_results, translator=translator, progress_cb=progress_cb, cancel_cb=cancel_cb)
    payload_path = os.path.join(output_base, "post_payload.json")
    with open(payload_path, "w") as f:
        json.dump(post_payload, f, ensure_ascii=False, indent=2)
    logger.info("Post payload saved to %s", payload_path)

    _emit_progress(
        progress_cb,
        {
            "event": "phase_end",
            "phase": "build_payload",
            "items": len(post_payload.get("items") or []),
            "images": len(post_payload.get("images") or []),
            "tags": len(post_payload.get("tags") or []),
            "payload_path": payload_path,
        },
    )
    return post_payload

def process_papers_step_hybrid(
    papers: List[Dict[str, Any]],
    progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> List[Dict[str, Any]]:
    """
    Step 2 (Hybrid): OpenAI summary + first page snapshot as featured image (no MinerU).
    """
    logger.info("--- Step 2: Hybrid Processing (OpenAI + First Page Snapshot) ---")

    output_base = "output_hybrid"
    os.makedirs(output_base, exist_ok=True)
    analysis_results = openai_analyze_step(papers, progress_cb=progress_cb)
    if not analysis_results:
        return []

    combined_results = render_images_step(analysis_results, output_base=output_base, progress_cb=progress_cb)
    post_payload = build_payload_step(combined_results, output_base=output_base, progress_cb=progress_cb)

    if ENABLE_MCP_PUBLISH:
        try:
            mcp_result = asyncio.run(publish_post_via_mcp(post_payload, XHS_MCP_URL))
            logger.info("MCP publish result: %s", mcp_result)
        except Exception as e:
            logger.error("MCP publish failed: %s", e)
    else:
        logger.info("MCP publish disabled (ENABLE_MCP_PUBLISH not set to true).")

    return combined_results


def build_post_payload(
    results: List[Dict[str, Any]],
    translator: Optional[DeepseekTranslator] = None,
    progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """
    Build Xiaohongshu-ready payload:
    - title: <=20 chars
    - content: ordered one-liners (中文) each ending with link
    - images: cover_page.png absolute paths (one per paper, same order as items)
    - tags: basic defaults
    - items: ordered details for reference
    """
    today = datetime.now().strftime("%m.%d")
    title = f"AI论文速览 {today}"
    items = []

    for idx, item in enumerate(results, start=1):
        if cancel_cb and cancel_cb():
            raise WorkflowCancelled("cancelled during build_payload")
        paper = item.get("paper", {})
        title_en = paper.get("title", "Paper")
        summary = item.get("summary", "")
        url = paper.get("pdf_url") or ""
        one_line = ""
        _emit_progress(
            progress_cb,
            {
                "event": "phase_progress",
                "phase": "build_payload",
                "done": idx - 1,
                "total": len(results),
                "title": title_en,
                "ok": True,
            },
        )
        if translator:
            one_line = translator.one_line_with_link(title_en, summary, url)
            one_line = normalize_one_liner(one_line)
            if len(one_line) < 12:
                one_line = generate_one_liner_via_openai(title_en, summary, url)
        if not one_line:
            one_line = normalize_one_liner(summary) or title_en
        if len(one_line) < 12:
            one_line = generate_one_liner_via_openai(title_en, summary, url) or one_line

        # image path
        img = item.get("selected_image") or {}
        img_path = img.get("path")
        if img_path:
            img_path = os.path.abspath(img_path)

        items.append(
            {
                "idx": idx,
                "title": title_en,
                "one_line": one_line,
                "link": url,
                "image": img_path,
            }
        )

    _emit_progress(
        progress_cb,
        {
            "event": "phase_progress",
            "phase": "build_payload",
            "done": len(results),
            "total": len(results),
            "ok": True,
        },
    )

    content_lines = [
        f"标题：{it['title']}\n摘要：{it['one_line']}\n链接：{it['link']}"
        for it in items
        if it.get("one_line")
    ]
    content = "\n---\n".join(content_lines)
    images = [it["image"] for it in items if it.get("image")]
    if cancel_cb and cancel_cb():
        raise WorkflowCancelled("cancelled during build_payload (before tags)")
    tags = generate_xhs_tags(items, translator=translator) if items else list(_DEFAULT_XHS_TAGS)

    payload = {
        "title": title[:20],
        "content": content,
        "content_lines": content_lines,
        "images": images,
        "tags": tags,
        "items": items,
    }
    return payload


async def publish_post_via_mcp(payload: Dict[str, Any], url: str) -> Dict[str, Any]:
    """Call publish_content tool via MCP StreamableHTTP."""
    try:
        async with streamable_http_client(url, terminate_on_close=False) as (read, write, get_session_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                logger.info("MCP session established, id=%s", get_session_id())
                res = await session.call_tool(
                    "publish_content",
                    {
                        "title": payload.get("title"),
                        "content": payload.get("content"),
                        "images": payload.get("images"),
                        "tags": payload.get("tags"),
                    },
                )
                return {
                    "isError": res.isError,
                    "content": _mcp_content_to_dict(res.content),
                    "session_id": get_session_id(),
                }
    except BaseException as e:
        if isinstance(e, BaseExceptionGroup):
            raise RuntimeError(_format_exception_group(e)) from e
        raise


def process_papers_step_openai(papers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Step 2 (Alt): Process papers with OpenAI (URL mode)."""
    logger.info(f"--- Step 2: Processing papers with OpenAI ({OPENAI_MODEL}) ---")
    
    if not OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY not found!")
        return []
    
    client = OpenAIClient(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL, model_name=OPENAI_MODEL)
    
    results = []
    
    output_base = "output_openai"
    if not os.path.exists(output_base):
        os.makedirs(output_base)

    for i, paper in enumerate(papers):
        title = paper.get("title", "Unknown")
        pdf_url = paper.get("pdf_url")
        
        logger.info(f"Processing ({i+1}/{len(papers)}): {title}")
        
        if not pdf_url: 
            continue
            
        # 1. Analyze PDF with OpenAI (Direct URL)
        analysis = client.analyze_pdf(pdf_url)
        
        # 2. Save Result
        safe_title = "".join([c for c in title if c.isalpha() or c.isdigit() or c==' ']).strip().replace(" ", "_")
        paper_dir = os.path.join(output_base, safe_title)
        if not os.path.exists(paper_dir):
            os.makedirs(paper_dir)

        md_path = os.path.join(paper_dir, "analysis.md")
        diagram_desc = analysis.get("diagram_description", "")
        summary = analysis.get("summary", "No summary")
        
        with open(md_path, "w") as f:
            f.write(f"# {title}\n\n")
            f.write(f"**PDF URL:** {pdf_url}\n\n")
            f.write(f"## Summary\n{summary}\n\n")
            f.write(f"## Core Architecture Diagram Description\n{diagram_desc}\n\n")
        
        results.append({
            "title": title,
            "analysis": analysis,
            "pdf_url": pdf_url
        })
        
    return results

def fetch_papers_step(input_config: dict) -> List[Dict[str, Any]]:
    """Step 1: Fetch papers using the crawler."""
    max_papers = input_config.get("max_papers", 5)
    logger.info(f"--- Step 1: Fetching top {max_papers} papers ---")
    papers = run_crawler_tool(max_papers=max_papers)
    logger.info(f"Fetched {len(papers)} papers.")
    return papers

def main():
    """Main execution flow using LangChain Runnables."""
    
    # Define the chain using LCEL
    if PIPELINE_MODE == "hybrid":
        processing_step = RunnableLambda(process_papers_step_hybrid)
    else:
        processing_step = RunnableLambda(process_papers_step_openai)

    workflow = (
        RunnableLambda(fetch_papers_step) 
        | processing_step
    )
    
    # Run the workflow
    final_results: List[Dict[str, Any]] = []
    try:
        logger.info(f"Starting LangChain Workflow (Mode: {PIPELINE_MODE})...")
        final_results = workflow.invoke({"max_papers": 4})  # Fetch 4 papers
        
        # Output summary
        logger.info("--- Workflow Completed ---")
        print(f"\nProcessed {len(final_results)} papers successfully.\n")
        
        # ... (rest of logging/saving logic if needed, can attach here or in steps)
            
    except Exception as e:
        logger.error(f"Workflow failed: {e}")
        if not final_results:
            return

        for res in final_results:
            info = res.get("paper") or {}
            print(f"Title: {info.get('title')}")
            print(f"PDF URL: {info.get('pdf_url')}")
            print("-" * 50)

if __name__ == "__main__":
    main()
