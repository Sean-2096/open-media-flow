import subprocess
from pathlib import Path

from open_media_flow.audit import ContentAuditor
from open_media_flow.llm import LLMReviewGeneration, ReviewVerdict
from open_media_flow.models import ContentTask, Platform

POLICY = Path(__file__).parents[1] / "config" / "policy.json"


def make_task(**overrides):
    values = {
        "topic": "本地自动化",
        "platforms": [Platform.BILIBILI],
        "title": "本地自动化工作流",
        "script": "这是一段用于验证自动审核门禁的完整示例脚本，它不会包含任何效果或收益承诺。",
        "description": "演示项目。本内容包含AI辅助生成素材",
        "tags": ["自动化", "开源"],
    }
    values.update(overrides)
    return ContentTask(**values)


def test_missing_media_is_rejected():
    report = ContentAuditor(POLICY).review(make_task())
    assert report.approved is False
    assert any(check.name == "media" and not check.passed for check in report.checks)


def test_blocked_term_is_rejected():
    report = ContentAuditor(POLICY).review(make_task(script="这个方案稳赚不赔，而且绝对安全。"))
    assert report.approved is False
    assert any(check.name == "blocked_terms" and not check.passed for check in report.checks)


def test_valid_media_is_approved(tmp_path):
    media = tmp_path / "sample.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=720x1280:d=5",
            "-pix_fmt",
            "yuv420p",
            str(media),
        ],
        check=True,
        timeout=30,
    )
    report = ContentAuditor(POLICY).review(make_task(media_path=str(media)))
    assert report.approved is True
    assert report.score == 100


def test_llm_review_is_part_of_approval(tmp_path):
    class Reviewer:
        def review_content(self, task):
            return LLMReviewGeneration(
                verdict=ReviewVerdict(
                    score=72,
                    risk_level="medium",
                    issues=["存在无法验证的效果描述"],
                    summary="建议修改后发布",
                ),
                endpoint="primary",
                model="local-review-model",
            )

    media = tmp_path / "sample.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=720x1280:d=5",
            "-pix_fmt",
            "yuv420p",
            str(media),
        ],
        check=True,
        timeout=30,
    )

    report = ContentAuditor(POLICY, llm_reviewer=Reviewer()).review(
        make_task(media_path=str(media))
    )

    assert report.approved is False
    assert any(check.name == "llm_review" and not check.passed for check in report.checks)
