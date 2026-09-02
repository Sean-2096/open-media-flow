import json

import pytest

from open_media_flow.llm import (
    FallbackLLMRouter,
    GeneratedMetadata,
    LLMError,
    LLMGeneration,
    LLMReviewGeneration,
    OpenAICompatibleClient,
    ReviewVerdict,
)
from open_media_flow.models import ContentTask, Platform
from open_media_flow.settings import LLMEndpointSettings


def task():
    return ContentTask(topic="本地内容生成", platforms=[Platform.BILIBILI])


def metadata():
    return GeneratedMetadata(
        title="本地内容工作流",
        script=(
            "这是一段用于验证本地内容工作流的测试脚本。它会覆盖结构化输出解析、"
            "主模型调用、失败重试和备用模型回退等关键路径。为了让测试数据与真实短视频"
            "脚本的质量门禁保持一致，这段文字会明确说明选题整理、脚本生成、内容审核、"
            "视频制作和发布前检查的完整过程。系统首先根据主题生成标题、正文、简介与标签，"
            "随后检查敏感表达、事实风险和平台适配情况。视频完成后还需要校验文件路径、"
            "分辨率、时长和编码信息，只有全部门禁通过才进入发布阶段。整个过程默认采用"
            "本地模型和模拟发布，既方便开发者重复调试，也避免测试期间误操作真实账号。"
            "当本地模型暂时不可用时，路由器会按配置重试，并且只在用户明确启用后调用"
            "云端备用服务。每次选择的模型和端点都会进入任务元数据，密钥与完整请求内容"
            "不会写入日志。这样既能验证自动化链路，也能保留清晰、安全、可追踪的执行记录。"
        ),
        description="测试简介。本内容包含AI辅助生成素材",
        tags=["本地AI", "自动化", "视频"],
    )


class StubGenerator:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def generate_metadata(self, _task):
        result = self.results[self.calls]
        self.calls += 1
        if isinstance(result, Exception):
            raise result
        return result

    def review_content(self, _task):
        result = self.results[self.calls]
        self.calls += 1
        if isinstance(result, Exception):
            raise result
        return result


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


def generation(endpoint="primary"):
    return LLMGeneration(metadata=metadata(), endpoint=endpoint, model="test-model")


def review_generation(score, endpoint="primary", risk_level="low"):
    return LLMReviewGeneration(
        verdict=ReviewVerdict(
            score=score,
            risk_level=risk_level,
            issues=[],
            summary="内容可以发布",
        ),
        endpoint=endpoint,
        model="review-model",
    )


def test_primary_retries_before_fallback():
    primary = StubGenerator([LLMError("temporary"), generation()])
    fallback = StubGenerator([generation("fallback")])
    router = FallbackLLMRouter(primary, fallback, primary_attempts=2)

    result = router.generate_metadata(task())

    assert result.endpoint == "primary"
    assert primary.calls == 2
    assert fallback.calls == 0


def test_fallback_runs_after_primary_exhausted():
    primary = StubGenerator([LLMError("first"), LLMError("second")])
    fallback = StubGenerator([generation("fallback")])
    router = FallbackLLMRouter(primary, fallback, primary_attempts=2)

    result = router.generate_metadata(task())

    assert result.endpoint == "fallback"
    assert primary.calls == 2
    assert fallback.calls == 1


def test_gray_review_uses_cloud_fallback():
    primary = StubGenerator([review_generation(78)])
    fallback = StubGenerator([review_generation(93, "fallback")])
    router = FallbackLLMRouter(
        primary,
        fallback,
        fallback_review_min_score=70,
        fallback_review_max_score=84,
    )

    result = router.review_content(task())

    assert result.endpoint == "fallback"
    assert result.verdict.score == 93
    assert result.local_score == 78
    assert fallback.calls == 1


def test_high_confidence_local_review_skips_fallback():
    primary = StubGenerator([review_generation(95)])
    fallback = StubGenerator([review_generation(90, "fallback")])
    router = FallbackLLMRouter(primary, fallback)

    result = router.review_content(task())

    assert result.endpoint == "primary"
    assert fallback.calls == 0


def test_openrouter_privacy_options_and_json_fence(monkeypatch):
    captured = {}
    response_payload = {
        "choices": [
            {
                "message": {
                    "content": f"```json\n{metadata().model_dump_json()}\n```",
                }
            }
        ]
    }

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        captured["authorization"] = request.headers["Authorization"]
        return FakeResponse(response_payload)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = OpenAICompatibleClient(
        LLMEndpointSettings(
            name="fallback",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            api_key="secret-test-key",
        ),
        timeout_seconds=33,
        openrouter_zdr=True,
        openrouter_data_collection="deny",
    )

    result = client.generate_metadata(task())

    assert result.metadata.title == "本地内容工作流"
    assert captured["timeout"] == 33
    assert captured["authorization"] == "Bearer secret-test-key"
    assert captured["body"]["provider"] == {
        "zdr": True,
        "data_collection": "deny",
        "require_parameters": True,
    }


def test_invalid_structured_output_is_safe_error(monkeypatch):
    payload = {"choices": [{"message": {"content": "not-json"}}]}
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: FakeResponse(payload))
    client = OpenAICompatibleClient(
        LLMEndpointSettings(
            name="primary",
            base_url="http://127.0.0.1:8081/v1",
            model="local",
            api_key="local",
        )
    )

    with pytest.raises(LLMError, match="invalid structured output"):
        client.generate_metadata(task())


def test_local_endpoint_can_disable_qwen_thinking(monkeypatch):
    captured = {}
    response_payload = {
        "choices": [{"message": {"content": metadata().model_dump_json()}}]
    }

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse(response_payload)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = OpenAICompatibleClient(
        LLMEndpointSettings(
            name="primary",
            base_url="http://127.0.0.1:8081/v1",
            model="Qwen/Qwen3-14B-GGUF:Q4_K_M",
            api_key="local",
            enable_thinking=False,
        )
    )

    client.generate_metadata(task())

    assert captured["body"]["chat_template_kwargs"] == {
        "enable_thinking": False
    }
