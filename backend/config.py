"""Validated, hardware-neutral settings. All presets are engineering starting points."""
import json
import re
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, Field, model_validator


class Settings(BaseModel):
    adapter: Literal['browser', 'csi', 'v4l2'] = 'browser'
    device: int = Field(0, ge=0, le=15)
    sensor_mode: int = Field(-1, ge=-1, le=20)
    width: int = Field(1920, ge=320, le=4096)
    height: int = Field(1080, ge=240, le=3072)
    fps: int = Field(30, ge=1, le=60)
    exposure_mode: Literal['auto', 'manual'] = 'auto'
    exposure_us: float = Field(10000, ge=13, le=33333, allow_inf_nan=False)
    analog_gain: float = Field(1, ge=1, le=10.625, allow_inf_nan=False)
    exposure_compensation: float = Field(0, ge=-2, le=2, allow_inf_nan=False)
    white_balance: int = Field(1, ge=0, le=8)
    focus_distance_mm: float = Field(101.6, ge=5, le=2000, allow_inf_nan=False)
    barcode_mode: Literal['data_matrix', 'qr_and_data_matrix'] = 'data_matrix'
    processing_fps: int = Field(24, ge=1, le=30)
    preprocess: bool = True
    auto_decode: bool = True
    overlays: bool = True
    clahe_clip: float = Field(2.0, ge=0.1, le=10)
    clahe_grid: int = Field(8, ge=2, le=32)
    contrast: float = Field(1, ge=0.2, le=3)
    gamma: float = Field(1, ge=0.2, le=3)
    sharpen: float = Field(0.5, ge=0, le=3)
    noise: int = Field(3, ge=1, le=11)
    threshold_block: int = Field(31, ge=3, le=151)
    threshold_offset: float = Field(5, ge=-30, le=30)
    morphology: Literal['none', 'open', 'close'] = 'none'
    min_candidate: int = Field(20, ge=8, le=500)
    max_candidate: int = Field(1600, ge=20, le=4096)
    search_scale: float = Field(0.5, ge=0.2, le=1)
    search_interval: int = Field(6, ge=1, le=30)
    candidate_timeout: float = Field(1.5, ge=0.1, le=10)
    decoder_budget_ms: int = Field(45, ge=10, le=500)
    confirmation_frames: int = Field(2, ge=1, le=5)
    duplicate_seconds: float = Field(3, ge=0.5, le=30)
    payload_pattern: str = Field(r'[\x20-\x7E]{1,128}', max_length=256)
    prefix: str = Field('', max_length=30)
    expected_length: int = Field(0, ge=0, le=128)
    whitelist: list[str] = Field(default_factory=list, max_length=5000)
    save_diagnostics: bool = False

    @model_validator(mode='after')
    def validate_settings(self):
        if self.noise % 2 == 0 or self.threshold_block % 2 == 0:
            raise ValueError('Noise kernel and threshold block must be odd numbers')
        if self.min_candidate > self.max_candidate:
            raise ValueError('Minimum candidate size must not exceed maximum')
        try:
            re.compile(self.payload_pattern)
        except re.error as exc:
            raise ValueError(f'Invalid payload validation pattern: {exc}') from exc
        return self


PRESETS = {
    'Default': {},
    '10×10 Data Matrix': {'barcode_mode': 'data_matrix', 'processing_fps': 24, 'decoder_budget_ms': 45, 'search_interval': 6},
    'QR + Data Matrix': {'barcode_mode': 'qr_and_data_matrix'},
    'Reflective metal': {'clahe_clip': 3, 'threshold_offset': 3, 'sharpen': 0.8},
    'Dark coating': {'gamma': 0.7, 'clahe_clip': 3},
    'Deep engraving': {'clahe_clip': 2.5, 'sharpen': 1},
    'High motion': {'processing_fps': 12, 'search_interval': 2, 'decoder_budget_ms': 60},
    'Low light': {'gamma': 0.6, 'noise': 5, 'clahe_clip': 3},
    'Low resource': {'processing_fps': 4, 'search_interval': 5, 'decoder_budget_ms': 40},
}


class ConfigStore:
    def __init__(self, directory: Path):
        self.path = directory / 'settings.json'
        self.preset_path = directory / 'presets.json'
        self.settings = Settings(**json.loads(self.path.read_text())) if self.path.exists() else Settings()
        self.custom = json.loads(self.preset_path.read_text()) if self.preset_path.exists() else {}

    def update(self, data):
        settings = Settings(**{**self.settings.model_dump(), **data})
        self._write(self.path, settings.model_dump())
        self.settings = settings
        return settings

    def presets(self):
        return {**{name: Settings(**params).model_dump() for name, params in PRESETS.items()}, **self.custom}

    def save_preset(self, name):
        if not name.strip() or len(name) > 60 or name in PRESETS:
            raise ValueError('Choose a new preset name (1–60 characters)')
        self.custom[name] = self.settings.model_dump()
        self._write(self.preset_path, self.custom)

    @staticmethod
    def _write(path, data):
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(data, indent=2), encoding='utf-8')
        temporary.replace(path)
