"""各家绘图 / 视频 API 响应体的解析。

- extract_text_failure_reason(): 无图片时提取模型的拒绝/过滤原因，供用户排查
- extract_all_image_data(): 从各家 API 响应中提取所有图片（URL 或 Base64）
- extract_video_data(): 从 API 响应中提取视频（Base64 或 "url:" 前缀的地址）
"""
import re
from typing import Any, Dict, List, Optional

from .log import logger, truncate_for_log

def extract_text_failure_reason(response_data: Dict[str, Any], max_length: int = 500) -> str:
    """从 API 响应中提取模型返回的拒绝、过滤或失败原因，供无图片时透传。"""

    success_finish_reasons = {"STOP", "FINISH_REASON_STOP", "SUCCESS", "SUCCEEDED", "COMPLETED"}
    finish_reason_labels = {
        "SAFETY": "安全策略拦截",
        "PROHIBITED_CONTENT": "内容策略拦截",
        "CONTENT_FILTER": "内容过滤器拦截",
        "RECITATION": "可能触发版权或复述限制",
        "MAX_TOKENS": "达到最大输出长度",
        "LENGTH": "达到最大输出长度",
        "MALFORMED_FUNCTION_CALL": "模型返回了格式错误的工具调用",
        "OTHER": "模型停止生成",
    }
    reason_keys = (
        "message",
        "msg",
        "detail",
        "details",
        "error",
        "reason",
        "description",
        "error_description",
        "failure_reason",
        "failureReason",
        "error_message",
        "errorMessage",
    )

    def clean(value: str) -> str:
        return truncate_for_log(value.strip(), max_length)

    def humanize_finish_reason(reason: Any) -> str:
        if not isinstance(reason, str) or not reason.strip():
            return ""
        raw = reason.strip()
        normalized = raw.upper()
        label = finish_reason_labels.get(normalized)
        if label:
            return f"{label} ({raw})"
        return raw

    def stringify_reason(value: Any, depth: int = 0) -> str:
        if depth > 4:
            return ""
        if isinstance(value, str):
            return clean(value) if value.strip() else ""
        if isinstance(value, (int, float, bool)):
            return clean(str(value))
        if isinstance(value, list):
            parts = [stringify_reason(item, depth + 1) for item in value]
            return clean("; ".join(part for part in parts if part))
        if isinstance(value, dict):
            for key in reason_keys:
                if key in value:
                    text = stringify_reason(value.get(key), depth + 1)
                    if text:
                        return text
            return summarize_content_filter(value) or summarize_safety_ratings(value.get("safetyRatings"))
        return ""

    def extract_content_text(content: Any) -> str:
        if isinstance(content, str) and content.strip():
            return clean(content)
        if isinstance(content, list):
            text_parts = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                for key in ("text", "content", "refusal"):
                    text = item.get(key)
                    if isinstance(text, str) and text.strip():
                        text_parts.append(text.strip())
                        break
            if text_parts:
                return clean("\n".join(text_parts))
        return ""

    def summarize_safety_ratings(ratings: Any) -> str:
        if not isinstance(ratings, list):
            return ""
        unsafe_parts = []
        for rating in ratings:
            if not isinstance(rating, dict):
                continue
            category = rating.get("category") or rating.get("label") or rating.get("type")
            probability = rating.get("probability") or rating.get("severity") or rating.get("level")
            blocked = rating.get("blocked") or rating.get("filtered")
            if blocked or str(probability).upper() in {"MEDIUM", "HIGH"}:
                label = str(category or "unknown")
                if probability:
                    label += f"={probability}"
                if blocked:
                    label += "(blocked)"
                unsafe_parts.append(label)
        if unsafe_parts:
            return clean("安全评级: " + ", ".join(unsafe_parts))
        return ""

    def summarize_content_filter(filter_data: Any) -> str:
        if isinstance(filter_data, list):
            parts = [summarize_content_filter(item) for item in filter_data]
            return clean("; ".join(part for part in parts if part))
        if not isinstance(filter_data, dict):
            return ""

        if "content_filter_results" in filter_data:
            return summarize_content_filter(filter_data.get("content_filter_results"))

        filtered_parts = []
        for name, result in filter_data.items():
            if isinstance(result, dict):
                filtered = result.get("filtered") or result.get("blocked")
                severity = result.get("severity") or result.get("probability") or result.get("level")
                if filtered or str(severity).lower() in {"medium", "high"}:
                    detail = str(name)
                    if severity:
                        detail += f"={severity}"
                    if filtered:
                        detail += "(filtered)"
                    filtered_parts.append(detail)
        if filtered_parts:
            return clean("内容过滤: " + ", ".join(filtered_parts))
        return ""

    def extract_prompt_feedback(feedback: Any) -> str:
        if not isinstance(feedback, dict):
            return ""
        parts = []
        block_reason = (
            feedback.get("blockReason")
            or feedback.get("blockedReason")
            or feedback.get("block_reason")
            or feedback.get("blocked_reason")
        )
        if block_reason:
            parts.append("请求被模型拦截: " + humanize_finish_reason(block_reason))

        block_message = (
            feedback.get("blockReasonMessage")
            or feedback.get("block_message")
            or feedback.get("message")
        )
        if isinstance(block_message, str) and block_message.strip():
            parts.append(block_message.strip())

        safety = summarize_safety_ratings(feedback.get("safetyRatings") or feedback.get("safety_ratings"))
        if safety:
            parts.append(safety)

        return clean("; ".join(part for part in parts if part))

    def extract_generic_reason(obj: Any, depth: int = 0) -> str:
        if depth > 4:
            return ""
        if isinstance(obj, dict):
            for key in reason_keys:
                if key in obj:
                    text = stringify_reason(obj.get(key), depth + 1)
                    if text:
                        return text
            for value in obj.values():
                text = extract_generic_reason(value, depth + 1)
                if text:
                    return text
        elif isinstance(obj, list):
            for item in obj:
                text = extract_generic_reason(item, depth + 1)
                if text:
                    return text
        return ""

    try:
        error_text = stringify_reason(response_data.get("error"))
        if error_text:
            return error_text

        for key in reason_keys:
            text = stringify_reason(response_data.get(key))
            if text:
                return text

        prompt_feedback = extract_prompt_feedback(
            response_data.get("promptFeedback") or response_data.get("prompt_feedback")
        )
        if prompt_feedback:
            return prompt_feedback

        prompt_filter = summarize_content_filter(
            response_data.get("prompt_filter_results") or response_data.get("promptFilterResults")
        )
        if prompt_filter:
            return prompt_filter

        choices = response_data.get("choices")
        if isinstance(choices, list) and choices:
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message") or choice.get("delta")
                if isinstance(message, dict):
                    refusal = message.get("refusal")
                    if isinstance(refusal, str) and refusal.strip():
                        return clean(refusal)
                    content_text = extract_content_text(message.get("content"))
                    if content_text:
                        return content_text

                filter_reason = summarize_content_filter(
                    choice.get("content_filter_results") or choice.get("contentFilterResults")
                )
                if filter_reason:
                    return filter_reason

                finish_reason = choice.get("finish_reason") or choice.get("finishReason")
                if isinstance(finish_reason, str) and finish_reason.strip():
                    if finish_reason.upper() not in success_finish_reasons:
                        return clean("模型停止生成: " + humanize_finish_reason(finish_reason))

        candidates = response_data.get("candidates")
        if isinstance(candidates, list) and candidates:
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                content = candidate.get("content")
                if isinstance(content, dict):
                    text_parts = []
                    parts = content.get("parts")
                    if isinstance(parts, list):
                        for part in parts:
                            if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"].strip():
                                text_parts.append(part["text"].strip())
                    if text_parts:
                        return clean("\n".join(text_parts))

                finish_message = candidate.get("finishMessage") or candidate.get("finish_message")
                if isinstance(finish_message, str) and finish_message.strip():
                    return clean(finish_message)

                safety = summarize_safety_ratings(candidate.get("safetyRatings") or candidate.get("safety_ratings"))
                finish_reason = candidate.get("finishReason") or candidate.get("finish_reason")
                if safety:
                    prefix = ""
                    if isinstance(finish_reason, str) and finish_reason.strip():
                        prefix = "模型停止生成: " + humanize_finish_reason(finish_reason) + "; "
                    return clean(prefix + safety)
                if isinstance(finish_reason, str) and finish_reason.strip():
                    if finish_reason.upper() not in success_finish_reasons:
                        return clean("模型停止生成: " + humanize_finish_reason(finish_reason))

        generic_reason = extract_generic_reason(response_data)
        if generic_reason:
            return generic_reason
    except Exception:
        return ""
    return ""

