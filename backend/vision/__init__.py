"""Webcam-based physical verification of LED behaviour (contracts section D).

Public API: VisionSession and its models. `synthetic` is a test-only renderer
and is deliberately not exported here.
"""

from .roi import LedRoi, RoiConfig
from .session import FrameResult, PhasePlan, VerificationResult, VisionSession, build_plan

__all__ = ["LedRoi", "RoiConfig", "FrameResult", "PhasePlan", "VerificationResult", "VisionSession", "build_plan"]
