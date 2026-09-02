import json

import pytest

from open_media_flow.llm import (
    ContentPackageValidationError,
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


def content_package():
    return {
        **metadata().model_dump(),
        "audience": "喜欢竖屏故事的年轻观众",
        "hook": "他以为门外没人，手机却响了。",
        "creative_direction": "低饱和电影感，快速切镜，结尾用暖光完成情绪反转。",
        "cover_prompt": "vertical cinematic hallway, young man, warm rim light, no text",
        "shots": [
            {
                "order": index,
                "narration": f"这是第{index}个镜头对应的完整中文旁白内容。",
                "visual_prompt": (
                    "cinematic vertical shot, young man in an apartment hallway, "
                    "natural movement, handheld camera, soft dramatic lighting"
                ),
                "negative_prompt": "text, logo, watermark, blur",
                "duration_seconds": 5,
                "kind": "video",
            }
            for index in range(1, 5)
        ],
    }


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
    response_payload = {"choices": [{"message": {"content": metadata().model_dump_json()}}]}

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

    assert captured["body"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_content_plan_validation_error_includes_failing_field(monkeypatch):
    invalid = {**content_package(), "script": "太短"}
    payload = {"choices": [{"message": {"content": json.dumps(invalid, ensure_ascii=False)}}]}
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: FakeResponse(payload))
    client = OpenAICompatibleClient(
        LLMEndpointSettings(
            name="primary",
            base_url="http://127.0.0.1:8081/v1",
            model="local",
            api_key="local",
        )
    )

    with pytest.raises(ContentPackageValidationError, match="字段 script"):
        client.generate_content_plan(task())


def test_content_plan_accepts_short_video_length_script(monkeypatch):
    package = content_package()
    package["script"] = (
        "门铃响了三次，他却不敢开门。监控里空无一人，门外只放着一把旧伞。"
        "他认出那是母亲生前常用的伞，打开门时，邻居递来一封迟到十年的信。"
    )
    payload = {"choices": [{"message": {"content": json.dumps(package, ensure_ascii=False)}}]}
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: FakeResponse(payload))
    client = OpenAICompatibleClient(
        LLMEndpointSettings(
            name="primary",
            base_url="http://127.0.0.1:8081/v1",
            model="local",
            api_key="local",
        )
    )

    result = client.generate_content_plan(task())

    assert result.plan is not None
    assert 60 <= len(result.metadata.script) < 180


def test_content_plan_retry_repairs_invalid_package(monkeypatch):
    captured_bodies = []
    invalid = {**content_package(), "script": "太短", "shots": []}
    responses = [invalid, content_package()]

    def fake_urlopen(request, timeout):
        captured_bodies.append(json.loads(request.data.decode("utf-8")))
        content = json.dumps(responses[len(captured_bodies) - 1], ensure_ascii=False)
        return FakeResponse({"choices": [{"message": {"content": content}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = OpenAICompatibleClient(
        LLMEndpointSettings(
            name="primary",
            base_url="http://127.0.0.1:8081/v1",
            model="local",
            api_key="local",
        )
    )
    router = FallbackLLMRouter(client, primary_attempts=2)

    result = router.generate_content_plan(task())

    assert result.plan is not None
    assert len(result.plan.shots) == 4
    assert len(captured_bodies) == 2
    repair_prompt = captured_bodies[1]["messages"][1]["content"]
    assert "校验反馈" in repair_prompt
    assert "字段 script" in repair_prompt
    assert "字段 shots" in repair_prompt
