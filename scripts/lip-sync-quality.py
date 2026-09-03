#!/usr/bin/env python3
"""Estimate visible-face coverage and audio/mouth-motion alignment."""

from __future__ import annotations

import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import librosa
import mediapipe as mp
import numpy as np


def main(video_path: Path, audio_path: Path) -> dict[str, float]:
    capture = cv2.VideoCapture(str(video_path))
    fps = capture.get(cv2.CAP_PROP_FPS) or 25.0
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    stride = max(1, math.ceil(frame_count / 300))
    mouth: list[float] = []
    times: list[float] = []
    detected = 0
    sampled = 0
    mesh = mp.solutions.face_mesh.FaceMesh(
        static_image_mode=False,
        max_num_faces=1,
        refine_landmarks=False,
        min_detection_confidence=0.5,
        min_tracking_confidence=0.5,
    )
    index = 0
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        if index % stride:
            index += 1
            continue
        sampled += 1
        result = mesh.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        if result.multi_face_landmarks:
            points = result.multi_face_landmarks[0].landmark
            width = math.hypot(points[308].x - points[78].x, points[308].y - points[78].y)
            opening = math.hypot(points[14].x - points[13].x, points[14].y - points[13].y)
            mouth.append(opening / max(width, 1e-6))
            times.append(index / fps)
            detected += 1
        index += 1
    mesh.close()
    capture.release()
    coverage = detected / max(sampled, 1)
    if detected < 8:
        return {"sync_score": 0.0, "face_coverage": round(coverage, 4)}

    with tempfile.TemporaryDirectory(prefix="omf_lip_quality_") as temp:
        wav = Path(temp) / "audio.wav"
        subprocess.run(
            ["/opt/homebrew/bin/ffmpeg", "-y", "-loglevel", "error", "-i", str(audio_path), "-ar", "16000", "-ac", "1", str(wav)],
            check=True,
            timeout=120,
        )
        samples, sample_rate = librosa.load(wav, sr=16_000, mono=True)

    window = max(1, int(sample_rate * 0.04))
    energy = []
    for timestamp in times:
        center = int(timestamp * sample_rate)
        segment = samples[max(0, center - window // 2) : min(len(samples), center + window // 2)]
        energy.append(float(np.sqrt(np.mean(np.square(segment))) if len(segment) else 0.0))
    mouth_values = np.asarray(mouth, dtype=np.float64)
    energy_values = np.asarray(energy, dtype=np.float64)
    mouth_values = np.convolve(mouth_values, np.ones(3) / 3, mode="same")
    energy_values = np.convolve(energy_values, np.ones(3) / 3, mode="same")
    correlations = []
    for lag in range(-4, 5):
        left = mouth_values[max(0, lag) : len(mouth_values) + min(0, lag)]
        right = energy_values[max(0, -lag) : len(energy_values) - max(0, lag)]
        if len(left) >= 6 and np.std(left) > 1e-6 and np.std(right) > 1e-6:
            correlations.append(float(np.corrcoef(left, right)[0, 1]))
    correlation = max(correlations, default=-1.0)
    score = max(0.0, min(1.0, 0.5 + 0.5 * correlation))
    return {"sync_score": round(score, 4), "face_coverage": round(coverage, 4)}


if __name__ == "__main__":
    print(json.dumps(main(Path(sys.argv[1]), Path(sys.argv[2]))))
