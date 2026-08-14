"""
Gemini Drawer 视频生成

视频侧各家 API 差异比图片更大（豆包与 TS-AI 都是"建任务 + 轮询"，
OpenAI 兼容接口是一次性/流式返回），且只有 4 家，因此没有像绘图那样
抽 provider 层，保持单函数内的分支结构。

send_video_via_napcat() 直连 NapCat 的正向 HTTP 接口发视频——
SDK 的 send 能力目前不支持视频段。
"""
from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from .managers import key_manager
from ..utils import extract_video_data


async def process_video_generation(
    prompt: str,
    base64_img: Optional[str],
    mime_type: Optional[str],
    endpoints: List[Dict[str, Any]],
    proxy: Optional[str],
    logger,
    debug_mode: bool = False
) -> Tuple[Optional[str], str]:
    """
    处理视频生成请求，返回 (video_base64_data, error_message)
    
    Args:
        prompt: 视频描述
        base64_img: 可选的 base64 编码图片
        mime_type: 图片 MIME 类型
        endpoints: 视频端点列表
        proxy: 代理地址
        logger: 日志对象
    """
    video_data = None
    last_error = ""
    
    for endpoint in endpoints:
        api_url = endpoint["url"]
        api_key = endpoint["key"]
        endpoint_type = endpoint["type"]
        
        logger.info(f"[视频] 尝试端点: {endpoint_type}")
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
        
        try:
            # 豆包 API (异步任务模式)
            if "volces.com" in api_url or "/contents/generations/tasks" in api_url:
                doubao_content = [{"type": "text", "text": prompt}]
                if base64_img:
                    doubao_content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{base64_img}"}
                    })
                
                model_name = endpoint.get("model") or "doubao-seedance-1-5-pro-251215"
                doubao_payload = {
                    "model": model_name,
                    "content": doubao_content,
                    "duration": -1,  # 自动时长: 模型在 4~12 秒范围内自主选择
                    "resolution": "1080p"
                }
                
                async with httpx.AsyncClient(proxy=proxy, timeout=60.0, follow_redirects=True) as client:
                    response = await client.post(api_url, json=doubao_payload, headers=headers)
                    if response.status_code != 200:
                        raise Exception(f"创建任务失败: {response.status_code} - {response.text}")
                    
                    task_id = response.json().get("id")
                    if not task_id:
                        raise Exception("未获取到任务ID")
                    
                    logger.info(f"[视频] 豆包任务已创建: {task_id}")
                    
                    # 轮询任务状态
                    poll_url = f"{api_url}/{task_id}"
                    for poll_count in range(120):  # 最多10分钟
                        await asyncio.sleep(5)
                        poll_resp = await client.get(poll_url, headers=headers)
                        if poll_resp.status_code != 200:
                            continue
                        
                        poll_data = poll_resp.json()
                        status = poll_data.get("status")
                        
                        if status == "succeeded":
                            content = poll_data.get("content", {})
                            video_url = None
                            if isinstance(content, list) and len(content) > 0 and isinstance(content[0], dict):
                                video_url = content[0].get("video_url") or content[0].get("url")
                            elif isinstance(content, dict):
                                video_url = content.get("video_url") or content.get("url")
                            
                            if video_url:
                                video_resp = await client.get(video_url)
                                if video_resp.status_code == 200:
                                    video_data = base64.b64encode(video_resp.content).decode('utf-8')
                                    logger.info(f"[视频] 豆包视频下载完成")
                            break
                        elif status == "failed":
                            error_msg = poll_data.get("error", {}).get("message", "未知错误")
                            raise Exception(f"任务失败: {error_msg}")
                    else:
                        raise Exception("任务超时")
            
            # OpenAI 格式
            elif "/chat/completions" in api_url:
                content_list = [{"type": "text", "text": prompt}]
                if base64_img:
                    content_list.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{base64_img}"}
                    })
                
                openai_payload = {
                    "model": endpoint.get("model") or "video-preview",
                    "messages": [{"role": "user", "content": content_list}],
                    "stream": endpoint.get("stream", False),
                    "video_config": {
                        "video_length": 10,
                        "resolution_name": "720p"
                    }
                }

                # current_payload = openai_payload

                # logger.info(f"[视频] OpenAI格式: {safe_json_dumps(current_payload)}")

                use_stream = endpoint.get("stream", False)
                
                async with httpx.AsyncClient(proxy=proxy, timeout=300.0, follow_redirects=True) as client:
                    if use_stream:
                        # 流式模式：累积所有 content，流结束后统一提取
                        accumulated_content = ""
                        async with client.stream("POST", api_url, json=openai_payload, headers=headers) as response:
                            if response.status_code != 200:
                                raw_body = await response.aread()
                                error_msg = raw_body.decode('utf-8', 'ignore')
                                raise Exception(f"API请求失败: {response.status_code} - {error_msg}")
                            
                            async for line in response.aiter_lines():
                                line = line.strip()
                                if not line or line.startswith(':'):
                                    continue
                                if line.startswith('data:'):
                                    data_str = line[5:].strip()
                                    if data_str in ["DONE", "[DONE]"]:
                                        break
                                    try:
                                        response_data = json.loads(data_str)
                                        # 只累积流式正文，避免在半截 base64 chunk 上误提取并截断。
                                        if "choices" in response_data and response_data["choices"]:
                                            choice = response_data["choices"][0]
                                            delta = choice.get("delta", {})
                                            chunk_content = delta.get("content", "")
                                            message = choice.get("message", {})
                                            if not chunk_content and isinstance(message, dict):
                                                chunk_content = message.get("content", "")
                                            if chunk_content:
                                                accumulated_content += chunk_content
                                    except json.JSONDecodeError:
                                        pass
                        
                        # 流结束后：如果还没拿到 video_data，尝试从累积内容中提取
                        if not video_data and accumulated_content:
                            logger.info(f"[视频] 流式响应累积内容长度: {len(accumulated_content)}")
                            if debug_mode:
                                logger.warning(f"[调试模式] 视频流式响应累积内容: {accumulated_content[:2000]}")
                            # 构造一个伪响应对象，用于 extract_video_data
                            pseudo_response = {
                                "choices": [{
                                    "message": {
                                        "content": accumulated_content
                                    }
                                }]
                            }
                            video_data = await extract_video_data(pseudo_response)
                    else:
                        response = await client.post(api_url, json=openai_payload, headers=headers)
                        if response.status_code == 200:
                            data = response.json()
                            video_data = await extract_video_data(data)
                            if not video_data and debug_mode:
                                logger.warning(f"[调试模式] 视频非流式响应未提取到数据，原始响应:")
                                logger.warning(f"[调试模式] {json.dumps(data, ensure_ascii=False)[:2000]}")
                        else:
                            raise Exception(f"API请求失败: {response.status_code} - {response.text}")
            
            # TS-AI 视频生成
            elif "api.tavr.top" in api_url or "api.tsart.lat" in api_url or "tsart.lat" in api_url or "endpoint=video_generation" in api_url:
                base_url = api_url.split("?")[0]
                request_url = f"{base_url}?endpoint=video_generation"
                headers["x-api-key"] = api_key
                
                tsai_video_payload = {
                    "prompt": prompt,
                    "seed": -1
                }
                
                if base64_img and mime_type:
                    tsai_video_payload["mode"] = "i2v"
                    tsai_video_payload["image"] = f"data:{mime_type};base64,{base64_img}"
                else:
                    tsai_video_payload["mode"] = "t2v"
                    tsai_video_payload["width"] = 832
                    tsai_video_payload["height"] = 480
                    
                async with httpx.AsyncClient(proxy=proxy, timeout=60.0, follow_redirects=True) as client:
                    response = await client.post(request_url, json=tsai_video_payload, headers=headers)
                    if response.status_code != 200:
                        raise Exception(f"创建TS-AI视频任务失败: {response.status_code} - {response.text}")
                    
                    task_id = response.json().get("data", {}).get("id")
                    if not task_id:
                        raise Exception(f"未获取到TS-AI视频任务ID: {response.text}")
                        
                    logger.info(f"[视频] TS-AI任务已创建: {task_id}")
                    
                    poll_url = f"{base_url}?endpoint=task_status&task_id={task_id}"
                    for _ in range(120): # 最多10分钟
                        await asyncio.sleep(5)
                        poll_resp = await client.get(poll_url, headers=headers)
                        if poll_resp.status_code != 200:
                            continue
                            
                        poll_data = poll_resp.json()
                        status = poll_data.get("data", {}).get("status")
                        if status == "completed":
                            result_data = poll_data.get("data", {}).get("result", {})
                            video_url = result_data.get("video_url") or result_data.get("image_url")
                            if video_url:
                                video_data = f"url:{video_url}"
                            break
                        elif status == "failed":
                            error_msg = poll_data.get("data", {}).get("error", "未知错误")
                            raise Exception(f"TS-AI视频生成失败: {error_msg}")
                    else:
                        raise Exception("TS-AI视频任务轮询超时")

            # Gemini 格式
            elif "generateContent" in api_url:
                parts = [{"text": prompt}]
                if base64_img:
                    parts.append({"inline_data": {"mime_type": mime_type, "data": base64_img}})
                
                gemini_payload = {"contents": [{"parts": parts}]}
                request_url = f"{api_url}?key={api_key}"
                
                async with httpx.AsyncClient(proxy=proxy, timeout=300.0, follow_redirects=True) as client:
                    response = await client.post(request_url, json=gemini_payload, headers={"Content-Type": "application/json"})
                    if response.status_code == 200:
                        data = response.json()
                        video_data = await extract_video_data(data)
                    else:
                        raise Exception(f"API请求失败: {response.status_code} - {response.text}")
            
            # 如果提取到的是 URL，需要下载视频并转为 base64
            if video_data and video_data.startswith("url:"):
                video_url = video_data[4:]  # 去掉 "url:" 前缀
                logger.info(f"[视频] 正在下载视频: {video_url[:100]}...")
                dl_headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "video/mp4,video/*,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Referer": video_url.split("?")[0],
                }
                api_host = urlparse(api_url).hostname
                video_host = urlparse(video_url).hostname
                if api_key and api_host and video_host and api_host == video_host:
                    dl_headers["Authorization"] = f"Bearer {api_key}"
                try:
                    async with httpx.AsyncClient(proxy=proxy, timeout=120.0, follow_redirects=True) as dl_client:
                        dl_response = await dl_client.get(video_url, headers=dl_headers)
                        if dl_response.status_code == 200 and dl_response.content:
                            video_data = base64.b64encode(dl_response.content).decode('utf-8')
                            logger.info(f"[视频] 视频下载完成，大小: {len(dl_response.content)} 字节")
                        else:
                            if "Authorization" in dl_headers:
                                logger.warning(f"[视频] 同域带认证头下载失败 (HTTP {dl_response.status_code})，尝试不带认证头...")
                                dl_headers.pop("Authorization", None)
                                dl_response2 = await dl_client.get(video_url, headers=dl_headers)
                                if dl_response2.status_code == 200 and dl_response2.content:
                                    video_data = base64.b64encode(dl_response2.content).decode('utf-8')
                                    logger.info(f"[视频] 视频下载完成（无认证头），大小: {len(dl_response2.content)} 字节")
                                else:
                                    raise Exception(f"下载视频失败: HTTP {dl_response.status_code} / {dl_response2.status_code}")
                            else:
                                raise Exception(f"下载视频失败: HTTP {dl_response.status_code}")
                except Exception as dl_err:
                    logger.error(f"[视频] 下载视频失败: {type(dl_err).__name__}: {dl_err!r}")
                    video_data = None
                    last_error = f"视频URL获取成功但下载失败: {dl_err}"
            
            if video_data:
                key_manager.record_key_usage(api_key, True)
                return video_data, ""
            else:
                # API 调用成功但未提取到视频数据
                error_msg = f"端点 {endpoint_type} 未返回有效视频数据"
                logger.warning(f"[视频] {error_msg}")
                last_error = error_msg
                
        except Exception as e:
            logger.warning(f"[视频] 端点 {endpoint_type} 失败: {type(e).__name__}: {e}")
            is_quota_error = "429" in str(e)
            key_manager.record_key_usage(api_key, False, force_disable=is_quota_error)
            last_error = str(e)
            await asyncio.sleep(1)
    
    # 所有端点都失败了，记录最终错误
    if not last_error:
        last_error = "所有端点均未返回有效视频数据"
    logger.error(f"[视频] 生成失败: {last_error}")
    return None, last_error


