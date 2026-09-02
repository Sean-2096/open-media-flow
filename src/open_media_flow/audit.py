from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from .llm import LLMError, LLMReviewGeneration
from .media import probe_media
from .models import AuditCheck, AuditReport, ContentTask


class ContentReviewer(Protocol):
    def review_content(self, task: ContentTask) -> LLMReviewGeneration: ...


class ContentAuditor:
    def __init__(self, policy_path: Path, llm_reviewer: ContentReviewer | None = None):
        self.policy = json.loads(policy_path.read_text(encoding="utf-8"))
        self.llm_reviewer = llm_reviewer

    def review(self, task: ContentTask) -> AuditReport:
        checks = [self._text_check(task), self._metadata_check(task), self._media_check(task)]
        if all(check.passed for check in checks) and self.llm_reviewer is not None:
            checks.append(self._llm_check(task))
        score = round(sum(check.score for check in checks) / len(checks))
        approved = all(check.passed for check in checks) and score >= self.policy["minimum_score"]
        return AuditReport(approved=approved, score=score, checks=checks)

    def _llm_check(self, task: ContentTask) -> AuditCheck:
        try:
            result = self.llm_reviewer.review_content(task)
        except LLMError as exc:
            return AuditCheck(
                name="llm_review",
                passed=False,
                score=0,
                detail=f"模型审核不可用：{exc}",
            )
        verdict = result.verdict
        minimum = int(self.policy.get("llm_minimum_score", 85))
        details = [
            f"{result.endpoint}/{result.model}",
            f"风险={verdict.risk_level}",
            verdict.summary,
        ]
        if result.local_score is not None:
            details.insert(1, f"本地灰区分={result.local_score}")
        if verdict.issues:
            details.append("问题=" + "；".join(verdict.issues))
        return AuditCheck(
            name="llm_review",
            passed=verdict.score >= minimum and verdict.risk_level.lower() == "low",
            score=verdict.score,
            detail="；".join(details),
        )

    def _text_check(self, task: ContentTask) -> AuditCheck:
        combined = "\n".join([task.title, task.script, task.description, " ".join(task.tags)])
        hits = [term for term in self.policy["blocked_terms"] if term in combined]
        if hits:
            return AuditCheck(
                name="blocked_terms",
                passed=False,
                score=0,
                detail=f"命中禁止词：{', '.join(hits)}",
            )
        if len(task.script.strip()) < 20:
            return AuditCheck(
                name="blocked_terms",
                passed=False,
                score=40,
                detail="脚本少于20字，无法完成可靠审核",
            )
        return AuditCheck(name="blocked_terms", passed=True, score=100, detail="未命中禁止词")

    def _metadata_check(self, task: ContentTask) -> AuditCheck:
        missing = []
        if not task.title.strip():
            missing.append("title")
        if not task.tags:
            missing.append("tags")
        disclosure = self.policy["required_disclosures"]["contains_synthetic_media"]
        if task.contains_synthetic_media and disclosure not in task.description:
            missing.append("AI disclosure")
        if missing:
            return AuditCheck(
                name="metadata",
                passed=False,
                score=max(0, 100 - 25 * len(missing)),
                detail=f"缺少：{', '.join(missing)}",
            )
        return AuditCheck(name="metadata", passed=True, score=100, detail="元数据完整")

    def _media_check(self, task: ContentTask) -> AuditCheck:
        if not task.media_path:
            return AuditCheck(name="media", passed=False, score=0, detail="尚未关联视频文件")
        try:
            info = probe_media(Path(task.media_path))
            streams = [item for item in info.get("streams", []) if item.get("codec_type") == "video"]
            if not streams:
                raise ValueError("没有视频流")
            video = streams[0]
            duration = float(info.get("format", {}).get("duration") or video.get("duration") or 0)
            width = int(video.get("width") or 0)
            height = int(video.get("height") or 0)
        except (OSError, ValueError, TypeError) as exc:
            return AuditCheck(name="media", passed=False, score=0, detail=f"媒体探测失败：{exc}")

        problems = []
        if duration < self.policy["minimum_duration_seconds"]:
            problems.append(f"时长仅{duration:.1f}秒")
        if duration > self.policy["maximum_duration_seconds"]:
            problems.append(f"时长达到{duration:.1f}秒")
        if width < self.policy["minimum_width"] or height < self.policy["minimum_height"]:
            problems.append(f"分辨率仅{width}x{height}")
        if problems:
            return AuditCheck(name="media", passed=False, score=40, detail="；".join(problems))
        return AuditCheck(
            name="media",
            passed=True,
            score=100,
            detail=f"{width}x{height}，{duration:.1f}秒，媒体结构正常",
        )
