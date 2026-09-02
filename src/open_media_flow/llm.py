from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, Field, ValidationError

from .models import ContentPlan, ContentTask, ShotSpec
from .settings import LLMEndpointSettings


class LLMError(RuntimeError):
    """Safe LLM error that never includes an API key or full request payload."""


class GeneratedMetadata(BaseModel):
    title: str = Field(min_length=2, max_length=200)
    script: str = Field(min_length=180, max_length=450)
    description: str = Field(min_length=2, max_length=5_000)
    tags: list[str] = Field(min_length=1, max_length=30)


class GeneratedContentPackage(GeneratedMetadata):
    audience: str = Field(min_length=2, max_length=300)
    hook: str = Field(min_length=5, max_length=300)
    creative_direction: str = Field(min_length=5, max_length=1_000)
    cover_prompt: str = Field(min_length=10, max_length=2_000)
    shots: list[ShotSpec] = Field(min_length=3, max_length=12)


@dataclass(frozen=True)
class LLMGeneration:
    metadata: GeneratedMetadata
    endpoint: str
    model: str
    plan: ContentPlan | None = None


class ReviewVerdict(BaseModel):
    score: int = Field(ge=0, le=100)
    risk_level: str = Field(min_length=2, max_length=30)
    issues: list[str] = Field(default_factory=list, max_length=20)
    summary: str = Field(min_length=2, max_length=1_000)


@dataclass(frozen=True)
class LLMReviewGeneration:
    verdict: ReviewVerdict
    endpoint: str
    model: str
    local_score: int | None = None


class MetadataGenerator(Protocol):
    def generate_metadata(self, task: ContentTask) -> LLMGeneration: ...

    def generate_content_plan(self, task: ContentTask) -> LLMGeneration: ...

    def review_content(self, task: ContentTask) -> LLMReviewGeneration: ...


def _metadata_prompt(task: ContentTask) -> tuple[str, str]:
    system = (
        "你是多平台短视频编辑。输出必须满足给定 JSON 结构，不要输出 Markdown。"
        "script 目标长度为 350 到 420 个中文字符，并且必须是 8 到 10 个完整句子；"
        "少于 180 字或超过 450 字均为无效输出。"
        "避免绝对化、医疗、金融收益承诺和无法验证的事实。"
    )
    user = f"""
根据以下信息生成发布素材：
主题：{task.topic}
目标平台：{', '.join(platform.value for platform in task.platforms)}

要求：
1. title 适合中文内容平台，不超过 200 字。
2. script 写成 8 到 10 个信息完整的句子，目标 350 到 420 个中文字符；
   硬性要求不少于 180 个且不超过 450 个中文字符，提交 JSON 前检查长度。
3. description 最后必须包含：本内容包含AI辅助生成素材
4. tags 为 3 到 8 个不带井号的字符串。
""".strip()
    return system, user


def _content_plan_prompt(task: ContentTask) -> tuple[str, str]:
    system = (
        "你是全自动短视频内容导演。只输出 JSON，不要输出 Markdown。"
        "你必须同时完成发布文案、完整旁白、封面提示词和可独立生成的分镜设计。"
        "每个 visual_prompt 必须用具体英文描述主体、动作、场景、镜头、光线和风格，"
        "不能要求生成模型绘制字幕、Logo 或界面文字。"
        "script 必须为 180 到 450 个中文字符，并与所有分镜 narration 的内容和顺序一致。"
        "避免无法验证的事实、收益承诺、医疗金融建议和版权角色。"
    )
    user = f"""
主题：{task.topic}
目标平台：{', '.join(platform.value for platform in task.platforms)}

输出 JSON 字段：
- title：中文标题，不超过 200 字
- script：完整中文旁白，180 到 450 字
- description：发布简介，结尾必须包含“本内容包含AI辅助生成素材”
- tags：3 到 8 个不带井号的标签
- audience：目标受众
- hook：前 3 秒钩子
- creative_direction：统一的画面风格、色彩、节奏和镜头规则
- cover_prompt：竖屏封面画面英文提示词，不包含文字
- shots：4 到 6 个分镜，按顺序输出。每项包含：
  - order：从 1 开始的整数
  - narration：该镜头对应的中文旁白
  - visual_prompt：可直接用于视频生成的详细英文提示词
  - negative_prompt：需要避免的画面问题，英文
  - duration_seconds：3 到 8 秒
  - kind：固定为 video
""".strip()
    return system, user


def _strip_code_fence(value: str) -> str:
    text = value.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