async def send_video_via_napcat(
    video_base64: str,
    group_id: Optional[str],
    user_id: Optional[str],
    napcat_host: str,
    napcat_port: int,
    logger
) -> Tuple[bool, str]:
    """
    通过 NapCat HTTP API 发送视频
    
    Returns:
        (success, error_message)
    """
    video_base64_uri = f"base64://{video_base64}"
    
    if group_id:
        api_url = f"http://{napcat_host}:{napcat_port}/send_group_msg"
        request_data = {"group_id": group_id, "message": [{"type": "video", "data": {"file": video_base64_uri}}]}
    elif user_id:
        api_url = f"http://{napcat_host}:{napcat_port}/send_private_msg"
        request_data = {"user_id": user_id, "message": [{"type": "video", "data": {"file": video_base64_uri}}]}
    else:
        return False, "无法确定发送目标"
    
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.post(api_url, json=request_data)
            if response.status_code == 200:
                result = response.json()
                if result.get("status") == "ok" or result.get("retcode") == 0:
                    logger.info(f"[视频] 发送成功")
                    return True, ""
                else:
                    return False, f"napcat返回错误: {result}"
            else:
                return False, f"HTTP {response.status_code}"
    except Exception as e:
        logger.error(f"[视频] 发送失败: {e}")
        return False, str(e)