async def extract_all_image_data(response_data: Dict[str, Any]) -> List[str]:
    """从API响应中提取所有图片数据（URL或Base64），支持多图响应
    
    与 extract_image_data 不同，此函数会提取响应中的所有图片，
    而不是只返回第一张。适用于一次返回多张图片的 API。
    
    Returns:
        图片数据列表（URL或Base64字符串），如果没有找到则返回空列表
    """
    try:
        results = []
        
        # 豆包格式响应解析 - 支持多张图
        if "data" in response_data and isinstance(response_data["data"], list):
            for item in response_data["data"]:
                if isinstance(item, dict):
                    if "url" in item and item["url"]:
                        logger.info(f"从豆包响应中提取到图片URL: {item['url'][:100]}...")
                        results.append(item["url"])
                    elif "b64_json" in item and item["b64_json"]:
                        logger.info("从豆包响应中提取到 base64 图片数据")
                        results.append(item["b64_json"])
            if results:
                return results
        
        if "choices" in response_data and isinstance(response_data["choices"], list) and response_data["choices"]:
            choice = response_data["choices"][0]
            content_data = None

            delta = choice.get("delta")
            if delta and "content" in delta:
                content_data = delta["content"]
            
            message = choice.get("message")
            if content_data is None and message:
                if "content" in message:
                    content_data = message["content"]
            
            # 检查 message.images 数组格式
            if message and "images" in message and isinstance(message["images"], list):
                for img_item in message["images"]:
                    if isinstance(img_item, dict):
                        img_type = img_item.get("type", "")
                        if img_type == "image_url":
                            image_url_obj = img_item.get("image_url", {})
                            if isinstance(image_url_obj, dict) and "url" in image_url_obj:
                                url = image_url_obj["url"]
                                if url.startswith("data:image") and "base64," in url:
                                    results.append(url.split("base64,")[1])
                                else:
                                    results.append(url)
                        elif "url" in img_item and img_item["url"]:
                            url = img_item["url"]
                            if url.startswith("data:image") and "base64," in url:
                                results.append(url.split("base64,")[1])
                            else:
                                results.append(url)
                if results:
                    logger.info(f"从 message.images 中提取到 {len(results)} 张图片")
                    return results

            if content_data is not None:
                # 处理 content 为数组格式
                if isinstance(content_data, list):
                    for item in content_data:
                        if isinstance(item, dict):
                            item_type = item.get("type", "")
                            if item_type == "image":
                                image_obj = item.get("image", {})
                                if isinstance(image_obj, dict):
                                    if "data" in image_obj and image_obj["data"]:
                                        results.append(image_obj["data"])
                                    elif "url" in image_obj and image_obj["url"]:
                                        results.append(image_obj["url"])
                            elif item_type == "image_url":
                                image_url_obj = item.get("image_url", {})
                                if isinstance(image_url_obj, dict) and "url" in image_url_obj:
                                    url = image_url_obj["url"]
                                    if url.startswith("data:image") and "base64," in url:
                                        results.append(url.split("base64,")[1])
                                    else:
                                        results.append(url)
                            elif item_type == "text" and "text" in item:
                                text_content = item["text"]
                                if isinstance(text_content, str):
                                    # 匹配所有 markdown 图片格式
                                    all_matches = re.findall(r"!\[.*?\]\((.*?)\)", text_content)
                                    for url in all_matches:
                                        results.append(url)
                    if results:
                        logger.info(f"从 content 数组中提取到 {len(results)} 张图片")
                        return results
                
                # 处理 content 为字符串格式（关键修改：使用 findall 提取所有图片）
                elif isinstance(content_data, str):
                    content_text = content_data
                    
                    # 匹配所有 markdown 图片格式 ![...](url)
                    all_md_urls = re.findall(r"!\[.*?\]\((.*?)\)", content_text)
                    if all_md_urls:
                        for url in all_md_urls:
                            if "base64," in url:
                                b64_data = url.split("base64,")[1]
                                if len(b64_data) < 1000:
                                    continue
                                logger.info("从响应中提取到图片 base64 数据 (markdown 格式)")
                                results.append(b64_data)
                            else:
                                log_url = url[:100] + "..." if len(url) > 100 else url
                                logger.info(f"从响应中提取到图片URL: {log_url}")
                                results.append(url)
                        if results:
                            return results

                    # 匹配裸露的HTTP/HTTPS URL（带图片后缀）
                    all_img_urls = re.findall(r"https?://[^\s]+\.(?:png|jpg|jpeg|gif|webp|bmp|ico|tiff?)(?:\?[^\s]*)?", content_text, re.IGNORECASE)
                    if all_img_urls:
                        for url in all_img_urls:
                            logger.info(f"从响应中提取到裸图片URL: {url[:100]}...")
                            results.append(url)
                        return results
                    
                    # 匹配所有 URL（排除非图片页面）
                    all_urls = re.findall(r"https?://[^\s]+", content_text)
                    for url in all_urls:
                        if not any(kw in url.lower() for kw in ['dashboard', 'login', 'signin', 'register', 'admin']):
                            results.append(url)
                    if results:
                        return results

                    # 匹配 base64 图片数据
                    all_b64 = re.findall(r"data:image/\w+;base64,([a-zA-Z0-9+/=\n]+)", content_text)
                    if all_b64:
                        for b64 in all_b64:
                            if len(b64) > 1000:
                                results.append(b64)
                        if results:
                            return results

                    # 匹配无 data: 前缀的 base64 图片数据
                    # 格式: image/jpeg;base64,... 或 image/png;base64,...
                    all_b64_noprefix = re.findall(r"(?:^|[\s,])image/\w+;base64,([a-zA-Z0-9+/=\n]+)", content_text)
                    if all_b64_noprefix:
                        for b64 in all_b64_noprefix:
                            if len(b64) > 1000:
                                results.append(b64)
                        if results:
                            logger.info(f"从响应中提取到 {len(results)} 张图片 (无data:前缀格式)")
                            return results

        # Gemini 格式
        candidates = response_data.get("candidates")
        if isinstance(candidates, list) and candidates:
            content = candidates[0].get("content")
            if isinstance(content, dict):
                parts = content.get("parts")
                if isinstance(parts, list):
                    for part in parts:
                        if not isinstance(part, dict):
                            continue
                        inline_data = part.get("inlineData") or part.get("inline_data")
                        if isinstance(inline_data, dict):
                            image_b64 = inline_data.get("data")
                            if isinstance(image_b64, str):
                                results.append(image_b64)
                        text_content = part.get("text")
                        if isinstance(text_content, str):
                            all_b64 = re.findall(r"data:image/\w+;base64,([a-zA-Z0-9+/=\n]+)", text_content)
                            for b64 in all_b64:
                                results.append(b64)
        
        if results:
            logger.info(f"共提取到 {len(results)} 张图片")
        return results
    except Exception:
        return []