class OpenAICompatibleClient:
    def __init__(
        self,
        endpoint: LLMEndpointSettings,
        *,
        timeout_seconds: int = 120,
        openrouter_zdr: bool = True,
        openrouter_data_collection: str = "deny",
    ):
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self.openrouter_zdr = openrouter_zdr
        self.openrouter_data_collection = openrouter_data_collection

    def generate_metadata(self, task: ContentTask) -> LLMGeneration:
        if not self.endpoint.model:
            raise LLMError(f"{self.endpoint.name} model is not configured")
        system, user = _metadata_prompt(task)
        raw_metadata = self._request_json(system, user)
        try:
            metadata = GeneratedMetadata.model_validate(raw_metadata)
        except ValidationError as exc:
            raise LLMError(f"{self.endpoint.name} returned invalid structured output") from exc

        return LLMGeneration(
            metadata=metadata,
            endpoint=self.endpoint.name,
            model=self.endpoint.model,
        )

    def generate_content_plan(self, task: ContentTask) -> LLMGeneration:
        if not self.endpoint.model:
            raise LLMError(f"{self.endpoint.name} model is not configured")
        system, user = _content_plan_prompt(task)
        raw_package = self._request_json(system, user)
        try:
            package = GeneratedContentPackage.model_validate(raw_package)
        except ValidationError as exc:
            raise LLMError(f"{self.endpoint.name} returned invalid content package") from exc
        metadata = GeneratedMetadata.model_validate(package.model_dump())
        plan = ContentPlan(
            audience=package.audience,
            hook=package.hook,
            creative_direction=package.creative_direction,
            cover_prompt=package.cover_prompt,
            shots=package.shots,
        )
        return LLMGeneration(
            metadata=metadata,
            endpoint=self.endpoint.name,
            model=self.endpoint.model,
            plan=plan,
        )

    def review_content(self, task: ContentTask) -> LLMReviewGeneration:
        if not self.endpoint.model:
            raise LLMError(f"{self.endpoint.name} model is not configured")
        system = (
            "你是短视频发布前的内容安全审核员。只输出 JSON。"
            "重点检查事实夸大、医疗金融法律风险、仇恨骚扰、隐私泄露、诱导交易、"
            "版权风险和平台不适宜表达。不要因为内容由 AI 辅助生成而直接扣分。"
        )
        user = f"""
审核以下待发布内容：
平台：{', '.join(platform.value for platform in task.platforms)}
标题：{task.title}
脚本：{task.script}
简介：{task.description}
标签：{', '.join(task.tags)}

输出字段：
- score：0 到 100，越高越适合发布
- risk_level：low、medium 或 high
- issues：具体问题数组，没有问题则为空数组
- summary：一句中文审核结论
""".strip()
        raw_verdict = self._request_json(system, user)
        try:
            verdict = ReviewVerdict.model_validate(raw_verdict)
        except ValidationError as exc:
            raise LLMError(f"{self.endpoint.name} returned invalid review output") from exc
        return LLMReviewGeneration(
            verdict=verdict,
            endpoint=self.endpoint.name,
            model=self.endpoint.model,
        )

    def _request_json(self, system: str, user: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.endpoint.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.4,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        if self.endpoint.enable_thinking is not None:
            body["chat_template_kwargs"] = {
                "enable_thinking": self.endpoint.enable_thinking
            }
        if "openrouter.ai" in self.endpoint.base_url:
            body["provider"] = {
                "zdr": self.openrouter_zdr,
                "data_collection": self.openrouter_data_collection,
                "require_parameters": True,
            }

        headers = {"Content-Type": "application/json"}
        if self.endpoint.api_key:
            headers["Authorization"] = f"Bearer {self.endpoint.api_key}"
        request = urllib.request.Request(
            f"{self.endpoint.base_url}/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
            content = result["choices"][0]["message"]["content"]
            structured = json.loads(_strip_code_fence(content))
        except urllib.error.HTTPError as exc:
            raise LLMError(
                f"{self.endpoint.name} returned HTTP {exc.code}"
            ) from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"{self.endpoint.name} is unreachable") from exc
        except TimeoutError as exc:
            raise LLMError(f"{self.endpoint.name} timed out") from exc
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise LLMError(f"{self.endpoint.name} returned invalid structured output") from exc
        if not isinstance(structured, dict):
            raise LLMError(f"{self.endpoint.name} returned invalid structured output")
        return structured


class FallbackLLMRouter:
    def __init__(
        self,
        primary: MetadataGenerator,
        fallback: MetadataGenerator | None = None,
        *,
        primary_attempts: int = 2,
        fallback_review_min_score: int = 70,
        fallback_review_max_score: int = 84,
    ):
        self.primary = primary
        self.fallback = fallback
        self.primary_attempts = max(1, primary_attempts)
        self.fallback_review_min_score = max(0, fallback_review_min_score)
        self.fallback_review_max_score = min(100, fallback_review_max_score)

    def generate_metadata(self, task: ContentTask) -> LLMGeneration:
        errors: list[str] = []
        for _ in range(self.primary_attempts):
            try:
                return self.primary.generate_metadata(task)
            except LLMError as exc:
                errors.append(str(exc))

        if self.fallback is not None:
            try:
                return self.fallback.generate_metadata(task)
            except LLMError as exc:
                errors.append(str(exc))

        raise LLMError("; ".join(errors) or "all LLM endpoints failed")

    def generate_content_plan(self, task: ContentTask) -> LLMGeneration:
        errors: list[str] = []
        for _ in range(self.primary_attempts):
            try:
                return self.primary.generate_content_plan(task)
            except LLMError as exc:
                errors.append(str(exc))
        if self.fallback is not None:
            try:
                return self.fallback.generate_content_plan(task)
            except LLMError as exc:
                errors.append(str(exc))
        raise LLMError("; ".join(errors) or "all content planning endpoints failed")

    def review_content(self, task: ContentTask) -> LLMReviewGeneration:
        errors: list[str] = []
        primary_result: LLMReviewGeneration | None = None
        for _ in range(self.primary_attempts):
            try:
                primary_result = self.primary.review_content(task)
                break
            except LLMError as exc:
                errors.append(str(exc))

        if primary_result is not None:
            score = primary_result.verdict.score
            is_gray = self.fallback_review_min_score <= score <= self.fallback_review_max_score
            if not is_gray or self.fallback is None:
                return primary_result
            try:
                fallback_result = self.fallback.review_content(task)
                return LLMReviewGeneration(
                    verdict=fallback_result.verdict,
                    endpoint=fallback_result.endpoint,
                    model=fallback_result.model,
                    local_score=score,
                )
            except LLMError:
                return primary_result

        if self.fallback is not None:
            try:
                return self.fallback.review_content(task)
            except LLMError as exc:
                errors.append(str(exc))
        raise LLMError("; ".join(errors) or "all LLM review endpoints failed")
