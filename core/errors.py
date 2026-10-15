"""上游错误的结构化分类。

此前的重试决策依赖 ``"429" in str(e)`` 之类的字符串子串判断：URL、任务 ID
或错误正文里出现的数字都会被误判成配额错误。这里把错误收敛成结构化对象：

- ``UpstreamHTTPError``：带状态码与阶段（submit / fetch / poll）的 HTTP 错误；
- ``SubmissionUncertainError``：提交结果不确定（请求可能已被上游接收）；
- ``submission_state_for(...)``：把异常映射为 rejected / uncertain 两种提交状态。

分类保留提交状态的不确定性；是否继续尝试由调用方结合已接收状态与重试策略决定。
HTTP 错误响应可继续换端点，5xx 不计为 Key 失效。自拍允许未收到成功响应时
在剩余预算内继续尝试超时请求，但不能据此宣称原服务一定没有创建任务。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx


#: 明确未提交或被拒绝：可以安全地切换下一个端点
SUBMISSION_REJECTED = "rejected"
#: 提交结果不确定：不得自动重复生成
SUBMISSION_UNCERTAIN = "uncertain"

#: 出现这些标记时把 429 视为配额耗尽（不可恢复），而不是可恢复的限流
_QUOTA_MARKERS = (
    "insufficient_quota",
    "quota_exceeded",
    "quota exceeded",
    "exceeded your current quota",
    "insufficient balance",
    "insufficient funds",
    "no credit",
    "余额不足",
    "额度不足",
    "欠费",
)

#: 这些状态码可能意味着上游已收到并开始处理请求：
#: - 504：网关等待上游响应超时，上游任务可能仍在运行；
#: - 408：服务器未收完请求就放弃等待，边界情况按保守处理。
_UNCERTAIN_STATUS_CODES = frozenset({408, 504})

#: 提交/生成阶段的 5xx 默认按"提交结果不确定"处理：网关拿不到上游的有效
#: 响应（502/503/504）或上游自身出错（500）都不能证明请求没有被接收——
#: 上游已处理、回传响应失败同样会表现为 5xx。本地中转把应用级错误（如
#: 额度用尽）包成 502 的情况也不能作为所有渠道统一安全重试的依据；需要
#: 继续换端点的场景由 4xx 等明确拒绝的失败覆盖。
_GATEWAY_ERROR_MIN = 500
_GATEWAY_ERROR_MAX = 599

#: 这些传输错误发生在请求已发出之后，提交结果不确定
_UNCERTAIN_TRANSPORT_ERRORS = (
    httpx.ReadTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.WriteTimeout,
    httpx.RemoteProtocolError,
)

#: 这些传输错误说明请求没有发出去，可安全切换端点
_REJECTED_TRANSPORT_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.InvalidURL,
    httpx.UnsupportedProtocol,
    httpx.LocalProtocolError,
)


class UpstreamHTTPError(RuntimeError):
    """带状态码、阶段与简短原因的上游 HTTP 错误。

    ``body`` 与 ``reason`` 都经过凭据/地址脱敏与截断；用户文案使用
    :meth:`user_message`，不包含上游原始响应体。
    """

    def __init__(
        self,
        status_code: int,
        *,
        stage: str = "fetch",
        body: str = "",
        reason: str = "",
    ) -> None:
        self.status_code = int(status_code)
        self.stage = str(stage or "fetch")
        # 原始响应体只用于日志诊断；截断之后仍可能含凭据或内网地址，
        # 进入日志/文案之前先脱敏，不允许原样输出（F5）。
        self._quota_exhausted = self.status_code == 429 and any(
            marker in (body or "").lower() for marker in _QUOTA_MARKERS
        )
        # 自定义渠道的 Key 没有固定格式；用户文案和诊断只采用本地类别。
        self.reason = {
            400: "请求参数被拒绝", 401: "认证失败", 403: "无访问权限",
            404: "接口不存在", 408: "上游等待超时", 413: "请求数据过大",
            429: "额度不足" if self._quota_exhausted else "请求被限流",
        }.get(self.status_code, "上游服务异常" if self.status_code >= 500 else "上游拒绝请求")
        self.body = self.reason
        super().__init__(self.user_message())

    def user_message(self) -> str:
        text = f"API请求失败 (HTTP {self.status_code})"
        if self.reason:
            text += f"：{self.reason}"
        return text

    @property
    def is_quota_exhausted(self) -> bool:
        return self._quota_exhausted

    @property
    def is_rate_limited(self) -> bool:
        return self.status_code == 429 and not self.is_quota_exhausted

    @property
    def submission_state(self) -> str:
        # 只有提交/生成阶段的状态码才代表"可能已提交"；查询阶段的错误不涉及提交。
        if self.stage in ("submit", "fetch"):
            return classify_http_status(self.status_code)
        return SUBMISSION_REJECTED


class SubmissionUncertainError(RuntimeError):
    """上游可能已经接收并开始处理请求，不能自动重复提交。"""


class GenerationRejectedError(RuntimeError):
    """协议明确报告任务失败或拒绝生成，可以尝试其他渠道。文案必须为本地常量。"""


@dataclass
class GenerationState:
    """在 HTTP 响应解析前记录上游已接收，避免本地异常丢失提交状态。"""
    accepted: bool = False

    async def observe(self, response: httpx.Response) -> None:
        if response.request.method == "POST" and response.is_success:
            self.accepted = True

    def prevents_retry(self, exc: BaseException) -> bool:
        return self.accepted and not isinstance(exc, GenerationRejectedError)

    def is_unconfirmed_timeout(self, exc: BaseException) -> bool:
        """仅识别尚未收到成功响应的超时，包含视频协议包装的原始超时。"""
        if self.accepted:
            return False
        seen = set()
        while isinstance(exc, SubmissionUncertainError) and exc.__cause__ is not None:
            if id(exc) in seen:
                return False
            seen.add(id(exc))
            exc = exc.__cause__
        return isinstance(exc, (TimeoutError, httpx.ReadTimeout, httpx.WriteTimeout))


def safe_model_rejection(data) -> Optional[str]:
    """只从协议规定的字段映射已知拒绝代码，绝不显示上游自由文本。"""
    if not isinstance(data, dict):
        return None
    feedback = data.get("promptFeedback") or {}
    if isinstance(feedback, dict) and feedback.get("blockReason") == "SAFETY":
        return "模型安全策略拦截 (SAFETY)"
    for candidate in data.get("candidates", []) or []:
        if isinstance(candidate, dict) and candidate.get("finishReason") in ("SAFETY", "RECITATION"):
            return "模型策略拒绝生成"
    for choice in data.get("choices", []) or []:
        if isinstance(choice, dict) and choice.get("finish_reason") == "content_filter":
            return "模型内容过滤 (content_filter)"
    return None


def classify_http_status(status_code: int) -> str:
    """把 HTTP 状态码映射为提交状态。

    408/504 与全部 5xx 可能已经提交，按 uncertain 处理；其余状态码
    （4xx 等明确拒绝）按 rejected 处理，允许调用方安全换端点。
    """
    code = int(status_code)
    if code in _UNCERTAIN_STATUS_CODES or _GATEWAY_ERROR_MIN <= code <= _GATEWAY_ERROR_MAX:
        return SUBMISSION_UNCERTAIN
    return SUBMISSION_REJECTED


def classify_transport_error(exc: BaseException) -> Optional[str]:
    """把 httpx 传输错误映射为提交状态；无法判断时返回 None。"""
    if isinstance(exc, _UNCERTAIN_TRANSPORT_ERRORS):
        return SUBMISSION_UNCERTAIN
    if isinstance(exc, _REJECTED_TRANSPORT_ERRORS):
        return SUBMISSION_REJECTED
    return None


def submission_state_for(exc: BaseException) -> Optional[str]:
    """统一入口：HTTP 错误、传输错误与已提交状态异常的提交状态判断。"""
    if isinstance(exc, UpstreamHTTPError):
        return exc.submission_state
    if isinstance(exc, SubmissionUncertainError):
        return SUBMISSION_UNCERTAIN
    return classify_transport_error(exc)


def describe_error(exc: BaseException) -> str:
    """把异常转成简短、可面向用户的说明（不含上游原始响应体）。"""
    if isinstance(exc, UpstreamHTTPError):
        return exc.user_message()
    if isinstance(exc, httpx.TimeoutException):
        return f"请求超时 ({type(exc).__name__})"
    if isinstance(exc, httpx.HTTPError):
        return f"网络请求失败 ({type(exc).__name__})"
    if isinstance(exc, (SubmissionUncertainError, GenerationRejectedError)):
        return str(exc)
    return f"处理失败 ({type(exc).__name__})"