async def extract_video_data(response_data: Dict[str, Any]) -> Optional[str]:
    """从API响应中提取视频数据（Base64 或 URL）
    
    支持的响应格式:
    1. Base64: content 中包含 data:video/mp4;base64,...
    2. 视频URL（纯文本）: content 中直接包含 https://...mp4
    3. 视频URL（HTML）: content 中包含 <source src="https://...mp4">
    
    Returns:
        - base64 编码的视频数据字符串
        - 或 "url:<视频URL>" 格式的字符串（表示需要下载）
        - 或 None
    """
    try:
        if "choices" in response_data and isinstance(response_data["choices"], list) and response_data["choices"]:
            choice = response_data["choices"][0]
            content_data = None

            # Handle streaming response with 'delta'
            delta = choice.get("delta")
            if delta and "content" in delta:
                content_data = delta["content"]
            
            # Handle non-streaming response with 'message'
            message = choice.get("message")
            if content_data is None and message:
                if "content" in message:
                    content_data = message["content"]

            if content_data is not None and isinstance(content_data, str):
                # 匹配 markdown 格式的视频 data URL
                # 格式: ![image](data:video/mp4;base64,...)
                match_video = re.search(r"!\[.*?\]\(data:video/[^;]+;base64,([a-zA-Z0-9+/=\n]+)\)", content_data)
                if match_video:
                    logger.info("从响应中提取到视频 base64 数据 (markdown 格式)")
                    return match_video.group(1)
                
                # 匹配裸露的 data URL 格式
                match_video_raw = re.search(r"data:video/[^;]+;base64,([a-zA-Z0-9+/=\n]+)", content_data)
                if match_video_raw:
                    logger.info("从响应中提取到视频 base64 数据 (裸 data URL 格式)")
                    return match_video_raw.group(1)
                
                # 匹配视频 URL（纯文本 .mp4 链接）
                match_video_url = re.search(r"(https?://[^\s<>\"]+\.mp4(?:\?[^\s<>\"]*)?)", content_data)
                if match_video_url:
                    video_url = match_video_url.group(1)
                    logger.info(f"从响应中提取到视频 URL: {video_url}")
                    return f"url:{video_url}"
                
                # 匹配 HTML <source> 标签中的视频 URL
                match_source_tag = re.search(r'<source[^>]+src="([^"]+)"', content_data)
                if match_source_tag:
                    video_url = match_source_tag.group(1)
                    logger.info(f"从响应中提取到视频 URL (HTML source 标签): {video_url}")
                    return f"url:{video_url}"
        
        # Gemini 格式响应解析
        candidates = response_data.get("candidates")
        if isinstance(candidates, list) and candidates:
            content = candidates[0].get("content")
            if isinstance(content, dict):
                parts = content.get("parts")
                if isinstance(parts, list):
                    for part in parts:
                        if isinstance(part, dict):
                            inline_data = part.get("inlineData") or part.get("inline_data")
                            if isinstance(inline_data, dict):
                                mime_type = inline_data.get("mimeType") or inline_data.get("mime_type", "")
                                if "video" in mime_type:
                                    video_b64 = inline_data.get("data")
                                    if isinstance(video_b64, str):
                                        logger.info("从 Gemini 响应中提取到视频 base64 数据")
                                        return video_b64
                            
                            # 检查文本内容中的视频 data URL
                            text_content = part.get("text")
                            if isinstance(text_content, str):
                                match = re.search(r"data:video/[^;]+;base64,([a-zA-Z0-9+/=\n]+)", text_content)
                                if match:
                                    logger.info("从 Gemini 文本响应中提取到视频 base64 数据")
                                    return match.group(1)
        
        return None
    except Exception:
        return None
